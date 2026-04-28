"""Synork Home — Turn-Taking / Pipeline Orchestrator.

Orchestrates the full voice assistant pipeline:
  1. Wake word detected -> start audio capture
  2. VAD monitors for speech -> accumulate audio frames
  3. User stops speaking -> STT transcription
  4. Send transcribed query to Synork relay
  5. Receive response -> TTS synthesis -> audio playback
  6. Handle interruption (user speaks during assistant output)

This is the glue that connects wake_word, vad, stt, tts_router, and
the relay client into a coherent voice interaction experience.

State machine:
  IDLE -> LISTENING -> CAPTURING -> TRANSCRIBING -> WAITING -> SPEAKING -> IDLE
  (SPEAKING can be interrupted back to CAPTURING)
"""

from __future__ import annotations

import asyncio
import logging
import time
from enum import Enum
from typing import Any, Callable, Coroutine, Optional

from .vad import VADProcessor, FRAME_SIZE, SAMPLE_RATE
from .stt import STTEngine
from .tts_router import TTSRouter
from .wake_word import WakeWordDetector

logger = logging.getLogger("synork.assistant.pipeline")

# Maximum recording duration (prevent stuck recordings)
MAX_RECORDING_S = 30.0

# Silence duration after speech to trigger end-of-turn
END_OF_TURN_SILENCE_S = 1.5


class PipelineState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"       # Wake word detected, waiting for speech
    CAPTURING = "capturing"       # Speech detected, accumulating audio
    TRANSCRIBING = "transcribing" # Running STT on captured audio
    WAITING = "waiting"           # Waiting for assistant response from relay
    SPEAKING = "speaking"         # Playing TTS audio response
    ERROR = "error"


# Type for the relay invoke callback
InvokeCallback = Callable[[str, str, str], Coroutine[Any, Any, Optional[str]]]
# (query, language, user_id) -> response_text


