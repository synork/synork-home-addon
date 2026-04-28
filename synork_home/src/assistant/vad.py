"""Synork Home — Voice Activity Detection.

Detects speech presence in audio chunks using webrtcvad. Used to determine
when a user starts and stops speaking during a voice interaction.

The VAD processes audio in frames (10, 20, or 30ms at 16kHz) and tracks
a sliding window of recent frames to smooth out noise and brief pauses.

Audio format: PCM 16-bit mono, 16kHz sample rate.
"""

from __future__ import annotations

import logging
from collections import deque
from typing import Optional

logger = logging.getLogger("synork.assistant.vad")

# Audio parameters
SAMPLE_RATE = 16000
FRAME_DURATION_MS = 30  # webrtcvad supports 10, 20, or 30ms
FRAME_SIZE = int(SAMPLE_RATE * FRAME_DURATION_MS / 1000) * 2  # bytes (16-bit)

# Smoothing: how many frames in the window, and what fraction must be speech
WINDOW_SIZE = 10
SPEECH_THRESHOLD = 0.6  # 60% of frames must be speech to count as "speaking"
SILENCE_THRESHOLD = 0.2  # Below 20% = silence (user stopped speaking)


class VADProcessor:
    """Processes audio chunks to detect voice activity.

    Uses webrtcvad with a sliding window for smooth speech detection.
    Supports mock mode for testing without audio hardware.
    """

    def __init__(
        self,
        sensitivity: float = 0.5,
        mock_mode: bool = False,
    ) -> None:
        self._mock_mode = mock_mode
        self._vad = None
        self._window: deque[bool] = deque(maxlen=WINDOW_SIZE)
        self._is_speaking = False

        # Map sensitivity (0.0-1.0) to webrtcvad aggressiveness (0-3)
        # Higher sensitivity = lower aggressiveness = more likely to detect speech
        if sensitivity <= 0.25:
            self._aggressiveness = 3
        elif sensitivity <= 0.5:
            self._aggressiveness = 2
        elif sensitivity <= 0.75:
            self._aggressiveness = 1
        else:
            self._aggressiveness = 0

        if not mock_mode:
            try:
                import webrtcvad
                self._vad = webrtcvad.Vad(self._aggressiveness)
            except ImportError:
                logger.warning("webrtcvad not available — using mock VAD")
                self._mock_mode = True

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking

    def process_chunk(self, audio_chunk: bytes) -> bool:
        """Process a single audio chunk and return whether speech is detected.

        Args:
            audio_chunk: Raw audio bytes (PCM 16-bit, 16kHz, 30ms frame).

        Returns:
            True if the user is currently speaking (smoothed).
        """
        if self._mock_mode:
            return self._mock_process(audio_chunk)

        if not self._vad:
            return False

        # webrtcvad expects exactly one frame
        try:
            is_speech = self._vad.is_speech(audio_chunk, SAMPLE_RATE)
        except Exception:
            is_speech = False

        self._window.append(is_speech)

        # Compute speech ratio in the window
        if len(self._window) < 3:
            return False

        speech_ratio = sum(self._window) / len(self._window)

        # Hysteresis: require higher ratio to start, lower to stop
        if not self._is_speaking and speech_ratio >= SPEECH_THRESHOLD:
            self._is_speaking = True
        elif self._is_speaking and speech_ratio <= SILENCE_THRESHOLD:
            self._is_speaking = False

        return self._is_speaking

    def reset(self) -> None:
        """Reset the VAD state for a new utterance."""
        self._window.clear()
        self._is_speaking = False

    def _mock_process(self, audio_chunk: bytes) -> bool:
        """Mock VAD: detect speech based on audio energy."""
        if not audio_chunk:
            return False

        # Simple energy-based detection for mock mode
        total = sum(abs(int.from_bytes(audio_chunk[i:i+2], "little", signed=True))
                     for i in range(0, min(len(audio_chunk), 200), 2))
        avg_energy = total / max(1, min(len(audio_chunk), 200) // 2)

        is_speech = avg_energy > 500  # Arbitrary threshold
        self._window.append(is_speech)

        if len(self._window) < 3:
            return False

        speech_ratio = sum(self._window) / len(self._window)
        if not self._is_speaking and speech_ratio >= SPEECH_THRESHOLD:
            self._is_speaking = True
        elif self._is_speaking and speech_ratio <= SILENCE_THRESHOLD:
            self._is_speaking = False

        return self._is_speaking
