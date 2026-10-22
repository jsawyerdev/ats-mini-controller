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
import stat
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

import cw_decoder
from audio_passthrough import serve_audio_websocket
from cw_monitor import CwMonitor
from cw_monitor import CwTarget as MonitorCwTarget
from radio_bridge import (
    AM_AUDIO_BW_HZ,
    DIRECT_SAMPLING_THRESHOLD_HZ,
    FM_AUDIO_BW_HZ,
    IQ_SAMPLE_RATE,
    SUPPORTED_GAIN_DB,
    RtlSdrBridge,
    audio_content_db,
    find_rtlsdr_device,
)


def configure_logging() -> None:
    """Level is set via the LOG_LEVEL env var (default INFO) rather than
    hardcoded, so verbose per-target scan/tune tracing can be turned on for
    debugging (`docker compose run -e LOG_LEVEL=DEBUG` or set it in
    compose.yaml) without a code change."""
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )


configure_logging()
logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent / "data" / "stations.db"
SIGNAL_DB_PATH = Path(__file__).parent / "data" / "signal_cache.db"
RECORDINGS_PATH = Path(__file__).parent / "data" / "recordings"
APP_VERSION = "2.8.3"

MIN_VIABLE_FM_SIGNAL_DB = 6
MIN_VIABLE_DIRECT_SIGNAL_DB = 10
MIN_AUDIBLE_DBFS = -50
AUDIO_CONTENT_THRESHOLD_DB = 3
CONTENT_CAPTURE_S = 1.5
CONTENT_EXTENDED_CAPTURE_S = 3.0
IQ_RECORDING_QUOTA_BYTES = 512 * 1024 * 1024

COUNTRY_NAMES = {"CA": "Canada", "GB": "United Kingdom", "US": "United States"}


def _is_rf_candidate(band: str, signal_db: int, audio_dbfs: int) -> bool:
    minimum_signal = (
        MIN_VIABLE_FM_SIGNAL_DB if band == "FM" else MIN_VIABLE_DIRECT_SIGNAL_DB
    )
    return signal_db >= minimum_signal and audio_dbfs >= MIN_AUDIBLE_DBFS


def _is_viable(
    band: str, signal_db: int, audio_dbfs: int, content_db: int | None
) -> bool:
    return (
        _is_rf_candidate(band, signal_db, audio_dbfs)
        and content_db is not None
        and content_db >= AUDIO_CONTENT_THRESHOLD_DB
    )


def _viable_sql() -> str:
    return (
        f"m.audio_dbfs >= {MIN_AUDIBLE_DBFS} AND "
        f"m.content_db >= {AUDIO_CONTENT_THRESHOLD_DB} AND "
        f"((s.band = 'FM' AND m.signal_db >= {MIN_VIABLE_FM_SIGNAL_DB}) OR "
        f"(s.band <> 'FM' AND m.signal_db >= {MIN_VIABLE_DIRECT_SIGNAL_DB}))"
    )


def _country_sql(alias: str = "s") -> str:
    return f"CASE WHEN {alias}.source LIKE 'ofcom_%' THEN 'GB' ELSE {alias}.country END"


