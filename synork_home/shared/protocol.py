"""
Synork Home — Relay ↔ Addon WebSocket Protocol

Defines all message types exchanged between the on-device addon and the
Synork backend relay over a persistent WebSocket connection.

Every message is wrapped in a base envelope with message_id, timestamp,
and message_type. The message_type field is a Literal discriminator that
enables polymorphic deserialization via Pydantic v2's Discriminator.

Contract locked in Session 1 (2026-04-25). Subsequent sessions MUST NOT
change field names or remove fields — only add new optional fields or
new message types.

Implementation: Session 2 (Backend Relay) + Session 3 (Addon Core).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MessageDirection(str, Enum):
    """Which side originates the message."""
    ADDON_TO_RELAY = "addon_to_relay"
    RELAY_TO_ADDON = "relay_to_addon"
    BIDIRECTIONAL = "bidirectional"


class ErrorCode(str, Enum):
    """Standardised error codes for ProtocolError messages."""
    AUTH_FAILED = "auth_failed"
    AUTH_EXPIRED = "auth_expired"
    PERMISSION_DENIED = "permission_denied"
    ENTITY_NOT_FOUND = "entity_not_found"
    SERVICE_CALL_FAILED = "service_call_failed"
    INVALID_MESSAGE = "invalid_message"
    RATE_LIMITED = "rate_limited"
    RELAY_UNAVAILABLE = "relay_unavailable"
    ADDON_UNAVAILABLE = "addon_unavailable"
    INTERNAL_ERROR = "internal_error"


class AssistantMode(str, Enum):
    """How the user is interacting with the assistant."""
    VOICE = "voice"
    TEXT = "text"


# ---------------------------------------------------------------------------
# Base envelope
# ---------------------------------------------------------------------------

class BaseMessage(BaseModel):
    """
    Base envelope for every relay ↔ addon message.

    All concrete message types inherit from this. The ``message_type`` field
    is overridden with a ``Literal`` on each subclass so the discriminated
    union works.
    """

    message_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="Unique identifier for this message, used for correlation.",
    )
    timestamp: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC timestamp when the message was created.",
    )
    message_type: str = Field(
        ...,
        description="Discriminator — each subclass sets a Literal value.",
    )


# ---------------------------------------------------------------------------
# Auth / Handshake
# ---------------------------------------------------------------------------

class AddonHello(BaseMessage):
    """
    First message sent by the addon after WebSocket connect.

    Contains the addon's identity (device_id from pairing), software version,
    and active persona list so the relay knows what capabilities are online.
    """
    message_type: Literal["addon_hello"] = "addon_hello"
    device_id: str = Field(..., description="Unique device identifier assigned during pairing.")
    addon_version: str = Field(..., description="Semantic version of the addon software.")
    ha_version: str = Field(..., description="Home Assistant Core version running on the device.")
    active_personas: list[str] = Field(
        default_factory=list,
        description="List of active persona names (e.g. ['hub', 'speaker']).",
    )
    capabilities: list[str] = Field(
        default_factory=list,
        description="Hardware capabilities detected (from HardwareCapability enum values).",
    )
    loaded_integrations: list[str] = Field(
        default_factory=list,
        description="Names of HA integrations / service domains currently loaded (e.g. ['zha', 'zwave_js', 'light']).",
    )


class AuthChallenge(BaseMessage):
    """
    Relay responds to AddonHello with a challenge the addon must sign
    using its device secret (established during pairing).
    """
    message_type: Literal["auth_challenge"] = "auth_challenge"
    challenge_nonce: str = Field(..., description="Random nonce the addon must sign.")
    challenge_expires_at: datetime = Field(
        ...,
        description="UTC deadline — addon must respond before this.",
    )


class AuthResponse(BaseMessage):
    """
    Addon's response to the AuthChallenge.

    Contains an HMAC signature of the challenge nonce using the device secret.
    """
    message_type: Literal["auth_response"] = "auth_response"
    device_id: str = Field(..., description="Same device_id as in AddonHello.")
    signature: str = Field(..., description="HMAC-SHA256 of the challenge nonce using device secret.")


class RelayWelcome(BaseMessage):
    """
    Relay confirms authentication succeeded.

    Includes the household context and relay-side configuration the addon
    needs to operate.
    """
    message_type: Literal["relay_welcome"] = "relay_welcome"
    household_id: str = Field(..., description="Household this device belongs to.")
    household_name: str = Field(..., description="Human-readable household name.")
    member_count: int = Field(..., description="Number of members in the household.")
    relay_features: list[str] = Field(
        default_factory=list,
        description="Feature flags enabled for this household on the relay side.",
    )
    session_token: str = Field(
        ...,
        description=(
            "Short-lived session token. The addon uses it as a Bearer token "
            "on subsequent HTTP calls to the Synork relay (e.g. POST "
            "/api/home/tts/synthesize, /api/home/ai/invoke). The relay "
            "holds the upstream provider keys server-side \u2014 they are "
            "never sent to the device."
        ),
    )
    session_expires_at: datetime = Field(..., description="When the session token expires.")


# ---------------------------------------------------------------------------
# Device state messages
# ---------------------------------------------------------------------------

class EntityStateUpdate(BaseMessage):
    """
    Addon pushes a state change for one or more HA entities to the relay.

    Sent whenever HA fires a state_changed event for entities the relay
    is subscribed to.
    """
    message_type: Literal["entity_state_update"] = "entity_state_update"
    entities: list[EntityStatePayload] = Field(
        ...,
        description="One or more entity state snapshots.",
    )


class EntityStatePayload(BaseModel):
    """Single entity state snapshot."""
    entity_id: str = Field(..., description="HA entity ID (e.g. 'light.living_room').")
    state: str = Field(..., description="Current state string (e.g. 'on', 'off', '22.5').")
    attributes: dict[str, Any] = Field(
        default_factory=dict,
        description="HA entity attributes (brightness, color_temp, etc.).",
    )
    last_changed: datetime = Field(..., description="When the state last changed in HA.")
    last_updated: datetime = Field(..., description="When the state was last updated in HA.")


class EntityStateQuery(BaseMessage):
    """
    Relay asks the addon for current state of specific entities.

    Used when the relay needs a fresh snapshot (e.g. user opens dashboard).
    """
    message_type: Literal["entity_state_query"] = "entity_state_query"
    entity_ids: list[str] = Field(
        ...,
        description="List of HA entity IDs to query.",
    )


# ---------------------------------------------------------------------------
# Device registry messages
# ---------------------------------------------------------------------------

class HaAreaPayload(BaseModel):
    """A single Home Assistant area (room)."""
    area_id: str = Field(..., description="HA area_registry id.")
    name: str = Field(..., description="Human-readable area name.")
    icon: Optional[str] = Field(None, description="MDI icon name (e.g. 'mdi:sofa').")
    picture: Optional[str] = Field(None, description="URL/path to area picture.")
    floor_id: Optional[str] = Field(None, description="HA floor id this area belongs to.")
    aliases: list[str] = Field(default_factory=list, description="Alternate names for voice / search.")


class HaDevicePayload(BaseModel):
    """A single Home Assistant device with its child entities."""
    ha_device_id: str = Field(..., description="HA device_registry id.")
    name: Optional[str] = Field(None, description="HA-assigned device name.")
    name_by_user: Optional[str] = Field(None, description="HA user-overridden name.")
    manufacturer: Optional[str] = Field(None, description="Device manufacturer.")
    model: Optional[str] = Field(None, description="Device model identifier.")
    area_id: Optional[str] = Field(None, description="HA area id this device is assigned to.")
    entity_ids: list[str] = Field(
        default_factory=list,
        description="HA entity IDs that belong to this device.",
    )
    disabled: bool = Field(default=False, description="True if disabled in HA.")


class DeviceRegistrySnapshot(BaseMessage):
    """
    Addon pushes the current HA device + entity registry mapping to the relay.

    Sent on first connect, then again whenever HA fires
    `device_registry_updated` or `entity_registry_updated` events.
    """
    message_type: Literal["device_registry_snapshot"] = "device_registry_snapshot"
    devices: list[HaDevicePayload] = Field(
        default_factory=list,
        description="All known HA devices and their entities.",
    )
    orphan_entity_ids: list[str] = Field(
        default_factory=list,
        description="Entities not bound to any device (helpers, integrations without a device).",
    )
    areas: list[HaAreaPayload] = Field(
        default_factory=list,
        description="All known HA areas (rooms). Empty list on legacy addons.",
    )


class ServiceCallRequest(BaseMessage):
    """
    Relay instructs the addon to call an HA service.

    The addon validates the call against local HA state and executes it.
    """
    message_type: Literal["service_call_request"] = "service_call_request"
    correlation_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="Correlates this request with its ServiceCallResponse.",
    )
    domain: str = Field(..., description="HA service domain (e.g. 'light', 'climate').")
    service: str = Field(..., description="HA service name (e.g. 'turn_on', 'set_temperature').")
    target: Optional[dict[str, Any]] = Field(
        None,
        description="HA service target (entity_id, area_id, device_id).",
    )
    service_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Service call data payload.",
    )
    requesting_user_id: str = Field(..., description="Synork user ID who initiated this action.")
    household_id: str = Field(..., description="Household context for permission checks.")


class ServiceCallResponse(BaseMessage):
    """
    Addon reports the result of a ServiceCallRequest.
    """
    message_type: Literal["service_call_response"] = "service_call_response"
    correlation_id: str = Field(..., description="Matches the ServiceCallRequest.")
    success: bool = Field(..., description="Whether the HA service call succeeded.")
    error_message: Optional[str] = Field(None, description="Error details if success is False.")
    affected_entities: list[str] = Field(
        default_factory=list,
        description="Entity IDs whose state changed as a result.",
    )


# ---------------------------------------------------------------------------
# Assistant messages
# ---------------------------------------------------------------------------

class AssistantInvoke(BaseMessage):
    """
    A user invokes the assistant (voice or text).

    Sent from the relay to the addon when the request needs local context,
    or from the addon to the relay when a local wake-word triggers a query
    that needs cloud intelligence.
    """
    message_type: Literal["assistant_invoke"] = "assistant_invoke"
    asking_user_id: str = Field(..., description="Synork user ID of the person asking.")
    household_id: str = Field(..., description="Household context.")
    query: str = Field(..., description="The user's query text (transcribed if voice).")
    language: str = Field(default="hu", description="ISO 639-1 language code.")
    mode: AssistantMode = Field(..., description="Voice or text interaction mode.")
    conversation_id: Optional[str] = Field(
        None,
        description="Ongoing conversation ID for multi-turn context.",
    )
    audio_data_ref: Optional[str] = Field(
        None,
        description="Reference to audio blob if mode is VOICE and raw audio is needed.",
    )


class AssistantResponse(BaseMessage):
    """
    Complete assistant response (non-streaming).
    """
    message_type: Literal["assistant_response"] = "assistant_response"
    conversation_id: str = Field(..., description="Conversation context ID.")
    text: str = Field(..., description="The assistant's text response.")
    language: str = Field(default="hu", description="Language of the response.")
    tool_calls: list[AssistantToolCall] = Field(
        default_factory=list,
        description="Any tool calls the assistant made (for transparency/logging).",
    )
    audio_url: Optional[str] = Field(
        None,
        description="URL to TTS audio if voice mode was requested.",
    )
    dashboard_actions: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Dashboard mutations the assistant wants to apply.",
    )


class AssistantToolCall(BaseModel):
    """Record of a single tool call made during assistant processing."""
    tool_name: str = Field(..., description="MCP tool name invoked.")
    tool_input: dict[str, Any] = Field(default_factory=dict, description="Input passed to the tool.")
    tool_output: Optional[str] = Field(None, description="Output returned by the tool.")
    success: bool = Field(default=True, description="Whether the tool call succeeded.")


class AssistantStreamChunk(BaseMessage):
    """
    Streaming chunk of an assistant response.

    Used for real-time text display while the full response is being generated.
    """
    message_type: Literal["assistant_stream_chunk"] = "assistant_stream_chunk"
    conversation_id: str = Field(..., description="Conversation context ID.")
    chunk_index: int = Field(..., description="Sequential chunk number starting from 0.")
    text_delta: str = Field(..., description="Incremental text content.")
    is_final: bool = Field(default=False, description="True if this is the last chunk.")
    language: str = Field(default="hu", description="Language of the chunk.")


# ---------------------------------------------------------------------------
# Lifecycle messages
# ---------------------------------------------------------------------------

class Heartbeat(BaseMessage):
    """
    Bidirectional keep-alive.

    Sent every 30 seconds by both sides. If 3 consecutive heartbeats are
    missed, the connection is considered dead and reconnection is triggered.
    """
    message_type: Literal["heartbeat"] = "heartbeat"
    uptime_seconds: float = Field(..., description="Seconds since the sender started.")
    entity_count: Optional[int] = Field(
        None,
        description="Number of entities tracked (addon→relay only).",
    )


class Disconnect(BaseMessage):
    """
    Graceful disconnect notification.

    Sent before intentional disconnect (addon shutdown, relay maintenance).
    """
    message_type: Literal["disconnect"] = "disconnect"
    reason: str = Field(..., description="Human-readable disconnect reason.")
    will_reconnect: bool = Field(
        default=True,
        description="Whether the sender intends to reconnect.",
    )
    estimated_downtime_seconds: Optional[int] = Field(
        None,
        description="Estimated seconds until reconnect, if known.",
    )


class Reconnect(BaseMessage):
    """
    Reconnection request/acknowledgement.

    Addon sends after re-establishing WebSocket. Relay responds with
    a RelayWelcome (re-auth flow) or a Reconnect ack if session is still valid.
    """
    message_type: Literal["reconnect"] = "reconnect"
    device_id: str = Field(..., description="Device identifier.")
    previous_session_token: Optional[str] = Field(
        None,
        description="Previous session token if attempting session resumption.",
    )
    reconnect_reason: str = Field(default="connection_lost", description="Why reconnection happened.")


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------

class ProtocolError(BaseMessage):
    """
    Error message for any protocol-level failure.

    Can be sent by either side. The correlation_id links to the message
    that caused the error, if applicable.
    """
    message_type: Literal["protocol_error"] = "protocol_error"
    code: ErrorCode = Field(..., description="Structured error code.")
    error_message: str = Field(..., description="Human-readable error description.")
    details: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional error context (entity_id, service, etc.).",
    )
    correlation_id: Optional[str] = Field(
        None,
        description="message_id of the message that caused this error.",
    )


# ---------------------------------------------------------------------------
# HA registry mutations (areas, devices, entities) — generic RPC
# ---------------------------------------------------------------------------

# kind = which HA registry to mutate; op = create / update / delete.
HaRegistryKind = Literal["area", "device", "entity"]
HaRegistryOp = Literal["create", "update", "delete"]


class HaRegistryMutationRequest(BaseMessage):
    """
    Relay → addon: mutate the HA area / device / entity registry.

    Lets Synork drive HA's config without users touching HA's frontend.
    The addon translates this into the matching ``config/<kind>_registry/<op>``
    HA WebSocket call. ``params`` is the body of that HA call (e.g.
    ``{"name": "Living room"}`` for area.create, ``{"area_id": "abc"}``
    for a device.update area-assignment).
    """
    message_type: Literal["ha_registry_mutation_request"] = "ha_registry_mutation_request"
    correlation_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="Correlates this request with its HaRegistryMutationResponse.",
    )
    kind: HaRegistryKind = Field(..., description="Which HA registry to touch.")
    op: HaRegistryOp = Field(..., description="create / update / delete.")
    params: dict[str, Any] = Field(
        default_factory=dict,
        description="Body of the HA WS call (area_id, device_id, entity_id, name, ...).",
    )
    requesting_user_id: str = Field(..., description="Synork user ID for audit / permission.")
    household_id: str = Field(..., description="Household context for permission checks.")


class HaRegistryMutationResponse(BaseMessage):
    """Addon → relay: outcome of a HaRegistryMutationRequest."""
    message_type: Literal["ha_registry_mutation_response"] = "ha_registry_mutation_response"
    correlation_id: str = Field(..., description="Matches the request.")
    success: bool = Field(..., description="True if HA accepted the mutation.")
    error_message: Optional[str] = Field(None, description="Error details if success is False.")
    result: dict[str, Any] = Field(
        default_factory=dict,
        description="HA's response payload (e.g. the created area row).",
    )


# ---------------------------------------------------------------------------
# Apple HomeKit pairing — multi-step config_flow proxy
# ---------------------------------------------------------------------------

class HomeKitPairingRequest(BaseMessage):
    """
    Relay → addon: drive HA's ``homekit_controller`` config_flow.

    A single message type covers the whole flow (start / step / abort) using
    ``action`` + optional ``flow_id`` + ``user_input``. Mirrors HA's WS API.
    """
    message_type: Literal["homekit_pairing_request"] = "homekit_pairing_request"
    correlation_id: str = Field(
        default_factory=lambda: uuid.uuid4().hex,
        description="Correlates this request with its HomeKitPairingResponse.",
    )
    action: Literal["start", "step", "abort"] = Field(
        ...,
        description="start: open a new flow. step: submit user input. abort: cancel a flow.",
    )
    flow_id: Optional[str] = Field(
        None,
        description="Existing flow_id (required for step / abort).",
    )
    user_input: dict[str, Any] = Field(
        default_factory=dict,
        description="Form data for the current step (e.g. {'pairing_code': '123-45-678'}).",
    )
    requesting_user_id: str = Field(..., description="Synork user ID who initiated the action.")
    household_id: str = Field(..., description="Household context for permission checks.")


class HomeKitPairingResponse(BaseMessage):
    """Addon → relay: result of a HomeKitPairingRequest step."""
    message_type: Literal["homekit_pairing_response"] = "homekit_pairing_response"
    correlation_id: str = Field(..., description="Matches the request.")
    success: bool = Field(..., description="True if HA accepted the action.")
    error_message: Optional[str] = Field(None, description="Error details if success is False.")
    # HA flow shape — passed straight through to the UI
    flow_id: Optional[str] = Field(None, description="Flow ID for subsequent steps.")
    flow_type: Optional[str] = Field(
        None,
        description="HA flow result type: 'form', 'create_entry', 'abort', 'menu', 'progress', 'external_step'.",
    )
    step_id: Optional[str] = Field(None, description="Current step id (for 'form'/'menu').")
    data_schema: Optional[list[dict[str, Any]]] = Field(
        None,
        description="HA voluptuous schema serialised as a list of {name, type, required, ...}.",
    )
    description_placeholders: Optional[dict[str, Any]] = Field(
        None,
        description="HA's i18n placeholders for the current step.",
    )
    errors: Optional[dict[str, Any]] = Field(
        None,
        description="Per-field validation errors from the previous step.",
    )
    discovered_devices: Optional[list[dict[str, Any]]] = Field(
        None,
        description="When step expects a device pick (e.g. 'pair'): list of {id, name, model, category}.",
    )
    created_entry: Optional[dict[str, Any]] = Field(
        None,
        description="On flow_type='create_entry': {entry_id, title, unique_id}.",
    )


# ---------------------------------------------------------------------------
# Discriminated union — the single type to use for deserialization
# ---------------------------------------------------------------------------

RelayMessage = Annotated[
    Union[
        AddonHello,
        AuthChallenge,
        AuthResponse,
        RelayWelcome,
        EntityStateUpdate,
        EntityStateQuery,
        DeviceRegistrySnapshot,
        ServiceCallRequest,
        ServiceCallResponse,
        HaRegistryMutationRequest,
        HaRegistryMutationResponse,
        HomeKitPairingRequest,
        HomeKitPairingResponse,
        AssistantInvoke,
        AssistantResponse,
        AssistantStreamChunk,
        Heartbeat,
        Disconnect,
        Reconnect,
        ProtocolError,
    ],
    Field(discriminator="message_type"),
]
"""
Discriminated union of all relay protocol messages.

