"""Synork Home — Relay WebSocket Client.

Maintains a persistent WebSocket connection to the Synork relay server.
Handles authentication (HMAC-SHA256), heartbeat, message dispatch, and
reconnection with exponential backoff + jitter.

This is the addon's backbone — all communication between the device and
the Synork cloud flows through this client.
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
from typing import Any, Callable, Coroutine, Optional

import websockets
from websockets.asyncio.client import ClientConnection
from pydantic import TypeAdapter

# Shared protocol types — imported via sys.path set up in main.py
from shared.protocol import (
    AddonHello,
    AuthChallenge,
    AuthResponse,
    BaseMessage,
    Disconnect,
    EntityStatePayload,
    EntityStateUpdate,
    Heartbeat,
    ProtocolError,
    Reconnect,
    RelayMessage,
    RelayWelcome,
    ServiceCallRequest,
    ServiceCallResponse,
    AssistantInvoke,
    AssistantResponse,
    AssistantStreamChunk,
    EntityStateQuery,
    ErrorCode,
)

logger = logging.getLogger("synork.relay_client")

MessageHandler = Callable[[Any], Coroutine[Any, Any, None]]

_relay_adapter = TypeAdapter(RelayMessage)

# Reconnection parameters (match integration's exponential backoff)
_RECONNECT_BASE_DELAY = 2.0
_RECONNECT_MAX_DELAY = 300.0
_RECONNECT_JITTER_FACTOR = 0.3

_HEARTBEAT_INTERVAL = 30.0
_HEARTBEAT_MISS_LIMIT = 3
_AUTH_TIMEOUT = 10.0


class RelayClient:
    """WebSocket client connecting to the Synork relay.

    Lifecycle:
        1. connect() — opens WebSocket, performs auth handshake
        2. Message loop runs until disconnect or connection lost
        3. On connection loss, reconnects with exponential backoff
        4. disconnect() — graceful shutdown with Disconnect message
    """

    def __init__(
        self,
        relay_url: str,
        device_id: str,
        device_secret: str,
        addon_version: str = "0.1.0",
        ha_version: str = "unknown",
    ) -> None:
        self.relay_url = relay_url
        self.device_id = device_id
        self._device_secret = device_secret
        self.addon_version = addon_version
        self.ha_version = ha_version

        # Connection state
        self._ws: Optional[ClientConnection] = None
        self._connected = False
        self._authenticated = False
        self._session_token: Optional[str] = None
        self._household_id: Optional[str] = None
        self._household_name: Optional[str] = None

        # Lifecycle
        self._start_time = time.monotonic()
        self._reconnect_attempt = 0
        self._running = False
        self._message_loop_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._last_heartbeat_recv = 0.0

        # Set the first time auth succeeds; awaited by voice setup so the
        # cloud_credentials pushed in RelayWelcome are in env before TTS /
        # brain clients capture them at construction time.
        self._first_auth_event = asyncio.Event()

        # Active personas and capabilities (set by main before connect)
        self.active_personas: list[str] = []
        self.capabilities: list[str] = []
        self.loaded_integrations: list[str] = []
        self.entity_count: int = 0

        # Message handlers keyed by message_type
        self._handlers: dict[str, MessageHandler] = {}

        # Callbacks fired once auth succeeds (after each (re)connect).
        self._connected_callbacks: list[Callable[[], Coroutine[Any, Any, None]]] = []

        # Pending service call futures keyed by correlation_id
        self._pending_calls: dict[str, asyncio.Future] = {}

    # -- Properties --------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return self._connected and self._authenticated

    @property
    def session_token(self) -> Optional[str]:
        return self._session_token

    @property
    def household_id(self) -> Optional[str]:
        return self._household_id

    async def wait_for_auth(self, timeout: float = 10.0) -> bool:
        """Block until first successful auth (so cloud creds are in env), or timeout."""
        try:
            await asyncio.wait_for(self._first_auth_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # -- Handler registration ----------------------------------------------- #

    def on(self, message_type: str, handler: MessageHandler) -> None:
        """Register a handler for a specific message type."""
        self._handlers[message_type] = handler

    def on_connected(self, callback: Callable[[], Coroutine[Any, Any, None]]) -> None:
        """Register a callback fired after the relay handshake completes
        (called once per (re)connect)."""
        self._connected_callbacks.append(callback)

    # -- Connection lifecycle ----------------------------------------------- #

    async def connect(self) -> None:
        """Open WebSocket and start the message loop with auto-reconnect."""
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
                    "Relay connection failed (%s), retrying in %.1fs",
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                self._reconnect_attempt += 1

    async def _connect_once(self) -> None:
        """Single connection attempt: open WS, auth, run message loop."""
        logger.info("Connecting to relay at %s", self.relay_url)

        async with websockets.connect(
            self.relay_url,
            ping_interval=None,  # We handle heartbeat ourselves
            close_timeout=5,
            max_size=2**20,  # 1MB max message
        ) as ws:
            self._ws = ws
            self._connected = True
            logger.info("WebSocket connected")

            # Authenticate
            if self._session_token and self._reconnect_attempt > 0:
                authed = await self._try_session_resume()
                if not authed:
                    authed = await self._authenticate()
            else:
                authed = await self._authenticate()

            if not authed:
                logger.error("Authentication failed")
                self._connected = False
                return

            self._authenticated = True
            self._reconnect_attempt = 0
            self._first_auth_event.set()
            logger.info(
                "Authenticated — household=%s (%s)",
                self._household_id,
                self._household_name,
            )

            # Fire post-connect callbacks (registry snapshot, etc.)
            for cb in self._connected_callbacks:
                try:
                    await cb()
                except Exception as exc:
                    logger.error("on_connected callback failed: %s", exc, exc_info=True)

            # Start heartbeat
            self._last_heartbeat_recv = time.monotonic()
            self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            # Run message loop
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
                # Connection dropped: wake up every in-flight service-call
                # waiter so they don't sit until their per-call timeout fires.
                # Each waiter's `finally` will pop its own correlation_id; we
                # also clear the dict here as a safety net.
                for fut in self._pending_calls.values():
                    if not fut.done():
                        fut.cancel()
                self._pending_calls.clear()

    async def disconnect(self) -> None:
        """Gracefully disconnect from the relay."""
        self._running = False

        if self._ws and self._connected:
            try:
                msg = Disconnect(
                    reason="addon_shutdown",
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

        # Cancel all pending service call futures
        for fut in self._pending_calls.values():
            if not fut.done():
                fut.cancel()
        self._pending_calls.clear()

        logger.info("Disconnected from relay")

    # -- Authentication ----------------------------------------------------- #

    async def _authenticate(self) -> bool:
        """Full HMAC-SHA256 auth handshake: AddonHello → AuthChallenge → AuthResponse → RelayWelcome."""
        hello = AddonHello(
            device_id=self.device_id,
            addon_version=self.addon_version,
            ha_version=self.ha_version,
            active_personas=self.active_personas,
            capabilities=self.capabilities,
            loaded_integrations=self.loaded_integrations,
        )
        await self._send_raw(hello)

        # Wait for AuthChallenge
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=_AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error("Auth timeout waiting for challenge")
            return False

        msg = _relay_adapter.validate_json(raw)
        if not isinstance(msg, AuthChallenge):
            logger.error("Expected AuthChallenge, got %s", type(msg).__name__)
            return False

        # Sign the nonce: HMAC-SHA256(key=SHA256(device_secret), msg=nonce)
        hmac_key = hashlib.sha256(self._device_secret.encode()).digest()
        signature = hmac.new(
            hmac_key,
            msg.challenge_nonce.encode(),
            hashlib.sha256,
        ).hexdigest()

        auth_resp = AuthResponse(
            device_id=self.device_id,
            signature=signature,
        )
        await self._send_raw(auth_resp)

        # Wait for RelayWelcome or ProtocolError
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=_AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            logger.error("Auth timeout waiting for welcome")
            return False

        msg = _relay_adapter.validate_json(raw)
        if isinstance(msg, RelayWelcome):
            self._session_token = msg.session_token
            self._household_id = msg.household_id
            self._household_name = msg.household_name
            self._apply_cloud_credentials(msg.cloud_credentials)
            return True
        elif isinstance(msg, ProtocolError):
            logger.error("Auth rejected: %s — %s", msg.code, msg.error_message)
            return False
        else:
            logger.error("Unexpected auth response: %s", type(msg).__name__)
            return False

    async def _try_session_resume(self) -> bool:
        """Attempt to resume a previous session using the stored session token."""
        if not self._session_token:
            return False

        reconnect = Reconnect(
            device_id=self.device_id,
            previous_session_token=self._session_token,
            reconnect_reason="connection_lost",
        )
        await self._send_raw(reconnect)

        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=_AUTH_TIMEOUT)
        except asyncio.TimeoutError:
            return False

        msg = _relay_adapter.validate_json(raw)
        if isinstance(msg, RelayWelcome):
            self._session_token = msg.session_token
            self._household_id = msg.household_id
            self._household_name = msg.household_name
            self._apply_cloud_credentials(msg.cloud_credentials)
            logger.info("Session resumed successfully")
            return True

        # Session expired or invalid — fall through to full auth
        return False

    # -- Cloud credentials -------------------------------------------------- #

    @staticmethod
    def _apply_cloud_credentials(creds: dict[str, str]) -> None:
        """Export relay-supplied secrets as env vars so cloud clients see them.

        The relay sends ``cloud_credentials`` in ``RelayWelcome`` so the user
        never has to paste API keys into the HA add-on options. Empty values
        are skipped so a dev override in the addon's environment wins. Each
        key is logged by name only — values are never printed.
        """
        if not creds:
            logger.info("Relay sent no cloud credentials (using addon env as-is)")
            return
        import os
        applied: list[str] = []
        for key, value in creds.items():
            if not value:
                continue
            env_name = key.upper()
            os.environ[env_name] = value
            applied.append(env_name)
        if applied:
            logger.info("Applied cloud credentials from relay: %s",
                        ", ".join(sorted(applied)))
        else:
            logger.info("Relay credentials payload was empty after filtering")

    # -- Message loop ------------------------------------------------------- #

    async def _message_loop(self) -> None:
        """Receive and dispatch messages until connection closes."""
        async for raw in self._ws:
            try:
                msg = _relay_adapter.validate_json(raw)
            except Exception as exc:
                logger.warning("Failed to parse relay message: %s", exc)
                continue

            await self._dispatch(msg)

    async def _dispatch(self, msg: BaseMessage) -> None:
        """Route a parsed message to the appropriate handler."""
        mt = msg.message_type

        # Built-in handlers
        if mt == "heartbeat":
            self._last_heartbeat_recv = time.monotonic()
            return

        if mt == "protocol_error":
            assert isinstance(msg, ProtocolError)
            logger.error("Relay error [%s]: %s", msg.code, msg.error_message)
            if msg.code == ErrorCode.AUTH_EXPIRED:
                # Force re-auth on next reconnect
                self._session_token = None
            return

        if mt == "disconnect":
            assert isinstance(msg, Disconnect)
            logger.info("Relay disconnect: %s (will_reconnect=%s)", msg.reason, msg.will_reconnect)
            return

        # Service call response — resolve pending future
        if mt == "service_call_response":
            assert isinstance(msg, ServiceCallResponse)
            fut = self._pending_calls.pop(msg.correlation_id, None)
            if fut and not fut.done():
                fut.set_result(msg)
            return

        # Delegate to registered handlers
        handler = self._handlers.get(mt)
        if handler:
            try:
                await handler(msg)
            except Exception as exc:
                logger.error("Handler for %s raised: %s", mt, exc, exc_info=True)
        else:
            logger.debug("No handler for message type: %s", mt)

    # -- Sending ------------------------------------------------------------ #

    async def send(self, message: BaseMessage) -> None:
        """Send a protocol message to the relay.

        Raises ConnectionError if not connected.
        """
        if not self._connected or not self._ws:
            raise ConnectionError("Not connected to relay")
        await self._send_raw(message)

    async def _send_raw(self, message: BaseMessage) -> None:
        """Serialize and send without connection checks (used during handshake)."""
        data = message.model_dump_json()
        await self._ws.send(data)

    async def send_entity_states(self, entities: list[EntityStatePayload]) -> None:
        """Push entity state updates to the relay."""
        if not entities:
            return
        msg = EntityStateUpdate(entities=entities)
        await self.send(msg)

    async def call_service_and_wait(
        self,
        correlation_id: str,
        response: ServiceCallResponse,
    ) -> None:
        """Send a service call response back to the relay."""
        await self.send(response)

    async def wait_for_service_response(
        self,
        correlation_id: str,
        timeout: float = 15.0,
    ) -> Optional[ServiceCallResponse]:
        """Wait for a correlated service call response from the relay.

        Returns the response, or ``None`` if the call timed out or the
        connection was dropped (the inner future is cancelled by
        ``_connect_once``'s cleanup in that case).

        On success the dispatcher pops the entry from ``_pending_calls`` when
        it resolves the future. The ``finally`` block here is a belt-and-
        suspenders guarantee that the entry is cleared in every path —
        timeout, cancellation, unexpected exception, or a race where the
        future was resolved but the dispatcher's pop was skipped.
        """
        fut: asyncio.Future[ServiceCallResponse] = asyncio.get_running_loop().create_future()
        self._pending_calls[correlation_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(
                "Service call %s timed out after %.1fs", correlation_id, timeout,
            )
            return None
        except asyncio.CancelledError:
            # If our own task was cancelled, propagate. If only the inner
            # future was cancelled (e.g. connection dropped), report no
            # response. Distinguish via fut.cancelled().
            if fut.cancelled():
                logger.warning(
                    "Service call %s aborted (connection lost)", correlation_id,
                )
                return None
            raise
        finally:
            # Idempotent: dispatcher already pops on success, but this
            # guarantees no leak on timeout / cancel / arbitrary exception.
            self._pending_calls.pop(correlation_id, None)

    # -- Heartbeat ---------------------------------------------------------- #

    async def _heartbeat_loop(self) -> None:
        """Send heartbeats every 30s and check for missed heartbeats from relay."""
        while self._running and self._connected:
            await asyncio.sleep(_HEARTBEAT_INTERVAL)

            # Check if relay has gone silent
            elapsed = time.monotonic() - self._last_heartbeat_recv
            if elapsed > _HEARTBEAT_INTERVAL * _HEARTBEAT_MISS_LIMIT:
                logger.warning(
                    "Missed %d heartbeats (%.0fs), closing connection",
                    _HEARTBEAT_MISS_LIMIT,
                    elapsed,
                )
                if self._ws:
                    await self._ws.close()
                return

            # Send our heartbeat
            try:
                hb = Heartbeat(
                    uptime_seconds=time.monotonic() - self._start_time,
                    entity_count=self.entity_count,
                )
                await self._send_raw(hb)
            except Exception as exc:
                logger.warning("Failed to send heartbeat: %s", exc)
                return

    # -- Reconnection backoff ----------------------------------------------- #

    def _backoff_delay(self) -> float:
        """Exponential backoff with jitter."""
        delay = min(
            _RECONNECT_BASE_DELAY * (2 ** self._reconnect_attempt),
            _RECONNECT_MAX_DELAY,
        )
        jitter = delay * _RECONNECT_JITTER_FACTOR * random.random()
        return delay + jitter
