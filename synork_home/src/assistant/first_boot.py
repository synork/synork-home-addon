"""Arlo first-boot setup — runs once per addon container.

Responsibilities (all idempotent, safe to re-run):
  1. Download the OpenWakeWord ``hey_arlo.onnx`` model into
     ``/data/synork/models/`` (Synork-internal location).
  2. Install the same model into ``/share/openwakeword/`` so the
     Wyoming ``core_openwakeword`` Home Assistant add-on picks it up,
     and best-effort patch that add-on's options + restart it so the
     model is preloaded.
  3. If Cartesia is configured but ``cartesia_voice_id`` is empty, fetch a
     recommended multilingual voice for the configured language.
  4. Pre-download the configured Whisper model.

Failures here are non-fatal — we log and continue. The runtime degrades
to mock implementations rather than crashing the addon.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
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
WAKE_WORD_NAME = "hey_arlo"  # name used in preload_models (no extension)
# Custom-trained model published from the synork/synork-wake-word repo.
WAKE_WORD_RELEASE_API = (
    "https://api.github.com/repos/synork/synork-wake-word/releases/latest"
)
WAKE_WORD_DIRECT_URL = (
    "https://github.com/synork/synork-wake-word/releases/latest/download/hey_arlo.onnx"
)
FALLBACK_WAKE_WORD = "hey_jarvis_v0.1"

# Wyoming openWakeWord HA add-on integration.
# The add-on scans this directory for custom .onnx / .tflite files and
# preloads any name listed in its ``preload_models`` option.
WYOMING_OPENWAKEWORD_DIR = Path("/share/openwakeword")
WYOMING_OPENWAKEWORD_SLUG = "core_openwakeword"


def _ensure_dirs() -> None:
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


# ── Wake word ─────────────────────────────────────────────────────────────

async def _setup_wake_word() -> None:
    target = MODELS_DIR / WAKE_WORD_MODEL_NAME
    log.info("─" * 60)
    log.info("Wake word setup: starting")
    log.info("  Synork model dir : %s", MODELS_DIR)
    log.info("  Wyoming model dir: %s", WYOMING_OPENWAKEWORD_DIR)
    log.info("  Target file      : %s", target)
    log.info("  Source release   : %s", WAKE_WORD_RELEASE_API)

    source = "missing"
    if target.exists() and target.stat().st_size > 1024:
        log.info("Phase 1/3 (download): SKIP — already present (%d bytes)",
                 target.stat().st_size)
        source = "cached"
    else:
        log.info("Phase 1/3 (download): no cached model, fetching")
        if _fetch_wake_word_from_release(target):
            source = "github-release-api"
        elif _fetch_wake_word_direct(target):
            source = "github-release-direct"
        else:
            log.warning(
                "Phase 1/3 (download): both release endpoints failed — "
                "falling back to community model %s", FALLBACK_WAKE_WORD,
            )
            try:
                import openwakeword  # type: ignore
                openwakeword.utils.download_models([FALLBACK_WAKE_WORD])
                log.info("Downloaded fallback wake word model: %s", FALLBACK_WAKE_WORD)
                source = f"fallback:{FALLBACK_WAKE_WORD}"
            except Exception as exc:
                log.warning("Could not pre-download wake word model: %s", exc)
                log.info("Wake word setup: FAILED — runtime will degrade to mock")
                log.info("─" * 60)
                return

    # Phase 2: copy into Wyoming custom-model dir.
    if source.startswith("fallback:"):
        log.info("Phase 2/3 (wyoming install): SKIP — using community model "
                 "(already present in core_openwakeword)")
        wyoming_installed = False
    else:
        wyoming_installed = _install_to_wyoming_openwakeword(target)

    # Phase 3: configure the core_openwakeword add-on (best effort).
    if wyoming_installed:
        wyoming_configured = await _configure_wyoming_openwakeword()
    else:
        log.info("Phase 3/3 (wyoming configure): SKIP — model not installed in /share")
        wyoming_configured = False

    log.info(
        "Wake word setup: done  source=%s  wyoming_installed=%s  wyoming_configured=%s",
        source, wyoming_installed, wyoming_configured,
    )
    log.info("─" * 60)


def _install_to_wyoming_openwakeword(source_path: Path) -> bool:
    """Copy the trained model into the Wyoming openWakeWord add-on's custom dir."""
    dst = WYOMING_OPENWAKEWORD_DIR / WAKE_WORD_MODEL_NAME
    try:
        WYOMING_OPENWAKEWORD_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        log.warning(
            "Phase 2/3 (wyoming install): could not create %s (%s) — "
            "is the addon's `share:rw` mapping present?",
            WYOMING_OPENWAKEWORD_DIR, exc,
        )
        return False

    src_size = source_path.stat().st_size
    if dst.exists() and dst.stat().st_size == src_size:
        log.info(
            "Phase 2/3 (wyoming install): SKIP — already in place at %s (%d bytes)",
            dst, dst.stat().st_size,
        )
        return True

    try:
        shutil.copy2(source_path, dst)
    except Exception as exc:
        log.warning(
            "Phase 2/3 (wyoming install): copy %s → %s FAILED: %s",
            source_path, dst, exc,
        )
        return False

    log.info(
        "Phase 2/3 (wyoming install): copied %s (%d bytes) → %s (%d bytes)",
        source_path, src_size, dst, dst.stat().st_size,
    )
    return True


