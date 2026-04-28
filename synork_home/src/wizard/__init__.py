"""Synork Home — Setup Wizard package.

Serves a setup wizard on the addon's ingress port (8099) for users who
install the addon onto an existing Home Assistant instance. Owns:

  - aiohttp HTTP server (wizard.server.WizardServer)
  - sidecar persistence at /data/synork/hub_config.json (wizard.persistence)
  - HA persistent_notification helper (wizard.notifications)
  - en/hu UI strings (wizard.strings)

The wizard is independent of the Synork-OS first-boot captive portal
(addon/src/provisioner/wifi_ap_fallback.py); they bind different ports
and only one runs at a time depending on which install path the user took.

See Docs/HOME_OOBE_FLOW.md for the addon-on-existing-HA setup path.
"""

from .persistence import (
    HubConfig,
    load_hub_config,
    save_hub_config,
    ensure_device_id,
)
from .server import WizardServer

__all__ = [
    "HubConfig",
    "WizardServer",
    "ensure_device_id",
    "load_hub_config",
    "save_hub_config",
]