Usage::

    from pydantic import TypeAdapter
    adapter = TypeAdapter(RelayMessage)
    msg = adapter.validate_json(raw_bytes)
"""


# ═══════════════════════════════════════════════════════════════════════════
# Hub ↔ Assistant (Satellite) Protocol
#
# Separate from the Relay protocol. Operates on the local network only.
# The Hub is the WebSocket server (port 8765); Assistants are clients.
#
# Design decision (Session 16): A parallel SatelliteMessage union rather
# than extending RelayMessage, because these are distinct security domains.
# Hub↔Relay runs over the internet with Synork account credentials.
# Hub↔Assistant runs on the local LAN with locally-issued device secrets.
# Separate TypeAdapters enforce the boundary at the type level.
#
# Reuses BaseMessage envelope, Heartbeat, Disconnect, Reconnect, and
# ProtocolError from the relay protocol — same serialization format.
# ═══════════════════════════════════════════════════════════════════════════


# ---------------------------------------------------------------------------
# Satellite Auth / Handshake
# ---------------------------------------------------------------------------

class AssistantHello(BaseMessage):
    """
    First message sent by an Assistant when connecting to a Hub.

    Contains the Assistant's identity, hardware capabilities, and optional
    room assignment. If this is the first connection (no device_secret_hash),
    the Hub treats it as a new pairing request that needs user approval.
    """
    message_type: Literal["assistant_hello"] = "assistant_hello"
    satellite_id: str = Field(
        ...,
        description="Unique identifier for this Assistant device (derived from machine-id).",
    )
    device_secret_hash: Optional[str] = Field(
        None,
        description="SHA-256 hash of the device secret (None for first-time pairing).",
    )
    hardware_capabilities: list[str] = Field(
        default_factory=list,
        description="HardwareCapability enum values detected on the Assistant.",
    )
    persona_config: Optional[str] = Field(
        None,
        description="JSON-encoded persona configuration active on the Assistant.",
    )
    claimed_room: Optional[str] = Field(
        None,
        description="Room name the Assistant claims to be in (user-configurable).",
    )
    addon_version: str = Field(default="0.1.0", description="Assistant addon version.")
    platform: str = Field(default="unknown", description="Hardware platform (pi_zero_2w, pi_3, pi_4, etc.).")


class HubChallenge(BaseMessage):
    """
    Hub's auth challenge to the Assistant.

    For returning Assistants: standard HMAC challenge.
    For new Assistants: includes a pairing_request_id that must be approved
    by the user before the handshake can complete.
    """
    message_type: Literal["hub_challenge"] = "hub_challenge"
    challenge_nonce: str = Field(..., description="Random nonce the Assistant must sign.")
    challenge_expires_at: datetime = Field(
        ...,
        description="UTC deadline for the Assistant's response.",
    )
    pairing_request_id: Optional[str] = Field(
        None,
        description="Non-null for new pairing requests awaiting user approval.",
    )


class AssistantAuthResponse(BaseMessage):
    """
    Assistant's HMAC response to the Hub's challenge.

    The signature is HMAC-SHA256(key=device_secret, msg=challenge_nonce).
    For new pairings, the device_secret is generated by the Hub and
    transmitted securely after user approval.
    """
    message_type: Literal["assistant_auth_response"] = "assistant_auth_response"
    satellite_id: str = Field(..., description="Same satellite_id as in AssistantHello.")
    signature: str = Field(..., description="HMAC-SHA256 of the challenge nonce.")


class HubWelcome(BaseMessage):
    """
    Hub confirms the Assistant is authenticated and paired.

    Includes the household identity and session token for the connection.
    The Assistant stores hub_id and device_secret locally for reconnection.
    """
    message_type: Literal["hub_welcome"] = "hub_welcome"
    hub_id: str = Field(..., description="The Hub's device_id.")
    household_id: str = Field(..., description="Household this Hub belongs to.")
    household_name: str = Field(..., description="Human-readable household name.")
    session_token: str = Field(
        ...,
        description="Session token for this connection (used for fast reconnection).",
    )
    session_expires_at: datetime = Field(..., description="When the session token expires.")
    assigned_room: Optional[str] = Field(
        None,
        description="The room the Hub has assigned to this Assistant.",
    )
    device_secret: Optional[str] = Field(
        None,
        description="Only set during first-time pairing. The Assistant must store this securely.",
    )


# ---------------------------------------------------------------------------
# Satellite Heartbeat (reuses relay Heartbeat for returning, adds satellite-specific)
# ---------------------------------------------------------------------------

class AssistantHeartbeat(BaseMessage):
    """Assistant-to-Hub keepalive with satellite-specific status."""
    message_type: Literal["assistant_heartbeat"] = "assistant_heartbeat"
    satellite_id: str = Field(..., description="Assistant's satellite ID.")
    uptime_seconds: float = Field(..., description="Seconds since the Assistant started.")
    audio_active: bool = Field(default=False, description="Whether audio pipeline is running.")
    cpu_percent: Optional[float] = Field(None, description="Current CPU usage percentage.")
    memory_percent: Optional[float] = Field(None, description="Current memory usage percentage.")


class HubHeartbeatAck(BaseMessage):
    """Hub's acknowledgement of an AssistantHeartbeat."""
    message_type: Literal["hub_heartbeat_ack"] = "hub_heartbeat_ack"
    hub_uptime_seconds: float = Field(..., description="Hub's uptime in seconds.")
    relay_connected: bool = Field(
        default=True,
        description="Whether the Hub is connected to the Synork backend.",
    )