async def _configure_wyoming_openwakeword() -> bool:
    """Patch core_openwakeword options to preload our model + restart it."""
    # Imported lazily so this module stays importable from outside the addon
    # container (where ``aiohttp`` and the Supervisor token aren't available).
    try:
        from supervisor import SupervisorClient, SupervisorError  # type: ignore
    except Exception as exc:
        log.info(
            "Phase 3/3 (wyoming configure): SKIP — Supervisor client unavailable (%s)",
            exc,
        )
        return False

    options = {
        "custom_model_dir": str(WYOMING_OPENWAKEWORD_DIR),
        "preload_models": [WAKE_WORD_NAME],
    }
    log.info(
        "Phase 3/3 (wyoming configure): slug=%s options=%s",
        WYOMING_OPENWAKEWORD_SLUG, options,
    )

    try:
        async with SupervisorClient() as sup:
            if not sup.available:
                log.info(
                    "Phase 3/3 (wyoming configure): SKIP — SUPERVISOR_TOKEN not set "
                    "(not running under Supervisor)"
                )
                return False

            state = await sup.addon_state(WYOMING_OPENWAKEWORD_SLUG)
            log.info(
                "Phase 3/3 (wyoming configure): %s current state = %s",
                WYOMING_OPENWAKEWORD_SLUG, state,
            )
            if state is None:
                log.info(
                    "Phase 3/3 (wyoming configure): %s not installed — "
                    "model file is in place for when user installs the add-on",
                    WYOMING_OPENWAKEWORD_SLUG,
                )
                return False

            try:
                await sup.set_addon_options(WYOMING_OPENWAKEWORD_SLUG, options)
                log.info(
                    "Phase 3/3 (wyoming configure): options applied to %s",
                    WYOMING_OPENWAKEWORD_SLUG,
                )
            except SupervisorError as exc:
                log.warning(
                    "Phase 3/3 (wyoming configure): set_addon_options failed (%s) — "
                    "schema may differ; model file is still in place",
                    exc,
                )
                return False

            try:
                await sup.restart_addon(WYOMING_OPENWAKEWORD_SLUG)
                log.info(
                    "Phase 3/3 (wyoming configure): %s restarted — "
                    "new model should be active",
                    WYOMING_OPENWAKEWORD_SLUG,
                )
            except SupervisorError as exc:
                log.warning(
                    "Phase 3/3 (wyoming configure): restart failed (%s) — "
                    "user must restart %s manually for changes to take effect",
                    exc, WYOMING_OPENWAKEWORD_SLUG,
                )
            return True
    except Exception as exc:
        log.warning(
            "Phase 3/3 (wyoming configure): unexpected error (%s) — "
            "model file is still in place at %s/%s",
            exc, WYOMING_OPENWAKEWORD_DIR, WAKE_WORD_MODEL_NAME,
        )
        return False


def _fetch_wake_word_direct(target: Path) -> bool:
    """Download via the GitHub /releases/latest/download/<asset> redirect."""
    try:
        import urllib.request
    except Exception:
        return False
    log.info("  → trying direct URL %s", WAKE_WORD_DIRECT_URL)
    tmp = target.with_suffix(".onnx.partial")
    try:
        req = urllib.request.Request(
            WAKE_WORD_DIRECT_URL,
            headers={"User-Agent": "synork-arlo"},
        )
        with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as fh:
            fh.write(resp.read())
        if tmp.stat().st_size < 1024:
            log.warning("  ✗ direct download too small (%d bytes) — discarding",
                        tmp.stat().st_size)
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(target)
        log.info("  ✓ direct download OK: %s (%d bytes)",
                 target, target.stat().st_size)
        return True
    except Exception as exc:
        log.info("  ✗ direct download failed: %s", exc)
        tmp.unlink(missing_ok=True)
        return False


def _fetch_wake_word_from_release(target: Path) -> bool:
    """Download hey_arlo.onnx from the latest GitHub Release. Returns True on success."""
    try:
        import urllib.request
        import urllib.error
    except Exception:
        return False

    log.info("  → querying release API %s", WAKE_WORD_RELEASE_API)
    req = urllib.request.Request(
        WAKE_WORD_RELEASE_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "synork-arlo"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError, TimeoutError) as exc:
        log.info("  ✗ release API failed: %s", exc)
        return False

    asset_url = next(
        (a.get("browser_download_url") for a in (data.get("assets") or [])
         if a.get("name") == WAKE_WORD_MODEL_NAME),
        None,
    )
    if not asset_url:
        log.info("  ✗ release %s has no %s asset",
                 data.get("tag_name"), WAKE_WORD_MODEL_NAME)
        return False

    log.info("  → downloading asset %s (release %s)", asset_url, data.get("tag_name"))
    tmp = target.with_suffix(".onnx.partial")
    try:
        with urllib.request.urlopen(asset_url, timeout=120) as resp, tmp.open("wb") as fh:
            fh.write(resp.read())
        if tmp.stat().st_size < 1024:
            log.warning("  ✗ asset suspiciously small (%d bytes) — discarding",
                        tmp.stat().st_size)
            tmp.unlink(missing_ok=True)
            return False
        tmp.replace(target)
        log.info("  ✓ release download OK: %s (%d bytes, tag=%s)",
                 target, target.stat().st_size, data.get("tag_name"))
        return True
    except Exception as exc:
        log.warning("  ✗ asset download failed: %s", exc)
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
    try:
        asyncio.run(_setup_wake_word())
    except Exception as exc:
        log.warning("Wake word setup crashed: %s", exc)
    try:
        asyncio.run(_setup_cartesia_voice())
    except Exception as exc:
        log.warning("Cartesia voice setup failed: %s", exc)
    _setup_whisper()
    log.info("Arlo first-boot complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
