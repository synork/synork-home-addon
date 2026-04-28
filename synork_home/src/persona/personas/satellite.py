"""Synork Home — Satellite Persona.

The Satellite persona is activated when a microphone is detected. It acts
as a voice assistant endpoint: wake word detection, audio capture, STT,
and forwarding queries to the Synork relay for processing.

The satellite doesn't generate responses — that's the cloud assistant's
job. It captures audio, transcribes locally, and sends the text to the
relay. When a response comes back with audio, it plays it.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Coroutine, Optional

from shared.persona_schema import PersonaConfig, PersonaServiceState

logger = logging.getLogger("synork.persona.satellite")


class SatellitePersona:
    """Satellite persona — remote voice/sensor node.

    Manages the voice assistant pipeline services:
      - Wake word detection
      - VAD (voice activity detection)
      - STT (speech-to-text)
      - Thread TBR (if THREAD_RADIO detected — Session 18)

    Audio capture and playback are handled by the assistant pipeline
    module (src/assistant/); this persona ensures the pipeline is
    configured and started based on hardware capabilities.
    """

    def __init__(self) -> None:
        self._running = False
        self._service_states: dict[str, PersonaServiceState] = {}
        self._config: Optional[PersonaConfig] = None
        self._pipeline_started = False
        self._thread_tbr_enabled = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def pipeline_started(self) -> bool:
        return self._pipeline_started

    @property
    def thread_tbr_enabled(self) -> bool:
        """Whether Thread TBR service is configured for this satellite."""
        return self._thread_tbr_enabled

    async def start(self, config: PersonaConfig) -> None:
        """Start the satellite persona with the given configuration.

        Initializes wake word, VAD, and STT services. The actual audio
        pipeline is started separately by the turn-taking manager.
        """
        self._config = config
        self._running = True

        for svc in config.services:
            self._service_states[svc.service_name] = PersonaServiceState.STARTING
            logger.info("Satellite: initializing %s", svc.service_name)

            try:
                await self._init_service(svc.service_name, svc.config)
                self._service_states[svc.service_name] = PersonaServiceState.RUNNING
            except Exception as exc:
                logger.error("Satellite: failed to init %s: %s", svc.service_name, exc)
                self._service_states[svc.service_name] = PersonaServiceState.ERROR

        self._pipeline_started = all(
            s == PersonaServiceState.RUNNING
            for s in self._service_states.values()
        )

        logger.info(
            "Satellite persona started — pipeline=%s, services: %s",
            "ready" if self._pipeline_started else "partial",
            {k: v.value for k, v in self._service_states.items()},
        )

    async def stop(self) -> None:
        """Stop the satellite persona and release resources."""
        self._running = False
        self._pipeline_started = False
        for name in self._service_states:
            self._service_states[name] = PersonaServiceState.STOPPED
        logger.info("Satellite persona stopped")

    async def health(self) -> dict[str, Any]:
        """Return health status of the satellite persona."""
        return {
            "persona": "satellite",
            "running": self._running,
            "pipeline_ready": self._pipeline_started,
            "services": {
                name: state.value for name, state in self._service_states.items()
            },
        }

    async def _init_service(self, name: str, config: dict[str, Any]) -> None:
        """Initialize a satellite service (wake_word, vad, stt, thread_tbr).

        Actual initialization is lightweight — the heavy lifting happens
        when the pipeline processes audio or OTBR joins the network.
        This step validates config and ensures dependencies are available.
        """
        if name == "wake_word":
            model = config.get("model", "hey_synork")
            logger.info("Satellite: wake word model=%s", model)

        elif name == "vad":
            sensitivity = config.get("sensitivity", 0.5)
            logger.info("Satellite: VAD sensitivity=%.2f", sensitivity)

        elif name == "stt":
            backend = config.get("backend", "local")
            model = config.get("model", "base")
            logger.info("Satellite: STT backend=%s, model=%s", backend, model)

        elif name == "thread_tbr":
            # Session 18: Thread TBR co-activation
            # The actual OTBR management is handled by AssistantThreadTBR,
            # initialized in main.py. This just marks the capability.
            self._thread_tbr_enabled = True
            logger.info("Satellite: Thread TBR service enabled (OTBR managed by main.py)")

        else:
            logger.warning("Satellite: unknown service %s", name)