# ---------------------------------------------------------------------------
# Voice query routing: Assistant → Hub → Backend → Hub → Assistant
# ---------------------------------------------------------------------------

class VoiceQueryFromAssistant(BaseMessage):
    """
    Text query from an Assistant after local STT processing.

    The Hub forwards this to the Synork backend as an AssistantInvoke,
    adding the room context and the Hub's identity credentials.
    """
    message_type: Literal["voice_query_from_assistant"] = "voice_query_from_assistant"
    satellite_id: str = Field(..., description="Which Assistant sent this query.")
    query: str = Field(..., description="Transcribed text from local STT.")
    language: str = Field(default="hu", description="ISO 639-1 language code.")
    room: Optional[str] = Field(None, description="Room context for the query.")
    conversation_id: Optional[str] = Field(
        None,
        description="Ongoing conversation ID for multi-turn context.",
    )
    audio_data_ref: Optional[str] = Field(
        None,
        description="Reference to raw audio if Hub needs to re-process.",
    )


class VoiceResponseToAssistant(BaseMessage):
    """
    Streaming response chunk sent from Hub to Assistant.

    The Hub receives AssistantResponse/AssistantStreamChunk from the backend
    and re-packages them for the originating Assistant.
    """
    message_type: Literal["voice_response_to_assistant"] = "voice_response_to_assistant"
    satellite_id: str = Field(..., description="Target Assistant for this response.")
    conversation_id: str = Field(..., description="Conversation context ID.")
    chunk_index: int = Field(..., description="Sequential chunk number starting from 0.")
    text_delta: str = Field(default="", description="Incremental text content.")
    is_final: bool = Field(default=False, description="True if this is the last chunk.")
    language: str = Field(default="hu", description="Language of the response.")
    audio_url: Optional[str] = Field(
        None,
        description="URL or local path to TTS audio for this chunk.",
    )
    tool_calls: list[AssistantToolCall] = Field(
        default_factory=list,
        description="Tool calls made during processing (for transparency).",
    )


