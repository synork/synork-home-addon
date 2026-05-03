"""Synork Home — Home Assistant Supervisor API client.

Tiny wrapper around the Supervisor REST API used by the addon to ensure
that the managed add-ons Synork depends on (Z-Wave JS, OTBR, Matter
Server, …) are installed and running. Authenticates with
``SUPERVISOR_TOKEN`` which the Supervisor injects into every addon
container.

Reference:
    https://developers.home-assistant.io/docs/api/supervisor/endpoints/

We only implement the handful of endpoints we need:
    * ``GET  /addons/{slug}/info``    — current install/state
    * ``POST /addons/{slug}/install`` — install from the addon's repo
    * ``POST /addons/{slug}/start``   — start an installed addon
    * ``POST /addons/{slug}/options`` — set addon options (used by zwave_js)
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

import aiohttp

logger = logging.getLogger("synork.supervisor")

_SUPERVISOR_BASE = "http://supervisor"
_INSTALL_TIMEOUT = 600.0  # add-on installs can be slow on small SoCs
_START_TIMEOUT = 120.0
_INFO_TIMEOUT = 15.0


class SupervisorError(RuntimeError):
    """Raised when the Supervisor API returns an error response."""


class SupervisorClient:
    """Minimal async client for the Home Assistant Supervisor REST API."""

    def __init__(self, token: Optional[str] = None) -> None:
        self._token = token or os.environ.get("SUPERVISOR_TOKEN", "")
        self._session: Optional[aiohttp.ClientSession] = None

    @property
    def available(self) -> bool:
        """True if a Supervisor token is present (we run inside Supervisor)."""
        return bool(self._token)

    async def __aenter__(self) -> "SupervisorClient":
        await self._ensure_session()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._token}"}
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Optional[dict[str, Any]] = None,
        timeout: float = _INFO_TIMEOUT,
    ) -> dict[str, Any]:
        if not self._token:
            raise SupervisorError("SUPERVISOR_TOKEN not set — not running under Supervisor")
        session = await self._ensure_session()
        url = f"{_SUPERVISOR_BASE}{path}"
        try:
            async with session.request(
                method, url, json=json, timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                payload = await resp.json(content_type=None)
                if resp.status >= 400 or (isinstance(payload, dict) and payload.get("result") == "error"):
                    msg = (payload or {}).get("message") if isinstance(payload, dict) else None
                    raise SupervisorError(
                        f"Supervisor {method} {path} failed ({resp.status}): {msg or payload}"
                    )
                return payload if isinstance(payload, dict) else {"result": "ok", "data": payload}
        except asyncio.TimeoutError as exc:
            raise SupervisorError(f"Supervisor {method} {path} timed out after {timeout}s") from exc
        except aiohttp.ClientError as exc:
            raise SupervisorError(f"Supervisor {method} {path} client error: {exc!r}") from exc

    # -- Add-on management ---------------------------------------------- #

    async def addon_info(self, slug: str) -> dict[str, Any]:
        """Return the addon info block ({version, state, options, ...})."""
        payload = await self._request("GET", f"/addons/{slug}/info")
        data = payload.get("data") if isinstance(payload, dict) else None
        return data or {}

    async def addon_state(self, slug: str) -> Optional[str]:
        """Return ``"started"`` / ``"stopped"`` / ``None`` if not installed."""
        try:
            info = await self.addon_info(slug)
        except SupervisorError as exc:
            logger.debug("addon_info(%s) failed: %s", slug, exc)
            return None
        # Not installed addons report ``version: None`` and ``state: None``.
        if not info.get("version"):
            return None
        return info.get("state")

    async def install_addon(self, slug: str) -> None:
        """Install a Supervisor addon (idempotent)."""
        logger.info("Supervisor: installing addon %s (this may take a few minutes)", slug)
        await self._request("POST", f"/addons/{slug}/install", timeout=_INSTALL_TIMEOUT)
        logger.info("Supervisor: addon %s installed", slug)

    async def start_addon(self, slug: str) -> None:
        """Start an installed addon."""
        logger.info("Supervisor: starting addon %s", slug)
        await self._request("POST", f"/addons/{slug}/start", timeout=_START_TIMEOUT)
        logger.info("Supervisor: addon %s started", slug)

    async def set_addon_options(self, slug: str, options: dict[str, Any]) -> None:
        """Patch the addon's ``options`` block (Supervisor merges with defaults)."""
        await self._request("POST", f"/addons/{slug}/options", json={"options": options})

    async def restart_addon(self, slug: str) -> None:
        """Restart an installed addon so option/file changes take effect."""
        logger.info("Supervisor: restarting addon %s", slug)
        await self._request("POST", f"/addons/{slug}/restart", timeout=_START_TIMEOUT)
        logger.info("Supervisor: addon %s restarted", slug)

    async def ensure_addon_running(
        self,
        slug: str,
        options: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Ensure ``slug`` is installed, configured, and started.

        Returns True if the addon ends up in ``state == "started"``. Failures
        are logged and surfaced as False (callers fall back to manual setup
        instructions rather than crashing the whole addon).
        """
        try:
            state = await self.addon_state(slug)
            if state is None:
                await self.install_addon(slug)
            if options:
                try:
                    await self.set_addon_options(slug, options)
                except SupervisorError as exc:
                    # Options schema mismatches shouldn't block startup.
                    logger.warning("Could not set options for %s: %s", slug, exc)
            # Re-check state after install.
            state = await self.addon_state(slug)
            if state != "started":
                await self.start_addon(slug)
                state = await self.addon_state(slug)
            return state == "started"
        except SupervisorError as exc:
            logger.warning("Could not auto-manage addon %s: %s", slug, exc)
            return False
