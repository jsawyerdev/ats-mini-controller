"""FastAPI backend: serves the station browser UI, tunes an RTL-SDR dongle
in software, and streams live signal telemetry and demodulated audio to
the browser.

Author: James Sawyer / JSLabs - https://www.jamessawyer.co.uk/ | https://labs.jamessawyer.co.uk/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import cw_decoder
from audio_passthrough import serve_audio_websocket
from cw_monitor import CwMonitor, CwTarget as MonitorCwTarget
from radio_bridge import RtlSdrBridge, find_rtlsdr_device


def configure_logging() -> None:
    """Level is set via the LOG_LEVEL env var (default INFO) rather than
    hardcoded, so verbose per-target scan/tune tracing can be turned on for
    debugging (`docker compose run -e LOG_LEVEL=DEBUG` or set it in
    compose.yaml) without a code change."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


configure_logging()
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "stations.db"
SIGNAL_DB_PATH = Path(__file__).parent / "data" / "signal_cache.db"
APP_VERSION = "2.3.0"

COUNTRY_NAMES = {"CA": "Canada", "GB": "United Kingdom", "US": "United States"}


def _country_sql(alias: str = "s") -> str:
    return f"CASE WHEN {alias}.source LIKE 'ofcom_%' THEN 'GB' ELSE {alias}.country END"