# ---------------------------------------------------------------------------
# Satellite lifecycle events
# ---------------------------------------------------------------------------

class SatelliteDisconnect(BaseMessage):
    """
    Graceful disconnect from a satellite (either direction).

    Sent before intentional disconnect (Assistant shutdown, Hub removing satellite).
    """
    message_type: Literal["satellite_disconnect"] = "satellite_disconnect"
    satellite_id: str = Field(..., description="Which satellite is disconnecting.")
    reason: str = Field(..., description="Human-readable disconnect reason.")
    will_reconnect: bool = Field(
        default=True,
        description="Whether the satellite intends to reconnect.",
    )


class SatelliteReconnect(BaseMessage):
    """
    Reconnection attempt from a satellite using a previous session token.

    If the session is still valid, the Hub responds with HubWelcome
    (fast path, no HMAC challenge needed). Otherwise falls back to
    full AssistantHello → HubChallenge → AssistantAuthResponse flow.
    """
    message_type: Literal["satellite_reconnect"] = "satellite_reconnect"
    satellite_id: str = Field(..., description="Satellite's device ID.")
    previous_session_token: Optional[str] = Field(
        None,
        description="Previous session token for fast reconnection.",
    )
    reconnect_reason: str = Field(
        default="connection_lost",
        description="Why reconnection happened.",
    )


