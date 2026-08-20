"""Tests for cw_decoder.py.

Author: James Sawyer / JSLabs - https://www.jamessawyer.co.uk/ | https://labs.jamessawyer.co.uk/
"""

from __future__ import annotations

import numpy as np

import cw_decoder

CHAR_TO_MORSE = {
    "S": "...",
    "O": "---",
}


def _tone(seconds: float, frequency_hz: float, samplerate: int) -> np.ndarray:
    samples = int(seconds * samplerate)
    t = np.arange(samples, dtype=np.float64) / samplerate
    audio = np.sin(2 * np.pi * frequency_hz * t)
    fade_samples = min(samples // 2, int(0.005 * samplerate))
    if fade_samples:
        fade = np.linspace(0.0, 1.0, fade_samples)
        audio[:fade_samples] *= fade
        audio[-fade_samples:] *= fade[::-1]
    return audio.astype(np.float32)


def _silence(seconds: float, samplerate: int) -> np.ndarray:
    return np.zeros(int(seconds * samplerate), dtype=np.float32)


def _morse_audio(
    text: str, dot: float, frequency_hz: float, samplerate: int
) -> np.ndarray:
    chunks = [_silence(0.2, samplerate)]
    for word_index, word in enumerate(text.split()):
        if word_index:
            chunks.append(_silence(7 * dot, samplerate))
        for char_index, char in enumerate(word):
            if char_index:
                chunks.append(_silence(3 * dot, samplerate))
            for element_index, element in enumerate(CHAR_TO_MORSE[char]):
                if element_index:
                    chunks.append(_silence(dot, samplerate))
                chunks.append(
                    _tone(dot if element == "." else 3 * dot, frequency_hz, samplerate)
                )
    chunks.append(_silence(0.2, samplerate))
    return np.concatenate(chunks)


def test_analyze_decodes_synthetic_cw() -> None:
    samplerate = 8000
    audio = _morse_audio("SOS SOS", dot=0.08, frequency_hz=600, samplerate=samplerate)

    result = cw_decoder.analyze(audio, samplerate)

    assert result.is_cw
    assert result.text == "SOS SOS"
    assert result.tone_hz is not None
    assert abs(result.tone_hz - 600) < 5
    assert result.wpm is not None
    assert 10 <= result.wpm <= 20


def test_analyze_rejects_continuous_tone() -> None:
    samplerate = 8000
    audio = _tone(4.0, frequency_hz=600, samplerate=samplerate)

    result = cw_decoder.analyze(audio, samplerate)

    assert not result.is_cw
    assert result.text == ""


def test_morse_to_text_marks_unknown_patterns() -> None:
    assert cw_decoder.morse_to_text("... --- ... / ......") == "SOS ?"
