"""FastAPI backend: serves the station browser UI, tunes the ATS Mini over
USB serial, and streams live RSSI/SNR telemetry to the browser.

Author: James Sawyer / JSLabs - https://labs.jamessawyer.co.uk/monitoring/
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
import time
from dataclasses import asdict
from pathlib import Path

import sounddevice as sd
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import cw_decoder
from audio_passthrough import AudioPassthrough
from cw_monitor import CwMonitor, CwTarget as MonitorCwTarget
from radio_bridge import AtsMiniBridge, find_ats_mini_port

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "stations.db"
RSSI_DB_PATH = Path(__file__).parent / "data" / "rssi_cache.db"

app = FastAPI(title="ATS Mini Controller")
bridge: AtsMiniBridge | None = None

# freq_hz -> {"rssi": int, "snr": int, "measured_at": float epoch seconds}.
# Kept in memory for fast lookups on every /api/stations call, backed by
# RSSI_DB_PATH so measurements survive restarts and accumulate across
# separate scan sessions instead of resetting each time the server starts.
# Deliberately a separate file from stations.db: rebuilding the station list
# (stations_db.py) deletes and recreates that file, and measurements
# shouldn't be lost just because the station list was refreshed.
rssi_cache: dict[int, dict] = {}


def _init_rssi_store() -> None:
    conn = sqlite3.connect(RSSI_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS rssi_measurements (
            freq_hz INTEGER PRIMARY KEY,
            rssi INTEGER NOT NULL,
            snr INTEGER NOT NULL,
            measured_at REAL NOT NULL
        )
        """
    )
    conn.commit()
    for freq_hz, rssi, snr, measured_at in conn.execute(
        "SELECT freq_hz, rssi, snr, measured_at FROM rssi_measurements"
    ):
        rssi_cache[freq_hz] = {"rssi": rssi, "snr": snr, "measured_at": measured_at}
    conn.close()
    logger.info("Loaded %d persisted RSSI measurements", len(rssi_cache))


def _persist_rssi(freq_hz: int, entry: dict) -> None:
    # sqlite3's context manager only commits/rolls back on exit, it does not
    # close the connection - that needs an explicit close() regardless of
    # whether execute() raised.
    conn = sqlite3.connect(RSSI_DB_PATH)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO rssi_measurements (freq_hz, rssi, snr, measured_at) "
                "VALUES (?, ?, ?, ?)",
                (freq_hz, entry["rssi"], entry["snr"], entry["measured_at"]),
            )
    finally:
        conn.close()


@app.on_event("startup")
def _connect_radio() -> None:
    global bridge
    _init_rssi_store()
    port = find_ats_mini_port()
    if port is None:
        logger.error("ATS Mini not found on any USB port; tuning endpoints will fail")
        return
    try:
        bridge = AtsMiniBridge(port)
        logger.info("Connected to ATS Mini on %s", port)
    except Exception:
        logger.exception("Could not open %s; tuning endpoints will fail", port)


@app.on_event("shutdown")
def _disconnect_radio() -> None:
    if cw_monitor is not None:
        cw_monitor.stop()
    if audio_passthrough is not None:
        audio_passthrough.stop()
    if bridge is not None:
        bridge.close()


def _require_bridge() -> AtsMiniBridge:
    if bridge is None:
        raise HTTPException(status_code=503, detail="Radio not connected")
    return bridge


def _find_audio_input_device() -> int | None:
    """Prefer an actual USB sound card over the Mac's own microphone: the
    built-in mic would just pick up room noise, not the ATS Mini's audio-out
    feeding a dongle like the UGREEN adapter (shows up generically as "USB
    Audio Device" - no per-brand driver, so brand names aren't in its name)."""
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and "usb" in d["name"].lower():
            return i
    for i, d in enumerate(devices):
        if d["max_input_channels"] > 0 and "macbook" not in d["name"].lower():
            return i
    return None


def _find_audio_output_device() -> int | None:
    """The Mac's own speakers/headphones, not the USB adapter's output -
    that's what you're actually listening on day to day."""
    try:
        return int(sd.default.device[1])
    except (TypeError, IndexError):
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d["max_output_channels"] > 0:
                return i
        return None


audio_passthrough: AudioPassthrough | None = None


# Set by a manual /api/tune while a scan or the CW monitor is mid-flight,
# checked by their loops between steps. Without this, background automation
# (which keeps retuning every ~1s) would keep dragging the radio away from
# whatever a manual click just landed on until it finished on its own — a
# manual click should always win outright, not just for one instant.
scan_cancel = threading.Event()