# ---------------------------------------------------------------------------
# Thread/Matter Mesh Extension (Session 18)
#
# These messages handle Thread credential distribution from the Hub (primary
# TBR) to Assistants (secondary TBRs). The operational dataset is the Thread
# network's "password" — treat as a household secret.
# ---------------------------------------------------------------------------

class ThreadOperationalDataset(BaseModel):
    """Thread network operational dataset — the credential set shared between TBRs.

    This is the minimal set needed for a TBR to join an existing Thread network.
    All fields come from the Hub's OTBR API.
    """
    network_name: str = Field(..., description="Thread network name (max 16 chars).")
    channel: int = Field(..., ge=11, le=26, description="IEEE 802.15.4 channel (11-26).")
    pan_id: str = Field(..., description="PAN ID as 4-char hex string (e.g. '1234').")
    extended_pan_id: str = Field(..., description="Extended PAN ID as 16-char hex string.")
    network_key: str = Field(
        ...,
        description="128-bit network key as 32-char hex string. NEVER log this value.",
    )
    mesh_local_prefix: str = Field(..., description="Mesh-local IPv6 prefix (e.g. 'fd11:22::/64').")
    security_policy: Optional[int] = Field(
        None,
        description="Security policy flags bitmap (from OTBR).",
    )
    dataset_timestamp: Optional[datetime] = Field(
        None,
        description="Active timestamp of the dataset (for conflict resolution).",
    )