def _mirror_frequency(freq_hz: int) -> int | None:
    if freq_hz >= DIRECT_SAMPLING_THRESHOLD_HZ:
        return None
    return DIRECT_SAMPLING_THRESHOLD_HZ - freq_hz


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
            audio_dbfs INTEGER,
            content_db INTEGER,
            measured_at REAL NOT NULL
        )
        """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(signal_measurements)")}
    if "audio_dbfs" not in columns:
        conn.execute("ALTER TABLE signal_measurements ADD COLUMN audio_dbfs INTEGER")
    if "content_db" not in columns:
        conn.execute("ALTER TABLE signal_measurements ADD COLUMN content_db INTEGER")
    conn.commit()
    for (
        freq_hz,
        signal_db,
        noise_dbfs,
        audio_dbfs,
        content_db,
        measured_at,
    ) in conn.execute(
        "SELECT freq_hz, signal_db, noise_dbfs, audio_dbfs, content_db, measured_at "
        "FROM signal_measurements"
    ):
        signal_cache[freq_hz] = {
            "signal_db": signal_db,
            "noise_dbfs": noise_dbfs,
            "audio_dbfs": audio_dbfs,
            "content_db": content_db,
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
                "(freq_hz, signal_db, noise_dbfs, audio_dbfs, content_db, measured_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    freq_hz,
                    entry["signal_db"],
                    entry["noise_dbfs"],
                    entry["audio_dbfs"],
                    entry["content_db"],
                    entry["measured_at"],
                ),
            )
    finally:
        conn.close()


def _connect_radio() -> None:
    global bridge
    _init_signal_store()
    if not find_rtlsdr_device():
        logger.error(
            "RTL-SDR dongle not found on any USB port; tuning endpoints will fail"
        )
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
scan_progress_lock = threading.Lock()
scan_progress: dict[str, int | float | str | bool | None] = {
    "running": False,
    "processed": 0,
    "total": 0,
    "frequency_hz": None,
    "band": None,
    "phase": "idle",
    "started_at": None,
    "elapsed_s": 0.0,
}

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
        if sort == "signal":
            missing_last = f"({_viable_sql()}) DESC, m.signal_db IS NULL ASC, "
        elif sort == "last_checked":
            missing_last = "m.measured_at IS NULL ASC, "
        else:
            missing_last = ""
        rows = conn.execute(
            "SELECT s.*, m.signal_db, m.noise_dbfs, m.audio_dbfs, m.content_db, "
            f"({_viable_sql()}) AS viable, m.measured_at "
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
        mirror_freq_hz = _mirror_frequency(d["freq_hz"])
        if mirror_freq_hz is not None:
            d["mirror_freq_hz"] = mirror_freq_hz
            mirror = signal_cache.get(mirror_freq_hz)
            d["mirror_signal_db"] = mirror["signal_db"] if mirror else None
        else:
            d["mirror_freq_hz"] = None
            d["mirror_signal_db"] = None
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
def list_scan_targets() -> list[dict]:
    """Return every catalogue frequency once so a full scan does no duplicate RF work."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT s.freq_hz, MIN(s.band) FROM stations s "
            "GROUP BY s.freq_hz ORDER BY s.freq_hz"
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


class GainRequest(BaseModel):
    gain_db: float


@app.post("/api/radio/gain")
def set_gain(req: GainRequest) -> dict:
    radio = _require_bridge()
    if req.gain_db not in SUPPORTED_GAIN_DB:
        raise HTTPException(
            status_code=422,
            detail=f"gain_db must be one of {', '.join(map(str, SUPPORTED_GAIN_DB))}",
        )
    radio.set_gain(req.gain_db)
    return {"ok": True, "gain_db": req.gain_db}


class PpmRequest(BaseModel):
    ppm: int = Field(ge=-200, le=200)


@app.post("/api/radio/ppm")
def set_ppm(req: PpmRequest) -> dict:
    radio = _require_bridge()
    radio.set_frequency_correction(req.ppm)
    return {"ok": True, "ppm": req.ppm}


class CalibrationRequest(BaseModel):
    reference_hz: int = Field(ge=MIN_FREQUENCY_HZ, le=MAX_FREQUENCY_HZ)
    band: Literal["SW", "AM", "FM"]
    settle_s: float = Field(default=1.5, ge=0.5, le=5.0)


@app.post("/api/radio/calibrate")
def calibrate(req: CalibrationRequest) -> dict:
    radio = _require_bridge()
    offset_hz = 100_000
    centre_hz = req.reference_hz - offset_hz
    if centre_hz < MIN_FREQUENCY_HZ:
        raise HTTPException(status_code=422, detail="reference frequency is too low")
    scan_cancel.set()
    radio.tune_to(centre_hz, req.band)
    time.sleep(req.settle_s)
    try:
        status = radio.latest_status()
        if status is None or status.frequency_hz != centre_hz:
            raise HTTPException(
                status_code=503, detail="radio did not settle on the reference"
            )
        measured_offset_hz = radio.estimate_reference(offset_hz)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        radio.tune_to(req.reference_hz, req.band)
    error_hz = measured_offset_hz - offset_hz
    current_ppm = status.ppm
    proposed_ppm = round(current_ppm - error_hz * 1_000_000 / req.reference_hz)
    return {
        "reference_hz": req.reference_hz,
        "measured_hz": round(centre_hz + measured_offset_hz),
        "error_hz": round(error_hz),
        "current_ppm": current_ppm,
        "proposed_ppm": max(-200, min(200, proposed_ppm)),
        "applied": False,
    }


@app.get("/api/spectrum")
def spectrum() -> dict:
    radio = _require_bridge()
    status = radio.latest_status()
    return {
        "frequency_hz": status.frequency_hz if status else None,
        "span_hz": IQ_SAMPLE_RATE,
        "power_dbfs": radio.spectrum(),
    }


