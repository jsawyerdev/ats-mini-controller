"""RTL-SDR USB dongle bridge: tunes an RTL2832U-based SDR by directly setting
its center frequency and demodulates AM/FM/CW in software from the raw IQ
stream. There is no firmware, no serial protocol, and no fixed band list to
navigate - a retune is a single register write, unlike the ATS Mini's
multi-second band-search this replaced.

FM broadcast (88-108 MHz) is within the R820T tuner's normal PLL range
(~24 MHz-1.7 GHz). AM/mediumwave (530-1700 kHz) and shortwave (1.6-30 MHz)
are both below that floor, so those bands are received via the RTL2832U's
direct-sampling ADC input instead of the normal tuner path. Direct sampling
is a standard RTL2832U capability (confirmed working on this dongle: it
enables and tunes without error), but whether a given board's PCB actually
routes that pin to a usable antenna path with adequate sensitivity varies -
AM/SW reception quality on this specific dongle is unverified until tested
against a real antenna and a known signal.

Author: James Sawyer / JSLabs - https://www.jamessawyer.co.uk/ | https://labs.jamessawyer.co.uk/
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections import deque
from dataclasses import dataclass

import numpy as np
from rtlsdr import RtlSdr
from scipy.signal import butter, sosfilt, sosfilt_zi

logger = logging.getLogger(__name__)

IQ_SAMPLE_RATE = 960_000  # Hz; well within the RTL2832U's reliable high-rate range
AUDIO_RATE = 48_000  # Hz; matches cw_decoder/cw_monitor's expected samplerate
_DECIMATION = IQ_SAMPLE_RATE // AUDIO_RATE  # 20
CHUNK_IQ_SAMPLES = 32_768  # ~34 ms per read

# The R820T's PLL can't lock below roughly this; anything under it needs
# direct sampling instead of the normal tuner path.
DIRECT_SAMPLING_THRESHOLD_HZ = 28_800_000
DIRECT_SAMPLING_MODE = 2  # Q-branch; the commonly-wired input on most boards

# One of the R820T's supported gain steps (confirmed via `rtl_test -t` on
# this dongle: 0.0 ... 49.6 dB in 29 steps). Fixed rather than "auto" - see
# the comment in RtlSdrBridge.__init__.
DEFAULT_GAIN_DB = 40.2

CW_TONE_LOWPASS_HZ = 2_000  # keeps cw_decoder's 300-1200Hz search band, rejects the rest
AM_AUDIO_BW_HZ = 5_000
FM_AUDIO_BW_HZ = 15_000

# Precomputed once rather than rebuilt every chunk (butter() isn't free),
# and shared with the persistent-zi handling in _decimate_lowpass below.
_AM_SOS = butter(4, AM_AUDIO_BW_HZ, btype="low", fs=IQ_SAMPLE_RATE, output="sos")
_FM_SOS = butter(4, FM_AUDIO_BW_HZ, btype="low", fs=IQ_SAMPLE_RATE, output="sos")
_CW_SOS = butter(4, CW_TONE_LOWPASS_HZ, btype="low", fs=IQ_SAMPLE_RATE, output="sos")

# RF channel bandwidths used to compare the tuned channel with nearby noise.
FM_CHANNEL_BW_HZ = 200_000  # broadcast FM: +/-75kHz deviation plus guard
AM_CHANNEL_BW_HZ = 10_000  # AM/SW broadcast: roughly double the audio bandwidth
CW_CHANNEL_BW_HZ = 4_000  # narrow - just enough to contain the beat tone

_AUDIO_HISTORY_S = 60.0  # ring buffer depth for capture_audio()


def find_rtlsdr_device() -> bool:
    """True if an RTL-SDR dongle is currently plugged in and openable."""
    try:
        sdr = RtlSdr()
    except Exception:  # noqa: BLE001 -- USB backends raise several unrelated exception types
        return False
    sdr.close()
    return True


@dataclass(frozen=True)
class RadioStatus:
    frequency_hz: int
    band: str  # SW | AM | FM - coarse tag, also selects the demod mode
    mode: str  # AM | FM | CW - demod actually in use
    signal_db: int  # in-channel power above the nearby RF noise floor
    noise_dbfs: int  # nearby RF noise floor, relative dBFS (diagnostic only)
    audio_dbfs: int  # demodulated audio RMS; -120 means digital silence
    volume: int  # software playback gain, 0-100
    seq: int


class _AudioBus:
    """Ring buffer of demodulated audio, shared by every consumer (the CW
    decoder's blocking captures, the signal/status readout, and any number of
    live websocket listeners). Unlike the old hardware passthrough, none of
    these need exclusive access - it's all just reads of the same stream."""

    def __init__(self, rate: int = AUDIO_RATE, history_s: float = _AUDIO_HISTORY_S) -> None:
        self._rate = rate
        self._max_samples = int(history_s * rate)
        self._chunks: deque[np.ndarray] = deque()
        self._buffered_samples = 0
        self._total_produced = 0
        self._cond = threading.Condition()

    def publish(self, chunk: np.ndarray) -> None:
        with self._cond:
            self._chunks.append(chunk)
            self._buffered_samples += len(chunk)
            self._total_produced += len(chunk)
            while self._buffered_samples - len(self._chunks[0]) > self._max_samples:
                self._buffered_samples -= len(self._chunks.popleft())
            self._cond.notify_all()

    def capture(self, seconds: float) -> np.ndarray:
        """Block until `seconds` of audio produced after this call has
        accumulated, then return it - a drop-in replacement for the old
        sd.rec()-based blocking capture."""
        n_needed = int(seconds * self._rate)
        deadline = time.monotonic() + seconds + 5.0  # generous safety margin
        with self._cond:
            start_total = self._total_produced
            while self._total_produced - start_total < n_needed:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not self._cond.wait(timeout=remaining):
                    break
            produced = self._total_produced - start_total
            if produced <= 0:
                return np.zeros(0, dtype=np.float32)
            audio = np.concatenate(list(self._chunks))
        n = min(produced, n_needed, len(audio))
        return audio[-n:]