cw_monitor: CwMonitor | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/api/stations")
def list_stations(
    q: str = "",
    band: str = "",
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
) -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        clauses: list[str] = []
        params: list[object] = []
        if q:
            clauses.append("(name LIKE ? OR country LIKE ? OR language LIKE ?)")
            like = f"%{q}%"
            params.extend([like, like, like])
        if band:
            clauses.append("band = ?")
            params.append(band)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = conn.execute(f"SELECT COUNT(*) FROM stations {where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM stations {where} ORDER BY freq_hz LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        cached = rssi_cache.get(d["freq_hz"])
        d["rssi"] = cached["rssi"] if cached else None
        d["snr"] = cached["snr"] if cached else None
        d["measured_at"] = cached["measured_at"] if cached else None
        out.append(d)
    return {"total": total, "offset": offset, "returned": len(out), "stations": out}


class TuneRequest(BaseModel):
    frequency_hz: int
    band: str


@app.post("/api/tune")
def tune(req: TuneRequest) -> dict:
    radio = _require_bridge()
    scan_cancel.set()  # a manual click always wins over a running scan
    landed = radio.tune_to(req.frequency_hz, req.band)
    return {"ok": landed}


@app.post("/api/volume/{direction}")
def volume(direction: str) -> dict:
    radio = _require_bridge()
    if direction == "up":
        radio.volume_up()
    elif direction == "down":
        radio.volume_down()
    else:
        raise HTTPException(status_code=400, detail="direction must be 'up' or 'down'")
    return {"ok": True}


class ScanTarget(BaseModel):
    freq_hz: int
    band: str


class ScanRequest(BaseModel):
    stations: list[ScanTarget]
    settle_s: float = 0.7


_MAX_SCAN_TARGETS = 200


@app.post("/api/scan")
def scan(req: ScanRequest) -> dict:
    """Tune through candidate frequencies and record real RSSI/SNR.

    Stations sharing a firmware band (e.g. all shortwave broadcasts, which
    all live under the 'ALL' band) only pay the multi-second band-search
    cost once; repeat tunes within the same band are near-instant. Duplicate
    frequencies (many stations share the same carrier) are measured once.
    """
    radio = _require_bridge()
    scan_cancel.clear()
    seen: set[int] = set()
    results = []
    for target in req.stations[:_MAX_SCAN_TARGETS]:
        if scan_cancel.is_set():
            break  # a manual tune took over; stop dragging the radio around
        if target.freq_hz in seen:
            continue
        seen.add(target.freq_hz)
        if not radio.tune_to(target.freq_hz, target.band):
            continue  # preempted by a concurrent manual tune, or band not found
        time.sleep(req.settle_s)
        if scan_cancel.is_set():
            break
        st = radio.latest_status()
        if st is None or st.frequency_hz != target.freq_hz:
            continue
        entry = {"rssi": st.rssi, "snr": st.snr, "measured_at": time.time()}
        rssi_cache[target.freq_hz] = entry
        _persist_rssi(target.freq_hz, entry)
        results.append({"freq_hz": target.freq_hz, **entry})
    return {"scanned": len(results), "results": results, "cancelled": scan_cancel.is_set()}


class CwListenRequest(BaseModel):
    seconds: float = 4.0


@app.post("/api/cw/listen")
def cw_listen(req: CwListenRequest) -> dict:
    """Record from the audio input and try to decode CW, without touching
    the tuned frequency - useful to check whatever's currently playing."""
    device = _find_audio_input_device()
    if device is None:
        raise HTTPException(status_code=503, detail="No audio input device found")
    result = cw_decoder.listen(device, seconds=req.seconds)
    return {"device": sd.query_devices()[device]["name"], **result.__dict__}


class CwTarget(BaseModel):
    freq_hz: int
    firmware_band: str
    mode: str = "USB"


class CwScanRequest(BaseModel):
    targets: list[CwTarget] | None = None
    listen_seconds: float = 4.0
    stop_on_first_hit: bool = True


# Known-unambiguous ham/CW-band names: the firmware's band list has two
# entries both named "15M" (an AM shortwave-broadcast meter band and the
# 15m ham band), and select_band() matches by name alone, so a target using
# that name could land on the wrong one depending on where the band pointer
# starts. 30M/20M/10M don't collide with any broadcast meter band, and are
# all confirmed (by direct test) to actually reach LSB/USB with a working
# BFO offset via the remote protocol.
_DEFAULT_CW_TARGETS = [
    # DK0WCY: transmits real CW during most of every 10-minute cycle (only
    # RTTY/PSK31 at :10/:50), not a brief shared slot - far higher duty
    # cycle than the beacon network below, so tried first.
    CwTarget(freq_hz=10144000, firmware_band="30M", mode="LSB"),
    # NCDXF/IARU beacon network: 18 stations round-robin their callsign in
    # CW every 10s (full cycle ~3 min), continuously, specifically so
    # anyone can test reception - far higher odds of a real decode than
    # waiting for unpredictable ham QSO traffic on the calling frequencies.
    CwTarget(freq_hz=14100000, firmware_band="20M", mode="USB"),
    CwTarget(freq_hz=28200000, firmware_band="10M", mode="USB"),
    # GB3RAL: a single station transmitting continuously (not round-robin),
    # so a higher duty cycle than the NCDXF network on the same band.
    CwTarget(freq_hz=28215000, firmware_band="10M", mode="USB"),
    CwTarget(freq_hz=14030000, firmware_band="20M", mode="USB"),
    CwTarget(freq_hz=28030000, firmware_band="10M", mode="USB"),
]


@app.post("/api/cw/scan")
def cw_scan(req: CwScanRequest) -> dict:
    """Tune through candidate CW frequencies, listen on each, and decode
    any that show clean on/off keying. Stops at the first hit by default -
    each listen takes several seconds, so scanning many candidates is slow.
    """
    radio = _require_bridge()
    device = _find_audio_input_device()
    if device is None:
        raise HTTPException(status_code=503, detail="No audio input device found")

    scan_cancel.clear()
    targets = req.targets or _DEFAULT_CW_TARGETS
    results = []
    for target in targets:
        if scan_cancel.is_set():
            break
        if not radio.tune_cw(target.freq_hz, target.firmware_band, target.mode):
            continue  # preempted by a concurrent manual tune, or band not found
        time.sleep(0.5)  # let the SSB demodulator settle before recording
        if scan_cancel.is_set():
            break
        result = cw_decoder.listen(device, seconds=req.listen_seconds)
        results.append({"freq_hz": target.freq_hz, "firmware_band": target.firmware_band, **result.__dict__})
        if result.is_cw and req.stop_on_first_hit:
            break
    return {"results": results, "cancelled": scan_cancel.is_set()}


class MonitorStartRequest(BaseModel):
    targets: list[CwTarget] | None = None


def _monitor_state_dict(state) -> dict:
    return {
        "running": state.running,
        "mode": state.mode,
        "current_freq_hz": state.current_freq_hz,
        "in_progress_text": state.in_progress_text,
        "in_progress_started_at": state.in_progress_started_at,
        "log": [
            {
                "freq_hz": e.freq_hz,
                "started_at": e.started_at,
                "ended_at": e.ended_at,
                "text": e.text,
                "morse": e.morse,
                "wpm": e.wpm,
            }
            for e in state.log
        ],
        "version": state.version,
    }


@app.post("/api/cw/monitor/start")
def cw_monitor_start(req: MonitorStartRequest) -> dict:
    global cw_monitor
    radio = _require_bridge()
    if audio_passthrough is not None and audio_passthrough.running:
        raise HTTPException(status_code=409, detail="Stop audio passthrough first — both need the same input device")
    device = _find_audio_input_device()
    if device is None:
        raise HTTPException(status_code=503, detail="No audio input device found")

    targets = [MonitorCwTarget(t.freq_hz, t.firmware_band, t.mode) for t in (req.targets or _DEFAULT_CW_TARGETS)]
    if cw_monitor is not None:
        cw_monitor.stop()
    cw_monitor = CwMonitor(radio, device, targets, manual_override=scan_cancel)
    cw_monitor.start()
    return {"ok": True}


@app.post("/api/cw/monitor/stop")
def cw_monitor_stop() -> dict:
    if cw_monitor is not None:
        cw_monitor.stop()
    return {"ok": True}


@app.get("/api/cw/monitor/stream")
async def cw_monitor_stream(request: Request) -> StreamingResponse:
    async def gen():
        last_version = None
        while True:
            if await request.is_disconnected():
                break
            if cw_monitor is None:
                yield f"data: {json.dumps({'running': False, 'mode': 'idle'})}\n\n"
            else:
                state = cw_monitor.state()
                if state.version != last_version:
                    last_version = state.version
                    yield f"data: {json.dumps(_monitor_state_dict(state))}\n\n"
            await asyncio.sleep(0.3)

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.post("/api/audio/passthrough/start")
def audio_passthrough_start() -> dict:
    global audio_passthrough
    if cw_monitor is not None and cw_monitor.state().running:
        raise HTTPException(status_code=409, detail="Stop the CW monitor first — both need the same input device")
    input_device = _find_audio_input_device()
    if input_device is None:
        raise HTTPException(status_code=503, detail="No audio input device found")
    output_device = _find_audio_output_device()

    if audio_passthrough is not None:
        audio_passthrough.stop()
    audio_passthrough = AudioPassthrough(input_device, output_device)
    audio_passthrough.start()
    return {"ok": True}


@app.post("/api/audio/passthrough/stop")
def audio_passthrough_stop() -> dict:
    if audio_passthrough is not None:
        audio_passthrough.stop()
    return {"ok": True}


@app.get("/api/audio/passthrough/status")
def audio_passthrough_status() -> dict:
    return {"running": audio_passthrough is not None and audio_passthrough.running}


@app.get("/api/status")
def status() -> dict:
    radio = _require_bridge()
    st = radio.latest_status()
    return asdict(st) if st else {}


@app.get("/api/status/stream")
async def status_stream(request: Request) -> StreamingResponse:
    radio = _require_bridge()

    async def gen():
        last_key = None
        while True:
            if await request.is_disconnected():
                break
            tuning = radio.is_tuning()
            st = radio.latest_status()
            # While a tune is mid-band-search, suppress the intermediate
            # hops (they'd otherwise flash through several unrelated bands
            # in the UI) and just report that a tune is in progress.
            key = (tuning, st.seq if st is not None else None)
            if key != last_key:
                last_key = key
                payload = {"tuning": tuning}
                if st is not None and not tuning:
                    payload.update(asdict(st))
                yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/event-stream")
