"""
Synork Home — Cartesia Sonic-3 TTS provider.

Streams synthesized speech from the Cartesia API. Designed for the lowest
practical first-audio latency so Arlo feels conversational, not "lookup-y".

Two interfaces:

  • synthesize(text, language, voice) -> bytes
        Convenience wrapper that buffers the stream into a single WAV blob.
        Used by the existing TTSRouter contract.

  • stream(text, language, voice) -> AsyncIterator[bytes]
        Yields raw PCM/WAV chunks as Cartesia produces them. Use this when
        the caller can play audio incrementally (preferred path).

Network failures fall back to a silent-WAV stub so the pipeline never
deadlocks waiting for cloud audio.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
from typing import Any, AsyncIterator, Optional

try:  # aiohttp is part of the addon's runtime; keep import optional for tests
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]

logger = logging.getLogger("synork.assistant.tts.cartesia")


CARTESIA_API_URL = "https://api.cartesia.ai/tts/bytes"
CARTESIA_API_VERSION = "2024-11-13"
CARTESIA_MODEL = "sonic-3"

# Cartesia returns 16-bit PCM by default; we wrap it in a WAV container.
SAMPLE_RATE = 22050

# Map ISO-639-1 language codes to Cartesia language tags.
_LANG_MAP = {
    "hu": "hu",
    "en": "en",
    "de": "de",
    "es": "es",
    "fr": "fr",
    "it": "it",
    "nl": "nl",
    "pl": "pl",
    "pt": "pt",
    "tr": "tr",
}


def _wav_header(data_size: int, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Minimal PCM mono 16-bit WAV header."""
    return struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        36 + data_size,
        b"WAVE",
        b"fmt ",
        16,                # subchunk size
        1,                 # PCM
        1,                 # mono
        sample_rate,
        sample_rate * 2,   # byte rate
        2,                 # block align
        16,                # bits per sample
        b"data",
        data_size,
    )


def _silence_wav(seconds: float = 0.4) -> bytes:
    """Tiny silent WAV used as a graceful fallback."""
    n = max(1, int(SAMPLE_RATE * seconds)) * 2
    return _wav_header(n) + b"\x00" * n


class CartesiaTTS:
    """Cartesia Sonic-3 client.

    The instance is cheap to create; the underlying aiohttp session is
    lazily opened on first use and reused across calls.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        voice_id: Optional[str] = None,
        model: str = CARTESIA_MODEL,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        self.api_key = api_key or os.getenv("CARTESIA_API_KEY", "")
        self.voice_id = voice_id or os.getenv("CARTESIA_VOICE_ID", "")
        self.model = model
        self.sample_rate = sample_rate
        self._session: Optional[Any] = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.voice_id and aiohttp is not None)

    async def _get_session(self) -> Any:
        if aiohttp is None:
            raise RuntimeError("aiohttp not available — Cartesia TTS unusable")
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
        """Buffer the entire stream into one WAV blob."""
        chunks: list[bytes] = []
        async for chunk in self.stream(text, language=language, voice=voice):
            chunks.append(chunk)
        pcm = b"".join(chunks)
        if not pcm:
            return _silence_wav()
        return _wav_header(len(pcm), self.sample_rate) + pcm

    async def stream(
        self,
        text: str,
        language: str = "hu",
        voice: Optional[str] = None,
    ) -> AsyncIterator[bytes]:
        """Yield raw 16-bit PCM chunks as Cartesia produces them.

        Caller is responsible for prepending a WAV header (or piping into
        an audio sink that accepts raw PCM at ``self.sample_rate``).
        """
        if not self.configured:
            logger.warning("Cartesia not configured (missing API key or voice_id)")
            return

        text = (text or "").strip()
        if not text:
            return

        payload = {
            "model_id": self.model,
            "transcript": text,
            "voice": {"mode": "id", "id": voice or self.voice_id},
            "output_format": {
                "container": "raw",
                "encoding": "pcm_s16le",
                "sample_rate": self.sample_rate,
            },
            "language": _LANG_MAP.get(language[:2].lower(), "en"),
        }

        headers = {
            "X-API-Key": self.api_key,
            "Cartesia-Version": CARTESIA_API_VERSION,
            "Content-Type": "application/json",
        }

        try:
            session = await self._get_session()
            async with session.post(
                CARTESIA_API_URL, json=payload, headers=headers
            ) as resp:
                if resp.status != 200:
                    body = (await resp.text())[:300]
                    logger.error(
                        "Cartesia TTS HTTP %d — %s", resp.status, body
                    )
                    return
                async for chunk in resp.content.iter_chunked(4096):
                    if chunk:
                        yield chunk
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("Cartesia TTS streaming failed: %s", exc)
            return


__all__ = ["CartesiaTTS"]