def _trimmed_mean(values: np.ndarray, keep_fraction: float = 0.9) -> float:
    """Mean without the strongest bins, which may contain adjacent stations."""
    if values.size == 0:
        return 1e-12
    keep = max(1, int(values.size * keep_fraction))
    return float(np.mean(np.partition(values, keep - 1)[:keep]))


def _estimate_signal(iq: np.ndarray, channel_bw_hz: float) -> tuple[int, int]:
    """Return in-channel power above local RF noise and the noise floor.

    Absolute IQ power rises with gain, antenna noise, and interference, so it
    cannot establish that a station is present. This periodogram compares the
    tuned channel with nearby spectrum captured at the same instant. Removing
    the IQ mean and a narrow centre notch prevents the RTL2832U DC spike from
    being mistaken for a carrier.
    """
    if len(iq) < 32:
        return 0, -120

    centered = iq - np.mean(iq)
    window = np.hanning(len(centered))
    spectrum = np.fft.fftshift(np.fft.fft(centered * window))
    power = np.abs(spectrum) ** 2 / max(float(np.sum(window**2)), 1.0)
    frequencies = np.fft.fftshift(np.fft.fftfreq(len(centered), 1 / IQ_SAMPLE_RATE))

    bin_hz = IQ_SAMPLE_RATE / len(centered)
    dc_notch_hz = 3 * bin_hz
    signal_mask = (np.abs(frequencies) <= channel_bw_hz / 2) & (
        np.abs(frequencies) >= dc_notch_hz
    )
    noise_mask = (np.abs(frequencies) >= channel_bw_hz * 0.75) & (
        np.abs(frequencies) <= IQ_SAMPLE_RATE * 0.45
    )

    signal_power = _trimmed_mean(power[signal_mask], keep_fraction=0.98)
    noise_power = _trimmed_mean(power[noise_mask])
    channel_excess_db = 10 * np.log10(signal_power / max(noise_power, 1e-12))
    # AM/CW carriers occupy only a handful of bins and would be diluted by a
    # channel-wide mean. Compare equal high percentiles as a second detector;
    # noise produces similar tails in both regions, while a carrier does not.
    signal_peak = float(np.percentile(power[signal_mask], 99.9))
    noise_peak = float(np.percentile(power[noise_mask], 99.9))
    carrier_excess_db = 10 * np.log10(signal_peak / max(noise_peak, 1e-12))
    signal_db = round(np.clip(max(channel_excess_db, carrier_excess_db), 0, 60))
    noise_dbfs = round(10 * np.log10(max(noise_power, 1e-12)))
    return signal_db, noise_dbfs


