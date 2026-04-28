"""Synork Home — Wizard persistence (hub_config.json sidecar).

The Hub edition's device_id and device_secret cannot be written by the
addon to HA's /data/options.json (only Supervisor mutates that, and a
mutation triggers an addon restart that would kill the wizard mid-flow).

So the wizard writes a sidecar at /data/synork/hub_config.json that the
addon's main.py prefers over CLI args from run.sh. This file survives
addon restarts, addon reinstalls, and HA OS updates because /data is
mounted on the addon's persistent volume.

Schema (v1):
  {
    "schema_version": 1,
    "device_id":      "syn-hub-<12hex>",
    "device_secret":  "<64hex>",            // HMAC-SHA256 keying material
    "household_id":   "<uuid hex>",
    "household_name": "...",
    "owner_user_id":  "...",
    "paired_at":      "<iso8601>",
    "relay_ws_url":   "wss://api.synork.dev/api/home/ws",
    "relay_api_url":  "https://api.synork.dev"
  }

Pre-pair state (after device_id is generated but before pairing) keeps
device_secret as "" so consumers can detect "identity assigned, not
paired yet".
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("synork.wizard.persistence")

_CONFIG_PATH = Path("/data/synork/hub_config.json")
_SCHEMA_VERSION = 1


@dataclass
class HubConfig:
    """Sidecar persistence for the Hub edition's identity and pairing state."""

    device_id: str = ""
    device_secret: str = ""
    household_id: str = ""
    household_name: str = ""
    owner_user_id: str = ""
    paired_at: str = ""
    relay_ws_url: str = ""
    relay_api_url: str = ""
    schema_version: int = _SCHEMA_VERSION

    @property
    def is_paired(self) -> bool:
        return bool(self.device_id) and bool(self.device_secret)


def load_hub_config(path: Path = _CONFIG_PATH) -> Optional[HubConfig]:
    """Read the hub config sidecar; return None if absent or unreadable.

    Unreadable / corrupt files are logged and treated as absent. The wizard
    will overwrite on next successful pair, so a corrupt file isn't fatal —
    it just means the user has to re-run the wizard.
    """
    if not path.exists():
        return None
    try:
        raw = path.read_text()
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("hub_config.json unreadable (%s) — treating as unpaired", exc)
        return None
    # Tolerate unknown fields from a future schema; ignore.
    return HubConfig(
        device_id=data.get("device_id", ""),
        device_secret=data.get("device_secret", ""),
        household_id=data.get("household_id", ""),
        household_name=data.get("household_name", ""),
        owner_user_id=data.get("owner_user_id", ""),
        paired_at=data.get("paired_at", ""),
        relay_ws_url=data.get("relay_ws_url", ""),
        relay_api_url=data.get("relay_api_url", ""),
        schema_version=data.get("schema_version", _SCHEMA_VERSION),
    )


def save_hub_config(config: HubConfig, path: Path = _CONFIG_PATH) -> None:
    """Atomically persist the config; chmod 0600 (device_secret is sensitive).

    Atomic write via tempfile+rename to avoid leaving a half-written file
    if the addon is killed mid-write — important because the next startup
    reads this file and would otherwise refuse to start.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(config), indent=2, sort_keys=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(payload)
    tmp.chmod(0o600)
    tmp.replace(path)
    logger.info("hub_config saved (paired=%s, device=%s)", config.is_paired, config.device_id)


def ensure_device_id(config: HubConfig, path: Path = _CONFIG_PATH) -> HubConfig:
    """Assign a device_id if missing; persist immediately so the wizard sees it.

    The wizard needs *some* stable identifier even before the user pairs,
    because the device_id is what gets written to home_household_devices on
    the backend. Generating it here means a wipe of hub_config.json gives
    the user a fresh identity (good for support: 'try a factory reset').
    """
    if config.device_id:
        return config
    config.device_id = f"syn-hub-{uuid.uuid4().hex[:12]}"
    save_hub_config(config, path)
    logger.info("generated device_id=%s (first run on this volume)", config.device_id)
    return config
