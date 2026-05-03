"""
Synork Home — Relay TTS provider.

Calls the Synork relay's ``POST /api/home/tts/synthesize`` endpoint instead
of speaking to any cloud TTS provider directly. The relay holds the upstream
provider's API key — the addon never sees it.

Auth: ``Authorization: Bearer <session_token>`` where ``session_token`` is
the value the addon received in its most recent ``RelayWelcome`` frame. The
session_token is provided by a callable (``session_token_getter``) so this
client always picks up the latest token after a session resume.

Same surface as the previous ``CartesiaTTS`` so callers don't need to care
about the transport:

  • synthesize(text, language, voice) -> bytes      (single WAV blob)
  • stream(text, language, voice)     -> AsyncIterator[bytes]   (raw PCM)
  • configured                                          (truthy when usable)
  • close()                                             (release HTTP pool)
"""

from __future__ import annotations

import asyncio
import logging
import struct
from typing import Any, AsyncIterator, Callable, Optional

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

logger = logging.getLogger("synork.assistant.tts.relay")

# Same WAV defaults the backend produces (mono 16-bit PCM @ 22.05 kHz).
SAMPLE_RATE = 22050


def _silence_wav(seconds: float = 0.4) -> bytes:
    n = max(1, int(SAMPLE_RATE * seconds)) * 2
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", 36 + n, b"WAVE", b"fmt ",
        16, 1, 1, SAMPLE_RATE, SAMPLE_RATE * 2, 2, 16, b"data", n,
    )
    return header + b"\x00" * n


def _strip_wav_header(buf: bytes) -> bytes:
    """Best-effort removal of a leading WAV header so we can yield raw PCM.

    The relay always returns PCM wrapped in a standard 44-byte header. If the
    payload doesn't look like WAV we pass it through as-is.
    """
    if len(buf) >= 44 and buf[:4] == b"RIFF" and buf[8:12] == b"WAVE":
        # Find the "data" chunk and skip past its 8-byte chunk header.
        idx = buf.find(b"data", 12)
        if idx > 0 and len(buf) >= idx + 8:
            return buf[idx + 8 :]
    return buf


class RelayTTS:
    """TTS client that synthesizes via the Synork relay (no upstream keys on device)."""

    def __init__(
        self,
        relay_api_url: str,
        session_token_getter: Callable[[], Optional[str]],
        voice_id: Optional[str] = None,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self._base = (relay_api_url or "").rstrip("/")
        self._token_getter = session_token_getter
        self.voice_id = (voice_id or "").strip() or None
        self.sample_rate = sample_rate
        self._session: Optional[Any] = None

    @property
    def configured(self) -> bool:
        """True once we have a relay URL, an HTTP client, and a session token."""
        if not self._base or aiohttp is None:
            return False
        try:
            return bool(self._token_getter())
        except Exception:
            return False

    async def _get_session(self) -> Any:
        if aiohttp is None:
            raise RuntimeError("aiohttp not available — RelayTTS unusable")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=30, connect=5),
            )
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Public API ────────────────────────────────────────────────────

    async def synthesize(
        self,
        text: str,
        language: str = "hu",
        voice: Optional[str] = None,
    ) -> bytes:
        """Buffer a single utterance into one WAV blob.

        Falls back to silence on any error so the pipeline never deadlocks.
        """
        wav = await self._fetch_wav(text, language=language, voice=voice)
        return wav or _silence_wav()

    async def stream(
        self,
        text: str,
        language: str = "hu",
        voice: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        """Yield raw 16-bit PCM chunks (WAV header stripped).

        First cut: we wait for the full WAV from the relay, strip its header,
        and yield the PCM in fixed-size slices. A future iteration could swap
        in a chunked-transfer endpoint for true incremental playback.
        """
        wav = await self._fetch_wav(text, language=language, voice=voice)
        if not wav:
            return
        pcm = _strip_wav_header(wav)
        chunk_size = 4096
        for i in range(0, len(pcm), chunk_size):
            yield pcm[i : i + chunk_size]

    # ── Internals ─────────────────────────────────────────────────────

    async def _fetch_wav(
        self,
        text: str,
        language: str,
        voice: Optional[str],
    ) -> Optional[bytes]:
        if not self.configured:
            logger.warning("RelayTTS not configured (no session token yet)")
            return None
        text = (text or "").strip()
        if not text:
            return None

        token = self._token_getter()
        url = f"{self._base}/api/home/tts/synthesize"
        payload: dict[str, Any] = {"text": text, "language": language}
        chosen_voice = voice or self.voice_id
        if chosen_voice:
            payload["voice_id"] = chosen_voice
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "audio/wav",
        }

        try:
            session = await self._get_session()
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status == 401:
                    logger.error("Relay TTS rejected session token (401)")
                    return None
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    logger.error("Relay TTS HTTP %d — %s", resp.status, body)
                    return None
                return await resp.read()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Relay TTS request failed: %s", exc)
            return None


__all__ = ["RelayTTS"]
