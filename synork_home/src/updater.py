"""Self-update task for the running addon (live channel switching + WS push).

Runs as an asyncio background task. Two triggers:

1. **Periodic poll** (default every 5 min): `git fetch` against origin/<branch>
   for the configured channel. If HEAD differs, restart (s6 brings us back
   up; the bootloader pulls the new code on start).

2. **Relay push (B)**: when the relay sends an `addon.update` event, an
   immediate check is performed. This makes rolloutsappear instant for
   paired devices without waiting for the next poll.

Restart strategy: send SIGTERM to PID 1 (s6's `/init`) so the whole
container restarts cleanly and the bootloader runs again. If that's not
allowed, exit the current process — s6 will respawn the legacy-services
script which re-execs run.sh → bootloader.
"""
from __future__ import annotations

import asyncio
import logging
import os
import pathlib
import signal
import subprocess

logger = logging.getLogger("synork.updater")

LIVE = pathlib.Path(os.environ.get("SYNORK_LIVE_DIR", "/data/synork/app"))
REPO = os.environ.get("SYNORK_REPO_URL", "https://github.com/synork/synork-home-addon.git")
CHANNEL = os.environ.get("SYNORK_UPDATE_CHANNEL", "stable").strip().lower() or "stable"
BRANCH = {"stable": "main", "beta": "beta", "dev": "dev"}.get(CHANNEL, CHANNEL)

DEFAULT_INTERVAL = 300  # seconds


def current_head() -> str | None:
    if not (LIVE / ".git").exists():
        return None
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(LIVE),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except Exception:  # noqa: BLE001
        return None


def remote_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "ls-remote", REPO, f"refs/heads/{BRANCH}"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
        return out.split()[0] if out else None
    except Exception as e:  # noqa: BLE001
        logger.debug("ls-remote failed: %r", e)
        return None


def restart_for_update() -> None:
    """Trigger a clean container restart so the bootloader runs again."""
    logger.warning("update available — restarting container")
    # PID 1 is /init from s6-overlay; SIGTERM tells it to bring everything down.
    try:
        os.kill(1, signal.SIGTERM)
    except PermissionError:
        # Fallback: exit; s6 legacy-services will respawn run.sh
        logger.warning("could not signal PID 1; exiting process")
        os._exit(0)


async def _check_once() -> bool:
    cur = current_head()
    rem = remote_head()
    if not cur or not rem:
        return False
    if cur == rem:
        return False
    logger.info("update detected: %s → %s on %s", cur[:8], rem[:8], BRANCH)
    restart_for_update()
    return True


async def run(interval: int = DEFAULT_INTERVAL) -> None:
    """Periodic update check loop. Cancel-safe."""
    if interval <= 0:
        logger.info("auto-update polling disabled (interval=%d)", interval)
        return
    if os.environ.get("SYNORK_DISABLE_AUTOUPDATE", "").strip() in ("1", "true", "yes"):
        logger.info("auto-update disabled by env")
        return
    logger.info(
        "auto-update poll started (channel=%s branch=%s interval=%ds)",
        CHANNEL,
        BRANCH,
        interval,
    )
    while True:
        try:
            await _check_once()
        except Exception as e:  # noqa: BLE001
            logger.warning("update check failed: %r", e)
        await asyncio.sleep(interval)


async def trigger_now() -> bool:
    """Hook for relay push (B). Returns True if a restart was triggered."""
    return await _check_once()
