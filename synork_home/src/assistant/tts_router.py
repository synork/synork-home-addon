"""Synork Home — TTS Routing.

Routes text-to-speech requests to the best available provider:
  - Cartesia Sonic-3: cloud, low-latency streaming — the preferred path for Arlo.
  - Piper:            local, fast, low-latency, good fallback when offline.
  - ElevenLabs:       cloud, high quality, used only when the relay TTS proxy is wired up.

Selection logic for ``provider="auto"``:
  1. Cartesia (if API key + voice_id configured)
  2. ElevenLabs (if relay reachable)         — currently a stub
  3. Piper (always last-resort)              — currently a stub

Audio format output: WAV (PCM 16-bit, 22050Hz).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .cartesia_tts import CartesiaTTS

logger = logging.getLogger("synork.assistant.tts")

# Threshold for auto-switching between Piper and ElevenLabs (legacy fallback).
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
        cartesia_api_key: Optional[str] = None,
        cartesia_voice_id: Optional[str] = None,
    ) -> None:
        self.provider = provider
        self.language = language
        self._relay_url = relay_url
        self._mock_mode = mock_mode
        self._piper_available = False
        self._elevenlabs_available = False

        # Cartesia client — may be unconfigured; ``configured`` reports state.
        self._cartesia = CartesiaTTS(
            api_key=cartesia_api_key,
            voice_id=cartesia_voice_id,
        )

    @property
    def cartesia(self) -> CartesiaTTS:
        """Direct access to the Cartesia client for callers that want streaming."""
        return self._cartesia

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
            "TTS router initialized: cartesia=%s, piper=%s, elevenlabs=%s, mode=%s",
            self._cartesia.configured,
            self._piper_available,
            self._elevenlabs_available,
            self.provider,
        )

    async def aclose(self) -> None:
        """Release any pooled HTTP sessions held by cloud providers."""
        try:
            await self._cartesia.close()
        except Exception:
            pass

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

        if provider == "cartesia" and self._cartesia.configured:
            return await self._synthesize_cartesia(text, lang, voice)
        if provider == "piper" and self._piper_available:
            return await self._synthesize_piper(text, lang, voice)
        if provider == "elevenlabs" and self._elevenlabs_available:
            return await self._synthesize_elevenlabs(text, lang, voice)
        # Auto-fallbacks in priority order.
        if self._cartesia.configured:
            return await self._synthesize_cartesia(text, lang, voice)
        if self._piper_available:
            return await self._synthesize_piper(text, lang, voice)

        logger.warning("No TTS provider available")
        return self._mock_synthesize(text, lang)

    def _select_provider(self, text: str) -> str:
        """Select the best provider based on config and availability."""
        if self.provider != "auto":
            return self.provider

        # Auto: Cartesia first when configured (best quality + streaming),
        # then fall through to local/legacy providers.
        if self._cartesia.configured:
            return "cartesia"
        if len(text) <= _SHORT_RESPONSE_CHARS:
            return "piper"
        if self._elevenlabs_available:
            return "elevenlabs"
        return "piper"

    async def _synthesize_cartesia(
        self, text: str, language: str, voice: Optional[str]
    ) -> bytes:
        """Synthesize via Cartesia Sonic-3 (cloud)."""
        try:
            return await self._cartesia.synthesize(text, language=language, voice=voice)
        except Exception as exc:
            logger.error("Cartesia synth failed (%s) — falling back", exc)
            return self._mock_synthesize(text, language)

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
