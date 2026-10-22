"""Deterministic tests for noise-relative station detection."""

from __future__ import annotations

import numpy as np

import radio_bridge
from radio_bridge import (
    AUDIO_RATE,
    FM_AUDIO_BW_HZ,
    FM_CHANNEL_BW_HZ,
    IQ_SAMPLE_RATE,
    _audio_level_dbfs,
    _audio_quality_db,
    _estimate_signal,
)


def _noise(rng: np.random.Generator, samples: int, amplitude: float = 0.01) -> np.ndarray:
    return amplitude * (
        rng.standard_normal(samples) + 1j * rng.standard_normal(samples)
    ).astype(np.complex64)


def test_audio_level_distinguishes_silence_from_demodulated_audio() -> None:
    assert _audio_level_dbfs(np.zeros(1024, dtype=np.float32)) == -120
    tone = np.sin(np.linspace(0, 20 * np.pi, 1024, endpoint=False)).astype(np.float32) * 0.1
    assert -24 <= _audio_level_dbfs(tone) <= -22


def test_audio_quality_rejects_noise_but_accepts_structured_audio() -> None:
    rng = np.random.default_rng(20261022)
    samples = 4096
    time_s = np.arange(samples) / AUDIO_RATE
    noise = rng.standard_normal(samples).astype(np.float32)
    programme = (
        np.sin(2 * np.pi * 220 * time_s)
        + 0.5 * np.sin(2 * np.pi * 880 * time_s)
        + 0.25 * np.sin(2 * np.pi * 1760 * time_s)
    ).astype(np.float32)

    assert _audio_quality_db(np.zeros(samples, dtype=np.float32), FM_AUDIO_BW_HZ) == 0
    assert _audio_quality_db(noise, FM_AUDIO_BW_HZ) < 6
    assert _audio_quality_db(programme, FM_AUDIO_BW_HZ) >= 6


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

    bridge._apply_frequency(98_800_000)

    assert bridge._sdr is reopened
    assert reopened.sample_rate == IQ_SAMPLE_RATE
    assert reopened.center_freq == 98_800_000
    assert bridge._direct_sampling_active is False


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
