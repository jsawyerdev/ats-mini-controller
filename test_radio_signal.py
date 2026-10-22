"""Deterministic tests for noise-relative station detection."""

from __future__ import annotations

import numpy as np

from radio_bridge import FM_CHANNEL_BW_HZ, IQ_SAMPLE_RATE, _estimate_signal


def _noise(rng: np.random.Generator, samples: int, amplitude: float = 0.01) -> np.ndarray:
    return amplitude * (
        rng.standard_normal(samples) + 1j * rng.standard_normal(samples)
    ).astype(np.complex64)


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
