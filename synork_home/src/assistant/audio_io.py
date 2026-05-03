"""
Synork Home — Automatic microphone discovery.

The addon must work zero-config: no user settings for which mic to use, no
``audio_device_index`` knob in the HA add-on options. This module picks the
right capture device automatically and verifies it can actually be opened
at the wake-word pipeline's required format (16 kHz mono, 16-bit PCM).

Discovery strategy:

  1. Enumerate every PyAudio input device.
  2. Skip anything that obviously isn't a real mic
     (HDMI loopback, ``null``, ``dummy``, pulse-monitor, etc.).
  3. Score remaining candidates by name keywords — USB / array / mic / 
     respeaker / seeed / etc. all rank higher than generic onboard codecs.
  4. Try to open each candidate at 16 kHz mono 16-bit. The first one that
     actually opens wins. This catches cases where PortAudio reports a
     device as supported but ALSA refuses to open it (busy, wrong perms,
     no capture pin routed, etc.).
  5. If nothing opens, fall back to ``None`` so PyAudio uses its own
     default — same behaviour the codebase had before.

The result is exposed as a small ``DiscoveredMic`` dataclass so callers
can log what was picked, while only ever needing the integer device index
for the actual stream open.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
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
    index: Optional[int]                     # ``None`` = let PortAudio choose
    name: str                                # human-readable description
    channels: int                            # device's max input channels
    sample_rate: int                         # default sample rate it reports
    reason: str                              # short note for logs


def _score(name: str, channels: int) -> int:
    """Score a candidate device by its reported name and channel count."""
    n = (name or "").lower()
    if not n:
        return -1000

    score = 0
    for bad in _BLACKLIST:
        if bad in n:
            score -= 200
            break

    keyword_hit = False
    for kw, weight in _KEYWORDS:
        if kw in n:
            score += weight
            keyword_hit = True
            break

    for soft in _SOFT_DEFAULTS:
        if soft in n:
            score += 5  # mild preference; concrete mic still wins

    # Mic arrays ride channel count — but only if they also matched a
    # positive keyword. A 32-channel HDMI loopback shouldn't win on count.
    if keyword_hit:
        score += min(channels, 8)

    return score


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
        # PortAudio raises ValueError when the format is rejected; we treat
        # any check failure the same — fall through to a real open below.
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


def discover_input_device(
    prefer_keywords: Optional[Iterable[str]] = None,
) -> DiscoveredMic:
    """Pick the best capture device available, or fall back to PortAudio's default.

    Caller passes the resulting ``.index`` to ``pyaudio.PyAudio.open(...)``
    via ``input_device_index``. ``None`` means "let PortAudio choose".
    """
    try:
        import pyaudio  # type: ignore
    except ImportError as exc:
        logger.warning("PyAudio missing — mic discovery skipped (%s)", exc)
        return DiscoveredMic(None, "(pyaudio unavailable)", 0, 0,
                              reason="pyaudio import failed")

    extras = tuple((kw.lower(), 30) for kw in (prefer_keywords or ()))
    keywords = extras + _KEYWORDS

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
            score = _score(name, ch)
            # Apply caller-provided keyword overrides on top.
            n = name.lower()
            for kw, weight in extras:
                if kw and kw in n:
                    score += weight
            candidates.append((score, i, info))

        if not candidates:
            logger.warning(
                "Mic discovery: no input-capable devices reported by PortAudio "
                "(check that /dev/snd is mapped through and the addon has "
                "audio group access)"
            )
            return DiscoveredMic(None, "(no devices)", 0, 0,
                                  reason="no input-capable devices")

        candidates.sort(key=lambda t: t[0], reverse=True)

        # Walk in score order, keep the first one that actually opens.
        for score, idx, info in candidates:
            name = str(info.get("name") or f"device #{idx}")
            ch = int(info.get("maxInputChannels", 0) or 0)
            rate = int(info.get("defaultSampleRate", 0) or 0)
            if score <= -200:
                # Blacklisted; keep iterating in case nothing else opens.
                continue
            if _try_open(pa, idx):
                logger.info(
                    "Mic discovery: picked '%s' (idx=%d, ch=%d, rate=%dHz, score=%d)",
                    name, idx, ch, rate, score,
                )
                return DiscoveredMic(idx, name, ch, rate,
                                      reason=f"score={score} (opened ok)")

        # Nothing concrete opened; let PortAudio fall back to its default.
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


__all__ = ["DiscoveredMic", "discover_input_device"]