class TurnTakingManager:
    """Manages conversational turn-taking for voice interactions.

    Wires together the voice pipeline components and manages the
    state machine for voice conversations.
    """

    def __init__(
        self,
        wake_word: WakeWordDetector,
        vad: VADProcessor,
        stt: STTEngine,
        tts: TTSRouter,
        language: str = "hu",
        mock_mode: bool = False,
    ) -> None:
        self._wake_word = wake_word
        self._vad = vad
        self._stt = stt
        self._tts = tts
        self._language = language
        self._mock_mode = mock_mode

        self._state = PipelineState.IDLE
        self._running = False
        self._invoke_callback: Optional[InvokeCallback] = None
        self._current_user_id: str = "default"

        # Audio state
        self._audio_buffer: list[bytes] = []
        self._last_speech_time: float = 0
        self._playback_task: Optional[asyncio.Task] = None

    @property
    def state(self) -> PipelineState:
        return self._state

    def set_invoke_callback(self, callback: InvokeCallback) -> None:
        """Set the callback to invoke when a user query is ready."""
        self._invoke_callback = callback

    async def start(self) -> None:
        """Start the voice pipeline.

        Starts wake word detection and registers the pipeline trigger.
        """
        self._running = True

        # Register wake word callback
        self._wake_word.on_wake_word(self._on_wake_word)

        # Start wake word detection
        await self._wake_word.start()

        self._state = PipelineState.IDLE
        logger.info("Voice pipeline started (language=%s)", self._language)

    async def stop(self) -> None:
        """Stop the voice pipeline."""
        self._running = False
        self._state = PipelineState.IDLE

        if self._playback_task:
            self._playback_task.cancel()

        await self._wake_word.stop()
        logger.info("Voice pipeline stopped")

    def _on_wake_word(self) -> None:
        """Called when wake word is detected — starts a new interaction turn."""
        if self._state == PipelineState.SPEAKING:
            # Barge-in: user interrupted the assistant
            logger.info("Barge-in detected — interrupting assistant")
            if self._playback_task:
                self._playback_task.cancel()

        if self._state not in (PipelineState.IDLE, PipelineState.SPEAKING):
            logger.debug("Wake word detected but pipeline busy (state=%s)", self._state.value)
            return

        logger.info("Wake word detected — starting capture")
        asyncio.create_task(self._run_turn())

    async def _run_turn(self) -> None:
        """Execute a complete voice interaction turn.

        Wake word -> capture audio -> STT -> relay invoke -> TTS -> playback.
        """
        try:
            # Phase 1: Capture audio until user stops speaking
            self._state = PipelineState.LISTENING
            self._audio_buffer.clear()
            self._vad.reset()

            audio_data = await self._capture_utterance()
            if not audio_data:
                logger.info("No speech captured — returning to idle")
                self._state = PipelineState.IDLE
                return

            # Phase 2: Transcribe
            self._state = PipelineState.TRANSCRIBING
            query = await self._stt.transcribe(audio_data)

            if not query.strip():
                logger.info("Empty transcription — returning to idle")
                self._state = PipelineState.IDLE
                return

            logger.info("Transcribed: %s", query[:100])

            # Phase 3: Send to relay and wait for response
            self._state = PipelineState.WAITING
            response_text = await self._invoke_assistant(query)

            if not response_text:
                logger.warning("No response from assistant")
                self._state = PipelineState.IDLE
                return

            logger.info("Assistant response: %s", response_text[:100])

            # Phase 4: TTS and playback
            self._state = PipelineState.SPEAKING
            audio_response = await self._tts.synthesize(
                response_text,
                language=self._language,
            )

            await self._play_audio(audio_response)

        except asyncio.CancelledError:
            logger.info("Turn cancelled")
        except Exception as exc:
            logger.error("Pipeline error: %s", exc, exc_info=True)
            self._state = PipelineState.ERROR
        finally:
            if self._state != PipelineState.IDLE:
                self._state = PipelineState.IDLE

    async def _capture_utterance(self) -> bytes:
        """Capture audio until the user stops speaking.

        Uses VAD to detect speech boundaries. Returns the complete
        audio buffer or empty bytes if no speech was detected.
        """
        if self._mock_mode:
            # In mock mode, simulate a brief utterance
            await asyncio.sleep(2.0)
            return b"\x00" * (SAMPLE_RATE * 2 * 2)  # 2 seconds of silence

        start_time = time.monotonic()
        speech_started = False
        silence_start: Optional[float] = None

        # TODO: Replace with actual pyaudio stream when dependency is available
        # For now, this is the pipeline structure with mock audio
        while self._running:
            elapsed = time.monotonic() - start_time
            if elapsed > MAX_RECORDING_S:
                logger.warning("Recording timeout after %.0fs", elapsed)
                break

            # In production, read from audio stream:
            # chunk = audio_stream.read(FRAME_SIZE // 2)  # FRAME_SIZE is in bytes
            chunk = b"\x00" * FRAME_SIZE  # Mock silent frame
            await asyncio.sleep(0.03)  # 30ms frame

            is_speech = self._vad.process_chunk(chunk)

            if is_speech:
                if not speech_started:
                    self._state = PipelineState.CAPTURING
                    speech_started = True
                    logger.debug("Speech start detected")
                silence_start = None
                self._audio_buffer.append(chunk)
                self._last_speech_time = time.monotonic()

            elif speech_started:
                self._audio_buffer.append(chunk)
                if silence_start is None:
                    silence_start = time.monotonic()
                elif time.monotonic() - silence_start > END_OF_TURN_SILENCE_S:
                    logger.debug("End of turn detected (%.1fs silence)", END_OF_TURN_SILENCE_S)
                    break

            # Break early in mock mode
            if self._mock_mode and elapsed > 0.5:
                break

        if not self._audio_buffer:
            return b""

        return b"".join(self._audio_buffer)

    async def _invoke_assistant(self, query: str) -> Optional[str]:
        """Send the transcribed query to the Synork relay."""
        if not self._invoke_callback:
            logger.warning("No invoke callback set — cannot send query to relay")
            return None

        try:
            response = await asyncio.wait_for(
                self._invoke_callback(query, self._language, self._current_user_id),
                timeout=15.0,
            )
            return response
        except asyncio.TimeoutError:
            logger.warning("Assistant invoke timed out")
            return None

    async def _play_audio(self, audio_data: bytes) -> None:
        """Play audio response through the speaker.

        In production, uses pyaudio or ALSA for playback.
        In mock mode, simulates playback duration.
        """
        if self._mock_mode or not audio_data:
            duration = len(audio_data) / (22050 * 2) if audio_data else 0
            logger.info("MOCK: Playing %.1fs of audio", duration)
            await asyncio.sleep(min(duration, 5.0))
            return

        # TODO: Implement actual audio playback via pyaudio/ALSA
        # This is the structure:
        # stream = pyaudio.open(format=..., channels=1, rate=22050, output=True)
        # stream.write(audio_data[44:])  # Skip WAV header
        # stream.close()
        logger.info("Audio playback: %d bytes", len(audio_data))

    # -- Status ------------------------------------------------------------- #

    def is_user_speaking(self) -> bool:
        return self._state in (PipelineState.LISTENING, PipelineState.CAPTURING)

    def is_assistant_speaking(self) -> bool:
        return self._state == PipelineState.SPEAKING
