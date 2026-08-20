"""USB serial bridge for the ATS Mini (esp32-si4732 firmware, ad-hoc serial protocol).

Protocol reference: https://esp32-si4732.github.io/ats-mini/remote.html
Single-character commands drive the receiver; the 't' command toggles a
continuous CSV telemetry stream (frequency, RSSI, SNR, etc.).

Author: James Sawyer / JSLabs - https://labs.jamessawyer.co.uk/monitoring/
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import serial
import serial.tools.list_ports

logger = logging.getLogger(__name__)

BAUD_RATE = 115200

# Espressif's assigned USB vendor ID; the ESP32-S3's native USB-JTAG/serial
# port always reports this regardless of which /dev/cu.usbmodemNNNN path the
# OS happens to assign it (that path changes across reconnects and even
# across reboots, which repeatedly broke a hardcoded path in practice).
_ESPRESSIF_VID = 0x303A


def find_ats_mini_port() -> str | None:
    for port in serial.tools.list_ports.comports():
        if port.vid == _ESPRESSIF_VID:
            return port.device
    return None

# CSV field order emitted by the 't' monitor stream.
_STATUS_FIELDS = (
    "app_version",
    "frequency",
    "bfo",
    "band_cal",
    "band",
    "mode",
    "step_idx",
    "bandwidth_idx",
    "agc_idx",
    "volume",
    "rssi",
    "snr",
    "tuning_capacitor",
    "voltage_raw",
    "seq",
)


@dataclass(frozen=True)
class RadioStatus:
    app_version: str
    frequency_hz: int
    bfo: int
    band: str
    mode: str
    step: str
    bandwidth: str
    agc_idx: int
    volume: int
    rssi: int
    snr: int
    voltage: float
    seq: int


def _parse_status_line(line: str) -> RadioStatus | None:
    parts = line.strip().split(",")
    if len(parts) != len(_STATUS_FIELDS):
        return None
    try:
        fields = dict(zip(_STATUS_FIELDS, parts))
        # frequency is reported in the band's native step unit: 10 kHz for
        # FM (band "VHF"), 1 kHz for everything else (AM/MW/SW/SSB).
        freq_unit_hz = 10_000 if fields["band"] == "VHF" else 1_000
        return RadioStatus(
            app_version=fields["app_version"],
            frequency_hz=int(fields["frequency"]) * freq_unit_hz,
            bfo=int(fields["bfo"]),
            band=fields["band"],
            mode=fields["mode"],
            step=fields["step_idx"],
            bandwidth=fields["bandwidth_idx"],
            agc_idx=int(fields["agc_idx"]),
            volume=int(fields["volume"]),
            rssi=int(fields["rssi"]),
            snr=int(fields["snr"]),
            voltage=float(fields["voltage_raw"]),
            seq=int(fields["seq"]),
        )
    except (ValueError, KeyError):
        return None


class AtsMiniBridge:
    """Owns the serial connection and the monitor-stream reader thread."""

    def __init__(self, port: str, baud: int = BAUD_RATE) -> None:
        self._serial = serial.Serial(port, baud, timeout=0.2)
        self._lock = threading.Lock()
        self._latest_status: RadioStatus | None = None
        self._status_lock = threading.Lock()
        self._stop = threading.Event()
        self._monitor_enabled = False
        self._tune_generation = 0
        self._settled_generation = 0
        self._tune_lock = threading.Lock()
        self._reader_thread = threading.Thread(target=self._read_loop, daemon=True)
        self._reader_thread.start()
        # Opening the port can toggle DTR/RTS and reset the ESP32-S3; give the
        # firmware time to finish booting before sending any commands.
        time.sleep(1.5)
        self.enable_monitor()

    def close(self) -> None:
        self._stop.set()
        self._reader_thread.join(timeout=2)
        if self._monitor_enabled:
            self._send_raw(b"t")
        self._serial.close()

    def _send_raw(self, data: bytes) -> None:
        with self._lock:
            self._serial.write(data)

    def _is_streaming(self, window_s: float) -> bool:
        with self._status_lock:
            self._latest_status = None
        time.sleep(window_s)
        with self._status_lock:
            return self._latest_status is not None

    def enable_monitor(self) -> None:
        """'t' is a toggle, and the device's current state is unknown at
        connect time, so probe for live CSV output before deciding whether
        to send it."""
        if self._is_streaming(1.5):
            self._monitor_enabled = True
            return
        self._send_raw(b"t")
        self._monitor_enabled = self._is_streaming(1.5)
        if not self._monitor_enabled:
            logger.warning("Could not confirm monitor stream is active")

    def _read_loop(self) -> None:
        buf = b""
        while not self._stop.is_set():
            with self._lock:
                chunk = self._serial.read(1024)
            if not chunk:
                time.sleep(0.02)
                continue
            buf += chunk
            while b"\r\n" in buf:
                line, buf = buf.split(b"\r\n", 1)
                status = _parse_status_line(line.decode(errors="ignore"))
                if status is not None:
                    with self._status_lock:
                        self._latest_status = status

    def latest_status(self) -> RadioStatus | None:
        with self._status_lock:
            return self._latest_status

    def is_tuning(self) -> bool:
        """True while a tune_to() band search is in flight. A single click
        can take several seconds when it must cycle through many
        intermediate bands (no direct 'select band N' command exists), and
        every hop briefly shows up in the status stream; callers use this to
        suppress that transient noise instead of visibly bouncing around."""
        return self._tune_generation != self._settled_generation

    def tune_hz(self, frequency_hz: int) -> None:
        """Tune directly to a frequency in Hz (F<hz>\\r command). The
        firmware's remoteSetFrequency() requires a trailing '\\r' to
        recognize the end of the digit sequence, and only accepts values
        within the currently selected firmware band's range."""
        self._send_raw(f"F{frequency_hz}\r".encode())

    def _press_band(
        self, direction: bytes, count: int, firmware_band: str, generation: int | None
    ) -> bool | None:
        """Press B/b `count` times, stopping early on a match.

        Returns True if found, None if preempted by a newer tune_to() call,
        False if it pressed `count` times without finding the target (the
        band pointer has moved `count` steps in `direction` at that point).
        """
        status = self.latest_status()
        for _ in range(count):
            if generation is not None and generation != self._tune_generation:
                return None
            prev_band = status.band if status is not None else None
            self._send_raw(direction)
            deadline = time.time() + 1.5
            status = self.latest_status()
            while time.time() < deadline and (status is None or status.band == prev_band):
                time.sleep(0.05)
                status = self.latest_status()
            if status is not None and status.band == firmware_band:
                return True
        return False

    def select_band(
        self, firmware_band: str, max_presses: int = 30, generation: int | None = None
    ) -> bool:
        """Move the band pointer to firmware_band, or give up.

        The firmware only exposes relative "next band" (B) / "previous band"
        (b) commands across a fixed 19-band cycle (..., CB, VHF, ALL, 11M,
        ...) — there's no direct "select band N", so every press is visible
        on the device's own screen. VHF sits immediately *before* ALL going
        forward, which means it's only one step *backward* from ALL or any
        of the SW meter bands — the common SW<->FM jump resolves in ~1 press
        backward instead of up to ~18 forward. That asymmetry only helps
        when VHF is the target (going the other way, forward from VHF
        reaches ALL in 1 step already), so the reverse probe is restricted
        to that case; every other target uses the plain forward sweep,
        which was already the shorter direction for them.

        A fixed inter-press delay is unreliable (settle time after a press
        varies), so each press waits for the reported band to actually
        change (not just for a new CSV line, since the monitor stream ticks
        ~10x/sec regardless of band changes) before sending the next one.

        If `generation` is given and a newer tune_to() call supersedes it
        mid-cycle (see tune_to), this aborts immediately instead of
        finishing a now-irrelevant multi-second band search.
        """
        status = self.latest_status()
        if status is not None and status.band == firmware_band:
            return True

        if firmware_band != "VHF":
            found = self._press_band(b"B", max_presses, firmware_band, generation)
            return bool(found)

        probe_budget = min(8, max_presses)
        found = self._press_band(b"b", probe_budget, firmware_band, generation)
        if found is None:
            return False
        if found:
            return True
        # Reverse probe missed: undo it so the forward sweep below covers
        # the full cycle instead of double-counting the backward detour.
        undone = self._press_band(b"B", probe_budget, "", generation)
        if undone is None:
            return False

        remaining = max_presses - 2 * probe_budget
        found = self._press_band(b"B", max(remaining, 0), firmware_band, generation)
        return bool(found)

    def tune_to(self, frequency_hz: int, coarse_band: str) -> bool:
        """Select the firmware band matching a coarse SW/AM/FM tag, then tune.

        Both SW and AM broadcasts are tuned via the firmware's 'ALL' band:
        confirmed by direct test that it spans the entire range continuously
        from 530 kHz (bottom of AM broadcast) through the whole shortwave
        spectrum, not just the shortwave portion its name implies. Routing
        AM through it too (instead of switching to MW1) means SW<->AM clicks
        never need a band search at all, only SW/AM<->FM does.

        Each call bumps a generation counter; if another tune_to() call
        starts before this one finishes its (multi-second) band search, this
        one aborts rather than completing a stale tune. Returns False if
        preempted or the band couldn't be found, True if the F<hz> command
        was actually sent.
        """
        with self._tune_lock:
            self._tune_generation += 1
            my_generation = self._tune_generation

        try:
            firmware_band = {"FM": "VHF", "SW": "ALL", "AM": "ALL"}.get(coarse_band.upper())
            if firmware_band and not self.select_band(firmware_band, generation=my_generation):
                return False
            if my_generation != self._tune_generation:
                return False
            self.tune_hz(frequency_hz)
            return True
        finally:
            # Only the most recent call gets to declare "settled"; a
            # preempted older call must not mask that a newer one is
            # still (or now) in flight.
            if my_generation == self._tune_generation:
                self._settled_generation = my_generation

    def tune_cw(
        self, carrier_hz: int, firmware_band: str, mode: str = "USB", tone_hz: int = 700
    ) -> bool:
        """Tune to a CW signal's carrier frequency with an audible beat tone.

        CW is an on/off-keyed carrier with no modulation of its own; AM mode
        (the coarse "SW" path) has no BFO and would only produce faint
        clicks, not a clean tone. This selects the given ham band (e.g.
        "20M"), switches to USB/LSB, and tunes `tone_hz` above the carrier
        (docs: in SSB modes, F<hz>'s sub-kHz digits set the BFO directly),
        so the demodulated audio is a steady `tone_hz` note while keyed.
        """
        with self._tune_lock:
            self._tune_generation += 1
            my_generation = self._tune_generation

        try:
            if not self.select_band(firmware_band, generation=my_generation):
                return False
            if my_generation != self._tune_generation:
                return False
            self.set_mode(mode)
            if my_generation != self._tune_generation:
                return False
            self.tune_hz(carrier_hz + tone_hz)
            return True
        finally:
            if my_generation == self._tune_generation:
                self._settled_generation = my_generation

    def set_mode(self, mode: str, max_presses: int = 8) -> bool:
        """Cycle mode (M/m) until it matches; firmware has no direct 'set
        mode' command. Same fixed-delay bug band-selection originally had:
        a flat sleep after each press can miss the actual change and either
        double-press or read stale state, so this waits for the reported
        mode to actually change before sending the next press."""
        target = mode.upper()
        status = self.latest_status()
        if status is not None and status.mode.upper() == target:
            return True
        for _ in range(max_presses):
            prev_mode = status.mode if status is not None else None
            self._send_raw(b"M")
            deadline = time.time() + 1.5
            status = self.latest_status()
            while time.time() < deadline and (status is None or status.mode == prev_mode):
                time.sleep(0.05)
                status = self.latest_status()
            if status is not None and status.mode.upper() == target:
                return True
        return False

    def volume_up(self) -> None:
        self._send_raw(b"V")

    def volume_down(self) -> None:
        self._send_raw(b"v")
