"""Synork Home — Home Assistant Bridge.

Bidirectional bridge between the HA WebSocket API and the Synork relay.

Addon → HA direction:
  - Executes ServiceCallRequest messages as HA service calls
  - Responds to EntityStateQuery with current HA entity states

HA → Addon direction:
  - Subscribes to HA state_changed events
  - Forwards entity state changes to the relay via RelayClient

Uses HA's WebSocket API (ws://supervisor/core/websocket) with the
SUPERVISOR_TOKEN for authentication. The Supervisor token is injected
by HA into the addon container automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, Optional

import aiohttp

from shared.protocol import (
    EntityStatePayload,
    EntityStateQuery,
    ServiceCallRequest,
    ServiceCallResponse,
)

logger = logging.getLogger("synork.ha_bridge")

StateChangeCallback = Callable[[list[EntityStatePayload]], Coroutine[Any, Any, None]]


class HABridge:
    """Bridge to the Home Assistant Supervisor/Core API.

    Two communication channels:
      1. REST API (http://supervisor/core/api/) — for one-off queries
      2. WebSocket API (ws://supervisor/core/websocket) — for event subscriptions

    Both authenticate via SUPERVISOR_TOKEN.
    """

    def __init__(
        self,
        ha_url: str = "http://supervisor/core",
        ha_token: Optional[str] = None,
    ) -> None:
        self.ha_url = ha_url.rstrip("/")
        self.ha_token = ha_token or os.environ.get("SUPERVISOR_TOKEN", "")
        self._ws_url = self.ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/websocket"

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWSMessage] = None
        self._ws_connection: Optional[aiohttp.ClientWebSocketResponse] = None
        self._msg_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._running = False
        self._event_task: Optional[asyncio.Task] = None
        self._state_change_callbacks: list[StateChangeCallback] = []
        self._subscription_id: Optional[int] = None
        self._entity_states: dict[str, dict[str, Any]] = {}

    # -- Properties --------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return self._ws_connection is not None and not self._ws_connection.closed

    @property
    def entity_count(self) -> int:
        return len(self._entity_states)

    # -- Callbacks ---------------------------------------------------------- #

    def on_state_change(self, callback: StateChangeCallback) -> None:
        """Register a callback for entity state changes."""
        self._state_change_callbacks.append(callback)

    # -- Connection --------------------------------------------------------- #

    async def connect(self) -> None:
        """Connect to HA's WebSocket API and subscribe to state changes."""
        self._running = True
        self._session = aiohttp.ClientSession()

        try:
            self._ws_connection = await self._session.ws_connect(
                self._ws_url,
                heartbeat=30,
            )
        except Exception as exc:
            logger.error("Failed to connect to HA WebSocket: %s", exc)
            raise

        # HA sends auth_required as first message
        msg = await self._ws_connection.receive_json()
        if msg.get("type") != "auth_required":
            raise RuntimeError(f"Expected auth_required, got: {msg.get('type')}")

        # Authenticate
        await self._ws_connection.send_json({
            "type": "auth",
            "access_token": self.ha_token,
        })

        msg = await self._ws_connection.receive_json()
        if msg.get("type") != "auth_ok":
            raise RuntimeError(f"HA auth failed: {msg}")

        logger.info("Connected to HA WebSocket (version: %s)", msg.get("ha_version"))

        # Start event listening loop FIRST so subsequent _ws_call() responses
        # can be dispatched into the pending-futures table.
        self._event_task = asyncio.create_task(self._event_loop())

        # Load initial states
        await self._load_all_states()

        # Subscribe to state_changed events
        await self._subscribe_state_changes()

    async def disconnect(self) -> None:
        """Disconnect from HA."""
        self._running = False

        if self._event_task:
            self._event_task.cancel()
            try:
                await self._event_task
            except asyncio.CancelledError:
                pass

        if self._ws_connection and not self._ws_connection.closed:
            await self._ws_connection.close()

        if self._session and not self._session.closed:
            await self._session.close()

        # Cancel pending futures
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        logger.info("Disconnected from HA")

    # -- WebSocket messaging ------------------------------------------------ #

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

    async def _ws_send(self, payload: dict[str, Any]) -> int:
        """Send a message on the HA WebSocket and return the message ID."""
        msg_id = self._next_id()
        payload["id"] = msg_id
        await self._ws_connection.send_json(payload)
        return msg_id

    async def _ws_call(self, payload: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        """Send a WS message and wait for the response."""
        msg_id = await self._ws_send(payload)
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(msg_id, None)
            raise

    # -- State management --------------------------------------------------- #

    async def _load_all_states(self) -> None:
        """Fetch all entity states from HA on connect."""
        result = await self._ws_call({"type": "get_states"})
        if result.get("success"):
            for state in result.get("result", []):
                entity_id = state.get("entity_id", "")
                self._entity_states[entity_id] = state
            logger.info("Loaded %d entity states from HA", len(self._entity_states))

    async def _subscribe_state_changes(self) -> None:
        """Subscribe to state_changed events on the HA WS."""
        msg_id = await self._ws_send({
            "type": "subscribe_events",
            "event_type": "state_changed",
        })
        self._subscription_id = msg_id

    async def _event_loop(self) -> None:
        """Listen for WS messages and dispatch events/responses."""
        try:
            async for ws_msg in self._ws_connection:
                if ws_msg.type == aiohttp.WSMsgType.TEXT:
                    data = json.loads(ws_msg.data)
                    await self._handle_ws_message(data)
                elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    logger.warning("HA WebSocket closed/error")
                    break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("HA event loop error: %s", exc, exc_info=True)

    async def _handle_ws_message(self, data: dict[str, Any]) -> None:
        """Handle a parsed WS message from HA."""
        msg_type = data.get("type")
        msg_id = data.get("id")

        # Response to a command we sent
        if msg_type == "result" and msg_id in self._pending:
            fut = self._pending.pop(msg_id)
            if not fut.done():
                fut.set_result(data)
            return

        # Event subscription
        if msg_type == "event":
            event = data.get("event", {})
            event_type = event.get("event_type")

            if event_type == "state_changed":
                await self._handle_state_changed(event.get("data", {}))
            return

    async def _handle_state_changed(self, data: dict[str, Any]) -> None:
        """Process a state_changed event from HA."""
        entity_id = data.get("entity_id", "")
        new_state = data.get("new_state")

        if not new_state:
            # Entity removed
            self._entity_states.pop(entity_id, None)
            return

        # Skip our own entities to avoid loops
        if entity_id.startswith(("sensor.synork_", "binary_sensor.synork_")):
            return

        # Update local cache
        self._entity_states[entity_id] = new_state

        # Build relay payload
        now = datetime.now(timezone.utc)
        payload = EntityStatePayload(
            entity_id=entity_id,
            state=str(new_state.get("state", "")),
            attributes=new_state.get("attributes", {}),
            last_changed=new_state.get("last_changed", now.isoformat()),
            last_updated=new_state.get("last_updated", now.isoformat()),
        )

        # Notify callbacks
        for cb in self._state_change_callbacks:
            try:
                await cb([payload])
            except Exception as exc:
                logger.error("State change callback error: %s", exc)

    # -- Public API --------------------------------------------------------- #

    async def get_states(self) -> list[dict[str, Any]]:
        """Return all cached entity states."""
        return list(self._entity_states.values())

    async def get_services(self) -> dict[str, Any]:
        """Fetch the loaded HA services map ({domain: {service: ...}})."""
        result = await self._ws_call({"type": "get_services"})
        if not result.get("success"):
            return {}
        return result.get("result", {}) or {}

    # -- Config flow / integration provisioning ---------------------------- #

    async def list_config_entries(self, domain: Optional[str] = None) -> list[dict[str, Any]]:
        """Return the list of configured integrations (optionally filtered by domain)."""
        payload: dict[str, Any] = {"type": "config_entries/get"}
        if domain:
            payload["domain"] = domain
        result = await self._ws_call(payload)
        if not result.get("success"):
            return []
        return result.get("result", []) or []

    async def has_config_entry(self, domain: str) -> bool:
        """True if HA already has at least one config entry for `domain`."""
        try:
            entries = await self.list_config_entries(domain)
            return len(entries) > 0
        except Exception:
            return False

    async def start_config_flow(
        self,
        handler: str,
        show_advanced_options: bool = True,
    ) -> dict[str, Any]:
        """Initiate a config flow for an integration. Returns the first step payload."""
        result = await self._ws_call({
            "type": "config_entries/flow/init",
            "handler": handler,
            "show_advanced_options": show_advanced_options,
        })
        if not result.get("success"):
            raise RuntimeError(f"flow/init failed for {handler}: {result.get('error')}")
        return result.get("result", {}) or {}

    async def submit_config_flow_step(
        self,
        flow_id: str,
        user_input: dict[str, Any],
    ) -> dict[str, Any]:
        """Submit user input for the current config flow step."""
        result = await self._ws_call({
            "type": "config_entries/flow/configure",
            "flow_id": flow_id,
            "user_input": user_input,
        })
        if not result.get("success"):
            raise RuntimeError(f"flow/configure failed: {result.get('error')}")
        return result.get("result", {}) or {}

    async def abort_config_flow(self, flow_id: str) -> None:
        """Best-effort abort of a config flow we couldn't complete."""
        try:
            await self._ws_call({
                "type": "config_entries/flow/abort",
                "flow_id": flow_id,
            })
        except Exception:
            pass

    async def auto_complete_config_flow(
        self,
        handler: str,
        step_inputs: dict[str, dict[str, Any]],
        default_input: Optional[dict[str, Any]] = None,
        max_steps: int = 8,
    ) -> bool:
        """Walk a config flow to completion using pre-supplied per-step inputs.

        ``step_inputs`` maps step_id -> user_input dict. ``default_input`` is
        used for any step we don't recognise. Returns True when the flow ends
        in ``create_entry``, False otherwise (the flow is aborted on failure
        so HA isn't left with a half-configured entry).
        """
        try:
            step = await self.start_config_flow(handler)
        except Exception as exc:
            logger.warning("Could not start %s config flow: %s", handler, exc)
            return False

        flow_id = step.get("flow_id") or ""
        if not flow_id:
            return step.get("type") == "create_entry"

        for _ in range(max_steps):
            step_type = step.get("type")
            if step_type == "create_entry":
                return True
            if step_type == "abort":
                logger.info(
                    "%s config flow aborted at step %s: %s",
                    handler, step.get("step_id"), step.get("reason"),
                )
                return False
            if step_type != "form":
                # Unknown / external_step / progress — bail out cleanly
                await self.abort_config_flow(flow_id)
                return False

            step_id = step.get("step_id") or ""
            user_input = step_inputs.get(step_id, default_input or {})
            try:
                step = await self.submit_config_flow_step(flow_id, user_input)
            except Exception as exc:
                logger.warning(
                    "%s config flow step %s failed: %s", handler, step_id, exc,
                )
                await self.abort_config_flow(flow_id)
                return False

        await self.abort_config_flow(flow_id)
        return False

    async def get_entity_state(self, entity_id: str) -> Optional[dict[str, Any]]:
        """Get the cached state of a specific entity."""
        return self._entity_states.get(entity_id)

    async def call_service(
        self,
        domain: str,
        service: str,
        target: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Call a HA service via the WebSocket API."""
        payload: dict[str, Any] = {
            "type": "call_service",
            "domain": domain,
            "service": service,
        }
        if target:
            payload["target"] = target
        if data:
            payload["service_data"] = data

        result = await self._ws_call(payload)
        return result

    async def handle_service_call_request(self, request: ServiceCallRequest) -> ServiceCallResponse:
        """Execute a ServiceCallRequest from the relay as a HA service call.

        Returns a ServiceCallResponse with the result.
        """
        try:
            target = request.target
            result = await self.call_service(
                domain=request.domain,
                service=request.service,
                target=target,
                data=request.service_data,
            )

            success = result.get("success", False)
            error_msg = None
            if not success:
                error_msg = str(result.get("error", {}).get("message", "Unknown error"))

            # Determine affected entities from target
            affected: list[str] = []
            if target:
                entity_ids = target.get("entity_id", [])
                if isinstance(entity_ids, str):
                    affected = [entity_ids]
                elif isinstance(entity_ids, list):
                    affected = entity_ids

            return ServiceCallResponse(
                correlation_id=request.correlation_id,
                success=success,
                error_message=error_msg,
                affected_entities=affected,
            )

        except Exception as exc:
            logger.error(
                "Service call failed: %s.%s — %s",
                request.domain,
                request.service,
                exc,
            )
            return ServiceCallResponse(
                correlation_id=request.correlation_id,
                success=False,
                error_message=str(exc),
            )

    async def handle_entity_state_query(self, query: EntityStateQuery) -> list[EntityStatePayload]:
        """Respond to an EntityStateQuery from the relay with current states."""
        now = datetime.now(timezone.utc)
        results: list[EntityStatePayload] = []

        for entity_id in query.entity_ids:
            state = self._entity_states.get(entity_id)
            if state:
                results.append(EntityStatePayload(
                    entity_id=entity_id,
                    state=str(state.get("state", "")),
                    attributes=state.get("attributes", {}),
                    last_changed=state.get("last_changed", now.isoformat()),
                    last_updated=state.get("last_updated", now.isoformat()),
                ))

        return results

    async def get_config(self) -> dict[str, Any]:
        """Fetch the HA configuration via WebSocket."""
        result = await self._ws_call({"type": "get_config"})
        return result.get("result", {})

    async def get_ha_version(self) -> str:
        """Get the running HA version."""
        config = await self.get_config()
        return config.get("version", "unknown")
