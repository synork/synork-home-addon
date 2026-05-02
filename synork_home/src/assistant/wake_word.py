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
        """Load the openWakeWord model.

        Looks for ``/data/synork/models/<model_name>.onnx`` first, then falls
        back to the bundled openwakeword community models. Either way we end
        up with an ``openwakeword.Model`` instance ready for prediction.
        """
        loop = asyncio.get_running_loop()

        def _load_sync():
            import openwakeword  # type: ignore
            from openwakeword.model import Model  # type: ignore

            custom = f"/data/synork/models/{self.model_name}.onnx"
            import os
            if os.path.exists(custom):
                logger.info("Loading custom wake word model: %s", custom)
                return Model(
                    wakeword_models=[custom],
                    inference_framework="onnx",
                )
            # Fallback to community model bundled with openwakeword.
            # ``hey_jarvis`` has the closest cadence to ``hey arlo``.
            try:
                openwakeword.utils.download_models(["hey_jarvis_v0.1"])
            except Exception:
                pass
            logger.info("Loading bundled wake word model: hey_jarvis_v0.1")
            return Model(
                wakeword_models=["hey_jarvis_v0.1"],
                inference_framework="onnx",
            )

        self._model = await loop.run_in_executor(None, _load_sync)

    async def _listen_loop(self) -> None:
        """Real audio capture + wake-word inference loop.

        16 kHz mono, 80 ms windows. After a positive detection we apply a
        2-second debounce so a single utterance can't double-trigger the
        downstream pipeline.
        """
        import time
        try:
            import numpy as np
            import pyaudio  # type: ignore
        except ImportError as exc:
            logger.error("Audio stack unavailable: %s — falling back to mock", exc)
            self._mock_mode = True
            await self._mock_listen_loop()
            return

        loop = asyncio.get_running_loop()
        pa = pyaudio.PyAudio()
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
                input_device_index=self._audio_device_index,
            )
        except Exception as exc:
            logger.error("Could not open mic: %s — falling back to mock", exc)
            pa.terminate()
            self._mock_mode = True
            await self._mock_listen_loop()
            return

        DEBOUNCE_S = 2.0
        last_fire = 0.0

        def _read_chunk() -> bytes:
            return stream.read(CHUNK_SIZE, exception_on_overflow=False)

        def _predict(audio: bytes) -> float:
            arr = np.frombuffer(audio, dtype=np.int16)
            scores = self._model.predict(arr)  # type: ignore[union-attr]
            # scores is a dict {model_label: confidence}; take the max.
            if not scores:
                return 0.0
            return float(max(scores.values()))

        try:
            while self._running:
                audio = await loop.run_in_executor(None, _read_chunk)
                conf = await loop.run_in_executor(None, _predict, audio)
                if conf:
                    logger.debug("wake_word confidence=%.3f", conf)
                if conf >= self.threshold:
                    now = time.monotonic()
                    if now - last_fire >= DEBOUNCE_S:
                        last_fire = now
                        logger.info("Wake word detected (%.3f)", conf)
                        if self._callback:
                            try:
                                self._callback()
                            except Exception as exc:
                                logger.exception("Wake-word callback raised: %s", exc)
        except asyncio.CancelledError:
            pass
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            pa.terminate()

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
