"""Capture audio from a USB sound card and decode Morse/CW from it.

Pipeline: record -> find the CW tone frequency (unknown in advance, depends
on receiver BFO offset) -> extract its on/off envelope -> convert envelope
timing to dot/dash/gap events -> assemble into Morse code -> text.

No established PyPI package does audio-to-Morse decoding (the well-known
tools - fldigi, CwGet - are standalone GUI apps, not embeddable libraries),
so this is a small local implementation on top of numpy/scipy, which are
already dependencies.

Author: James Sawyer / JSLabs - https://labs.jamessawyer.co.uk/monitoring/
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np
import sounddevice as sd
from scipy.signal import butter, hilbert, sosfiltfilt

logger = logging.getLogger(__name__)

_MORSE_TO_CHAR = {
    ".-": "A",
    "-...": "B",
    "-.-.": "C",
    "-..": "D",
    ".": "E",
    "..-.": "F",
    "--.": "G",
    "....": "H",
    "..": "I",
    ".---": "J",
    "-.-": "K",
    ".-..": "L",
    "--": "M",
    "-.": "N",
    "---": "O",
    ".--.": "P",
    "--.-": "Q",
    ".-.": "R",
    "...": "S",
    "-": "T",
    "..-": "U",
    "...-": "V",
    ".--": "W",
    "-..-": "X",
    "-.--": "Y",
    "--..": "Z",
    "-----": "0",
    ".----": "1",
    "..---": "2",
    "...--": "3",
    "....-": "4",
    ".....": "5",
    "-....": "6",
    "--...": "7",
    "---..": "8",
    "----.": "9",
    ".-.-.-": ".",
    "--..--": ",",
    "..--..": "?",
    "-.-.--": "!",
    "-....-": "-",
    "-..-.": "/",
    ".--.-.": "@",
    "---...": ":",
    "-.-.-.": ";",
    ".-...": "&",
}

# CW tone (sidetone) pitch is typically 400-1000 Hz; search this range for
# the dominant carrier rather than assuming a fixed pitch, since it depends
# on the receiver's BFO offset from the actual signal frequency.
_TONE_SEARCH_HZ = (300, 1200)


@dataclass(frozen=True)
class CwResult:
    is_cw: bool
    tone_hz: float | None
    morse: str
    text: str
    wpm: float | None
    confidence: float  # 0-1, combines timing regularity and decoded-symbol quality


def record_audio(device: int, seconds: float, samplerate: int = 48000) -> np.ndarray:
    audio = sd.rec(
        int(seconds * samplerate), samplerate=samplerate, channels=1, device=device
    )
    sd.wait()
    return audio[:, 0]


def _find_tone_hz(audio: np.ndarray, samplerate: int) -> tuple[float, float] | None:
    """Return (frequency, magnitude) of the strongest peak in the CW search
    band, or None if there's essentially no energy there."""
    spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
    freqs = np.fft.rfftfreq(len(audio), 1 / samplerate)
    band = (freqs >= _TONE_SEARCH_HZ[0]) & (freqs <= _TONE_SEARCH_HZ[1])
    if not band.any():
        return None
    band_spectrum = spectrum[band]
    band_freqs = freqs[band]
    peak_idx = int(np.argmax(band_spectrum))
    peak_mag = float(band_spectrum[peak_idx])
    noise_floor = float(np.median(band_spectrum)) + 1e-9
    if peak_mag < noise_floor * 2.5:  # no distinct tone above the noise
        return None
    return float(band_freqs[peak_idx]), peak_mag / noise_floor


def _tone_envelope(audio: np.ndarray, samplerate: int, tone_hz: float) -> np.ndarray:
    """Bandpass around tone_hz, then Hilbert envelope, to isolate the CW
    carrier's on/off keying from band noise and other signals."""
    sos = butter(
        4, [tone_hz - 60, tone_hz + 60], btype="bandpass", fs=samplerate, output="sos"
    )
    filtered = sosfiltfilt(sos, audio)
    return np.abs(hilbert(filtered))


