#!/usr/bin/env python3
"""Synork Home addon bootloader.

Runs at container start. Tries to fetch the latest app code from the
public addon repo via `git`, then exec's the live code. Falls back to
the baked-in /app baseline if the network is unavailable.

Channels map to git branches:
    stable -> main
    beta   -> beta
    dev    -> dev

Environment:
    SYNORK_REPO_URL        Override repo URL (default: public addon repo)
    SYNORK_UPDATE_CHANNEL  stable|beta|dev (default: stable)
    SYNORK_DISABLE_AUTOUPDATE=1  Skip the live pull (use baked-in)

The bootloader passes through all sys.argv to the real entrypoint, so
run.sh can invoke it with the same args it would pass to main.py.
"""
from __future__ import annotations

import hashlib
import logging
import os
import pathlib
import shutil
import subprocess
import sys

REPO = os.environ.get(
    "SYNORK_REPO_URL", "https://github.com/synork/synork-home-addon.git"
)
CHANNEL = os.environ.get("SYNORK_UPDATE_CHANNEL", "stable").strip().lower() or "stable"
DISABLE = os.environ.get("SYNORK_DISABLE_AUTOUPDATE", "").strip() in ("1", "true", "yes")
BRANCH = {"stable": "main", "beta": "beta", "dev": "dev"}.get(CHANNEL, CHANNEL)

LIVE = pathlib.Path("/data/synork/app")
BAKED = pathlib.Path("/app")

logging.basicConfig(level=logging.INFO, format="[bootloader] %(message)s")
log = logging.getLogger("bootloader")


def _sh(cmd: list[str], cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _try_update() -> bool:
    """Clone or fast-forward LIVE to origin/BRANCH. Returns True on success."""
    LIVE.parent.mkdir(parents=True, exist_ok=True)
    try:
        if (LIVE / ".git").is_dir():
            _sh(["git", "remote", "set-url", "origin", REPO], cwd=LIVE)
            _sh(["git", "fetch", "--depth", "1", "origin", BRANCH], cwd=LIVE)
            _sh(["git", "reset", "--hard", f"origin/{BRANCH}"], cwd=LIVE)
            _sh(["git", "clean", "-fd"], cwd=LIVE)
        else:
            if LIVE.exists():
                shutil.rmtree(LIVE)
            _sh(["git", "clone", "--depth", "1", "-b", BRANCH, REPO, str(LIVE)])
        head = _sh(["git", "rev-parse", "--short", "HEAD"], cwd=LIVE).stdout.strip()
        log.info("live code @ %s/%s (%s)", BRANCH, head, REPO)
        return True
    except subprocess.CalledProcessError as e:
        log.warning("git update failed: %s", (e.stderr or e.stdout or "").strip())
    except subprocess.TimeoutExpired:
        log.warning("git update timed out")
    except Exception as e:  # noqa: BLE001
        log.warning("git update error: %r", e)
    return False


def _maybe_pip_install(addon_dir: pathlib.Path) -> None:
    req = addon_dir / "requirements.txt"
    if not req.exists():
        return
    stamp = LIVE / ".req.sha256"
    h = hashlib.sha256(req.read_bytes()).hexdigest()
    if stamp.exists() and stamp.read_text().strip() == h:
        return
    log.info("requirements.txt changed → pip install")
    r = subprocess.run(
        [
            "pip3",
            "install",
            "--no-cache-dir",
            "--break-system-packages",
            "-r",
            str(req),
        ]
    )
    if r.returncode == 0:
        stamp.write_text(h)
    else:
        log.warning("pip install failed (rc=%d); continuing", r.returncode)


def _exec_live() -> None:
    """Exec the live app. The repo layout is synork_home/{src,shared,...}."""
    addon_dir = LIVE / "synork_home"
    src_dir = addon_dir / "src"
    main_py = src_dir / "main.py"
    if not main_py.exists():
        log.warning("live tree missing %s — falling back", main_py)
        return
    _maybe_pip_install(addon_dir)
    env = os.environ.copy()
    pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{addon_dir}:{src_dir}" + (f":{pp}" if pp else "")
    env["SYNORK_LIVE_DIR"] = str(LIVE)
    env["SYNORK_UPDATE_CHANNEL"] = CHANNEL
    env["SYNORK_REPO_URL"] = REPO
    log.info("exec live %s", main_py)
    os.chdir(addon_dir)
    os.execvpe("python3", ["python3", str(main_py), *sys.argv[1:]], env)


def _exec_baseline() -> None:
    log.warning("using baked-in baseline /app")
    os.chdir(BAKED)
    os.execvp("python3", ["python3", str(BAKED / "main.py"), *sys.argv[1:]])


def main() -> None:
    if DISABLE:
        log.info("auto-update disabled by env; skipping git pull")
    else:
        if _try_update():
            _exec_live()  # noreturn on success
    _exec_baseline()


if __name__ == "__main__":
    main()
