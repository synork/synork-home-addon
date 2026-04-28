"""Synork Home — Home Assistant notification helper.

Creates and dismisses HA persistent_notifications via the Supervisor
proxy, signalling unconfigured/configured state to the user from inside
HA's UI. Without this, an unpaired addon idles silently and the user has
no breadcrumb to find the wizard.

Uses SUPERVISOR_TOKEN injected into the addon container by HA Supervisor
(see addon/run.sh). Calls are best-effort: if Supervisor is briefly
unreachable on startup, we log and move on — the wizard panel is still
reachable, the user just won't see the notification banner.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger("synork.wizard.notifications")

_NOTIFICATION_ID = "synork_home_setup_needed"
_CORE_API_BASE = "http://supervisor/core/api"


def _supervisor_token() -> Optional[str]:
    return os.environ.get("SUPERVISOR_TOKEN") or None


async def notify_setup_needed() -> None:
    """Post a persistent_notification telling the user to open the addon panel.

    Idempotent: HA's persistent_notification.create with a fixed
    notification_id replaces any existing notification of that id, so
    calling this on every unpaired startup is safe.
    """
    token = _supervisor_token()
    if not token:
        logger.debug("SUPERVISOR_TOKEN not set; skipping setup notification")
        return

    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "title": "Synork Home — finish setup",
        "message": (
            "The Synork Home add-on is installed but not paired with a "
            "Synork account yet. Open it from the sidebar (or click "
            "**Open Web UI** on the add-on page) to complete the 30-second "
            "setup wizard."
        ),
        "notification_id": _NOTIFICATION_ID,
    }
    url = f"{_CORE_API_BASE}/services/persistent_notification/create"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning("persistent_notification.create failed (%d): %s", resp.status, body)
                else:
                    logger.info("posted persistent_notification (setup needed)")
    except (aiohttp.ClientError, OSError) as exc:
        logger.warning("could not reach Supervisor to post setup notification: %s", exc)


async def dismiss_setup_notification() -> None:
    """Clear the setup-needed notification after successful pairing."""
    token = _supervisor_token()
    if not token:
        return

    headers = {"Authorization": f"Bearer {token}"}
    payload = {"notification_id": _NOTIFICATION_ID}
    url = f"{_CORE_API_BASE}/services/persistent_notification/dismiss"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status >= 400:
                    logger.debug("persistent_notification.dismiss returned %d (already dismissed?)", resp.status)
    except (aiohttp.ClientError, OSError) as exc:
        logger.debug("dismiss notification failed (non-fatal): %s", exc)
