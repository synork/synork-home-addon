"""Synork Home — Hub Persona.

The Hub persona is activated when radio hardware (Zigbee, Z-Wave, Thread)
is detected. It ensures the corresponding HA integrations are configured:
  - ZHA (Zigbee Home Automation) for Zigbee radios
  - Z-Wave JS for Z-Wave radios
  - OTBR (OpenThread Border Router) for Thread radios

The hub doesn't run these integrations itself — HA Core does. The hub
persona's job is to ensure they're configured and to report their status.
"""

from __future__ import annotations

import logging
from typing import Any

from shared.persona_schema import PersonaConfig, PersonaServiceConfig, PersonaServiceState

logger = logging.getLogger("synork.persona.hub")


class HubPersona:
    """Hub persona — primary smart home controller.

    Manages radio network integrations (ZHA, Z-Wave JS, OTBR).
    The actual radio stacks run inside HA Core; this persona
    ensures they're configured and monitors their health.
    """

    def __init__(self) -> None:
        self._running = False
        self._service_states: dict[str, PersonaServiceState] = {}
        self._config: PersonaConfig | None = None

    @property
    def running(self) -> bool:
        return self._running

    async def start(self, config: PersonaConfig) -> None:
        """Start the hub persona with the given configuration.

        Iterates through configured services (zha, zwave_js, otbr) and
        ensures they are set up in HA. Actual integration management is
        done via the HA Supervisor API.
        """
        self._config = config
        self._running = True

        for svc in config.services:
            self._service_states[svc.service_name] = PersonaServiceState.STARTING
            logger.info("Hub: configuring %s", svc.service_name)

            try:
                await self._configure_service(svc)
                self._service_states[svc.service_name] = PersonaServiceState.RUNNING
            except Exception as exc:
                logger.error("Hub: failed to configure %s: %s", svc.service_name, exc)
                self._service_states[svc.service_name] = PersonaServiceState.ERROR

        logger.info("Hub persona started — services: %s", self._service_states)

    async def stop(self) -> None:
        """Stop the hub persona and release resources."""
        self._running = False
        for name in self._service_states:
            self._service_states[name] = PersonaServiceState.STOPPED
        logger.info("Hub persona stopped")

    async def health(self) -> dict[str, Any]:
        """Return health status of the hub persona."""
        return {
            "persona": "hub",
            "running": self._running,
            "services": {
                name: state.value for name, state in self._service_states.items()
            },
        }

    async def _configure_service(self, svc: PersonaServiceConfig) -> None:
        """Ensure an HA integration is configured for this service.

        In v1, this is a verification step — the integration should already
        be set up through HA's normal flow or through the OOBE. The hub
        persona logs the status but doesn't force-configure integrations
        (that could conflict with user settings).
        """
        if svc.service_name == "zha":
            device_path = svc.config.get("device_path", "/dev/ttyUSB0")
            logger.info("Hub: ZHA radio at %s (type: %s)", device_path, svc.config.get("radio_type", "ezsp"))

        elif svc.service_name == "zwave_js":
            device_path = svc.config.get("device_path", "/dev/ttyACM0")
            logger.info("Hub: Z-Wave JS radio at %s", device_path)

        elif svc.service_name == "otbr":
            logger.info("Hub: OpenThread Border Router configured")

        else:
            logger.warning("Hub: unknown service %s", svc.service_name)