class ThreadCredentialsOffer(BaseMessage):
    """Hub → Assistant: offers Thread network credentials after pairing.

    Sent only to Assistants that reported THREAD_RADIO in their capabilities.
    The dataset contains everything the Assistant's OTBR needs to join the
    Hub's Thread network as an additional Border Router.
    """
    message_type: Literal["thread_credentials_offer"] = "thread_credentials_offer"
    dataset: ThreadOperationalDataset = Field(
        ...,
        description="Thread operational dataset for the household's network.",
    )
    hub_tbr_id: str = Field(
        ...,
        description="Hub's TBR identifier (for the Assistant to register as secondary).",
    )
    network_established_at: datetime = Field(
        ...,
        description="When the Hub first established the Thread network.",
    )


class ThreadCredentialsAck(BaseMessage):
    """Assistant → Hub: confirms receipt of Thread credentials.

    The Assistant sends this after validating the dataset and before
    attempting to join the network. The Hub uses this to track which
    Assistants have received credentials.
    """
    message_type: Literal["thread_credentials_ack"] = "thread_credentials_ack"
    satellite_id: str = Field(..., description="Assistant that received the credentials.")
    accepted: bool = Field(
        ...,
        description="True if the Assistant will attempt to join; False if OTBR is unavailable.",
    )
    reason: Optional[str] = Field(
        None,
        description="Rejection reason if accepted is False (e.g. 'otbr_not_installed').",
    )


