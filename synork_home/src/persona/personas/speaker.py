"""Synork Home — Speaker Persona.

The Speaker persona is activated when audio output hardware is detected.
It handles TTS output for the assistant, media playback notifications,
and intercom functionality.

The speaker persona registers the audio output device and configures
TTS routing (Piper for local/short, ElevenLabs for cloud/long).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from shared.persona_schema import PersonaConfig, PersonaServiceState

logger = logging.getLogger("synork.persona.speaker")


class SpeakerPersona:
    """Speaker persona — audio-focused device.

    Manages:
      - TTS output routing
      - Audio playback for assistant responses
      - Volume and audio device management
    """

    def __init__(self) -> None:
        self._running = False
        self._service_states: dict[str, PersonaServiceState] = {}
        self._config: Optional[PersonaConfig] = None
        self._output_device: str = "default"

    @property
    def running(self) -> bool:
        return self._running

    @property
    def output_device(self) -> str:
        return self._output_device

    async def start(self, config: PersonaConfig) -> None:
        """Start the speaker persona with the given configuration."""
        self._config = config
        self._running = True

        for svc in config.services:
            self._service_states[svc.service_name] = PersonaServiceState.STARTING
            logger.info("Speaker: initializing %s", svc.service_name)

            try:
                await self._init_service(svc.service_name, svc.config)
                self._service_states[svc.service_name] = PersonaServiceState.RUNNING
            except Exception as exc:
                logger.error("Speaker: failed to init %s: %s", svc.service_name, exc)
                self._service_states[svc.service_name] = PersonaServiceState.ERROR

        logger.info("Speaker persona started — output: %s, services: %s",
                     self._output_device,
                     {k: v.value for k, v in self._service_states.items()})

    async def stop(self) -> None:
        """Stop the speaker persona and release resources."""
        self._running = False
        for name in self._service_states:
            self._service_states[name] = PersonaServiceState.STOPPED
        logger.info("Speaker persona stopped")

    async def health(self) -> dict[str, Any]:
        """Return health status of the speaker persona."""
        return {
            "persona": "speaker",
            "running": self._running,
            "output_device": self._output_device,
            "services": {
                name: state.value for name, state in self._service_states.items()
            },
        }

    async def _init_service(self, name: str, config: dict[str, Any]) -> None:
        """Initialize a speaker service (tts, audio_playback)."""
        if name == "tts":
            provider = config.get("provider", "auto")
            language = config.get("language", "hu")
            self._output_device = config.get("output_device", "default")
            logger.info("Speaker: TTS provider=%s, language=%s", provider, language)

        elif name == "audio_playback":
            self._output_device = config.get("output_device", self._output_device)
            logger.info("Speaker: audio output device=%s", self._output_device)

        else:
            logger.warning("Speaker: unknown service %s", name)
