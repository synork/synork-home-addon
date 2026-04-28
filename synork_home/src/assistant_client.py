"""Synork Home — Assistant Client (Satellite-side).

WebSocket client that connects an Assistant Edition device to its
paired Hub on the local network. This is the satellite's backbone —
all communication between the Assistant and the Synork ecosystem
flows through this client via the Hub.

Voice query flow:
  local wake word → local STT → text query
  → AssistantClient sends VoiceQueryFromAssistant to Hub
  → Hub forwards to Synork backend as AssistantInvoke
  → response streams back via VoiceResponseToAssistant
  → local TTS plays the response

The client handles:
  - HMAC-SHA256 auth handshake with the Hub
  - Session resumption for fast reconnection
  - Heartbeat with satellite-specific status
  - Voice query routing to Hub
  - Reconnection with exponential backoff
  - Graceful degradation when Hub is unreachable

Created in Session 16: Assistant Edition Build Target & Hub-Assistant Pairing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

import websockets
from websockets.asyncio.client import ClientConnection
from pydantic import TypeAdapter

from shared.protocol import (
    AssistantHello,
    AssistantAuthResponse,
    AssistantHeartbeat,
    HubChallenge,
    HubHeartbeatAck,
    HubWelcome,
    ProtocolError,
    SatelliteDisconnect,
    SatelliteMessage,
    SatelliteReconnect,
    VoiceQueryFromAssistant,
    VoiceResponseToAssistant,
    ErrorCode,
)
from shared.persona_schema import AssistantEditionConfig

logger = logging.getLogger("synork.assistant_client")

_satellite_adapter = TypeAdapter(SatelliteMessage)

MessageHandler = Callable[[Any], Coroutine[Any, Any, None]]

# Reconnection parameters
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 120.0
_RECONNECT_JITTER_FACTOR = 0.3

_HEARTBEAT_INTERVAL = 30.0
_HEARTBEAT_MISS_LIMIT = 3
_AUTH_TIMEOUT = 10.0

# Path where pairing config is persisted
_CONFIG_PATH = "/data/synork/assistant_config.json"


class AssistantClient:
    """WebSocket client connecting a satellite Assistant to its Hub.

    Lifecycle:
        1. connect(hub_url) → opens WS, performs auth handshake
        2. Message loop runs until disconnect or connection lost
        3. On connection loss, reconnects with exponential backoff
        4. disconnect() → graceful shutdown
    """

    def __init__(
        self,
        satellite_id: str,
        device_secret: str,
        hub_url: str,
        capabilities: Optional[list[str]] = None,
        platform: str = "unknown",
        room: Optional[str] = None,
    ) -> None:
        self.satellite_id = satellite_id
        self._device_secret = device_secret
        self.hub_url = hub_url
        self.capabilities = capabilities or []
        self.platform = platform
        self.room = room

        # Connection state
        self._ws: Optional[ClientConnection] = None
        self._connected = False
        self._authenticated = False
        self._session_token: Optional[str] = None
        self._hub_id: Optional[str] = None
        self._household_id: Optional[str] = None
        self._household_name: Optional[str] = None
        self._hub_relay_connected = True

        # Lifecycle
        self._start_time = time.monotonic()
        self._reconnect_attempt = 0
        self._running = False
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_heartbeat_recv = 0.0

        # Message handlers
        self._handlers: dict[str, MessageHandler] = {}

    # -- Properties -------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return self._connected and self._authenticated

    @property
    def hub_relay_connected(self) -> bool:
        """Whether the Hub is connected to the Synork backend."""
        return self._hub_relay_connected

    @property
    def household_id(self) -> Optional[str]:
        return self._household_id

    # -- Handler registration ---------------------------------------------- #

    def on(self, message_type: str, handler: MessageHandler) -> None:
        """Register a handler for a specific message type."""
        self._handlers[message_type] = handler

    # -- Connection lifecycle ---------------------------------------------- #

    async def connect(self) -> None:
        """Open WebSocket and start message loop with auto-reconnect."""
        self._running = True
        self._start_time = time.monotonic()

        while self._running:
            try:
                await self._connect_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if not self._running:
                    break
                self._connected = False
                self._authenticated = False
                delay = self._backoff_delay()
                logger.warning(
                    "Hub connection failed (%s), retrying in %.1fs",
                    exc, delay,
                )
                await asyncio.sleep(delay)
                self._reconnect_attempt += 1

    async def _connect_once(self) -> None:
        """Single connection attempt: open WS, auth, run message loop."""
        logger.info("Connecting to Hub at %s", self.hub_url)

        async with websockets.connect(
            self.hub_url,
            ping_interval=None,
            close_timeout=5,
            max_size=2**20,
        ) as ws:
            self._ws = ws
            self._connected = True
            logger.info("WebSocket connected to Hub")

            # Try session resumption first, then full auth
            if self._session_token and self._reconnect_attempt > 0:
                authed = await self._try_session_resume()
                if not authed:
                    authed = await self._authenticate()
            else:
                authed = await self._authenticate()

            if not authed:
                logger.error("Hub authentication failed")
                self._connected = False
                return

            self._authenticated = True
            self._reconnect_attempt = 0
            logger.info(
                "Authenticated with Hub %s — household=%s (%s)",
                self._hub_id, self._household_id, self._household_name,
            )

            # Persist config on successful auth
            self._save_config()

            # Start heartbeat
            self._last_heartbeat_recv = time.monotonic()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            try:
                await self._message_loop()
            finally:
                if self._heartbeat_task:
                    self._heartbeat_task.cancel()
                    try:
                        await self._heartbeat_task
                    except asyncio.CancelledError:
                        pass
                self._connected = False
                self._authenticated = False
                self._ws = None

    async def disconnect(self) -> None:
        """Gracefully disconnect from the Hub."""
        self._running = False

        if self._ws and self._connected:
            try:
                msg = SatelliteDisconnect(
                    satellite_id=self.satellite_id,
                    reason="assistant_shutdown",
                    will_reconnect=False,
                )
                await self._send_raw(msg)
            except Exception:
                pass

        if self._heartbeat_task:
            self._heartbeat_task.cancel()

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

        self._connected = False
        self._authenticated = False
        self._ws = None
        logger.info("Disconnected from Hub")

    # -- Authentication ---------------------------------------------------- #

    async def _authenticate(self) -> bool:
        """Full HMAC auth: AssistantHello → HubChallenge → AssistantAuthResponse → HubWelcome."""
        device_secret_hash = hashlib.sha256(self._device_secret.encode()).hexdigest()

        hello = AssistantHello(
            satellite_id=self.satellite_id,
            device_secret_hash=device_secret_hash,
            hardware_capabilities=self.capabilities,
            claimed_room=self.room,
            platform=self.platform,
        )
        await self._send_raw(hello)

        # Wait for HubChallenge
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=_AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error("Auth timeout waiting for hub challenge")
            return False

        msg = _satellite_adapter.validate_json(raw)
        if isinstance(msg, ProtocolError):
            logger.error("Hub rejected hello: [%s] %s", msg.code, msg.error_message)
            return False
        if not isinstance(msg, HubChallenge):
            logger.error("Expected HubChallenge, got %s", type(msg).__name__)
            return False

        if msg.pairing_request_id:
            # New pairing — wait for user approval
            logger.info(
                "Pairing request %s created — waiting for user approval...",
                msg.pairing_request_id,
            )

        # Sign the nonce: HMAC-SHA256(key=SHA256(device_secret), msg=nonce)
        hmac_key = hashlib.sha256(self._device_secret.encode()).digest()
        signature = hmac.new(
            hmac_key,
            msg.challenge_nonce.encode(),
            hashlib.sha256,
        ).hexdigest()

        auth_resp = AssistantAuthResponse(
            satellite_id=self.satellite_id,
            signature=signature,
        )
        await self._send_raw(auth_resp)

        # Wait for HubWelcome (may take up to 5 min for new pairing approval)
        timeout = 310.0 if msg.pairing_request_id else _AUTH_TIMEOUT
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.error("Auth timeout waiting for hub welcome")
            return False

        msg = _satellite_adapter.validate_json(raw)
        if isinstance(msg, HubWelcome):
            self._hub_id = msg.hub_id
            self._household_id = msg.household_id
            self._household_name = msg.household_name
            self._session_token = msg.session_token

            # If this was first-time pairing, the Hub sends the device_secret
            if msg.device_secret:
                self._device_secret = msg.device_secret
                logger.info("Received device secret from Hub (first-time pairing)")

            if msg.assigned_room:
                self.room = msg.assigned_room

            return True
        elif isinstance(msg, ProtocolError):
            logger.error("Hub auth rejected: [%s] %s", msg.code, msg.error_message)
            return False
        else:
            logger.error("Unexpected auth response: %s", type(msg).__name__)
            return False

    async def _try_session_resume(self) -> bool:
        """Attempt session resumption using stored session token."""
        if not self._session_token:
            return False

        reconnect = SatelliteReconnect(
            satellite_id=self.satellite_id,
            previous_session_token=self._session_token,
            reconnect_reason="connection_lost",
        )
        await self._send_raw(reconnect)

        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=_AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            return False

        msg = _satellite_adapter.validate_json(raw)
        if isinstance(msg, HubWelcome):
            self._hub_id = msg.hub_id
            self._household_id = msg.household_id
            self._household_name = msg.household_name
            self._session_token = msg.session_token
            if msg.assigned_room:
                self.room = msg.assigned_room
            logger.info("Session resumed with Hub")
            return True

        return False

    # -- Message loop ------------------------------------------------------ #

    async def _message_loop(self) -> None:
        """Receive and dispatch messages from the Hub."""
        async for raw in self._ws:
            try:
                msg = _satellite_adapter.validate_json(raw)
            except Exception as exc:
                logger.warning("Failed to parse Hub message: %s", exc)
                continue
            await self._dispatch(msg)

    async def _dispatch(self, msg: Any) -> None:
        """Route a parsed message."""
        mt = msg.message_type

        if mt == "hub_heartbeat_ack":
            self._last_heartbeat_recv = time.monotonic()
            if isinstance(msg, HubHeartbeatAck):
                self._hub_relay_connected = msg.relay_connected
            return

        if mt == "protocol_error":
            if isinstance(msg, ProtocolError):
                logger.error("Hub error [%s]: %s", msg.code, msg.error_message)
                if msg.code == ErrorCode.AUTH_EXPIRED:
                    self._session_token = None
            return

        if mt == "satellite_disconnect":
            if isinstance(msg, SatelliteDisconnect):
                logger.info("Hub disconnect: %s", msg.reason)
            return

        handler = self._handlers.get(mt)
        if handler:
            try:
                await handler(msg)
            except Exception as exc:
                logger.error("Handler for %s raised: %s", mt, exc, exc_info=True)
        else:
            logger.debug("No handler for message type: %s", mt)

    # -- Sending ----------------------------------------------------------- #

    async def send(self, message: Any) -> None:
        """Send a message to the Hub."""
        if not self._connected or not self._ws:
            raise ConnectionError("Not connected to Hub")
        await self._send_raw(message)

    async def _send_raw(self, message: Any) -> None:
        """Serialize and send."""
        await self._ws.send(message.model_dump_json())

    async def send_voice_query(
        self,
        query: str,
        language: str = "hu",
        conversation_id: Optional[str] = None,
    ) -> None:
        """Send a transcribed voice query to the Hub for routing."""
        msg = VoiceQueryFromAssistant(
            satellite_id=self.satellite_id,
            query=query,
            language=language,
            room=self.room,
            conversation_id=conversation_id,
        )
        await self.send(msg)

    # -- Heartbeat --------------------------------------------------------- #

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats and check for missed heartbeats from Hub."""
        while self._running and self._connected:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

            elapsed = time.monotonic() - self._last_heartbeat_recv
            if elapsed > _HEARTBEAT_INTERVAL * _HEARTBEAT_MISS_LIMIT:
                logger.warning(
                    "Missed %d heartbeats from Hub (%.0fs), closing",
                    _HEARTBEAT_MISS_LIMIT, elapsed,
                )
                if self._ws:
                    await self._ws.close()
                return

            try:
                hb = AssistantHeartbeat(
                    satellite_id=self.satellite_id,
                    uptime_seconds=time.monotonic() - self._start_time,
                    audio_active=True,  # Updated by pipeline
                )
                await self._send_raw(hb)
            except Exception as exc:
                logger.warning("Failed to send heartbeat: %s", exc)
                return

    # -- Reconnection backoff ---------------------------------------------- #

    def _backoff_delay(self) -> float:
        """Exponential backoff with jitter."""
        delay = min(
            _RECONNECT_BASE_DELAY * (2 ** self._reconnect_attempt),
            _RECONNECT_MAX_DELAY,
        )
        jitter = delay * _RECONNECT_JITTER_FACTOR * random.random()
        return delay + jitter

    # -- Config persistence ------------------------------------------------ #

    def _save_config(self) -> None:
        """Persist pairing config locally for reconnection across reboots."""
        config = {
            "paired_hub_id": self._hub_id,
            "paired_hub_address": self.hub_url,
            "satellite_id": self.satellite_id,
            "device_secret": self._device_secret,
            "hub_session_token": self._session_token,
            "room": self.room,
            "household_id": self._household_id,
            "household_name": self._household_name,
        }
        path = Path(_CONFIG_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2))
        # Restrictive permissions — device_secret is sensitive
        path.chmod(0o600)

    @staticmethod
    def load_config() -> Optional[dict[str, Any]]:
        """Load persisted pairing config, if it exists."""
        path = Path(_CONFIG_PATH)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except Exception:
            return None
