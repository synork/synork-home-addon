"""Synork Home — Frontend Installer.

Installs the Synork theme, JS patcher, and custom panel into HA's
configuration directory. Runs on every addon startup but is idempotent —
skips installation if files are already present and up-to-date.

Installation steps:
  1. Copy synork-theme.yaml to /config/themes/
  2. Copy synork-patcher.js to /config/www/synork/
  3. Copy synork logo/icon assets to /config/www/synork/
  4. Copy synork-panel/ directory to /config/www/synork-panel/
  5. Register the theme as default via HA REST API
  6. Register the patcher as extra_module_url via HA REST API

All operations use the HA config directory (/config) which the addon
has mapped as read-write.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path
from typing import Optional

import aiohttp

try:
    from _version import ADDON_VERSION
except ImportError:  # pragma: no cover - addon-only import
    ADDON_VERSION = "0.0.0"

logger = logging.getLogger("synork.frontend.installer")

# Source paths (inside the addon container)
_SRC_DIR = Path("/app/frontend")
_THEME_SRC = _SRC_DIR / "synork-theme.yaml"
_PATCHER_SRC = _SRC_DIR / "synork-patcher.js"
_PANEL_SRC = _SRC_DIR / "synork-panel"


def _detect_ha_config_dir() -> Path:
    """Return the path inside the addon where HA's config dir is mounted.

    The Supervisor mounts HA's config directory at one of two locations
    depending on which ``map:`` entry the addon declared:

      * ``homeassistant_config:rw`` (current schema)  -> ``/homeassistant``
      * ``config:rw`` (legacy schema)                 -> ``/config``

    Synork Home uses the new schema, so the active mount is
    ``/homeassistant``. We probe for a ``configuration.yaml`` to pick the
    right one and fall back to ``/homeassistant`` if neither exists yet.
    """
    for candidate in (Path("/homeassistant"), Path("/config")):
        if (candidate / "configuration.yaml").exists():
            return candidate
    # Prefer the new path even if no configuration.yaml was found.
    if Path("/homeassistant").exists():
        return Path("/homeassistant")
    return Path("/config")


_CONFIG_DIR = _detect_ha_config_dir()
_THEMES_DIR = _CONFIG_DIR / "themes"
_WWW_DIR = _CONFIG_DIR / "www"
_SYNORK_WWW = _WWW_DIR / "synork"
_PANEL_DEST = _WWW_DIR / "synork-panel"

# HA API base
_HA_API = "http://supervisor/core/api"


class FrontendInstaller:
    """Installs Synork frontend assets into HA configuration.

    Idempotent: compares file checksums before copying. Only updates
    files that have changed.
    """

    def __init__(
        self,
        ha_token: Optional[str] = None,
        enable_patcher: bool = True,
    ) -> None:
        self._ha_token = ha_token or os.environ.get("SUPERVISOR_TOKEN", "")
        self._enable_patcher = enable_patcher

    async def install(self) -> None:
        """Run the full frontend installation."""
        logger.info("Installing Synork frontend assets...")

        # Ensure directories exist
        _THEMES_DIR.mkdir(parents=True, exist_ok=True)
        _SYNORK_WWW.mkdir(parents=True, exist_ok=True)
        _PANEL_DEST.mkdir(parents=True, exist_ok=True)

        installed_count = 0

        # 1. Theme
        if self._copy_if_changed(_THEME_SRC, _THEMES_DIR / "synork-home.yaml"):
            installed_count += 1
            logger.info("Theme installed: synork-home.yaml")

        # 1a. Make sure HA actually loads themes from /config/themes/.
        # Without `frontend: themes: !include_dir_merge_named themes/`
        # in configuration.yaml the theme YAML is silently ignored and
        # the user can't pick "Synork Home" from the theme picker.
        try:
            self._ensure_themes_enabled()
        except Exception as exc:
            logger.warning("Could not patch configuration.yaml for themes: %s", exc)

        # 2. JS Patcher
        if self._enable_patcher:
            if self._copy_if_changed(_PATCHER_SRC, _SYNORK_WWW / "synork-patcher.js"):
                installed_count += 1
                logger.info("Patcher installed: synork-patcher.js")

        # 3. Panel directory (copy entire directory if source exists)
        if _PANEL_SRC.exists() and any(_PANEL_SRC.iterdir()):
            panel_updated = self._sync_directory(_PANEL_SRC, _PANEL_DEST)
            if panel_updated:
                installed_count += 1
                logger.info("Panel installed: synork-panel/")

        # 4. Register with HA
        try:
            await self._register_theme()
            if self._enable_patcher:
                await self._register_patcher()
            await self._publish_version_sensor()
        except Exception as exc:
            logger.warning("Failed to register with HA API (non-fatal): %s", exc)

        if installed_count > 0:
            logger.info("Frontend installation complete (%d files updated)", installed_count)
        else:
            logger.info("Frontend assets up-to-date, no changes needed")

    def _copy_if_changed(self, src: Path, dest: Path) -> bool:
        """Copy a file only if the source differs from the destination.

        Returns True if the file was copied.
        """
        if not src.exists():
            logger.debug("Source not found: %s", src)
            return False

        if dest.exists():
            src_hash = self._file_hash(src)
            dest_hash = self._file_hash(dest)
            if src_hash == dest_hash:
                return False

        shutil.copy2(src, dest)
        return True

    def _sync_directory(self, src: Path, dest: Path) -> bool:
        """Sync a directory, copying only changed files.

        Returns True if any files were updated.
        """
        updated = False

        for src_file in src.rglob("*"):
            if src_file.is_dir():
                continue

            rel = src_file.relative_to(src)
            dest_file = dest / rel
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            if self._copy_if_changed(src_file, dest_file):
                updated = True

        return updated

    def _file_hash(self, path: Path) -> str:
        """Compute SHA256 hash of a file."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                h.update(chunk)
        return h.hexdigest()

    async def _register_theme(self) -> None:
        """Set Synork Home as the default theme via HA API.

        Calls the frontend.set_theme service.
        """
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {self._ha_token}",
                "Content-Type": "application/json",
            }

            # Reload themes first
            await session.post(
                f"{_HA_API}/services/frontend/reload_themes",
                headers=headers,
                json={},
            )

            # Set as default
            await session.post(
                f"{_HA_API}/services/frontend/set_theme",
                headers=headers,
                json={"name": "Synork Home"},
            )

            logger.info("Theme 'Synork Home' set as default")

    async def _publish_version_sensor(self) -> None:
        """Publish a sensor.synork_home_version entity in HA.

        Uses the REST API ``POST /api/states/{entity_id}``. The state value
        is the addon version string; attributes carry friendly_name and
        an icon so the entity looks polished in the HA UI.
        """
        if not self._ha_token:
            return
        entity_id = "sensor.synork_home_version"
        body = {
            "state": ADDON_VERSION,
            "attributes": {
                "friendly_name": "Synork Home Version",
                "icon": "mdi:home-assistant",
                "source": "synork_home_addon",
            },
        }
        async with aiohttp.ClientSession() as session:
            headers = {
                "Authorization": f"Bearer {self._ha_token}",
                "Content-Type": "application/json",
            }
            async with session.post(
                f"{_HA_API}/states/{entity_id}",
                headers=headers,
                json=body,
            ) as resp:
                if resp.status >= 400:
                    text = await resp.text()
                    logger.warning(
                        "Could not publish %s (HTTP %s): %s",
                        entity_id, resp.status, text[:200],
                    )
                else:
                    logger.info("Published %s = %s", entity_id, ADDON_VERSION)

    async def _register_patcher(self) -> None:
        """Register the JS patcher as an extra module URL.

        HA loads extra_module_url JS files on every frontend page load.
        We register our patcher so it runs automatically.

        Note: This requires adding to configuration.yaml's frontend config.
        The HA API doesn't support dynamic extra_module_url registration,
        so we write to configuration.yaml directly.
        """
        config_path = _CONFIG_DIR / "configuration.yaml"
        patcher_url = "/local/synork/synork-patcher.js"
        marker = "# synork-patcher"

        if not config_path.exists():
            return

        content = config_path.read_text()

        # Check if already registered
        if patcher_url in content:
            return

        # Add frontend extra_module_url entry
        # Look for existing frontend: section
        if "frontend:" in content:
            # Check if extra_module_url already exists
            if "extra_module_url:" in content:
                # Add to existing list
                insertion = f"    - {patcher_url}  {marker}\n"
                content = content.replace(
                    "extra_module_url:\n",
                    f"extra_module_url:\n{insertion}",
                )
            else:
                # Add extra_module_url to frontend section
                content = content.replace(
                    "frontend:",
                    f"frontend:\n  extra_module_url:\n    - {patcher_url}  {marker}",
                )
        else:
            # Add frontend section
            content += (
                f"\nfrontend:\n"
                f"  extra_module_url:\n"
                f"    - {patcher_url}  {marker}\n"
            )

        config_path.write_text(content)
        logger.info("Patcher registered in configuration.yaml")

    def _ensure_themes_enabled(self) -> None:
        """Make sure configuration.yaml loads /config/themes/ as merged themes.

        HA only sees theme YAMLs if the user opted in by adding
        ``frontend: themes: !include_dir_merge_named themes/``. Without
        this line, the theme picker won't list "Synork Home" no matter
        how many YAMLs are in /config/themes/.

        We patch configuration.yaml exactly once (idempotent via marker).
        """
        config_path = _CONFIG_DIR / "configuration.yaml"
        marker = "# synork-themes"

        if not config_path.exists():
            return

        content = config_path.read_text()

        # Already patched, or user already configured themes manually
        if marker in content or "themes:" in content:
            return

        if "frontend:" in content:
            content = content.replace(
                "frontend:",
                f"frontend:\n  themes: !include_dir_merge_named themes  {marker}",
                1,
            )
        else:
            content += (
                f"\nfrontend:\n"
                f"  themes: !include_dir_merge_named themes  {marker}\n"
            )

        config_path.write_text(content)
        logger.info(
            "Themes enabled in configuration.yaml \u2014 restart HA Core "
            "to pick up the Synork Home theme"
        )