@app.get("/api/spectrum/stream")
async def spectrum_stream(request: Request) -> StreamingResponse:
    async def gen():
        while not await request.is_disconnected():
            yield f"data: {json.dumps(spectrum(), separators=(',', ':'))}\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(gen(), media_type="text/event-stream")


class IqRecordingRequest(BaseModel):
    seconds: float = Field(default=5.0, ge=1.0, le=10.0)


@app.post("/api/iq/record")
def record_iq(req: IqRecordingRequest) -> dict:
    radio = _require_bridge()
    status = radio.latest_status()
    if status is None:
        raise HTTPException(status_code=503, detail="radio has no current status")
    RECORDINGS_PATH.mkdir(parents=True, exist_ok=True)
    used_bytes = 0
    with os.scandir(RECORDINGS_PATH) as entries:
        for entry in entries:
            file_stat = entry.stat(follow_symlinks=False)
            if stat.S_ISREG(file_stat.st_mode):
                used_bytes += file_stat.st_size
    expected_bytes = round(req.seconds * IQ_SAMPLE_RATE * 2)
    if used_bytes + expected_bytes > IQ_RECORDING_QUOTA_BYTES:
        raise HTTPException(status_code=507, detail="IQ recording quota exceeded")
    stem = f"{time.time_ns()}-{status.frequency_hz}Hz"
    data_path = RECORDINGS_PATH / f"{stem}.sigmf-data"
    meta_path = RECORDINGS_PATH / f"{stem}.sigmf-meta"
    output = data_path.open("xb")
    try:
        samples = radio.record_iq(output, req.seconds)
        finished_status = radio.latest_status()
        if (
            finished_status is None
            or finished_status.frequency_hz != status.frequency_hz
        ):
            raise RuntimeError("radio retuned during IQ recording")
        metadata = {
            "global": {
                "core:datatype": "ci8",
                "core:sample_rate": IQ_SAMPLE_RATE,
                "core:version": "1.2.0",
                "recorder": f"ats-mini-controller {APP_VERSION}",
            },
            "captures": [
                {
                    "core:sample_start": 0,
                    "core:frequency": status.frequency_hz,
                    "gain_db": status.gain_db,
                    "ppm": status.ppm,
                    "direct_sampling": status.direct_sampling,
                }
            ],
            "annotations": [],
        }
        meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
    except (OSError, RuntimeError, TimeoutError) as exc:
        if not output.closed:
            output.close()
        data_path.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {
        "samples": samples,
        "seconds": samples / IQ_SAMPLE_RATE,
        "data_file": data_path.name,
        "metadata_file": meta_path.name,
    }


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


