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

    async def auto_provision_ha(self, ha_bridge: Any) -> dict[str, bool]:
        """Auto-configure HA integrations for any radios this hub manages.

        Synork detects the radio hardware before HA's config flow ever runs
        — so when the user installs the addon for the first time, HA has no
        ``zha`` / ``zwave_js`` integration loaded and pairing requests fail
        with "Service zha.permit not found". This method walks HA's config
        flow programmatically using the device path the persona already
        resolved, so the user never has to dig through Settings → Devices &
        Services → Add Integration.

        Idempotent: if HA already has a config entry for the integration we
        skip it. Failures are logged but never fatal — the addon keeps
        running and the user can always configure the integration manually.
        """
        results: dict[str, bool] = {}
        if not self._config:
            return results

        loaded_services = await _safe_get_services(ha_bridge)

        for svc in self._config.services:
            name = svc.service_name
            if name in loaded_services:
                results[name] = True
                continue
            try:
                if await ha_bridge.has_config_entry(name):
                    # Entry exists but services not loaded yet (e.g. setup in
                    # progress) — nothing for us to do.
                    results[name] = True
                    continue
            except Exception:
                pass

            if name == "zha":
                results[name] = await self._provision_zha(ha_bridge, svc)
            elif name == "zwave_js":
                results[name] = await self._provision_zwave_js(ha_bridge, svc)
            else:
                # otbr and friends are managed by their own add-ons; nothing
                # to do via the config-flow API.
                results[name] = False

        ok = [k for k, v in results.items() if v]
        skip = [k for k, v in results.items() if not v]
        if ok:
            logger.info("Auto-provisioned HA integrations: %s", ok)
        if skip:
            logger.info("HA integrations not auto-provisioned: %s", skip)
        return results

    async def _provision_zha(self, ha_bridge: Any, svc: PersonaServiceConfig) -> bool:
        device_path = svc.config.get("device_path", "/dev/ttyUSB0")
        radio_type = svc.config.get("radio_type", "ezsp")
        baudrate = svc.config.get("baudrate", 115200)
        flow_control = svc.config.get("flow_control", "software")
        port_label = f"{device_path} - Synork autoprovisioned"

        # ZHA's flow has changed across HA versions; supply inputs for the
        # most common step ids so the flow walker matches whichever the
        # current HA exposes.
        step_inputs = {
            "user": {"path": port_label},
            "choose_serial_port": {"path": port_label},
            "manual_pick_radio_type": {"radio_type": radio_type},
            "manual_port_config": {
                "path": device_path,
                "baudrate": baudrate,
                "flow_control": flow_control,
            },
            "choose_formation_strategy": {
                "next_step_id": "form_new_network",
            },
            "form_new_network": {},
            "confirm": {},
        }

        logger.info(
            "Hub: auto-configuring ZHA in HA (path=%s, radio=%s)",
            device_path, radio_type,
        )
        ok = await ha_bridge.auto_complete_config_flow(
            handler="zha",
            step_inputs=step_inputs,
            default_input={},
            max_steps=10,
        )
        if ok:
            logger.info("Hub: ZHA auto-configured in HA")
        else:
            logger.warning(
                "Hub: ZHA auto-configuration did not complete — "
                "configure ZHA manually in HA (Settings → Devices & Services)",
            )
        return ok

    async def _provision_zwave_js(self, ha_bridge: Any, svc: PersonaServiceConfig) -> bool:
        device_path = svc.config.get("device_path", "/dev/ttyACM0")

        # Z-Wave JS in HA Supervisor uses a managed add-on; the integration
        # config flow asks "use Supervisor add-on?" → "device path?".
        step_inputs = {
            "user": {"use_addon": True},
            "on_supervisor": {"use_addon": True},
            "install_addon": {},
            "configure_addon": {"usb_path": device_path},
            "start_addon": {},
            "manual": {"url": "ws://localhost:3000"},
            "confirm": {},
        }

        logger.info("Hub: auto-configuring Z-Wave JS in HA (path=%s)", device_path)
        ok = await ha_bridge.auto_complete_config_flow(
            handler="zwave_js",
            step_inputs=step_inputs,
            default_input={},
            max_steps=12,
        )
        if ok:
            logger.info("Hub: Z-Wave JS auto-configured in HA")
        else:
            logger.warning(
                "Hub: Z-Wave JS auto-configuration did not complete — "
                "the Z-Wave JS add-on may need to be installed first",
            )
        return ok

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


async def _safe_get_services(ha_bridge: Any) -> dict[str, Any]:
    try:
        return await ha_bridge.get_services()
    except Exception:
        return {}