def _envelope_to_events(
    envelope: np.ndarray, samplerate: int
) -> list[tuple[bool, float]]:
    """Threshold the envelope into a sequence of (is_mark, duration_seconds)."""
    threshold = (np.percentile(envelope, 10) + np.percentile(envelope, 90)) / 2
    is_mark = envelope > threshold
    events: list[tuple[bool, float]] = []
    run_start = 0
    for i in range(1, len(is_mark) + 1):
        if i == len(is_mark) or is_mark[i] != is_mark[run_start]:
            duration = (i - run_start) / samplerate
            events.append((bool(is_mark[run_start]), duration))
            run_start = i
    return events


def _events_to_morse(
    events: list[tuple[bool, float]],
) -> tuple[str, float | None, float]:
    """Classify durations relative to the estimated dot length (the unit).

    Standard CW timing: dash = 3x dot, intra-char gap = 1x dot, inter-char
    gap = 3x dot, word gap = 7x dot. The dot length itself isn't known in
    advance (depends on sending speed), so it's estimated as the shortest
    common mark duration in this clip.
    """
    marks = [d for is_mark, d in events if is_mark and d > 0.02]
    if len(marks) < 3:
        return "", None, 0.0

    unit = float(np.percentile(marks, 20))  # robust estimate of the dot length
    if unit <= 0:
        return "", None, 0.0

    morse_chars: list[str] = []
    current = ""
    for is_mark, duration in events:
        units = duration / unit
        if is_mark:
            if duration < 0.02:
                continue
            current += "-" if units >= 2 else "."
        else:
            if units >= 5:  # word gap
                if current:
                    morse_chars.append(current)
                    current = ""
                morse_chars.append("/")
            elif units >= 2:  # inter-character gap
                if current:
                    morse_chars.append(current)
                    current = ""
    if current:
        morse_chars.append(current)

    morse = " ".join(morse_chars)
    wpm = 1.2 / unit if unit > 0 else None  # PARIS-standard dot-length-to-WPM

    # Confidence: how cleanly the mark durations cluster into two groups
    # (dot/dash) rather than a smear, using the coefficient of variation of
    # the shorter cluster as a proxy for how "regular" the keying is.
    short_marks = [m for m in marks if m < unit * 2]
    confidence = 0.0
    if len(short_marks) >= 2:
        cv = np.std(short_marks) / (np.mean(short_marks) + 1e-9)
        confidence = max(0.0, min(1.0, 1.0 - cv))

    return morse, wpm, confidence


def morse_to_text(morse: str) -> str:
    words = morse.strip().split(" / ")
    return " ".join(
        "".join(_MORSE_TO_CHAR.get(code, "?") for code in word.split())
        for word in words
    )


def _morse_tokens(morse: str) -> list[str]:
    return [token for token in morse.split() if token != "/"]


def analyze(audio: np.ndarray, samplerate: int) -> CwResult:
    tone = _find_tone_hz(audio, samplerate)
    if tone is None:
        return CwResult(False, None, "", "", None, 0.0)
    tone_hz, _magnitude = tone

    envelope = _tone_envelope(audio, samplerate, tone_hz)
    events = _envelope_to_events(envelope, samplerate)
    morse, wpm, confidence = _events_to_morse(events)
    text = morse_to_text(morse) if morse else ""
    tokens = _morse_tokens(morse)
    known_tokens = sum(1 for token in tokens if token in _MORSE_TO_CHAR)
    symbol_quality = known_tokens / len(tokens) if tokens else 0.0
    confidence *= symbol_quality
    decoded_chars = [char for char in text if char not in {" ", "?"}]
    wpm_is_plausible = wpm is not None and 5.0 <= wpm <= 45.0
    if not wpm_is_plausible:
        confidence = min(confidence, 0.49)
    is_cw = (
        confidence > 0.5
        and symbol_quality >= 0.5
        and len(decoded_chars) >= 2
        and wpm_is_plausible
    )
    return CwResult(is_cw, tone_hz, morse, text, wpm, confidence)


def listen(device: int, seconds: float = 4.0, samplerate: int = 48000) -> CwResult:
    audio = record_audio(device, seconds, samplerate)
    return analyze(audio, samplerate)
