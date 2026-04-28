"""Synork Home — Satellite Broker (Hub-side).

WebSocket server running on the Hub that accepts connections from
Assistant Edition satellites on the local network.

The broker:
  - Listens on port 8765 for incoming Assistant WebSocket connections
  - Delegates authentication to hub_pairing.py
  - Multiplexes N satellite connections (one per Assistant device)
  - Routes voice queries from Assistants to the relay client (→ Synork backend)
  - Routes responses from the relay back to the originating Assistant
  - Manages heartbeat/keepalive per satellite
  - Tracks satellite online/offline state in the local DB

Structurally mirrors relay_client.py's patterns (HMAC auth, heartbeat,
message dispatch) but inverted — Hub is the server, Assistants are clients.

Created in Session 16: Assistant Edition Build Target & Hub-Assistant Pairing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Optional

import websockets
from websockets.asyncio.server import ServerConnection
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
    ThreadCredentialsAck,
    ThreadStatusUpdate,
    ThreadCommissioningRequest,
    VoiceQueryFromAssistant,
    VoiceResponseToAssistant,
    ErrorCode,
)
from shared.persona_schema import HardwareCapability

logger = logging.getLogger("synork.satellite_broker")

_satellite_adapter = TypeAdapter(SatelliteMessage)

_HEARTBEAT_INTERVAL = 30.0
_HEARTBEAT_MISS_LIMIT = 3

MessageHandler = Callable[[str, Any], Coroutine[Any, Any, None]]


class SatelliteConnection:
    """Tracks state for a single connected satellite."""

    def __init__(self, ws: ServerConnection, satellite_id: str) -> None:
        self.ws = ws
        self.satellite_id = satellite_id
        self.authenticated = False
        self.session_token: Optional[str] = None
        self.room: Optional[str] = None
        self.connected_at = time.monotonic()
        self.last_heartbeat = time.monotonic()
        self.capabilities: list[str] = []
        self.platform: str = "unknown"


class SatelliteBroker:
    """WebSocket server managing connections from Assistant satellites.

    Lifecycle:
        start(port) → runs until stop()

    The broker maintains a dict of satellite_id → SatelliteConnection.
    Authentication is delegated to the hub_pairing module.
    """

    def __init__(self, hub_pairing: Any) -> None:
        """
        Args:
            hub_pairing: HubPairingService instance for auth/pairing.
        """
        self._pairing = hub_pairing
        self._server: Optional[Any] = None
        self._running = False
        self._start_time = 0.0

        # Connected satellites keyed by satellite_id
        self._connections: dict[str, SatelliteConnection] = {}

        # Message handlers keyed by message_type
        self._handlers: dict[str, MessageHandler] = {}

        # Heartbeat monitoring task
        self._heartbeat_task: Optional[asyncio.Task] = None

        # Thread coordinator reference (set by main.py after init)
        self._thread_coordinator: Optional[Any] = None  # ThreadCoordinator

    def set_thread_coordinator(self, coordinator: Any) -> None:
        """Set the Thread coordinator for post-pairing credential distribution."""
        self._thread_coordinator = coordinator

    # -- Properties -------------------------------------------------------- #

    @property
    def connected_satellites(self) -> list[str]:
        """List of currently connected satellite IDs."""
        return [
            sid for sid, conn in self._connections.items()
            if conn.authenticated
        ]

    def get_connection(self, satellite_id: str) -> Optional[SatelliteConnection]:
        """Get a satellite connection by ID."""
        return self._connections.get(satellite_id)

    # -- Handler registration ---------------------------------------------- #

    def on(self, message_type: str, handler: MessageHandler) -> None:
        """Register a handler for a message type.

        Handler signature: async (satellite_id: str, message: BaseMessage) -> None
        """
        self._handlers[message_type] = handler

    # -- Server lifecycle -------------------------------------------------- #

    async def start(self, host: str = "0.0.0.0", port: int = 8765) -> None:
        """Start the satellite WebSocket server."""
        self._running = True
        self._start_time = time.monotonic()

        self._server = await websockets.serve(
            self._handle_connection,
            host,
            port,
            ping_interval=None,  # We handle heartbeat ourselves
            max_size=2**20,  # 1MB max message
        )

        # Start heartbeat monitor
        self._heartbeat_task = asyncio.create_task(self._heartbeat_monitor())

        logger.info("Satellite broker listening on %s:%d", host, port)

    async def stop(self) -> None:
        """Stop the broker and disconnect all satellites."""
        self._running = False

        if self._heartbeat_task:
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Gracefully disconnect all satellites
        for conn in list(self._connections.values()):
            try:
                msg = SatelliteDisconnect(
                    satellite_id=conn.satellite_id,
                    reason="hub_shutdown",
                    will_reconnect=True,
                )
                await conn.ws.send(msg.model_dump_json())
            except Exception:
                pass
            try:
                await conn.ws.close()
            except Exception:
                pass

        self._connections.clear()

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        logger.info("Satellite broker stopped")

    # -- Connection handler ------------------------------------------------ #

    async def _handle_connection(self, ws: ServerConnection) -> None:
        """Handle a new WebSocket connection from an Assistant."""
        satellite_id = "unknown"
        conn: Optional[SatelliteConnection] = None

        try:
            # Wait for first message (AssistantHello or SatelliteReconnect)
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            msg = _satellite_adapter.validate_json(raw)

            if isinstance(msg, SatelliteReconnect):
                conn = await self._handle_reconnect(ws, msg)
            elif isinstance(msg, AssistantHello):
                conn = await self._handle_hello(ws, msg)
            else:
                error = ProtocolError(
                    code=ErrorCode.INVALID_MESSAGE,
                    error_message=f"Expected AssistantHello or SatelliteReconnect, got {type(msg).__name__}",
                )
                await ws.send(error.model_dump_json())
                return

            if not conn or not conn.authenticated:
                return

            satellite_id = conn.satellite_id
            self._connections[satellite_id] = conn

            # Notify pairing service that satellite is online
            await self._pairing.mark_satellite_online(satellite_id)

            logger.info("Satellite connected: %s (room=%s)", satellite_id, conn.room)

            # Session 18: If satellite has Thread radio, offer Thread credentials
            await self._maybe_offer_thread_credentials(conn)

            # Message loop
            async for raw_msg in ws:
                try:
                    parsed = _satellite_adapter.validate_json(raw_msg)
                    await self._dispatch(conn, parsed)
                except Exception as exc:
                    logger.warning(
                        "Failed to parse message from %s: %s",
                        satellite_id, exc,
                    )

        except asyncio.TimeoutError:
            logger.debug("Satellite connection timed out during handshake")
        except websockets.exceptions.ConnectionClosed:
            logger.info("Satellite disconnected: %s", satellite_id)
        except Exception as exc:
            logger.error("Error handling satellite connection: %s", exc, exc_info=True)
        finally:
            if conn and conn.satellite_id in self._connections:
                del self._connections[conn.satellite_id]
                await self._pairing.mark_satellite_offline(conn.satellite_id)
                # Session 18: Mark TBR as offline when satellite disconnects
                if self._thread_coordinator:
                    tbr = self._thread_coordinator._tbrs.get(conn.satellite_id)
                    if tbr:
                        from shared.protocol import ThreadTBRStatus
                        tbr.status = ThreadTBRStatus.OFFLINE
                logger.info("Satellite removed: %s", conn.satellite_id)

    async def _maybe_offer_thread_credentials(self, conn: SatelliteConnection) -> None:
        """After pairing, offer Thread credentials if satellite has THREAD_RADIO."""
        if not self._thread_coordinator:
            return

        has_thread = HardwareCapability.THREAD_RADIO.value in conn.capabilities
        if not has_thread:
            return

        logger.info(
            "Satellite %s has Thread radio — offering network credentials",
            conn.satellite_id,
        )

        # Run in background so it doesn't block the message loop
        asyncio.create_task(
            self._thread_coordinator.offer_credentials_to_satellite(conn.satellite_id)
        )

    async def _handle_hello(
        self,
        ws: ServerConnection,
        hello: AssistantHello,
    ) -> Optional[SatelliteConnection]:
        """Handle an AssistantHello — either authenticate or create pairing request."""
        conn = SatelliteConnection(ws, hello.satellite_id)
        conn.capabilities = hello.hardware_capabilities
        conn.platform = hello.platform
        conn.room = hello.claimed_room

        if hello.device_secret_hash:
            # Returning satellite — verify via HMAC challenge
            welcome = await self._pairing.authenticate_satellite(
                satellite_id=hello.satellite_id,
                device_secret_hash=hello.device_secret_hash,
                ws=ws,
            )
            if welcome:
                conn.authenticated = True
                conn.session_token = welcome.session_token
                conn.room = welcome.assigned_room or hello.claimed_room
                return conn
            return None
        else:
            # New satellite — create pending pairing request
            challenge = await self._pairing.create_pairing_request(
                satellite_id=hello.satellite_id,
                capabilities=hello.hardware_capabilities,
                platform=hello.platform,
                claimed_room=hello.claimed_room,
                ws=ws,
            )
            if not challenge:
                return None

            # Wait for user approval + auth response from satellite
            welcome = await self._pairing.wait_for_pairing_approval(
                request_id=challenge.pairing_request_id,
                ws=ws,
                timeout=300.0,  # 5 minute window
            )
            if welcome:
                conn.authenticated = True
                conn.session_token = welcome.session_token
                conn.room = welcome.assigned_room or hello.claimed_room
                return conn
            return None

    async def _handle_reconnect(
        self,
        ws: ServerConnection,
        msg: SatelliteReconnect,
    ) -> Optional[SatelliteConnection]:
        """Handle session resumption from a returning satellite."""
        welcome = await self._pairing.try_session_resume(
            satellite_id=msg.satellite_id,
            previous_session_token=msg.previous_session_token,
        )

        if welcome:
            conn = SatelliteConnection(ws, msg.satellite_id)
            conn.authenticated = True
            conn.session_token = welcome.session_token
            conn.room = welcome.assigned_room
            await ws.send(welcome.model_dump_json())
            return conn

        # Session expired — tell satellite to do full auth
        error = ProtocolError(
            code=ErrorCode.AUTH_EXPIRED,
            error_message="Session expired, please re-authenticate with AssistantHello",
        )
        await ws.send(error.model_dump_json())
        return None

    # -- Message dispatch -------------------------------------------------- #

    async def _dispatch(self, conn: SatelliteConnection, msg: Any) -> None:
        """Route a parsed satellite message to the appropriate handler."""
        mt = msg.message_type

        if mt == "assistant_heartbeat":
            conn.last_heartbeat = time.monotonic()
            ack = HubHeartbeatAck(
                hub_uptime_seconds=time.monotonic() - self._start_time,
                relay_connected=True,  # Updated by main.py
            )
            try:
                await conn.ws.send(ack.model_dump_json())
            except Exception:
                pass
            return

        if mt == "satellite_disconnect":
            logger.info(
                "Satellite %s disconnecting: %s",
                conn.satellite_id,
                getattr(msg, 'reason', 'unknown'),
            )
            return

        # Delegate to registered handlers
        handler = self._handlers.get(mt)
        if handler:
            try:
                await handler(conn.satellite_id, msg)
            except Exception as exc:
                logger.error(
                    "Handler for %s from %s raised: %s",
                    mt, conn.satellite_id, exc, exc_info=True,
                )
        else:
            logger.debug("No handler for satellite message type: %s", mt)

    # -- Sending to satellites --------------------------------------------- #

    async def send_to_satellite(self, satellite_id: str, message: Any) -> bool:
        """Send a message to a specific satellite.

        Returns True if sent, False if satellite not connected.
        """
        conn = self._connections.get(satellite_id)
        if not conn or not conn.authenticated:
            return False

        try:
            await conn.ws.send(message.model_dump_json())
            return True
        except Exception as exc:
            logger.warning("Failed to send to satellite %s: %s", satellite_id, exc)
            return False

    async def broadcast_to_satellites(self, message: Any) -> int:
        """Send a message to all connected satellites. Returns count of successful sends."""
        sent = 0
        for sid in list(self._connections.keys()):
            if await self.send_to_satellite(sid, message):
                sent += 1
        return sent

    # -- Heartbeat monitor ------------------------------------------------- #

    async def _heartbeat_monitor(self) -> None:
        """Check for satellites that have missed heartbeats."""
        while self._running:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

            now = time.monotonic()
            stale = []

            for sid, conn in self._connections.items():
                if not conn.authenticated:
                    continue
                elapsed = now - conn.last_heartbeat
                if elapsed > _HEARTBEAT_INTERVAL * _HEARTBEAT_MISS_LIMIT:
                    stale.append(sid)

            for sid in stale:
                conn = self._connections.get(sid)
                if conn:
                    logger.warning(
                        "Satellite %s missed %d heartbeats — closing",
                        sid, _HEARTBEAT_MISS_LIMIT,
                    )
                    try:
                        await conn.ws.close()
                    except Exception:
                        pass
