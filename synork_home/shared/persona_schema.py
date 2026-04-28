"""
Synork Home — Hardware Persona Schema

Defines the types for hardware detection and persona resolution.

The persona system works in three stages:
  1. Hardware probe — detect what's physically attached (radios, mics, speakers)
  2. Persona resolution — map capabilities to personas (hub, satellite, speaker, composite)
  3. Persona configuration — per-persona settings and service activation

Same OS image runs on all hardware; personas determine which services activate.

Contract locked in Session 1 (2026-04-25).
Implementation: Session 3 (Addon Core) + Session 4 (OS Image).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class HardwareCapability(str, Enum):
    """Detectable hardware capabilities via udev rules + Python probing."""
    ZIGBEE_RADIO = "zigbee_radio"
    ZWAVE_RADIO = "zwave_radio"
    THREAD_RADIO = "thread_radio"
    MATTER_RADIO = "matter_radio"
    MICROPHONE = "microphone"
    AUDIO_OUTPUT = "audio_output"
    DISPLAY = "display"
    CAMERA = "camera"
    GPIO = "gpio"
    BLUETOOTH = "bluetooth"
    ETHERNET = "ethernet"
    WIFI = "wifi"
    USB_HUB = "usb_hub"
    SSD_STORAGE = "ssd_storage"


class Persona(str, Enum):
    """
    Device personas determine which services are activated.

    A device can have multiple active personas (→ COMPOSITE).
    """
    HUB = "hub"
    SATELLITE = "satellite"
    SPEAKER = "speaker"
    COMPOSITE = "composite"


class PersonaServiceState(str, Enum):
    """Lifecycle state of a persona-bound service."""
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class SupportedPlatform(str, Enum):
    """Known hardware platforms for persona optimization."""
    PI_ZERO_2W = "pi_zero_2w"
    PI_4 = "pi_4"
    PI_5 = "pi_5"
    X86_GENERIC = "x86_generic"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Hardware probe
# ---------------------------------------------------------------------------

class DetectedHardware(BaseModel):
    """A single piece of detected hardware."""
    capability: HardwareCapability = Field(..., description="What capability this hardware provides.")
    device_path: str = Field(..., description="System device path (e.g. /dev/ttyUSB0, /dev/snd/pcmC0D0c).")
    device_name: str = Field(..., description="Human-readable device name from udev or lsusb.")
    vendor_id: Optional[str] = Field(None, description="USB vendor ID if applicable.")
    product_id: Optional[str] = Field(None, description="USB product ID if applicable.")
    driver: Optional[str] = Field(None, description="Kernel driver in use.")
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        description="Additional device-specific metadata.",
    )


class HardwareProbeResult(BaseModel):
    """
    Result of scanning the device for attached hardware.

    Produced by the hardware_probe.py module on boot and on USB hotplug events.
    """
    platform: SupportedPlatform = Field(..., description="Detected hardware platform.")
    capabilities: list[HardwareCapability] = Field(
        default_factory=list,
        description="Aggregate list of detected capabilities.",
    )
    devices: list[DetectedHardware] = Field(
        default_factory=list,
        description="Individual hardware devices found.",
    )
    cpu_model: str = Field(default="unknown", description="CPU model string.")
    ram_mb: int = Field(default=0, description="Total RAM in megabytes.")
    storage_gb: float = Field(default=0.0, description="Total storage in gigabytes.")
    probed_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the probe was performed.",
    )


# ---------------------------------------------------------------------------
# Persona resolution
# ---------------------------------------------------------------------------

class PersonaResolution(BaseModel):
    """
    Which personas are active given the detected hardware capabilities.

    Resolution rules (applied in order):
      - ZIGBEE_RADIO or ZWAVE_RADIO or THREAD_RADIO → HUB
      - MICROPHONE → SATELLITE
      - AUDIO_OUTPUT → SPEAKER
      - More than one of the above → COMPOSITE (all sub-personas listed)

    The ``active_personas`` list contains individual personas;
    ``effective_persona`` is COMPOSITE when len(active_personas) > 1.
    """
    active_personas: list[Persona] = Field(
        ...,
        description="Individual personas that are active.",
    )
    effective_persona: Persona = Field(
        ...,
        description="The top-level persona (COMPOSITE if multiple active).",
    )
    probe_result: HardwareProbeResult = Field(
        ...,
        description="The hardware probe that led to this resolution.",
    )
    resolved_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When persona resolution was performed.",
    )


# ---------------------------------------------------------------------------
# Persona configuration
# ---------------------------------------------------------------------------

class PersonaServiceConfig(BaseModel):
    """Configuration for a single service bound to a persona."""
    service_name: str = Field(..., description="Service identifier (e.g. 'zha', 'whisper', 'piper').")
    enabled: bool = Field(default=True, description="Whether this service should be started.")
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="Service-specific configuration values.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="Other service names that must start before this one.",
    )


class PersonaConfig(BaseModel):
    """
    Per-persona settings and service activation list.

    Each active persona has its own config. For COMPOSITE devices, there
    is one PersonaConfig per sub-persona.
    """
    persona: Persona = Field(..., description="Which persona this config is for.")
    services: list[PersonaServiceConfig] = Field(
        default_factory=list,
        description="Services to activate for this persona.",
    )
    priority: int = Field(
        default=0,
        description="Startup priority (lower = starts first). Hub typically 0, speaker 10, satellite 20.",
    )
    resource_limits: Optional[ResourceLimits] = Field(
        None,
        description="Optional resource constraints for constrained devices.",
    )


class ResourceLimits(BaseModel):
    """Resource constraints for persona services on constrained hardware."""
    max_memory_mb: Optional[int] = Field(None, description="Max memory allocation in MB.")
    max_cpu_percent: Optional[float] = Field(None, description="Max CPU usage as percentage.")
    io_priority: Optional[str] = Field(None, description="I/O scheduling class (idle, best-effort, realtime).")


# Fix forward reference
PersonaConfig.model_rebuild()


# ---------------------------------------------------------------------------
# Assistant Edition — satellite-specific configuration (Session 16)
# ---------------------------------------------------------------------------

class AssistantEditionConfig(BaseModel):
    """
    Configuration for a device running Synork OS Assistant Edition.

    Persisted locally on the Assistant device after successful pairing.
    The device_secret is the shared secret established during the pairing
    handshake — stored securely and never transmitted in plaintext after
    initial provisioning.
    """
    paired_hub_id: str = Field(..., description="Device ID of the Hub this Assistant is paired to.")
    paired_hub_address: str = Field(
        ...,
        description="Last known Hub address (IP:port or mDNS hostname). Updated on reconnection.",
    )
    device_secret: str = Field(..., description="Shared secret for HMAC auth with the Hub.")
    hub_session_token: Optional[str] = Field(
        None,
        description="Current session token for fast reconnection (expires).",
    )
    room: Optional[str] = Field(None, description="Room this Assistant is assigned to.")
    voice_settings: VoiceSettings = Field(
        default_factory=lambda: VoiceSettings(),
        description="Assistant-specific voice pipeline settings.",
    )
    paired_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the pairing was established.",
    )


class VoiceSettings(BaseModel):
    """Voice pipeline configuration for an Assistant device."""
    wake_word: str = Field(default="hey_synork", description="Wake word model name.")
    language: str = Field(default="hu", description="Default language for STT/TTS.")
    local_stt: bool = Field(default=True, description="Use local STT (Whisper) when hardware allows.")
    tts_provider: str = Field(default="auto", description="TTS provider (auto/piper/elevenlabs).")
    volume: float = Field(default=0.8, ge=0.0, le=1.0, description="Output volume (0.0–1.0).")


class SatelliteRegistration(BaseModel):
    """
    Hub-side record of a paired Assistant satellite.

    Stored in the Hub's local SQLite database (satellites table).
    The Synork backend does NOT have this data directly — it accesses
    satellite info through Hub-mediated REST endpoints.
    """
    satellite_id: str = Field(..., description="Unique identifier for the Assistant device.")
    device_secret_hash: str = Field(
        ...,
        description="SHA-256 hash of the device secret (Hub never stores the plaintext).",
    )
    room: Optional[str] = Field(None, description="Room assignment.")
    display_name: Optional[str] = Field(None, description="User-assigned friendly name.")
    capabilities: list[str] = Field(
        default_factory=list,
        description="HardwareCapability enum values detected on the Assistant.",
    )
    platform: str = Field(default="unknown", description="Hardware platform (pi_zero_2w, pi_3, etc.).")
    addon_version: str = Field(default="0.1.0", description="Assistant's addon version.")
    paired_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the satellite was paired.",
    )
    last_seen: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Last successful communication timestamp.",
    )
    online: bool = Field(default=False, description="Whether the satellite is currently connected.")


class PendingSatellitePairing(BaseModel):
    """
    A satellite pairing request awaiting user approval.

    Created when an unknown Assistant sends an AssistantHello to the Hub.
    The user must approve via the Synork app before the handshake completes.
    """
    request_id: str = Field(..., description="Unique identifier for this pairing request.")
    satellite_id: str = Field(..., description="Device ID of the requesting Assistant.")
    capabilities: list[str] = Field(
        default_factory=list,
        description="Hardware capabilities reported by the Assistant.",
    )
    platform: str = Field(default="unknown", description="Hardware platform of the Assistant.")
    claimed_room: Optional[str] = Field(None, description="Room the Assistant claims to be in.")
    initiated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="When the pairing request was created.",
    )
    expires_at: datetime = Field(
        ...,
        description="When the pairing request expires if not approved.",
    )