def _station_where(q: str, band: str, country: str) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if q:
        clauses.append("(s.name LIKE ? OR s.country LIKE ? OR s.language LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if band:
        clauses.append("s.band = ?")
        params.append(band)
    if country:
        clauses.append(f"{_country_sql()} = ?")
        params.append(country)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return where, params


@asynccontextmanager
async def lifespan(_: FastAPI):
    _connect_radio()
    try:
        yield
    finally:
        _disconnect_radio()


app = FastAPI(title="RTL-SDR Station Browser", version=APP_VERSION, lifespan=lifespan)
bridge: RtlSdrBridge | None = None

MIN_FREQUENCY_HZ = 1
MAX_FREQUENCY_HZ = 1_766_000_000

# freq_hz -> signal/noise measurements plus their epoch timestamp.
# Kept in memory for fast lookups on every /api/stations call, backed by
# SIGNAL_DB_PATH so measurements survive restarts and accumulate across
# separate scan sessions instead of resetting each time the server starts.
# Deliberately a separate file from stations.db: rebuilding the station list
# (stations_db.py) deletes and recreates that file, and measurements
# shouldn't be lost just because the station list was refreshed.
signal_cache: dict[int, dict] = {}


def _init_signal_store() -> None:
    conn = sqlite3.connect(SIGNAL_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS signal_measurements (
            freq_hz INTEGER PRIMARY KEY,
            signal_db INTEGER NOT NULL,
            noise_dbfs INTEGER NOT NULL,
            measured_at REAL NOT NULL
        )
        """)
    conn.commit()
    for freq_hz, signal_db, noise_dbfs, measured_at in conn.execute(
        "SELECT freq_hz, signal_db, noise_dbfs, measured_at FROM signal_measurements"
    ):
        signal_cache[freq_hz] = {
            "signal_db": signal_db,
            "noise_dbfs": noise_dbfs,
            "measured_at": measured_at,
        }
    conn.close()
    logger.info("Loaded %d persisted signal measurements", len(signal_cache))


def _persist_signal(freq_hz: int, entry: dict) -> None:
    # sqlite3's context manager only commits/rolls back on exit, it does not
    # close the connection - that needs an explicit close() regardless of
    # whether execute() raised.
    conn = sqlite3.connect(SIGNAL_DB_PATH)
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO signal_measurements "
                "(freq_hz, signal_db, noise_dbfs, measured_at) "
                "VALUES (?, ?, ?, ?)",
                (freq_hz, entry["signal_db"], entry["noise_dbfs"], entry["measured_at"]),
            )
    finally:
        conn.close()


def _connect_radio() -> None:
    global bridge
    _init_signal_store()
    if not find_rtlsdr_device():
        logger.error("RTL-SDR dongle not found on any USB port; tuning endpoints will fail")
        return
    try:
        bridge = RtlSdrBridge()
        logger.info("Connected to RTL-SDR dongle")
    except Exception:
        logger.exception("Could not open RTL-SDR device; tuning endpoints will fail")


def _disconnect_radio() -> None:
    if cw_monitor is not None:
        cw_monitor.stop()
    if bridge is not None:
        bridge.close()


def _require_bridge() -> RtlSdrBridge:
    if bridge is None:
        raise HTTPException(status_code=503, detail="Radio not connected")
    return bridge


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


@app.get("/api/health")
def health(response: Response) -> dict[str, bool]:
    """Report process and radio readiness for operators and container health checks."""
    connected = bridge is not None
    if not connected:
        response.status_code = 503
    return {"ok": connected, "radio_connected": connected}


@app.get("/api/stations")
def list_stations(
    q: str = "",
    band: str = "",
    country: str = "",
    limit: int = Query(default=500, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    sort: Literal[
        "frequency", "band", "name", "country", "language", "signal", "last_checked"
    ] = "frequency",
    order: Literal["asc", "desc"] = "asc",
) -> dict:
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("ATTACH DATABASE ? AS signal_store", (str(SIGNAL_DB_PATH),))
        where, params = _station_where(q, band, country)
        total = conn.execute(
            f"SELECT COUNT(*) FROM stations s {where}", params
        ).fetchone()[0]
        direction = "ASC" if order == "asc" else "DESC"
        sort_columns = {
            "frequency": "s.freq_hz",
            "band": "s.band COLLATE NOCASE",
            "name": "s.name COLLATE NOCASE",
            "country": f"{_country_sql()} COLLATE NOCASE",
            "language": "s.language COLLATE NOCASE",
            "signal": "m.signal_db",
            "last_checked": "m.measured_at",
        }
        sort_column = sort_columns[sort]
        missing_last = (
            f"{sort_column} IS NULL ASC, " if sort in {"signal", "last_checked"} else ""
        )
        rows = conn.execute(
            "SELECT s.*, m.signal_db, m.noise_dbfs, m.measured_at "
            "FROM stations s LEFT JOIN signal_store.signal_measurements m "
            "ON m.freq_hz = s.freq_hz "
            f"{where} ORDER BY {missing_last}{sort_column} {direction}, "
            "s.freq_hz ASC, s.name COLLATE NOCASE ASC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if d["source"].startswith("ofcom_"):
            d["country"] = "GB"
        out.append(d)
    return {"total": total, "offset": offset, "returned": len(out), "stations": out}


@app.get("/api/countries")
def list_countries() -> list[dict[str, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        codes = [
            row[0]
            for row in conn.execute(
                f"SELECT DISTINCT {_country_sql()} AS code FROM stations s "
                "WHERE code <> '' ORDER BY code COLLATE NOCASE"
            )
        ]
    return [{"code": code, "name": COUNTRY_NAMES.get(code, code)} for code in codes]


@app.get("/api/scan/targets")
def list_scan_targets(q: str = "", band: str = "", country: str = "") -> list[dict]:
    """Return each filtered frequency once so a full scan does no duplicate RF work."""
    where, params = _station_where(q, band, country)
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            f"SELECT s.freq_hz, s.band FROM stations s {where} "
            "GROUP BY s.freq_hz, s.band ORDER BY s.freq_hz, s.band",
            params,
        ).fetchall()
    return [{"freq_hz": freq_hz, "band": row_band} for freq_hz, row_band in rows]


class TuneRequest(BaseModel):
    frequency_hz: int = Field(ge=MIN_FREQUENCY_HZ, le=MAX_FREQUENCY_HZ)
    band: Literal["SW", "AM", "FM"]


@app.post("/api/tune")
def tune(req: TuneRequest) -> dict:
    radio = _require_bridge()
    scan_cancel.set()  # a manual click always wins over a running scan
    landed = radio.tune_to(req.frequency_hz, req.band)
    logger.debug("tune: %d Hz band=%s ok=%s", req.frequency_hz, req.band, landed)
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
    freq_hz: int = Field(ge=MIN_FREQUENCY_HZ, le=MAX_FREQUENCY_HZ)
    band: Literal["SW", "AM", "FM"]


class ScanRequest(BaseModel):
    stations: list[ScanTarget] = Field(min_length=1, max_length=200)
    settle_s: float = Field(default=0.7, ge=0.05, le=10.0)


@app.post("/api/scan/cancel")
def cancel_scan() -> dict[str, bool]:
    scan_cancel.set()
    return {"ok": True}


@app.post("/api/scan")
def scan(req: ScanRequest) -> dict:
    """Tune through candidates and record noise-relative signal strength. Each
    tune is a direct frequency set (no band search), so this is limited
    only by the settle delay between measurements. Duplicate frequencies
    (many stations share the same carrier) are measured once."""
    radio = _require_bridge()
    scan_cancel.clear()
    seen: set[int] = set()
    results = []
    requested = len(req.stations)
    targets = req.stations
    started_at = time.monotonic()
    logger.info(
        "scan: starting %d targets (requested %d, settle_s=%.2f)",
        len(targets), requested, req.settle_s,
    )
    skipped_duplicate = 0
    skipped_mismatch = 0
    for target in targets:
        if scan_cancel.is_set():
            logger.info("scan: cancelled by a manual tune, stopping early")
            break  # a manual tune took over; stop dragging the radio around
        if target.freq_hz in seen:
            skipped_duplicate += 1
            continue
        seen.add(target.freq_hz)
        radio.tune_to(target.freq_hz, target.band)
        time.sleep(req.settle_s)
        if scan_cancel.is_set():
            logger.info("scan: cancelled by a manual tune, stopping early")
            break
        st = radio.latest_status()
        if st is None or st.frequency_hz != target.freq_hz:
            skipped_mismatch += 1
            logger.debug(
                "scan: skipped %d Hz - status mismatch (got %s)",
                target.freq_hz, st.frequency_hz if st else None,
            )
            continue
        entry = {
            "signal_db": st.signal_db,
            "noise_dbfs": st.noise_dbfs,
            "measured_at": time.time(),
        }
        signal_cache[target.freq_hz] = entry
        _persist_signal(target.freq_hz, entry)
        results.append({"freq_hz": target.freq_hz, **entry})
        logger.debug(
            "scan: %d Hz signal=%d dB noise=%d dBFS",
            target.freq_hz,
            st.signal_db,
            st.noise_dbfs,
        )
    logger.info(
        "scan: done - %d measured, %d duplicate, %d mismatch, %.1fs elapsed, cancelled=%s",
        len(results), skipped_duplicate, skipped_mismatch,
        time.monotonic() - started_at, scan_cancel.is_set(),
    )
    return {
        "scanned": len(results),
        "results": results,
        "cancelled": scan_cancel.is_set(),
    }


class CwListenRequest(BaseModel):
    seconds: float = Field(default=4.0, ge=0.1, le=60.0)


@app.post("/api/cw/listen")
def cw_listen(req: CwListenRequest) -> dict:
    """Capture from the SDR's current demodulated stream and try to decode
    CW, without touching the tuned frequency - useful to check whatever's
    currently playing."""
    radio = _require_bridge()
    result = cw_decoder.listen(radio, seconds=req.seconds)
    return {"device": "rtl-sdr", **result.__dict__}


class CwTarget(BaseModel):
    freq_hz: int = Field(ge=MIN_FREQUENCY_HZ, le=MAX_FREQUENCY_HZ)
    band_label: str = Field(min_length=1, max_length=32)
    mode: Literal["USB", "LSB"] = "USB"


class CwScanRequest(BaseModel):
    targets: list[CwTarget] | None = Field(default=None, min_length=1, max_length=200)
    listen_seconds: float = Field(default=4.0, ge=0.1, le=60.0)
    stop_on_first_hit: bool = True


# Known-active CW frequencies: propagation beacons and calling frequencies
# with a high enough duty cycle to reliably test reception against. The
# band_label field is informational only (RTL-SDR tunes directly, with no
# band list to navigate).
_DEFAULT_CW_TARGETS = [
    # DK0WCY: transmits real CW during most of every 10-minute cycle (only
    # RTTY/PSK31 at :10/:50), not a brief shared slot - far higher duty
    # cycle than the beacon network below, so tried first.
    CwTarget(freq_hz=10144000, band_label="30M", mode="LSB"),
    # NCDXF/IARU beacon network: 18 stations round-robin their callsign in
    # CW every 10s (full cycle ~3 min), continuously, specifically so
    # anyone can test reception - far higher odds of a real decode than
    # waiting for unpredictable ham QSO traffic on the calling frequencies.
    CwTarget(freq_hz=14100000, band_label="20M", mode="USB"),
    CwTarget(freq_hz=28200000, band_label="10M", mode="USB"),
    # GB3RAL: a single station transmitting continuously (not round-robin),
    # so a higher duty cycle than the NCDXF network on the same band.
    CwTarget(freq_hz=28215000, band_label="10M", mode="USB"),
    CwTarget(freq_hz=14030000, band_label="20M", mode="USB"),
    CwTarget(freq_hz=28030000, band_label="10M", mode="USB"),
]


@app.post("/api/cw/scan")
def cw_scan(req: CwScanRequest) -> dict:
    """Tune through candidate CW frequencies, listen on each, and decode
    any that show clean on/off keying. Stops at the first hit by default -
    each listen takes several seconds, so scanning many candidates is slow.
    """
    radio = _require_bridge()

    scan_cancel.clear()
    targets = req.targets or _DEFAULT_CW_TARGETS
    results = []
    for target in targets:
        if scan_cancel.is_set():
            break
        if not radio.tune_cw(target.freq_hz, target.band_label, target.mode):
            continue  # preempted by a concurrent manual tune, or band not found
        time.sleep(0.5)  # let the CW demodulator settle before recording
        if scan_cancel.is_set():
            break
        result = cw_decoder.listen(radio, seconds=req.listen_seconds)
        results.append(
            {
                "freq_hz": target.freq_hz,
                "band_label": target.band_label,
                **result.__dict__,
            }
        )
        if result.is_cw and req.stop_on_first_hit:
            break
    return {"results": results, "cancelled": scan_cancel.is_set()}


class MonitorStartRequest(BaseModel):
    targets: list[CwTarget] | None = Field(default=None, min_length=1, max_length=200)


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

    targets = [
        MonitorCwTarget(t.freq_hz, t.band_label, t.mode)
        for t in (req.targets or _DEFAULT_CW_TARGETS)
    ]
    if cw_monitor is not None:
        cw_monitor.stop()
    cw_monitor = CwMonitor(radio, targets, manual_override=scan_cancel)
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


@app.websocket("/ws/audio")
async def ws_audio(websocket: WebSocket) -> None:
    """Live demodulated audio for the browser to play - the RTL-SDR
    equivalent of the old local speaker passthrough. Any number of clients
    can connect at once; it runs alongside the CW monitor without
    contention since both just read the same underlying stream."""
    if bridge is None:
        await websocket.close(code=1013)  # 1013 = try again later
        return
    await serve_audio_websocket(websocket, bridge)


@app.get("/api/status")
def status() -> dict:
    radio = _require_bridge()
    st = radio.latest_status()
    return asdict(st) if st else {}


@app.get("/api/status/stream")
async def status_stream(request: Request) -> StreamingResponse:
    radio = _require_bridge()

    async def gen():
        last_seq = None
        while True:
            if await request.is_disconnected():
                break
            st = radio.latest_status()
            if st is not None and st.seq != last_seq:
                last_seq = st.seq
                yield f"data: {json.dumps(asdict(st))}\n\n"
            await asyncio.sleep(0.15)

    return StreamingResponse(gen(), media_type="text/event-stream")
