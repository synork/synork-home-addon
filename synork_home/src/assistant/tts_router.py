"""Synork Home — TTS Routing.

Routes text-to-speech requests to the best available provider:
  - Piper: local, fast, low-latency, good for short responses
  - ElevenLabs: cloud, higher quality, better for long/polished responses

Selection logic:
  - "auto" mode: Piper for <100 chars, ElevenLabs for longer (if online)
  - "piper" mode: always local
  - "elevenlabs" mode: always cloud (fallback to Piper if offline)

Audio format output: WAV (PCM 16-bit, 22050Hz or 24000Hz depending on model).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

logger = logging.getLogger("synork.assistant.tts")

# Threshold for auto-switching between Piper and ElevenLabs
_SHORT_RESPONSE_CHARS = 100


class TTSRouter:
    """Routes TTS requests between online and offline providers.

    Supports three providers:
      - piper: local neural TTS (low latency, decent quality)
      - elevenlabs: cloud neural TTS (higher quality, network dependent)
      - auto: picks based on text length and network availability
    """

    def __init__(
        self,
        provider: str = "auto",
        language: str = "hu",
        relay_url: Optional[str] = None,
        mock_mode: bool = False,
    ) -> None:
        self.provider = provider
        self.language = language
        self._relay_url = relay_url
        self._mock_mode = mock_mode
        self._piper_available = False
        self._elevenlabs_available = False

    async def initialize(self) -> None:
        """Check availability of TTS providers."""
        if self._mock_mode:
            logger.info("TTS router initialized (MOCK mode)")
            return

        # Check Piper availability
        self._piper_available = await self._check_piper()
        # ElevenLabs availability depends on relay connectivity
        self._elevenlabs_available = self._relay_url is not None

        logger.info(
            "TTS router initialized: piper=%s, elevenlabs=%s, mode=%s",
            self._piper_available,
            self._elevenlabs_available,
            self.provider,
        )

    async def synthesize(
        self,
        text: str,
        language: Optional[str] = None,
        voice: Optional[str] = None,
    ) -> bytes:
        """Synthesize speech from text using the best available provider.

        Args:
            text: The text to synthesize.
            language: Language code (defaults to configured language).
            voice: Optional voice identifier.

        Returns:
            Raw audio bytes (WAV format).
        """
        lang = language or self.language

        if self._mock_mode:
            return self._mock_synthesize(text, lang)

        provider = self._select_provider(text)

        if provider == "piper" and self._piper_available:
            return await self._synthesize_piper(text, lang, voice)
        elif provider == "elevenlabs" and self._elevenlabs_available:
            return await self._synthesize_elevenlabs(text, lang, voice)
        elif self._piper_available:
            # Fallback to Piper if requested provider unavailable
            return await self._synthesize_piper(text, lang, voice)
        else:
            logger.warning("No TTS provider available")
            return self._mock_synthesize(text, lang)

    def _select_provider(self, text: str) -> str:
        """Select the best provider based on config and text length."""
        if self.provider != "auto":
            return self.provider

        # Auto mode: short = Piper (low latency), long = ElevenLabs (quality)
        if len(text) <= _SHORT_RESPONSE_CHARS:
            return "piper"
        if self._elevenlabs_available:
            return "elevenlabs"
        return "piper"

    async def _synthesize_piper(self, text: str, language: str, voice: Optional[str]) -> bytes:
        """Synthesize using local Piper TTS."""
        # TODO: Implement when piper-tts dependency is available
        # from piper import PiperVoice
        # voice_model = voice or f"{language}-medium"
        # return PiperVoice(voice_model).synthesize(text)
        logger.warning("Piper TTS not yet installed — using mock")
        return self._mock_synthesize(text, language)

    async def _synthesize_elevenlabs(self, text: str, language: str, voice: Optional[str]) -> bytes:
        """Synthesize using ElevenLabs via the Synork relay."""
        # TODO: Implement when relay has TTS proxy endpoint
        # POST /api/home/tts with text + voice config
        logger.warning("ElevenLabs TTS via relay not yet implemented — using mock")
        return self._mock_synthesize(text, language)

    async def _check_piper(self) -> bool:
        """Check if Piper TTS is available."""
        try:
            # TODO: Check actual piper-tts import
            # import piper
            return False
        except ImportError:
            return False

    def _mock_synthesize(self, text: str, language: str) -> bytes:
        """Generate silent WAV audio for testing."""
        logger.info("MOCK TTS: synthesized %d chars in %s", len(text), language)
        # Generate a minimal WAV header + silence
        sample_rate = 22050
        duration_s = max(0.5, len(text) * 0.05)  # ~50ms per character
        num_samples = int(sample_rate * duration_s)
        data_size = num_samples * 2  # 16-bit

        # WAV header (44 bytes)
        import struct
        header = struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            36 + data_size,
            b"WAVE",
            b"fmt ",
            16,  # chunk size
            1,   # PCM
            1,   # mono
            sample_rate,
            sample_rate * 2,  # byte rate
            2,   # block align
            16,  # bits per sample
            b"data",
            data_size,
        )
        return header + b"\x00" * data_size
