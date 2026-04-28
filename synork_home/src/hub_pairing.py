"""Synork Home — Hub Pairing Service.

Handles satellite pairing and authentication on the Hub side:
  - Validates incoming AssistantHello messages
  - Runs HMAC-SHA256 challenge/response authentication
  - Manages the pending pairing approval flow (user must approve via app)
  - Maintains the local SQLite database of paired satellites
  - Provides session token management for fast reconnection

The Hub's device_secret is the shared secret between Hub and satellite.
The Hub stores only the SHA-256 hash; the plaintext lives on the satellite.

Created in Session 16: Assistant Edition Build Target & Hub-Assistant Pairing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from shared.protocol import (
    AssistantAuthResponse,
    HubChallenge,
    HubWelcome,
    ProtocolError,
    SatelliteMessage,
    ErrorCode,
)
from shared.persona_schema import (
    SatelliteRegistration,
    PendingSatellitePairing,
)

from pydantic import TypeAdapter

logger = logging.getLogger("synork.hub_pairing")

_satellite_adapter = TypeAdapter(SatelliteMessage)

_AUTH_TIMEOUT = 10.0
_SESSION_DURATION = timedelta(hours=24)
_PAIRING_REQUEST_TTL = timedelta(minutes=5)


class HubPairingService:
    """Manages satellite pairing, authentication, and state on the Hub.

    Uses a local SQLite database for satellite records. This data never
    leaves the Hub — the Synork backend accesses it through Hub-mediated
    REST endpoints via the relay.
    """

    def __init__(
        self,
        db_path: str = "/data/synork/satellites.db",
        hub_device_id: str = "",
        household_id: str = "",
        household_name: str = "",
    ) -> None:
        self.hub_device_id = hub_device_id
        self.household_id = household_id
        self.household_name = household_name
        self._db_path = db_path
        self._db: Optional[sqlite3.Connection] = None

        # Active sessions: satellite_id → (session_token, expires_at)
        self._sessions: dict[str, tuple[str, datetime]] = {}

        # Pending pairing requests: request_id → (PendingSatellitePairing, approval_event)
        self._pending: dict[str, tuple[PendingSatellitePairing, asyncio.Event]] = {}

        # Approved pairing secrets: request_id → device_secret
        self._approved_secrets: dict[str, str] = {}

    async def initialize(self) -> None:
        """Initialize the SQLite database and create tables if needed."""
        db_dir = Path(self._db_path).parent
        db_dir.mkdir(parents=True, exist_ok=True)

        self._db = sqlite3.connect(self._db_path)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")

        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS satellites (
                satellite_id TEXT PRIMARY KEY,
                device_secret_hash TEXT NOT NULL,
                room TEXT,
                display_name TEXT,
                capabilities TEXT NOT NULL DEFAULT '[]',
                platform TEXT NOT NULL DEFAULT 'unknown',
                addon_version TEXT NOT NULL DEFAULT '0.1.0',
                paired_at TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                online INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS pending_satellite_pairings (
                request_id TEXT PRIMARY KEY,
                satellite_id TEXT NOT NULL,
                capabilities TEXT NOT NULL,
                platform TEXT NOT NULL DEFAULT 'unknown',
                claimed_room TEXT,
                initiated_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
        """)
        self._db.commit()
        logger.info("Hub pairing DB initialized at %s", self._db_path)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            self._db.close()
            self._db = None

    # -- Authentication ---------------------------------------------------- #

    async def authenticate_satellite(
        self,
        satellite_id: str,
        device_secret_hash: str,
        ws: Any,
    ) -> Optional[HubWelcome]:
        """Authenticate a returning satellite via HMAC challenge.

        Returns HubWelcome on success, None on failure.
        """
        # Verify satellite exists in DB
        row = self._get_satellite(satellite_id)
        if not row:
            error = ProtocolError(
                code=ErrorCode.AUTH_FAILED,
                error_message=f"Unknown satellite: {satellite_id}",
            )
            await ws.send(error.model_dump_json())
            return None

        # Verify device_secret_hash matches
        if row["device_secret_hash"] != device_secret_hash:
            error = ProtocolError(
                code=ErrorCode.AUTH_FAILED,
                error_message="Device secret mismatch",
            )
            await ws.send(error.model_dump_json())
            return None

        # Send challenge
        nonce = secrets.token_hex(32)
        challenge = HubChallenge(
            challenge_nonce=nonce,
            challenge_expires_at=datetime.now(timezone.utc) + timedelta(seconds=_AUTH_TIMEOUT),
        )
        await ws.send(challenge.model_dump_json())

        # Wait for AssistantAuthResponse
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=_AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Auth timeout for satellite %s", satellite_id)
            return None

        msg = _satellite_adapter.validate_json(raw)
        if not isinstance(msg, AssistantAuthResponse):
            logger.warning("Expected AssistantAuthResponse, got %s", type(msg).__name__)
            return None

        # Verify HMAC: the satellite signs the nonce with its device_secret.
        # We can't verify directly since we only have the hash, so we verify
        # by having both sides derive the same HMAC key from device_secret_hash.
        # The satellite computes HMAC(key=SHA256(device_secret), msg=nonce).
        # We stored SHA256(device_secret) as device_secret_hash, so we use that.
        expected = hmac.new(
            bytes.fromhex(device_secret_hash),
            nonce.encode(),
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(msg.signature, expected):
            error = ProtocolError(
                code=ErrorCode.AUTH_FAILED,
                error_message="Invalid HMAC signature",
            )
            await ws.send(error.model_dump_json())
            return None

        # Auth success — issue session token
        session_token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + _SESSION_DURATION
        self._sessions[satellite_id] = (session_token, expires)

        welcome = HubWelcome(
            hub_id=self.hub_device_id,
            household_id=self.household_id,
            household_name=self.household_name,
            session_token=session_token,
            session_expires_at=expires,
            assigned_room=row["room"],
        )
        await ws.send(welcome.model_dump_json())
        return welcome

    # -- New pairing flow -------------------------------------------------- #

    async def create_pairing_request(
        self,
        satellite_id: str,
        capabilities: list[str],
        platform: str,
        claimed_room: Optional[str],
        ws: Any,
    ) -> Optional[HubChallenge]:
        """Create a pending pairing request for a new satellite.

        Returns the HubChallenge sent to the satellite (with pairing_request_id).
        The satellite must wait for user approval before the handshake completes.
        """
        # Check if already paired
        if self._get_satellite(satellite_id):
            error = ProtocolError(
                code=ErrorCode.AUTH_FAILED,
                error_message="Satellite already paired. Use device_secret_hash to reconnect.",
            )
            await ws.send(error.model_dump_json())
            return None

        request_id = uuid.uuid4().hex
        now = datetime.now(timezone.utc)
        expires = now + _PAIRING_REQUEST_TTL

        pending = PendingSatellitePairing(
            request_id=request_id,
            satellite_id=satellite_id,
            capabilities=capabilities,
            platform=platform,
            claimed_room=claimed_room,
            initiated_at=now,
            expires_at=expires,
        )

        # Store in DB
        self._db.execute(
            """INSERT INTO pending_satellite_pairings
               (request_id, satellite_id, capabilities, platform, claimed_room, initiated_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id, satellite_id, json.dumps(capabilities),
                platform, claimed_room,
                now.isoformat(), expires.isoformat(),
            ),
        )
        self._db.commit()

        # Create an event for approval notification
        approval_event = asyncio.Event()
        self._pending[request_id] = (pending, approval_event)

        # Send challenge with pairing_request_id (satellite knows to wait)
        challenge = HubChallenge(
            challenge_nonce=secrets.token_hex(32),
            challenge_expires_at=expires,
            pairing_request_id=request_id,
        )
        await ws.send(challenge.model_dump_json())

        logger.info(
            "Pairing request created: %s for satellite %s (expires %s)",
            request_id, satellite_id, expires.isoformat(),
        )
        return challenge

    async def wait_for_pairing_approval(
        self,
        request_id: str,
        ws: Any,
        timeout: float = 300.0,
    ) -> Optional[HubWelcome]:
        """Wait for user approval of a pairing request.

        Blocks until approved, rejected, or timed out.
        On approval, completes the HMAC handshake and returns HubWelcome.
        """
        entry = self._pending.get(request_id)
        if not entry:
            return None

        pending, approval_event = entry

        try:
            await asyncio.wait_for(approval_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.info("Pairing request %s timed out", request_id)
            self._cleanup_pending(request_id)
            error = ProtocolError(
                code=ErrorCode.AUTH_EXPIRED,
                error_message="Pairing request timed out. Please try again.",
            )
            try:
                await ws.send(error.model_dump_json())
            except Exception:
                pass
            return None

        # Check if approved (secret was generated) or rejected
        device_secret = self._approved_secrets.pop(request_id, None)
        if not device_secret:
            # Rejected
            error = ProtocolError(
                code=ErrorCode.PERMISSION_DENIED,
                error_message="Pairing request was rejected by the household owner.",
            )
            try:
                await ws.send(error.model_dump_json())
            except Exception:
                pass
            self._cleanup_pending(request_id)
            return None

        # Approved — register the satellite
        device_secret_hash = hashlib.sha256(device_secret.encode()).hexdigest()
        now = datetime.now(timezone.utc)

        self._db.execute(
            """INSERT INTO satellites
               (satellite_id, device_secret_hash, room, capabilities, platform, paired_at, last_seen, online)
               VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
            (
                pending.satellite_id, device_secret_hash,
                pending.claimed_room, json.dumps(pending.capabilities),
                pending.platform, now.isoformat(), now.isoformat(),
            ),
        )
        self._db.commit()

        # Issue session
        session_token = secrets.token_urlsafe(32)
        expires = now + _SESSION_DURATION
        self._sessions[pending.satellite_id] = (session_token, expires)

        welcome = HubWelcome(
            hub_id=self.hub_device_id,
            household_id=self.household_id,
            household_name=self.household_name,
            session_token=session_token,
            session_expires_at=expires,
            assigned_room=pending.claimed_room,
            device_secret=device_secret,  # Only sent during first-time pairing
        )
        try:
            await ws.send(welcome.model_dump_json())
        except Exception:
            pass

        self._cleanup_pending(request_id)
        logger.info("Satellite %s paired successfully", pending.satellite_id)
        return welcome

    async def approve_pairing(self, request_id: str) -> bool:
        """Approve a pending pairing request (called from REST endpoint).

        Generates a device_secret and signals the waiting coroutine.
        """
        entry = self._pending.get(request_id)
        if not entry:
            return False

        pending, approval_event = entry
        device_secret = secrets.token_urlsafe(32)
        self._approved_secrets[request_id] = device_secret
        approval_event.set()
        return True

    async def reject_pairing(self, request_id: str) -> bool:
        """Reject a pending pairing request."""
        entry = self._pending.get(request_id)
        if not entry:
            return False

        _, approval_event = entry
        # Don't set a secret — the waiter checks for its absence
        approval_event.set()
        return True

    # -- Session resumption ------------------------------------------------ #

    async def try_session_resume(
        self,
        satellite_id: str,
        previous_session_token: Optional[str],
    ) -> Optional[HubWelcome]:
        """Try to resume a satellite session without full re-auth."""
        if not previous_session_token:
            return None

        stored = self._sessions.get(satellite_id)
        if not stored:
            return None

        token, expires = stored
        if token != previous_session_token:
            return None
        if datetime.now(timezone.utc) > expires:
            del self._sessions[satellite_id]
            return None

        # Session valid — issue a new token
        row = self._get_satellite(satellite_id)
        if not row:
            return None

        new_token = secrets.token_urlsafe(32)
        new_expires = datetime.now(timezone.utc) + _SESSION_DURATION
        self._sessions[satellite_id] = (new_token, new_expires)

        return HubWelcome(
            hub_id=self.hub_device_id,
            household_id=self.household_id,
            household_name=self.household_name,
            session_token=new_token,
            session_expires_at=new_expires,
            assigned_room=row["room"],
        )

    # -- Satellite state management ---------------------------------------- #

    async def mark_satellite_online(self, satellite_id: str) -> None:
        """Update satellite state to online."""
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "UPDATE satellites SET online = 1, last_seen = ? WHERE satellite_id = ?",
            (now, satellite_id),
        )
        self._db.commit()

    async def mark_satellite_offline(self, satellite_id: str) -> None:
        """Update satellite state to offline."""
        now = datetime.now(timezone.utc).isoformat()
        self._db.execute(
            "UPDATE satellites SET online = 0, last_seen = ? WHERE satellite_id = ?",
            (now, satellite_id),
        )
        self._db.commit()

    # -- CRUD for REST endpoints ------------------------------------------- #

    def list_satellites(self) -> list[SatelliteRegistration]:
        """List all paired satellites."""
        rows = self._db.execute("SELECT * FROM satellites ORDER BY paired_at DESC").fetchall()
        return [self._row_to_registration(row) for row in rows]

    def get_satellite_detail(self, satellite_id: str) -> Optional[SatelliteRegistration]:
        """Get a single satellite's details."""
        row = self._get_satellite(satellite_id)
        if not row:
            return None
        return self._row_to_registration(row)

    def update_satellite(self, satellite_id: str, room: Optional[str] = None, display_name: Optional[str] = None) -> bool:
        """Update a satellite's config (room, display_name)."""
        row = self._get_satellite(satellite_id)
        if not row:
            return False

        updates = []
        params = []
        if room is not None:
            updates.append("room = ?")
            params.append(room)
        if display_name is not None:
            updates.append("display_name = ?")
            params.append(display_name)

        if not updates:
            return True

        params.append(satellite_id)
        self._db.execute(
            f"UPDATE satellites SET {', '.join(updates)} WHERE satellite_id = ?",
            params,
        )
        self._db.commit()
        return True

    def unpair_satellite(self, satellite_id: str) -> bool:
        """Remove a satellite from the paired list."""
        row = self._get_satellite(satellite_id)
        if not row:
            return False

        self._db.execute("DELETE FROM satellites WHERE satellite_id = ?", (satellite_id,))
        self._db.commit()

        # Invalidate session
        self._sessions.pop(satellite_id, None)

        logger.info("Satellite %s unpaired", satellite_id)
        return True

    def list_pending_pairings(self) -> list[PendingSatellitePairing]:
        """List pending pairing requests."""
        now = datetime.now(timezone.utc)
        result = []
        for request_id, (pending, _) in self._pending.items():
            if pending.expires_at > now:
                result.append(pending)
        return result

    # -- Internal helpers -------------------------------------------------- #

    def _get_satellite(self, satellite_id: str) -> Optional[sqlite3.Row]:
        """Fetch a satellite row from the DB."""
        return self._db.execute(
            "SELECT * FROM satellites WHERE satellite_id = ?",
            (satellite_id,),
        ).fetchone()

    def _row_to_registration(self, row: sqlite3.Row) -> SatelliteRegistration:
        """Convert a DB row to a SatelliteRegistration model."""
        return SatelliteRegistration(
            satellite_id=row["satellite_id"],
            device_secret_hash=row["device_secret_hash"],
            room=row["room"],
            display_name=row["display_name"],
            capabilities=json.loads(row["capabilities"]),
            platform=row["platform"],
            addon_version=row["addon_version"],
            paired_at=datetime.fromisoformat(row["paired_at"]),
            last_seen=datetime.fromisoformat(row["last_seen"]),
            online=bool(row["online"]),
        )

    def _cleanup_pending(self, request_id: str) -> None:
        """Remove a pending pairing request."""
        self._pending.pop(request_id, None)
        self._db.execute(
            "DELETE FROM pending_satellite_pairings WHERE request_id = ?",
            (request_id,),
        )
        self._db.commit()