def _audio_level_dbfs(audio: np.ndarray) -> int:
    """RMS level of demodulated audio, with deterministic silence handling."""
    if audio.size == 0:
        return -120
    rms = float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))
    return round(np.clip(20 * np.log10(max(rms, 1e-6)), -120, 0))


def _decimate_lowpass(
    x: np.ndarray, sos: np.ndarray, zi: np.ndarray | None
) -> tuple[np.ndarray, np.ndarray]:
    """Lowpass + decimate, carrying filter state (zi) across calls.

    Without this, sosfilt() implicitly starts from zero state on every
    call, which - since this runs once per ~34ms audio chunk - puts a
    small discontinuity at every chunk boundary: audible as a persistent
    low-level click/crackle laid over the real signal, worse on anything
    already weak. Passing the previous call's final state (zf) back in as
    the next call's zi makes the filter treat the stream as continuous.
    """
    if zi is None:
        zi = sosfilt_zi(sos) * x[0]  # init as if already at rest at the first sample
        if np.iscomplexobj(x):
            zi = zi.astype(complex)
    y, zf = sosfilt(sos, x, zi=zi)
    return y[::_DECIMATION], zf


def _demod_am(iq: np.ndarray, zi: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    envelope = np.abs(iq)
    envelope = envelope - np.mean(envelope)  # strip the DC/carrier component
    decimated, zf = _decimate_lowpass(envelope, _AM_SOS, zi)
    peak = float(np.max(np.abs(decimated))) + 1e-9
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "am demod: iq |mean|=%.6g iq_max=%.6g env_std=%.6g decimated_peak=%.6g",
            float(np.mean(np.abs(iq))), float(np.max(np.abs(iq))),
            float(np.std(envelope)), peak,
        )
    # AM/SW go through direct sampling (see module docstring), which
    # bypasses the R820T tuner - and its gain stage - entirely, so these
    # signals arrive at the ADC dramatically weaker than FM's (which does
    # get DEFAULT_GAIN_DB of real analog gain first). The floor here only
    # exists to stop true silence from blowing up into full-volume hiss on
    # division by a near-zero peak; it must stay well below any real
    # direct-sampling signal's peak, or genuine weak signals get divided by
    # the floor instead of their own peak and come out far too quiet to
    # hear - which is exactly what was happening at 0.05.
    return (decimated / max(peak, 0.001)).astype(np.float32), zf  # crude AGC


def _demod_fm(
    iq: np.ndarray, prev_sample: complex | None, zi: np.ndarray | None
) -> tuple[np.ndarray, complex, np.ndarray]:
    iq_full = np.concatenate([[prev_sample], iq]) if prev_sample is not None else iq
    # Polar discriminator: instantaneous frequency is the phase difference
    # between consecutive samples.
    demod = np.angle(iq_full[1:] * np.conj(iq_full[:-1]))
    decimated, zf = _decimate_lowpass(demod, _FM_SOS, zi)
    peak = float(np.max(np.abs(decimated))) + 1e-9
    audio = (decimated / max(peak, 0.3)).astype(np.float32)
    return audio, iq[-1], zf


def _demod_cw_tone(iq: np.ndarray, zi: np.ndarray | None) -> tuple[np.ndarray, np.ndarray]:
    """tune_cw() physically tunes tone_hz off the carrier, so the keyed
    carrier beats at exactly that offset in our own baseband - a lowpass
    isolates it from everything else in the capture, and the real part is
    an audible tone whenever the carrier is present. Fed straight into
    cw_decoder.analyze() unchanged, same as it consumed the ATS Mini's own
    BFO output before."""
    decimated, zf = _decimate_lowpass(iq, _CW_SOS, zi)
    audio = np.real(decimated)
    peak = float(np.max(np.abs(audio))) + 1e-9
    # Same direct-sampling gain gap as _demod_am() - see its comment.
    return (audio / max(peak, 0.0005)).astype(np.float32), zf


