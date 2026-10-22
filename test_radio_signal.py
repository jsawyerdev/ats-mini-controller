"""Deterministic tests for noise-relative station detection."""

from __future__ import annotations

import threading

import numpy as np

import radio_bridge
from radio_bridge import (
    AM_AUDIO_BW_HZ,
    AUDIO_RATE,
    FM_CHANNEL_BW_HZ,
    IQ_SAMPLE_RATE,
    _AmDemodState,
    _audio_level_dbfs,
    _demod_am,
    _estimate_signal,
    audio_content_db,
    carrier_offset_hz,
    spectrum_dbfs,
)


def _noise(
    rng: np.random.Generator, samples: int, amplitude: float = 0.01
) -> np.ndarray:
    return amplitude * (
        rng.standard_normal(samples) + 1j * rng.standard_normal(samples)
    ).astype(np.complex64)


def test_audio_level_distinguishes_silence_from_demodulated_audio() -> None:
    assert _audio_level_dbfs(np.zeros(1024, dtype=np.float32)) == -120
    tone = (
        np.sin(np.linspace(0, 20 * np.pi, 1024, endpoint=False)).astype(np.float32)
        * 0.1
    )
    assert -24 <= _audio_level_dbfs(tone) <= -22


def test_audio_content_rejects_stationary_noise_and_accepts_programme_structure() -> (
    None
):
    rng = np.random.default_rng(20261022)
    samples = AUDIO_RATE * 2
    time_s = np.arange(samples) / AUDIO_RATE
    noise = rng.standard_normal(samples).astype(np.float32)
    programme = (
        np.sin(2 * np.pi * 220 * time_s)
        + 0.5 * np.sin(2 * np.pi * 880 * time_s)
        + 0.25 * np.sin(2 * np.pi * 1760 * time_s)
    ).astype(np.float32)

    assert audio_content_db(np.zeros(samples, dtype=np.float32), AM_AUDIO_BW_HZ) == 0
    assert audio_content_db(noise, AM_AUDIO_BW_HZ) < 3
    assert audio_content_db(programme, AM_AUDIO_BW_HZ) >= 3


def test_failed_direct_sampling_switch_reopens_device(monkeypatch) -> None:
    class BrokenSdr:
        def set_direct_sampling(self, _: int) -> None:
            raise OSError("mode switch failed")

        def close(self) -> None:
            pass

    class ReopenedSdr:
        sample_rate = 0
        gain = 0.0
        center_freq = 0

    reopened = ReopenedSdr()
    monkeypatch.setattr(radio_bridge, "RtlSdr", lambda _: reopened)
    bridge = radio_bridge.RtlSdrBridge.__new__(radio_bridge.RtlSdrBridge)
    bridge._device_index = 0
    bridge._sdr = BrokenSdr()
    bridge._direct_sampling_active = True
    bridge._gain_db = radio_bridge.DEFAULT_GAIN_DB
    bridge._ppm = 0
    bridge._state_lock = threading.Lock()

    bridge._apply_frequency(98_800_000)

    assert bridge._sdr is reopened
    assert reopened.sample_rate == IQ_SAMPLE_RATE
    assert reopened.center_freq == 98_800_000
    assert bridge._direct_sampling_active is False


def test_stateful_am_agc_is_bounded_and_tracks_programme() -> None:
    time_s = np.arange(32_768) / IQ_SAMPLE_RATE
    carrier = (1 + 0.2 * np.sin(2 * np.pi * 1_000 * time_s)).astype(np.complex64)
    state = _AmDemodState()

    first, state = _demod_am(carrier, state)
    second, state = _demod_am(carrier, state)

    assert np.max(np.abs(second)) <= 1
    assert _audio_level_dbfs(second) > -30
    assert state.level > 0
    assert abs(_audio_level_dbfs(first) - _audio_level_dbfs(second)) <= 6


def test_spectrum_is_bounded_for_browser_transport() -> None:
    row = spectrum_dbfs(np.ones(4096, dtype=np.complex64), bins=512)
    assert len(row) == 512
    assert all(-120 <= value <= 0 for value in row)


def test_carrier_offset_estimator_finds_known_reference() -> None:
    samples = 32_768
    expected_hz = 100_000
    actual_hz = 100_250
    time_s = np.arange(samples) / IQ_SAMPLE_RATE
    carrier = np.exp(2j * np.pi * actual_hz * time_s).astype(np.complex64)

    measured_hz = carrier_offset_hz(carrier, expected_hz)

    assert abs(measured_hz - actual_hz) <= IQ_SAMPLE_RATE / samples


def test_broadband_noise_is_not_a_station() -> None:
    rng = np.random.default_rng(20261022)

    signal_db, _ = _estimate_signal(_noise(rng, 32_768), FM_CHANNEL_BW_HZ)

    assert signal_db < 3


def test_center_dc_offset_is_not_a_station() -> None:
    rng = np.random.default_rng(20261022)
    iq = _noise(rng, 32_768) + (0.2 + 0.2j)

    signal_db, _ = _estimate_signal(iq, FM_CHANNEL_BW_HZ)

    assert signal_db < 3


def test_in_channel_carrier_stands_above_noise() -> None:
    rng = np.random.default_rng(20261022)
    samples = 32_768
    time_s = np.arange(samples) / IQ_SAMPLE_RATE
    carrier = 0.08 * np.exp(2j * np.pi * 40_000 * time_s)
    iq = _noise(rng, samples) + carrier

    signal_db, noise_dbfs = _estimate_signal(iq, FM_CHANNEL_BW_HZ)

    assert signal_db >= 6
    assert noise_dbfs < -30


def test_out_of_channel_carrier_does_not_win() -> None:
    rng = np.random.default_rng(20261022)
    samples = 32_768
    time_s = np.arange(samples) / IQ_SAMPLE_RATE
    adjacent_carrier = 0.08 * np.exp(2j * np.pi * 300_000 * time_s)
    iq = _noise(rng, samples) + adjacent_carrier

    signal_db, _ = _estimate_signal(iq, FM_CHANNEL_BW_HZ)

    assert signal_db < 3
