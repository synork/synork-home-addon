"""Arlo first-boot setup — runs once per addon container.

Responsibilities (all idempotent, safe to re-run):
  1. Download the OpenWakeWord ``hey_arlo.onnx`` model into
     ``/data/synork/models/`` if not present.
  2. If Cartesia is configured but ``cartesia_voice_id`` is empty, fetch a
     recommended multilingual voice for the configured language and persist
     it to ``/data/synork/arlo_voice.json`` so the runtime can pick it up.
  3. Pre-download the configured Whisper model so the first user request
     doesn't pay the model-download latency.

Failures here are non-fatal — we log and continue. The runtime degrades
to mock implementations rather than crashing the addon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [arlo.first_boot] %(levelname)s %(message)s",
)
log = logging.getLogger("arlo.first_boot")

DATA_DIR = Path("/data/synork")
MODELS_DIR = DATA_DIR / "models"
VOICE_CONFIG = DATA_DIR / "arlo_voice.json"

WAKE_WORD_MODEL_NAME = "hey_arlo.onnx"
# Custom-trained model published from the synork/synork-wake-word repo.
# Releases are tagged v* — we always pull the latest non-prerelease.
WAKE_WORD_RELEASE_API = (
    "https://api.github.com/repos/synork/synork-wake-word/releases/latest"
)
# Direct download of the asset on the latest release (no API rate limits).
WAKE_WORD_DIRECT_URL = (
    "https://github.com/synork/synork-wake-word/releases/latest/download/hey_arlo.onnx"
)
# Fallback if no release exists yet — closest community model.
FALLBACK_WAKE_WORD = "hey_jarvis_v0.1"


def _ensure_dirs() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


# ── Wake word ─────────────────────────────────────────────────────────────

def _setup_wake_word() -> None:
    target = MODELS_DIR / WAKE_WORD_MODEL_NAME
    if target.exists() and target.stat().st_size > 1024:
        log.info("Wake word model already present: %s", target)
        return

    # Try to fetch the latest custom-trained release first.
    if _fetch_wake_word_from_release(target):
        return

    # API call failed (rate limited, no network, etc.) — try the direct
    # /releases/latest/download URL which always points at the newest
    # asset and doesn't count against the GitHub REST API quota.
    if _fetch_wake_word_direct(target):
        return

    # Otherwise fall back to a community model so the runtime has something.
    try:
        import openwakeword  # type: ignore
        openwakeword.utils.download_models([FALLBACK_WAKE_WORD])
        log.info("Downloaded fallback wake word model: %s", FALLBACK_WAKE_WORD)
    except Exception as exc:
        log.warning("Could not pre-download wake word model: %s", exc)


def _fetch_wake_word_direct(target: Path) -> bool:
    """Download via the GitHub /releases/latest/download/<asset> redirect."""
    try:
        import urllib.request
    except Exception:
        return False
    tmp = target.with_suffix(".onnx.partial")
    try:
        req = urllib.request.Request(
            WAKE_WORD_DIRECT_URL,
            headers={"User-Agent": "synork-arlo"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
            fh.write(resp.read())
        if tmp.stat().st_size < 1024:
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(target)
        log.info("Downloaded wake word model via direct URL: %s (%d bytes)",
                 target, target.stat().st_size)
        return True
    except Exception as exc:
        log.info("Direct wake word download failed: %s", exc)
        tmp.unlink(missing_ok=True)
        return False


def _fetch_wake_word_from_release(target: Path) -> bool:
    """Download hey_arlo.onnx from the latest GitHub Release. Returns True on success."""
    try:
        import urllib.request
        import urllib.error
    except Exception:
        return False

    req = urllib.request.Request(
        WAKE_WORD_RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "synork-arlo"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        log.info("No release available for wake word (%s) — using fallback", exc)
        return False

    asset_url = next(
        (a.get("browser_download_url") for a in (data.get("assets") or [])
         if a.get("name") == WAKE_WORD_MODEL_NAME),
        None,
    )
    if not asset_url:
        log.info("Latest release has no %s asset — using fallback", WAKE_WORD_MODEL_NAME)
        return False

    tmp = target.with_suffix(".onnx.partial")
    try:
        with urllib.request.urlopen(asset_url, timeout=120) as resp, tmp.open("wb") as fh:
            fh.write(resp.read())
        # Reject smoke-test placeholders.
        if tmp.stat().st_size < 1024:
            log.warning("Downloaded model is suspiciously small (%d bytes) — discarding",
                        tmp.stat().st_size)
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(target)
        log.info("Downloaded custom wake word model: %s (%d bytes, tag=%s)",
                 target, target.stat().st_size, data.get("tag_name"))
        return True
    except Exception as exc:
        log.warning("Wake word release download failed: %s", exc)
        tmp.unlink(missing_ok=True)
        return False


# ── Cartesia voice ────────────────────────────────────────────────────────

async def _setup_cartesia_voice() -> None:
    api_key = os.environ.get("CARTESIA_API_KEY", "").strip()
    voice_id = os.environ.get("CARTESIA_VOICE_ID", "").strip()
    language = (os.environ.get("ARLO_LANGUAGE") or "en")[:2]

    if not api_key:
        log.info("Cartesia not configured — skipping voice fetch")
        return

    if voice_id:
        # Persist what the operator configured so the runtime always sees a value.
        VOICE_CONFIG.write_text(json.dumps({"voice_id": voice_id, "source": "config"}))
        return

    # No voice_id configured — pick a recommended one for the language.
    try:
        import aiohttp
    except ImportError:
        log.warning("aiohttp missing — skipping Cartesia voice fetch")
        return

    headers = {
        "X-API-Key": api_key,
        "Cartesia-Version": "2024-11-13",
    }
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://api.cartesia.ai/voices", headers=headers) as resp:
                if resp.status != 200:
                    log.warning("Cartesia voices API returned %d", resp.status)
                    return
                voices = await resp.json()
    except Exception as exc:
        log.warning("Cartesia voices fetch failed: %s", exc)
        return

    if not isinstance(voices, list):
        return

    def _score(v: dict) -> int:
        langs = v.get("language") or v.get("languages") or []
        if isinstance(langs, str):
            langs = [langs]
        score = 0
        if len(langs) > 1:
            score += 2
        if any((l or "")[:2].lower() == language for l in langs):
            score += 4
        return score

    best = max(voices, key=_score, default=None)
    if not isinstance(best, dict):
        return
    chosen = best.get("id")
    if not chosen:
        return
    VOICE_CONFIG.write_text(json.dumps({
        "voice_id": chosen,
        "source": "auto",
        "language": language,
    }))
    log.info("Cartesia voice auto-selected: %s (lang=%s)", chosen, language)


# ── Whisper ───────────────────────────────────────────────────────────────

def _setup_whisper() -> None:
    model = (os.environ.get("WHISPER_MODEL") or "large-v3-turbo").strip()
    cache_dir = MODELS_DIR / "whisper"
    cache_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("HF_HOME", str(cache_dir))
    try:
        from faster_whisper import WhisperModel  # type: ignore
        # Constructing the model triggers a download into the HF cache.
        WhisperModel(model, device="cpu", compute_type="int8", download_root=str(cache_dir))
        log.info("Whisper model ready: %s (cache=%s)", model, cache_dir)
    except Exception as exc:
        log.warning("Whisper pre-download failed (%s) — runtime will retry", exc)


def main() -> int:
    _ensure_dirs()
    _setup_wake_word()
    try:
        asyncio.run(_setup_cartesia_voice())
    except Exception as exc:
        log.warning("Cartesia voice setup failed: %s", exc)
    _setup_whisper()
    log.info("Arlo first-boot complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