class ThreadTBRStatus(str, Enum):
    """Status of a Thread Border Router."""
    JOINING = "joining"
    ONLINE = "online"
    OFFLINE = "offline"
    ERROR = "error"


class ThreadStatusUpdate(BaseMessage):
    """Assistant → Hub: periodic Thread TBR status report.

    Sent at regular intervals (default 60s) when the Assistant is running
    as a TBR. The Hub aggregates these for the household's Thread overview.
    """
    message_type: Literal["thread_status_update"] = "thread_status_update"
    satellite_id: str = Field(..., description="Assistant reporting status.")
    tbr_status: ThreadTBRStatus = Field(..., description="Current TBR operational status.")
    connected_device_count: int = Field(
        default=0,
        description="Number of Thread devices routing through this TBR.",
    )
    connected_device_ids: list[str] = Field(
        default_factory=list,
        description="Thread device EUI-64 identifiers visible to this TBR.",
    )
    signal_dbm: Optional[int] = Field(
        None,
        description="Average signal strength in dBm for connected devices.",
    )
    uptime_seconds: float = Field(
        default=0.0,
        description="How long this TBR has been running since last join.",
    )
    error_message: Optional[str] = Field(
        None,
        description="Error details if tbr_status is ERROR.",
    )


class ThreadCommissioningRequest(BaseMessage):
    """Bidirectional: request to commission a new Thread device.

    Can be sent by either Hub or Assistant. The recipient should attempt
    commissioning if it has better signal to the target device. The Hub
    coordinates which TBR actually performs the commissioning.
    """
    message_type: Literal["thread_commissioning_request"] = "thread_commissioning_request"
    requesting_tbr_id: str = Field(
        ...,
        description="TBR ID that initiated the request (Hub device_id or satellite_id).",
    )
    target_device_eui64: Optional[str] = Field(
        None,
        description="EUI-64 of the device to commission (if known).",
    )
    commissioning_data: dict[str, Any] = Field(
        default_factory=dict,
        description="Device-specific commissioning data (setup code, discriminator, etc.).",
    )
    preferred_tbr_id: Optional[str] = Field(
        None,
        description="If set, the Hub prefers this TBR to handle commissioning.",
    )


# ---------------------------------------------------------------------------
# Satellite discriminated union
# ---------------------------------------------------------------------------

SatelliteMessage = Annotated[
    Union[
        # Auth / handshake
        AssistantHello,
        HubChallenge,
        AssistantAuthResponse,
        HubWelcome,
        # Heartbeat
        AssistantHeartbeat,
        HubHeartbeatAck,
        # Voice query routing
        VoiceQueryFromAssistant,
        VoiceResponseToAssistant,
        # Thread/Matter mesh extension (Session 18)
        ThreadCredentialsOffer,
        ThreadCredentialsAck,
        ThreadStatusUpdate,
        ThreadCommissioningRequest,
        # Lifecycle
        SatelliteDisconnect,
        SatelliteReconnect,
        # Reused from relay protocol (same envelope, same semantics)
        ProtocolError,
    ],
    Field(discriminator="message_type"),
]
"""
Discriminated union of all Hub ↔ Assistant (satellite) protocol messages.

Separate from RelayMessage — different security domain (local LAN vs cloud).
The Hub is the WebSocket server; Assistants are clients.

Usage::

    from pydantic import TypeAdapter
    adapter = TypeAdapter(SatelliteMessage)
    msg = adapter.validate_json(raw_bytes)
"""
