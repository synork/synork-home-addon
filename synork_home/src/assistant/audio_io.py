"""
Synork Home — Automatic microphone discovery.

Two-stage strategy:

  Stage 1 — PulseAudio (preferred when available).
    The HA Supervisor mounts a Pulse socket at /run/audio when the addon
    declares ``audio: true`` in config.yaml. Pulse sees every source the
    *host* sees — including USB mics and Bluetooth devices that hot-plug
    after container start, which raw ALSA enumeration misses entirely.

    We enumerate Pulse sources via ``pulsectl`` (pure-Python, no compile)
    and pick the best by name keyword. The chosen source name is exported
    as ``PULSE_SOURCE`` for the consumer; PortAudio's Pulse backend then
    routes the default device to that source automatically.

  Stage 2 — PortAudio fallback.
    If Pulse is unavailable (no socket, no ``pulsectl``, host without
    PulseAudio), enumerate input devices via PyAudio and probe-open the
    best candidate at 16 kHz mono. Matches the old behaviour.

Caller passes ``DiscoveredMic.index`` to ``pyaudio.open(input_device_index=…)``.
``index=None`` means "use PortAudio's default" — which is the right answer
for Pulse-routed sources (the env var does the routing).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Iterable, Optional

logger = logging.getLogger("synork.assistant.audio_io")

# Required wake-word capture format. Must match assistant/wake_word.py.
_REQUIRED_RATE = 16000
_REQUIRED_CHANNELS = 1

# Substrings that are almost never a real mic. Negative score.
_BLACKLIST = (
    "hdmi", "null", "dummy", "loopback", "monitor",
    "snoop", "samplerate", "speex", "iec958", "spdif",
)

# Substrings that boost a candidate. Match in order; first match counts most.
_KEYWORDS = (
    ("respeaker", 60),     # ReSpeaker 2-mic / 4-mic / mic-array
    ("seeed", 50),         # Seeed Voicecard
    ("array", 40),         # Generic mic arrays
    ("airpods", 35),       # Apple BT earbuds
    ("bluetooth", 30),
    ("mic", 25),
    ("usb", 20),
    ("input", 10),
    ("capture", 10),
)

# Substrings that hint the device is the PortAudio "default" wrapper —
# slightly bonus so we prefer it over a raw ``hw:`` if nothing concrete
# scores above it.
_SOFT_DEFAULTS = ("default", "pulse", "pipewire", "sysdefault")


@dataclass(frozen=True)
class DiscoveredMic:
    """Result of an automatic microphone scan."""
    index: Optional[int]                     # PortAudio device index (``None`` = default)
    name: str                                # human-readable description
    channels: int                            # device's max input channels
    sample_rate: int                         # default sample rate it reports
    reason: str                              # short note for logs
    pulse_source: Optional[str] = None       # Pulse source name to export as PULSE_SOURCE
    backend: str = "portaudio"               # "pulse" or "portaudio"


@dataclass(frozen=True)
class AudioSource:
    """A capture source as listed for the web UI dropdown."""
    name: str          # canonical name (Pulse source name or PortAudio idx string)
    description: str   # human-readable
    backend: str       # "pulse" or "portaudio"
    channels: int = 0


def _score(name: str, channels: int, extras: tuple[tuple[str, int], ...] = ()) -> int:
    """Score a candidate by reported name + channels + caller keywords."""
    n = (name or "").lower()
    if not n:
        return -1000

    score = 0
    for bad in _BLACKLIST:
        if bad in n:
            score -= 200
            break

    keyword_hit = False
    for kw, weight in extras:
        if kw and kw in n:
            score += weight
            keyword_hit = True
    for kw, weight in _KEYWORDS:
        if kw in n:
            score += weight
            keyword_hit = True
            break

    for soft in _SOFT_DEFAULTS:
        if soft in n:
            score += 5

    if keyword_hit:
        score += min(channels, 8)

    return score


# --------------------------------------------------------------------------- #
# Stage 1 — PulseAudio
# --------------------------------------------------------------------------- #

def _pulse_socket_available() -> bool:
    """Return True if a Pulse server is reachable from this container."""
    server = os.environ.get("PULSE_SERVER", "").strip()
    if server:
        # ``unix:/run/audio/external`` — strip the prefix and check the path.
        path = server.split(":", 1)[-1] if server.startswith("unix:") else None
        if path and os.path.exists(path):
            return True
        # Anything else (tcp:host:port) — we can't easily probe; trust env.
        return True
    # Common HA Supervisor mount point.
    return os.path.exists("/run/audio/external") or os.path.exists("/run/audio")


def _list_pulse_sources() -> list[AudioSource]:
    """List capture sources via pulsectl. Empty list if Pulse unreachable."""
    try:
        import pulsectl  # type: ignore
    except ImportError:
        logger.debug("Pulse discovery: pulsectl not installed")
        return []

    if not _pulse_socket_available():
        logger.debug("Pulse discovery: no Pulse socket reachable")
        return []

    try:
        with pulsectl.Pulse("synork-mic-discovery") as pulse:
            sources = []
            for src in pulse.source_list():
                # Skip monitors (loopback of speaker output) — they aren't
                # mics in any useful sense.
                if getattr(src, "monitor_of_sink", None) not in (None, 0xffffffff):
                    continue
                name = src.name or ""
                desc = src.description or name
                ch = int(getattr(src, "channel_count", 0) or 0)
                sources.append(AudioSource(
                    name=name, description=desc, backend="pulse", channels=ch,
                ))
            return sources
    except Exception as exc:
        logger.warning("Pulse discovery failed: %s", exc)
        return []


def _pick_pulse_source(
    sources: list[AudioSource],
    extras: tuple[tuple[str, int], ...],
) -> Optional[AudioSource]:
    """Return the best-scoring Pulse source, or ``None`` if nothing scores >0."""
    best: Optional[tuple[int, AudioSource]] = None
    for s in sources:
        # Score against both the technical name and the description.
        sc = max(
            _score(s.name, s.channels, extras),
            _score(s.description, s.channels, extras),
        )
        if best is None or sc > best[0]:
            best = (sc, s)
    if best is None or best[0] <= -200:
        return None
    return best[1]


# --------------------------------------------------------------------------- #
# Stage 2 — PortAudio fallback
# --------------------------------------------------------------------------- #

def _try_open(pa, index: int) -> bool:
    """Probe by actually opening the stream — the only reliable test."""
    import pyaudio  # type: ignore

    try:
        if not pa.is_format_supported(
            _REQUIRED_RATE,
            input_device=index,
            input_channels=_REQUIRED_CHANNELS,
            input_format=pyaudio.paInt16,
        ):
            return False
    except (ValueError, Exception):
        pass

    try:
        s = pa.open(
            format=pyaudio.paInt16,
            channels=_REQUIRED_CHANNELS,
            rate=_REQUIRED_RATE,
            input=True,
            input_device_index=index,
            frames_per_buffer=1024,
            start=False,
        )
    except Exception as exc:
        logger.debug("device #%d: open probe failed (%s)", index, exc)
        return False
    try:
        s.close()
    except Exception:
        pass
    return True


def _list_portaudio_devices() -> list[AudioSource]:
    """List input-capable PortAudio devices (used for the web UI dropdown)."""
    try:
        import pyaudio  # type: ignore
    except ImportError:
        return []
    out: list[AudioSource] = []
    pa = pyaudio.PyAudio()
    try:
        for i in range(pa.get_device_count()):
            try:
                info = pa.get_device_info_by_index(i)
            except Exception:
                continue
            if int(info.get("maxInputChannels", 0) or 0) < 1:
                continue
            name = str(info.get("name") or f"device #{i}")
            ch = int(info.get("maxInputChannels", 0) or 0)
            out.append(AudioSource(
                name=str(i), description=name, backend="portaudio", channels=ch,
            ))
    finally:
        try:
            pa.terminate()
        except Exception:
            pass
    return out


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

def list_audio_sources() -> list[AudioSource]:
    """Enumerate every capture source the addon can offer to the user.

    Pulse sources first (the rich list — sees hot-plugged USB / BT mics),
    then PortAudio devices as a fallback. Used by the web UI dropdown so
    the user can pick by real name instead of guessing an index.
    """
    sources = _list_pulse_sources()
    sources.extend(_list_portaudio_devices())
    return sources


def discover_input_device(
    prefer_keywords: Optional[Iterable[str]] = None,
) -> DiscoveredMic:
    """Pick the best capture device available.

    Tries Pulse first (the right answer in any HA-addon container with
    ``audio: true``); falls back to PortAudio enumeration if Pulse isn't
    reachable. ``prefer_keywords`` lets the caller boost specific names
    (e.g. ``("airpods",)`` to lock onto a paired Bluetooth source).
    """
    extras = tuple((kw.lower(), 50) for kw in (prefer_keywords or ()))

    # ── Stage 1: Pulse ──────────────────────────────────────────────────
    pulse_sources = _list_pulse_sources()
    if pulse_sources:
        picked = _pick_pulse_source(pulse_sources, extras)
        if picked is not None:
            logger.info(
                "Mic discovery: picked Pulse source '%s' (%s, ch=%d)",
                picked.description, picked.name, picked.channels,
            )
            return DiscoveredMic(
                index=None,
                name=picked.description,
                channels=picked.channels,
                sample_rate=_REQUIRED_RATE,
                reason="pulse-best",
                pulse_source=picked.name,
                backend="pulse",
            )
        logger.warning(
            "Pulse discovery: %d sources visible but none matched a "
            "useful keyword — falling through to PortAudio probe",
            len(pulse_sources),
        )

    # ── Stage 2: PortAudio fallback ─────────────────────────────────────
    try:
        import pyaudio  # type: ignore
    except ImportError as exc:
        logger.warning("PyAudio missing — mic discovery skipped (%s)", exc)
        return DiscoveredMic(None, "(pyaudio unavailable)", 0, 0,
                              reason="pyaudio import failed")

    pa = pyaudio.PyAudio()
    try:
        try:
            count = pa.get_device_count()
        except Exception as exc:
            logger.warning("PortAudio enumeration failed: %s", exc)
            return DiscoveredMic(None, "(enumeration failed)", 0, 0,
                                  reason=f"get_device_count error: {exc}")

        candidates: list[tuple[int, int, dict]] = []
        for i in range(count):
            try:
                info = pa.get_device_info_by_index(i)
            except Exception:
                continue
            if int(info.get("maxInputChannels", 0) or 0) < 1:
                continue
            name = str(info.get("name") or "")
            ch = int(info.get("maxInputChannels", 0) or 0)
            candidates.append((_score(name, ch, extras), i, info))

        if not candidates:
            logger.warning(
                "Mic discovery: no input-capable devices reported by either "
                "PulseAudio or PortAudio — check /dev/snd is mapped through "
                "and the addon has audio group access"
            )
            return DiscoveredMic(None, "(no devices)", 0, 0,
                                  reason="no input-capable devices")

        candidates.sort(key=lambda t: t[0], reverse=True)

        for score, idx, info in candidates:
            name = str(info.get("name") or f"device #{idx}")
            ch = int(info.get("maxInputChannels", 0) or 0)
            rate = int(info.get("defaultSampleRate", 0) or 0)
            if score <= -200:
                continue
            if _try_open(pa, idx):
                logger.info(
                    "Mic discovery: picked PortAudio '%s' (idx=%d, ch=%d, rate=%dHz, score=%d)",
                    name, idx, ch, rate, score,
                )
                return DiscoveredMic(idx, name, ch, rate,
                                      reason=f"score={score} (opened ok)",
                                      backend="portaudio")

        top_score, top_idx, top_info = candidates[0]
        top_name = str(top_info.get("name") or f"device #{top_idx}")
        logger.warning(
            "Mic discovery: no candidate opened at %dHz mono 16-bit — "
            "falling back to PortAudio default (best non-opening was '%s')",
            _REQUIRED_RATE, top_name,
        )
        return DiscoveredMic(None, "(default)", 0, 0,
                              reason=f"no candidate opened; top was '{top_name}'")
    finally:
        try:
            pa.terminate()
        except Exception:
            pass


__all__ = [
    "DiscoveredMic",
    "AudioSource",
    "discover_input_device",
    "list_audio_sources",
]