class RtlSdrBridge:
    """Owns the RTL-SDR device and a background thread that continuously
    reads IQ samples, demodulates them, updates status, and feeds both
    blocking capture_audio() callers and any live websocket audio
    subscribers from one shared stream."""

    def __init__(self, device_index: int = 0) -> None:
        self._device_index = device_index
        self._sdr = RtlSdr(device_index)
        self._sdr.sample_rate = IQ_SAMPLE_RATE
        # Fixed gain, not "auto": hardware AGC actively normalizes IQ power
        # to fill the ADC's dynamic range regardless of actual input signal
        # strength, which also moves the measured noise floor. Fixed gain keeps
        # noise-relative measurements comparable between tuning operations.
        self._sdr.gain = DEFAULT_GAIN_DB
        self._direct_sampling_active = False

        self._state_lock = threading.Lock()
        self._frequency_hz = 10_000_000
        self._band = "SW"
        self._mode = "AM"
        self._volume = 70

        self._status_lock = threading.Lock()
        self._latest_status: RadioStatus | None = None
        self._seq = 0

        self._audio_bus = _AudioBus()

        self._subscribers_lock = threading.Lock()
        self._subscribers: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = {}
        self._next_subscriber_id = 0

        self._stop = threading.Event()
        self._retune = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True, name="rtlsdr-demod")
        self._worker.start()

    def close(self) -> None:
        self._stop.set()
        self._worker.join(timeout=2)
        self._sdr.close()

    # ---- tuning -------------------------------------------------------

    def _apply_frequency(self, frequency_hz: int) -> None:
        needs_direct = frequency_hz < DIRECT_SAMPLING_THRESHOLD_HZ
        if needs_direct != self._direct_sampling_active:
            try:
                self._sdr.set_direct_sampling(DIRECT_SAMPLING_MODE if needs_direct else 0)
            except Exception:  # noqa: BLE001 -- librtlsdr exposes backend-specific errors
                # Some librtlsdr/R820T combinations apply the mode change but
                # return LIBUSB_ERROR_IO while reinitialising the tuner. Reopen
                # the device into a known state instead of retrying forever
                # with a dead audio stream.
                logger.warning("Reopening RTL-SDR after direct-sampling mode switch failed")
                self._sdr.close()
                self._sdr = RtlSdr(self._device_index)
                self._sdr.sample_rate = IQ_SAMPLE_RATE
                self._sdr.gain = DEFAULT_GAIN_DB
                if needs_direct:
                    self._sdr.set_direct_sampling(DIRECT_SAMPLING_MODE)
            self._direct_sampling_active = needs_direct
            logger.debug(
                "direct sampling %s at %d Hz", "enabled" if needs_direct else "disabled", frequency_hz
            )
        self._sdr.center_freq = frequency_hz

    def tune_to(self, frequency_hz: int, band: str) -> bool:
        """Direct tune - no band search, unlike the firmware this replaced."""
        band = band.upper()
        mode = "FM" if band == "FM" else "AM"
        with self._state_lock:
            self._frequency_hz = frequency_hz
            self._band = band if band in ("SW", "AM", "FM") else "SW"
            self._mode = mode
        self._retune.set()
        logger.debug("tune_to: %d Hz band=%s mode=%s", frequency_hz, self._band, mode)
        return True

    def tune_cw(
        self, carrier_hz: int, band_label: str = "", mode: str = "USB", tone_hz: int = 700
    ) -> bool:
        """Tune tone_hz off the carrier so a keyed CW signal beats at an
        audible pitch - the RF-vs-image sideband distinction USB/LSB
        implies on a real receiver doesn't apply here, since we choose
        which side of the carrier to sit on ourselves rather than relying
        on hardware sideband selection."""
        offset = tone_hz if mode.upper() != "LSB" else -tone_hz
        with self._state_lock:
            self._frequency_hz = carrier_hz + offset
            self._band = "SW"
            self._mode = "CW"
        self._retune.set()
        logger.debug(
            "tune_cw: carrier=%d Hz offset=%+d -> %d Hz (%s)",
            carrier_hz, offset, carrier_hz + offset, band_label,
        )
        return True

    def set_mode(self, mode: str) -> bool:
        with self._state_lock:
            self._mode = mode.upper()
        return True

    def volume_up(self) -> None:
        with self._state_lock:
            self._volume = min(100, self._volume + 5)

    def volume_down(self) -> None:
        with self._state_lock:
            self._volume = max(0, self._volume - 5)

    def is_tuning(self) -> bool:
        """Always False: a direct RTL-SDR retune is effectively instant, so
        there's no multi-second band-search state to report."""
        return False

    def latest_status(self) -> RadioStatus | None:
        with self._status_lock:
            return self._latest_status

    # ---- audio ----------------------------------------------------------

    def capture_audio(self, seconds: float) -> np.ndarray:
        return self._audio_bus.capture(seconds)

    def subscribe_audio(
        self, loop: asyncio.AbstractEventLoop
    ) -> tuple[int, asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=50)
        with self._subscribers_lock:
            subscriber_id = self._next_subscriber_id
            self._next_subscriber_id += 1
            self._subscribers[subscriber_id] = (loop, queue)
        return subscriber_id, queue

    def unsubscribe_audio(self, subscriber_id: int) -> None:
        with self._subscribers_lock:
            self._subscribers.pop(subscriber_id, None)

    def _publish_to_subscribers(self, pcm_bytes: bytes) -> None:
        with self._subscribers_lock:
            subs = list(self._subscribers.items())
        for subscriber_id, (loop, queue) in subs:
            try:
                loop.call_soon_threadsafe(queue.put_nowait, pcm_bytes)
            except RuntimeError:
                pass  # event loop closed; disconnect handler will clean up

    # ---- worker loop ------------------------------------------------------

    def _run(self) -> None:
        with self._state_lock:
            freq, band, mode = self._frequency_hz, self._band, self._mode
        self._apply_frequency(freq)
        prev_fm_sample: complex | None = None
        filter_zi: np.ndarray | None = None
        signal_history: deque[tuple[int, int, int]] = deque(maxlen=12)

        while not self._stop.is_set():
            # The whole iteration is one try/except: this thread has no
            # external supervisor, so an uncaught exception anywhere in here
            # (a bad retune, a transient read glitch, a DSP edge case) would
            # otherwise silently and permanently kill the radio until the
            # container restarts - log it and keep going instead.
            try:
                if self._retune.is_set():
                    with self._state_lock:
                        freq, band, mode = self._frequency_hz, self._band, self._mode
                    self._apply_frequency(freq)
                    prev_fm_sample = None
                    filter_zi = None  # a new frequency isn't a continuation of the old stream
                    signal_history.clear()
                    self._retune.clear()

                iq = self._sdr.read_samples(CHUNK_IQ_SAMPLES)

                if mode == "FM":
                    audio, prev_fm_sample, filter_zi = _demod_fm(iq, prev_fm_sample, filter_zi)
                    metrics = _estimate_signal(iq, FM_CHANNEL_BW_HZ)
                elif mode == "CW":
                    audio, filter_zi = _demod_cw_tone(iq, filter_zi)
                    metrics = _estimate_signal(iq, CW_CHANNEL_BW_HZ)
                else:
                    audio, filter_zi = _demod_am(iq, filter_zi)
                    metrics = _estimate_signal(iq, AM_CHANNEL_BW_HZ)

                audio_dbfs = _audio_level_dbfs(audio)
                signal_history.append((*metrics, audio_dbfs))
                signal_db = round(float(np.median([item[0] for item in signal_history])))
                noise_dbfs = round(float(np.median([item[1] for item in signal_history])))
                audio_dbfs = round(float(np.median([item[2] for item in signal_history])))

                with self._state_lock:
                    volume = self._volume
                audio = np.clip(audio * (volume / 100.0), -1.0, 1.0)

                self._audio_bus.publish(audio)
                self._publish_to_subscribers((audio * 32767).astype(np.int16).tobytes())

                with self._status_lock:
                    self._seq += 1
                    self._latest_status = RadioStatus(
                        frequency_hz=freq,
                        band=band,
                        mode=mode,
                        signal_db=signal_db,
                        noise_dbfs=noise_dbfs,
                        audio_dbfs=audio_dbfs,
                        volume=volume,
                        seq=self._seq,
                    )
            except Exception:
                logger.exception("RTL-SDR worker iteration failed")
                time.sleep(0.1)
