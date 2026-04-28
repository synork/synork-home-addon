"""Synork Home — Speech-to-Text.

STT with two modes:
  - Local: faster-whisper for on-device transcription (privacy-first)
  - Remote: forward audio to Synork backend for cloud transcription

The privacy model prefers local. Cloud STT is only used when:
  1. User explicitly enables it in config
  2. Device hardware is too constrained for local (Pi Zero 2W)
  3. Local transcription fails and cloud is configured as fallback

Audio format: PCM 16-bit mono, 16kHz sample rate.
"""

from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Optional

logger = logging.getLogger("synork.assistant.stt")


class STTEngine:
    """Speech-to-text engine with local and cloud backends.

    Local backend: faster-whisper (CTranslate2-optimized Whisper)
    Cloud backend: Synork relay API (forwarded to cloud STT service)
    """

    def __init__(
        self,
        backend: str = "local",
        model: str = "base",
        language: str = "hu",
        relay_url: Optional[str] = None,
        mock_mode: bool = False,
    ) -> None:
        self.backend = backend
        self.model_name = model
        self.language = language
        self._relay_url = relay_url
        self._mock_mode = mock_mode
        self._whisper_model = None
        self._loaded = False

    async def load(self) -> None:
        """Load the STT model (local) or verify connectivity (remote)."""
        if self._mock_mode:
            self._loaded = True
            logger.info("STT loaded (MOCK mode)")
            return

        if self.backend == "local":
            await self._load_local_model()
        else:
            # Remote: just verify the relay URL is set
            if not self._relay_url:
                logger.warning("Remote STT configured but no relay URL — falling back to mock")
                self._mock_mode = True
            self._loaded = True
            logger.info("STT ready (remote via relay)")

    async def _load_local_model(self) -> None:
        """Load the faster-whisper model."""
        try:
            # TODO: Load actual faster-whisper when dependency is available
            # from faster_whisper import WhisperModel
            # self._whisper_model = WhisperModel(
            #     self.model_name,
            #     device="cpu",
            #     compute_type="int8",
            # )
            raise ImportError("faster-whisper not yet installed")
        except ImportError:
            logger.warning("faster-whisper not available — falling back to mock STT")
            self._mock_mode = True
            self._loaded = True

    async def transcribe(self, audio_data: bytes) -> str:
        """Transcribe a complete audio buffer to text.

        Args:
            audio_data: Raw audio bytes (PCM 16-bit, 16kHz).

        Returns:
            Transcribed text string.
        """
        if self._mock_mode:
            return self._mock_transcribe(audio_data)

        if self.backend == "local" and self._whisper_model:
            return await self._transcribe_local(audio_data)
        elif self.backend == "remote":
            return await self._transcribe_remote(audio_data)

        return ""

    async def transcribe_stream(self, audio_stream: AsyncIterator[bytes]) -> str:
        """Transcribe a streaming audio source to text.

        Collects all chunks and transcribes as a single buffer.
        Streaming transcription (word-by-word) is a v2 feature.
        """
        chunks: list[bytes] = []
        async for chunk in audio_stream:
            chunks.append(chunk)

        if not chunks:
            return ""

        return await self.transcribe(b"".join(chunks))

    async def _transcribe_local(self, audio_data: bytes) -> str:
        """Transcribe using the local faster-whisper model."""
        # Run in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()

        def _run():
            import io
            import numpy as np
            audio_array = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0
            segments, _ = self._whisper_model.transcribe(
                audio_array,
                language=self.language,
                beam_size=5,
            )
            return " ".join(seg.text.strip() for seg in segments)

        return await loop.run_in_executor(None, _run)

    async def _transcribe_remote(self, audio_data: bytes) -> str:
        """Forward audio to the Synork relay for cloud STT."""
        # TODO: Implement when relay has STT endpoint
        # POST /api/home/stt with audio blob
        logger.warning("Remote STT not yet implemented")
        return self._mock_transcribe(audio_data)

    def _mock_transcribe(self, audio_data: bytes) -> str:
        """Mock transcription for testing."""
        duration_s = len(audio_data) / (16000 * 2)  # 16kHz, 16-bit
        logger.info("MOCK STT: transcribed %.1fs of audio", duration_s)
        return f"[Mock transcription of {duration_s:.1f}s audio]"
