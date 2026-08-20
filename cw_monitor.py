"""Background CW monitor: cycles candidate frequencies looking for Morse,
locks onto whichever one has it, decodes continuously while it's present,
and releases back to scanning once the signal stops or fades out.

Continuous decoding works by re-running the batch decoder (cw_decoder.py)
over a growing audio buffer rather than decoding fixed independent chunks: a
chunk boundary landing mid-character would corrupt it, whereas re-analyzing
the whole buffer each pass stays correct as more audio arrives and
dot-length/timing estimates improve. The buffer isn't unbounded, though: past
MAX_BUFFER_S it's flushed to the log as a completed entry and a fresh buffer
starts immediately (still locked on the same frequency) - a long-running
transmission comes out as consecutive log entries rather than one that grows
forever.

Author: James Sawyer / JSLabs - https://labs.jamessawyer.co.uk/monitoring/
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field

import numpy as np
import sounddevice as sd

import cw_decoder
from radio_bridge import AtsMiniBridge

logger = logging.getLogger(__name__)

CHUNK_S = 1.5
MAX_BUFFER_S = 40.0  # roll a log entry and start fresh past this, even mid-signal
SILENCE_CHUNKS_TO_RELEASE = 3  # ~4.5s of no tone at all before giving up the lock
# Long enough to almost certainly overlap real keying rather than landing in
# a gap between characters/words (or, for the NCDXF beacon network, between
# one station's 10s slot and the next).
PROBE_S = 5.0
SAMPLERATE = 48000


@dataclass
class CwTarget:
    freq_hz: int
    firmware_band: str
    mode: str = "USB"


@dataclass
class LogEntry:
    freq_hz: int
    started_at: float
    ended_at: float
    text: str
    morse: str
    wpm: float | None


@dataclass
class MonitorState:
    running: bool = False
    mode: str = "idle"  # idle | scanning | locked
    current_freq_hz: int | None = None
    in_progress_text: str = ""
    in_progress_started_at: float | None = None
    log: list[LogEntry] = field(default_factory=list)
    version: int = 0  # bumped on every change, for the SSE loop to diff against


class CwMonitor:
    def __init__(
        self,
        bridge: AtsMiniBridge,
        audio_device: int,
        candidates: list[CwTarget],
        manual_override: threading.Event,
    ):
        self._bridge = bridge
        self._device = audio_device
        self._candidates = candidates
        # Same signal /api/tune sets: a manual click should always win over
        # any background automation (this monitor's continuous tuning, or
        # the RSSI scan), not just get raced against it.
        self._manual_override = manual_override
        self._state = MonitorState()
        self._state_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def state(self) -> MonitorState:
        with self._state_lock:
            return MonitorState(
                running=self._state.running,
                mode=self._state.mode,
                current_freq_hz=self._state.current_freq_hz,
                in_progress_text=self._state.in_progress_text,
                in_progress_started_at=self._state.in_progress_started_at,
                log=list(self._state.log),
                version=self._state.version,
            )

    def _update(self, **kwargs) -> None:
        with self._state_lock:
            for k, v in kwargs.items():
                setattr(self._state, k, v)
            self._state.version += 1

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._manual_override.clear()
        self._update(running=True, mode="scanning", log=[])
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _should_stop(self) -> bool:
        return self._stop_event.is_set() or self._manual_override.is_set()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            # Worst case the thread is mid band-search (select_band's own
            # budget is up to ~30 presses x 1.5s); wait comfortably past
            # that rather than declaring "stopped" while it's still tuning.
            self._thread.join(timeout=50)
            if self._thread.is_alive():
                logger.warning("CW monitor thread did not stop within 50s")
                return  # don't report idle while it's still actually running
        self._update(
            running=False, mode="idle", current_freq_hz=None, in_progress_text=""
        )

    def _record_chunk(self) -> np.ndarray:
        audio = sd.rec(
            int(CHUNK_S * SAMPLERATE),
            samplerate=SAMPLERATE,
            channels=1,
            device=self._device,
        )
        sd.wait()
        return audio[:, 0]

    def _finalize(
        self, freq_hz: int, started_at: float, result: cw_decoder.CwResult
    ) -> None:
        if not result.is_cw or not result.text:
            return
        entry = LogEntry(
            freq_hz=freq_hz,
            started_at=started_at,
            ended_at=time.time(),
            text=result.text,
            morse=result.morse,
            wpm=result.wpm,
        )
        with self._state_lock:
            self._state.log.append(entry)
            if len(self._state.log) > 200:
                self._state.log = self._state.log[-200:]
        self._update(in_progress_text="", in_progress_started_at=None)

    def _run(self) -> None:
        candidate_idx = 0
        try:
            while not self._should_stop():
                target = self._candidates[candidate_idx % len(self._candidates)]
                candidate_idx += 1
                self._update(mode="scanning", current_freq_hz=target.freq_hz)

                if not self._bridge.tune_cw(
                    target.freq_hz, target.firmware_band, target.mode
                ):
                    time.sleep(0.5)
                    continue
                time.sleep(0.5)
                if self._should_stop():
                    break

                probe_audio = sd.rec(
                    int(PROBE_S * SAMPLERATE),
                    samplerate=SAMPLERATE,
                    channels=1,
                    device=self._device,
                )
                sd.wait()
                probe_audio = probe_audio[:, 0]
                probe_result = cw_decoder.analyze(probe_audio, SAMPLERATE)
                if not probe_result.is_cw:
                    continue  # nothing CW-shaped here, move to the next candidate

                # Something CW-shaped is present: lock on and decode
                # continuously until it goes quiet or fades below detection.
                self._lock_and_decode(target, probe_audio)
        finally:
            self._update(running=False, mode="idle", current_freq_hz=None)

    def _lock_and_decode(self, target: CwTarget, seed_audio: np.ndarray) -> None:
        buffer = seed_audio.copy()
        started_at = time.time()
        silent_chunks = 0
        self._update(
            mode="locked",
            current_freq_hz=target.freq_hz,
            in_progress_started_at=started_at,
        )

        while not self._should_stop():
            if len(buffer) > 0:
                result = cw_decoder.analyze(buffer, SAMPLERATE)
                self._update(in_progress_text=result.text if result.is_cw else "")

            chunk = self._record_chunk()
            if self._should_stop():
                break
            chunk_result = cw_decoder.analyze(chunk, SAMPLERATE)
            if chunk_result.tone_hz is None:
                silent_chunks += 1
            else:
                silent_chunks = 0
            buffer = np.concatenate([buffer, chunk])

            if silent_chunks >= SILENCE_CHUNKS_TO_RELEASE:
                self._finalize(
                    target.freq_hz, started_at, cw_decoder.analyze(buffer, SAMPLERATE)
                )
                return

            if len(buffer) / SAMPLERATE >= MAX_BUFFER_S:
                self._finalize(
                    target.freq_hz, started_at, cw_decoder.analyze(buffer, SAMPLERATE)
                )
                buffer = np.array([], dtype=buffer.dtype)
                started_at = time.time()
                self._update(in_progress_started_at=started_at)

        # Stopped by the caller (not a silence timeout): flush whatever we had.
        self._finalize(
            target.freq_hz, started_at, cw_decoder.analyze(buffer, SAMPLERATE)
        )