@app.get("/api/scan/status")
def scan_status() -> dict[str, int | float | str | bool | None]:
    with scan_progress_lock:
        status = dict(scan_progress)
    started_at = status["started_at"]
    if status["running"] and isinstance(started_at, float):
        status["elapsed_s"] = max(0.0, time.time() - started_at)
    return status


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
    with scan_progress_lock:
        scan_progress.update(
            running=True,
            processed=0,
            total=len(targets),
            frequency_hz=None,
            band=None,
            phase="starting",
            started_at=time.time(),
            elapsed_s=0.0,
        )
    started_at = time.monotonic()
    logger.info(
        "scan: starting %d targets (requested %d, settle_s=%.2f)",
        len(targets),
        requested,
        req.settle_s,
    )
    skipped_duplicate = 0
    skipped_mismatch = 0
    processed_count = 0
    for target in targets:
        if scan_cancel.is_set():
            logger.info("scan: cancelled by a manual tune, stopping early")
            break  # a manual tune took over; stop dragging the radio around
        if target.freq_hz in seen:
            skipped_duplicate += 1
            processed_count += 1
            with scan_progress_lock:
                scan_progress["processed"] = processed_count
            continue
        seen.add(target.freq_hz)
        with scan_progress_lock:
            scan_progress.update(
                frequency_hz=target.freq_hz, band=target.band, phase="tuning"
            )
        radio.tune_to(target.freq_hz, target.band)
        with scan_progress_lock:
            scan_progress["phase"] = "sampling"
        time.sleep(req.settle_s)
        if scan_cancel.is_set():
            logger.info("scan: cancelled by a manual tune, stopping early")
            break
        st = radio.latest_status()
        if st is None or st.frequency_hz != target.freq_hz:
            skipped_mismatch += 1
            logger.debug(
                "scan: skipped %d Hz - status mismatch (got %s)",
                target.freq_hz,
                st.frequency_hz if st else None,
            )
            processed_count += 1
            with scan_progress_lock:
                scan_progress["processed"] = processed_count
            continue
        content_db = None
        if _is_rf_candidate(target.band, st.signal_db, st.audio_dbfs):
            with scan_progress_lock:
                scan_progress["phase"] = "analysing audio"
            captured_audio = radio.capture_audio(CONTENT_CAPTURE_S)
            if scan_cancel.is_set():
                logger.info("scan: cancelled during audio analysis")
                break
            current = radio.latest_status()
            if current is None or current.frequency_hz != target.freq_hz:
                skipped_mismatch += 1
                logger.debug(
                    "scan: skipped %d Hz - retuned during audio analysis",
                    target.freq_hz,
                )
                processed_count += 1
                with scan_progress_lock:
                    scan_progress["processed"] = processed_count
                continue
            content_db = audio_content_db(
                captured_audio,
                FM_AUDIO_BW_HZ if target.band == "FM" else AM_AUDIO_BW_HZ,
            )
            if 1 <= content_db <= 4:
                extra_audio = radio.capture_audio(CONTENT_EXTENDED_CAPTURE_S)
                if scan_cancel.is_set():
                    logger.info("scan: cancelled during extended audio analysis")
                    break
                current = radio.latest_status()
                if current is None or current.frequency_hz != target.freq_hz:
                    skipped_mismatch += 1
                    logger.debug(
                        "scan: skipped %d Hz - retuned during extended audio analysis",
                        target.freq_hz,
                    )
                    processed_count += 1
                    with scan_progress_lock:
                        scan_progress["processed"] = processed_count
                    continue
                content_db = audio_content_db(
                    np.concatenate((captured_audio, extra_audio)),
                    FM_AUDIO_BW_HZ if target.band == "FM" else AM_AUDIO_BW_HZ,
                )
        entry = {
            "signal_db": st.signal_db,
            "noise_dbfs": st.noise_dbfs,
            "audio_dbfs": st.audio_dbfs,
            "content_db": content_db,
            "viable": _is_viable(target.band, st.signal_db, st.audio_dbfs, content_db),
            "measured_at": time.time(),
        }
        signal_cache[target.freq_hz] = entry
        _persist_signal(target.freq_hz, entry)
        results.append({"freq_hz": target.freq_hz, **entry})
        logger.debug(
            "scan: %d Hz signal=%d dB noise=%d dBFS audio=%d dBFS content=%s dB",
            target.freq_hz,
            st.signal_db,
            st.noise_dbfs,
            st.audio_dbfs,
            content_db,
        )
        processed_count += 1
        with scan_progress_lock:
            scan_progress["processed"] = processed_count
    with scan_progress_lock:
        progress_started_at = scan_progress["started_at"]
        scan_progress.update(
            running=False,
            processed=processed_count,
            frequency_hz=None,
            band=None,
            phase="cancelled" if scan_cancel.is_set() else "complete",
            elapsed_s=(
                max(0.0, time.time() - progress_started_at)
                if isinstance(progress_started_at, float)
                else 0.0
            ),
        )
    logger.info(
        "scan: done - %d measured, %d duplicate, %d mismatch, %.1fs elapsed, cancelled=%s",
        len(results),
        skipped_duplicate,
        skipped_mismatch,
        time.monotonic() - started_at,
        scan_cancel.is_set(),
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


@app.post("/api/cw/self-test")
def cw_self_test() -> dict:
    """Prove the decoder path with synthetic input without claiming RF reception."""
    expected, result = cw_decoder.self_test()
    return {
        "synthetic": True,
        "expected": expected,
        "passed": result.is_cw and result.text == expected,
        **result.__dict__,
    }


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
        "in_progress_morse": state.in_progress_morse,
        "in_progress_wpm": state.in_progress_wpm,
        "in_progress_tone_hz": state.in_progress_tone_hz,
        "in_progress_confidence": state.in_progress_confidence,
        "in_progress_started_at": state.in_progress_started_at,
        "log": [
            {
                "freq_hz": e.freq_hz,
                "started_at": e.started_at,
                "ended_at": e.ended_at,
                "text": e.text,
                "morse": e.morse,
                "wpm": e.wpm,
                "tone_hz": e.tone_hz,
                "confidence": e.confidence,
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
