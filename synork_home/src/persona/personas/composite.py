"""Synork Home — Composite Persona.

The Composite persona is activated when multiple persona-triggering hardware
is detected (e.g., a Pi 4 with Zigbee radio + microphone + speaker gets
HUB + SATELLITE + SPEAKER = COMPOSITE).

Composite simply delegates to the individual persona implementations and
manages their lifecycle in priority order.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from shared.persona_schema import Persona, PersonaConfig

from .hub import HubPersona
from .satellite import SatellitePersona
from .speaker import SpeakerPersona

logger = logging.getLogger("synork.persona.composite")


class CompositePersona:
    """Composite persona — combines multiple persona capabilities.

    Manages sub-personas (hub, satellite, speaker) and starts/stops them
    in priority order. Each sub-persona gets its own PersonaConfig.
    """

    def __init__(self) -> None:
        self._running = False
        self._sub_personas: dict[Persona, Any] = {}

    @property
    def running(self) -> bool:
        return self._running

    async def start(self, configs: list[PersonaConfig]) -> None:
        """Start all sub-personas in priority order (lower priority number first)."""
        self._running = True

        # Sort by priority
        sorted_configs = sorted(configs, key=lambda c: c.priority)

        for config in sorted_configs:
            persona_impl = self._create_persona(config.persona)
            if persona_impl is None:
                logger.warning("Composite: unknown sub-persona %s", config.persona.value)
                continue

            self._sub_personas[config.persona] = persona_impl

            try:
                await persona_impl.start(config)
                logger.info("Composite: started %s", config.persona.value)
            except Exception as exc:
                logger.error("Composite: failed to start %s: %s", config.persona.value, exc)

        logger.info(
            "Composite persona started with sub-personas: %s",
            [p.value for p in self._sub_personas.keys()],
        )

    async def stop(self) -> None:
        """Stop all sub-personas in reverse priority order."""
        self._running = False

        for persona_type in reversed(list(self._sub_personas.keys())):
            impl = self._sub_personas[persona_type]
            try:
                await impl.stop()
            except Exception as exc:
                logger.error("Composite: error stopping %s: %s", persona_type.value, exc)

        self._sub_personas.clear()
        logger.info("Composite persona stopped")

    async def health(self) -> dict[str, Any]:
        """Return health status of all sub-personas."""
        sub_health = {}
        for persona_type, impl in self._sub_personas.items():
            try:
                sub_health[persona_type.value] = await impl.health()
            except Exception:
                sub_health[persona_type.value] = {"error": "health check failed"}

        return {
            "persona": "composite",
            "running": self._running,
            "sub_personas": sub_health,
        }

    def get_sub_persona(self, persona_type: Persona) -> Optional[Any]:
        """Get a specific sub-persona instance."""
        return self._sub_personas.get(persona_type)

    def _create_persona(self, persona_type: Persona) -> Optional[Any]:
        """Create a persona implementation instance."""
        if persona_type == Persona.HUB:
            return HubPersona()
        elif persona_type == Persona.SATELLITE:
            return SatellitePersona()
        elif persona_type == Persona.SPEAKER:
            return SpeakerPersona()
        return None
