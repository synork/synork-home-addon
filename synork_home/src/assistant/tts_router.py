"""Synork Home — TTS Routing.

Routes text-to-speech requests to the best available provider:
  - Relay TTS: cloud, calls Synork's relay (which proxies to Cartesia / etc.) — the preferred path for Arlo.
  - Piper:     local, fast, low-latency, good fallback when offline.

Selection logic for ``provider="auto"``:
  1. Relay TTS (if a relay session_token is available)
  2. Piper     (always last-resort)              — currently a stub

Audio format output: WAV (PCM 16-bit, 22050Hz).
​
The addon never holds upstream provider API keys: synthesis goes through the
Synork relay which authenticates the device by its short-lived session_token
and calls the upstream provider with server-held credentials.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .relay_tts import RelayTTS

logger = logging.getLogger("synork.assistant.tts")


class TTSRouter:
    """Routes TTS requests between cloud (relay-proxied) and local providers.

    Supports three providers:
      - cloud: synthesizes via the Synork relay (proxies to Cartesia / etc.)
      - piper: local neural TTS (low latency, decent quality)
      - auto:  cloud when the relay session is available, otherwise piper
    """

    def __init__(
        self,
        provider: str = "auto",
        language: str = "hu",
        relay_url: Optional[str] = None,
        mock_mode: bool = False,
        session_token_getter: Optional[Callable[[], Optional[str]]] = None,
        cartesia_voice_id: Optional[str] = None,
    ) -> None:
        self.provider = provider
        self.language = language
        self._relay_url = relay_url
        self._mock_mode = mock_mode
        self._piper_available = False

        # Cloud TTS goes through the Synork relay — no upstream API key on device.
        self._cloud = RelayTTS(
            relay_api_url=relay_url or "",
            session_token_getter=session_token_getter or (lambda: None),
            voice_id=cartesia_voice_id,
        )

    @property
    def cloud(self) -> RelayTTS:
        """Direct access to the relay-proxied client for callers that want streaming."""
        return self._cloud

    async def initialize(self) -> None:
        """Check availability of TTS providers."""
        if self._mock_mode:
            logger.info("TTS router initialized (MOCK mode)")
            return

        # Check Piper availability
        self._piper_available = await self._check_piper()

        logger.info(
            "TTS router initialized: cloud=%s, piper=%s, mode=%s",
            self._cloud.configured,
            self._piper_available,
            self.provider,
        )

    async def aclose(self) -> None:
        """Release any pooled HTTP sessions held by cloud providers."""
        try:
            await self._cloud.close()
        except Exception:
            pass

    async def synthesize(
        self,
        text: str,
        language: Optional[str] = None,
        voice: Optional[str] = None,
    ) -> bytes:
        """Synthesize speech from text using the best available provider."""
        lang = language or self.language

        if self._mock_mode:
            return self._mock_synthesize(text, lang)

        provider = self._select_provider(text)

        if provider in ("cloud", "cartesia", "elevenlabs") and self._cloud.configured:
            return await self._synthesize_cloud(text, lang, voice)
        if provider == "piper" and self._piper_available:
            return await self._synthesize_piper(text, lang, voice)
        # Auto-fallbacks in priority order.
        if self._cloud.configured:
            return await self._synthesize_cloud(text, lang, voice)
        if self._piper_available:
            return await self._synthesize_piper(text, lang, voice)

        logger.warning("No TTS provider available")
        return self._mock_synthesize(text, lang)

    def _select_provider(self, text: str) -> str:
        """Select the best provider based on config and availability."""
        if self.provider != "auto":
            return self.provider

        if self._cloud.configured:
            return "cloud"
        return "piper"

    async def _synthesize_cloud(
        self, text: str, language: str, voice: Optional[str]
    ) -> bytes:
        """Synthesize via the Synork relay (which proxies to Cartesia upstream)."""
        try:
            return await self._cloud.synthesize(text, language=language, voice=voice)
        except Exception as exc:
            logger.error("Cloud TTS failed (%s) — falling back", exc)
            return self._mock_synthesize(text, language)

    async def _synthesize_piper(self, text: str, language: str, voice: Optional[str]) -> bytes:
        """Synthesize using local Piper TTS."""
        # TODO: Implement when piper-tts dependency is available
        logger.warning("Piper TTS not yet installed — using mock")
        return self._mock_synthesize(text, language)

    async def _check_piper(self) -> bool:
        """Check if Piper TTS is available."""
        try:
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
