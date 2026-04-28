"""Synork Home — Persona Resolver.

Resolves which persona(s) the device should run based on the hardware probe
result. Maps hardware capabilities to persona configurations.

Resolution rules (from the locked contract in persona_schema.py):
  - ZIGBEE_RADIO or ZWAVE_RADIO or THREAD_RADIO -> HUB
  - MICROPHONE -> SATELLITE
  - AUDIO_OUTPUT -> SPEAKER
  - More than one of the above -> COMPOSITE (all sub-personas listed)
"""

from __future__ import annotations

import logging
from typing import Any

from shared.persona_schema import (
    HardwareCapability,
    HardwareProbeResult,
    Persona,
    PersonaConfig,
    PersonaResolution,
    PersonaServiceConfig,
    PersonaServiceState,
    SupportedPlatform,
)

logger = logging.getLogger("synork.persona.resolver")

# Radio capabilities that trigger the HUB persona
_RADIO_CAPABILITIES = {
    HardwareCapability.ZIGBEE_RADIO,
    HardwareCapability.ZWAVE_RADIO,
    HardwareCapability.THREAD_RADIO,
}


class PersonaResolver:
    """Resolves device persona(s) from hardware probe results."""

    def resolve(self, probe: HardwareProbeResult) -> PersonaResolution:
        """Resolve the persona(s) for this device based on detected hardware.

        Returns PersonaResolution with active personas, effective persona,
        and the probe result that led to the resolution.
        """
        active: list[Persona] = []
        caps = set(probe.capabilities)

        # Check for HUB: any radio capability
        if caps & _RADIO_CAPABILITIES:
            active.append(Persona.HUB)

        # Check for SATELLITE: microphone present
        if HardwareCapability.MICROPHONE in caps:
            active.append(Persona.SATELLITE)

        # Check for SPEAKER: audio output present
        if HardwareCapability.AUDIO_OUTPUT in caps:
            active.append(Persona.SPEAKER)

        # Determine effective persona
        if len(active) > 1:
            effective = Persona.COMPOSITE
        elif len(active) == 1:
            effective = active[0]
        else:
            # No recognized capabilities — minimal persona (hub without radio)
            logger.warning("No persona-triggering hardware detected")
            effective = Persona.HUB
            active = [Persona.HUB]

        resolution = PersonaResolution(
            active_personas=active,
            effective_persona=effective,
            probe_result=probe,
        )

        logger.info(
            "Persona resolved: effective=%s, active=%s",
            effective.value,
            [p.value for p in active],
        )
        return resolution

    def get_configs(self, resolution: PersonaResolution) -> list[PersonaConfig]:
        """Generate persona configs with service definitions for each active persona.

        Returns a list of PersonaConfig, one per active persona, with the
        appropriate services configured based on hardware and platform.
        """
        configs: list[PersonaConfig] = []
        probe = resolution.probe_result
        caps = set(probe.capabilities)

        for persona in resolution.active_personas:
            if persona == Persona.HUB:
                configs.append(self._hub_config(caps, probe))
            elif persona == Persona.SATELLITE:
                configs.append(self._satellite_config(caps, probe))
            elif persona == Persona.SPEAKER:
                configs.append(self._speaker_config(caps, probe))

        return configs

    def _hub_config(self, caps: set[HardwareCapability], probe: HardwareProbeResult) -> PersonaConfig:
        """Build hub persona config based on detected radios."""
        services: list[PersonaServiceConfig] = []

        if HardwareCapability.ZIGBEE_RADIO in caps:
            # Find the Zigbee device path
            zigbee_path = "/dev/ttyUSB0"  # Default
            for dev in probe.devices:
                if dev.capability == HardwareCapability.ZIGBEE_RADIO:
                    zigbee_path = dev.device_path
                    break

            services.append(PersonaServiceConfig(
                service_name="zha",
                enabled=True,
                config={
                    "radio_type": "ezsp",
                    "device_path": zigbee_path,
                },
            ))

        if HardwareCapability.ZWAVE_RADIO in caps:
            zwave_path = "/dev/ttyACM0"
            for dev in probe.devices:
                if dev.capability == HardwareCapability.ZWAVE_RADIO:
                    zwave_path = dev.device_path
                    break

            services.append(PersonaServiceConfig(
                service_name="zwave_js",
                enabled=True,
                config={"device_path": zwave_path},
            ))

        if HardwareCapability.THREAD_RADIO in caps:
            services.append(PersonaServiceConfig(
                service_name="otbr",
                enabled=True,
                config={},
            ))

        return PersonaConfig(
            persona=Persona.HUB,
            services=services,
            priority=0,  # Hub starts first
        )

    def _satellite_config(self, caps: set[HardwareCapability], probe: HardwareProbeResult) -> PersonaConfig:
        """Build satellite persona config for voice assistant endpoint."""
        services: list[PersonaServiceConfig] = []

        # Wake word detection
        services.append(PersonaServiceConfig(
            service_name="wake_word",
            enabled=True,
            config={"model": "hey_synork"},
            depends_on=[],
        ))

        # Voice Activity Detection
        services.append(PersonaServiceConfig(
            service_name="vad",
            enabled=True,
            config={"sensitivity": 0.5},
            depends_on=[],
        ))

        # STT — prefer local, adapt to platform
        use_local = probe.ram_mb >= 2048  # Need at least 2GB for Whisper
        services.append(PersonaServiceConfig(
            service_name="stt",
            enabled=True,
            config={
                "backend": "local" if use_local else "remote",
                "model": "base" if probe.ram_mb < 4096 else "small",
                "language": "hu",
            },
            depends_on=["wake_word", "vad"],
        ))

        # Session 18: Thread TBR co-activation
        # If the satellite has a Thread radio, activate the TBR service.
        # The actual OTBR management is in AssistantThreadTBR (main.py),
        # but the persona tracks it for health reporting.
        if HardwareCapability.THREAD_RADIO in caps:
            services.append(PersonaServiceConfig(
                service_name="thread_tbr",
                enabled=True,
                config={},
                depends_on=[],
            ))

        return PersonaConfig(
            persona=Persona.SATELLITE,
            services=services,
            priority=20,  # Satellite starts after hub
        )

    def _speaker_config(self, caps: set[HardwareCapability], probe: HardwareProbeResult) -> PersonaConfig:
        """Build speaker persona config for TTS output."""
        # Find audio output device
        audio_path = "default"
        for dev in probe.devices:
            if dev.capability == HardwareCapability.AUDIO_OUTPUT:
                audio_path = dev.device_path
                break

        services: list[PersonaServiceConfig] = []

        services.append(PersonaServiceConfig(
            service_name="tts",
            enabled=True,
            config={
                "provider": "auto",  # Piper local, ElevenLabs cloud
                "output_device": audio_path,
                "language": "hu",
            },
            depends_on=[],
        ))

        services.append(PersonaServiceConfig(
            service_name="audio_playback",
            enabled=True,
            config={"output_device": audio_path},
            depends_on=[],
        ))

        return PersonaConfig(
            persona=Persona.SPEAKER,
            services=services,
            priority=10,  # Speaker starts after hub, before satellite
        )
