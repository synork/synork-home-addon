"""Synork Home — Wake Word Detection.

Wake word detection — listens for "Hey Synork" (or configured phrase).
Uses openWakeWord for on-device detection with minimal CPU usage.

The wake word detector runs continuously on the audio input stream,
processing small chunks (e.g., 1280 samples at 16kHz = 80ms). When
the wake word is detected with confidence above threshold, it fires
the registered callback to start the voice pipeline.

Audio format: PCM 16-bit mono, 16kHz sample rate.

Dependencies:
  - openwakeword (TODO: evaluate package size on ARM)
  - pyaudio for audio capture

In mock mode, triggers the callback periodically for testing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

logger = logging.getLogger("synork.assistant.wake_word")

# Audio parameters
SAMPLE_RATE = 16000
CHUNK_SIZE = 1280  # 80ms at 16kHz
CHANNELS = 1
FORMAT_WIDTH = 2  # 16-bit

# Detection threshold
DEFAULT_THRESHOLD = 0.5


class WakeWordDetector:
    """Detects wake words in an audio stream.

    In production, uses openWakeWord for efficient on-device detection.
    In mock mode, simulates detection at configurable intervals.
    """

    def __init__(
        self,
        model_name: str = "hey_synork",
        threshold: float = DEFAULT_THRESHOLD,
        mock_mode: bool = False,
        audio_device_index: Optional[int] = None,
    ) -> None:
        self.model_name = model_name
        self.threshold = threshold
        self._mock_mode = mock_mode
        self._audio_device_index = audio_device_index
        self._callback: Optional[Callable[[], None]] = None
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._model = None

    def on_wake_word(self, callback: Callable[[], None]) -> None:
        """Register a callback to be invoked when the wake word is detected."""
        self._callback = callback

    async def start(self) -> None:
        """Start listening for wake words on the audio input."""
        if not self._callback:
            logger.warning("Wake word detector started without a callback")

        self._running = True

        if self._mock_mode:
            self._task = asyncio.create_task(self._mock_listen_loop())
            logger.info("Wake word detector started (MOCK mode, model=%s)", self.model_name)
            return

        # Initialize the wake word model
        try:
            await self._load_model()
        except Exception as exc:
            logger.error("Failed to load wake word model: %s", exc)
            logger.info("Falling back to mock mode")
            self._mock_mode = True
            self._task = asyncio.create_task(self._mock_listen_loop())
            return

        self._task = asyncio.create_task(self._listen_loop())
        logger.info("Wake word detector started (model=%s, threshold=%.2f)", self.model_name, self.threshold)

    async def stop(self) -> None:
        """Stop wake word detection."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Wake word detector stopped")

    async def _load_model(self) -> None:
        """Load the openWakeWord model."""
        # TODO: Load actual openWakeWord model when dependency is available
        # import openwakeword
        # self._model = openwakeword.Model(wakeword_models=[self.model_name])
        raise ImportError("openwakeword not yet installed — using mock mode")

    async def _listen_loop(self) -> None:
        """Main audio capture and detection loop (production)."""
        # TODO: Implement with pyaudio when dependencies are available
        # This is the real pipeline:
        # 1. Open audio stream (pyaudio)
        # 2. Read chunks of CHUNK_SIZE samples
        # 3. Feed to openWakeWord model
        # 4. If confidence > threshold, fire callback
        # 5. After detection, pause briefly to avoid re-triggers
        pass

    async def _mock_listen_loop(self) -> None:
        """Simulate wake word detection for testing."""
        try:
            while self._running:
                # Simulate detection every 30 seconds in mock mode
                await asyncio.sleep(30)
                if self._callback and self._running:
                    logger.info("MOCK: Wake word detected ('%s')", self.model_name)
                    self._callback()
        except asyncio.CancelledError:
            pass
