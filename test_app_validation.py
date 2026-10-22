"""Boundary tests for API request validation and health reporting."""

from __future__ import annotations

import sqlite3

from fastapi import Response
from pydantic import BaseModel, ValidationError

import app


def _assert_invalid(model: type[BaseModel], **values: object) -> None:
    try:
        model(**values)
    except ValidationError:
        return
    raise AssertionError(f"{model.__name__} accepted invalid input: {values!r}")


def test_tune_request_rejects_invalid_frequency_and_band() -> None:
    _assert_invalid(app.TuneRequest, frequency_hz=0, band="FM")
    _assert_invalid(app.TuneRequest, frequency_hz=100_000_000, band="invalid")


def test_scan_request_bounds_work_and_duration() -> None:
    target = app.ScanTarget(freq_hz=100_000_000, band="FM")
    _assert_invalid(app.ScanRequest, stations=[])
    _assert_invalid(app.ScanRequest, stations=[target], settle_s=10.1)
    _assert_invalid(app.ScanRequest, stations=[target] * 201)


def test_cw_requests_reject_empty_or_unbounded_work() -> None:
    _assert_invalid(app.CwListenRequest, seconds=60.1)
    _assert_invalid(app.CwTarget, freq_hz=10_144_000, band_label="30M", mode="AM")
    _assert_invalid(app.CwScanRequest, targets=[])
    _assert_invalid(app.MonitorStartRequest, targets=[])


def test_health_reports_radio_readiness() -> None:
    response = Response()
    original_bridge = app.bridge
    try:
        app.bridge = None
        assert app.health(response) == {"ok": False, "radio_connected": False}
        assert response.status_code == 503

        app.bridge = object()  # type: ignore[assignment]
        response = Response()
        assert app.health(response) == {"ok": True, "radio_connected": True}
        assert response.status_code == 200
    finally:
        app.bridge = original_bridge


def test_station_signal_sort_is_numeric_global_and_unscanned_last(tmp_path) -> None:
    stations_path = tmp_path / "stations.db"
    signal_path = tmp_path / "signal.db"
    with sqlite3.connect(stations_path) as conn:
        conn.execute(
            "CREATE TABLE stations (freq_hz INTEGER, band TEXT, name TEXT, country TEXT, "
            "mode TEXT, time_range TEXT, days_code TEXT, language TEXT, source TEXT)"
        )
        conn.executemany(
            "INSERT INTO stations VALUES (?, 'FM', ?, 'GB', 'FM', '', '', 'English', 'test')",
            [
                (90_000_000, "Unscanned"),
                (91_000_000, "Weak"),
                (92_000_000, "Strong"),
                (94_000_000, "Silent spur"),
            ],
        )
        conn.execute(
            "INSERT INTO stations VALUES "
            "(93000000, 'FM', 'Ofcom', 'London', 'FM', '', '', '', 'ofcom_fm')"
        )
    with sqlite3.connect(signal_path) as conn:
        conn.execute(
            "CREATE TABLE signal_measurements (freq_hz INTEGER PRIMARY KEY, signal_db INTEGER, "
            "noise_dbfs INTEGER, audio_dbfs INTEGER, measured_at REAL)"
        )
        conn.executemany(
            "INSERT INTO signal_measurements VALUES (?, ?, -50, -20, 1.0)",
            [(91_000_000, 7), (92_000_000, 21)],
        )
        conn.execute(
            "INSERT INTO signal_measurements VALUES (94000000, 30, -50, -120, 1.0)"
        )

    original_db_path = app.DB_PATH
    original_signal_path = app.SIGNAL_DB_PATH
    try:
        app.DB_PATH = stations_path
        app.SIGNAL_DB_PATH = signal_path
        descending = app.list_stations("", "", "", 10, 0, "signal", "desc")["stations"]
        ascending = app.list_stations("", "", "", 10, 0, "signal", "asc")["stations"]
        uk_stations = app.list_stations("", "", "GB", 10, 0, "frequency", "asc")["stations"]
        countries = app.list_countries()
        scan_targets = app.list_scan_targets()
    finally:
        app.DB_PATH = original_db_path
        app.SIGNAL_DB_PATH = original_signal_path

    assert [station["name"] for station in descending] == [
        "Strong", "Weak", "Silent spur", "Unscanned", "Ofcom"
    ]
    assert [station["name"] for station in ascending] == [
        "Weak", "Strong", "Silent spur", "Unscanned", "Ofcom"
    ]
    assert [(station["name"], station["country"]) for station in uk_stations] == [
        ("Unscanned", "GB"),
        ("Weak", "GB"),
        ("Strong", "GB"),
        ("Ofcom", "GB"),
        ("Silent spur", "GB"),
    ]
    assert countries == [{"code": "GB", "name": "United Kingdom"}]
    assert scan_targets == [
        {"freq_hz": 90_000_000, "band": "FM"},
        {"freq_hz": 91_000_000, "band": "FM"},
        {"freq_hz": 92_000_000, "band": "FM"},
        {"freq_hz": 93_000_000, "band": "FM"},
        {"freq_hz": 94_000_000, "band": "FM"},
    ]


def test_scan_cancel_sets_shared_cancellation_event() -> None:
    app.scan_cancel.clear()
    try:
        assert app.cancel_scan() == {"ok": True}
        assert app.scan_cancel.is_set()
    finally:
        app.scan_cancel.clear()


def test_scan_status_exposes_operator_progress() -> None:
    with app.scan_progress_lock:
        original = app.scan_progress.copy()
        app.scan_progress.update(
            running=True,
            processed=7,
            total=20,
            frequency_hz=98_800_000,
            band="FM",
            phase="sampling",
            started_at=1.0,
        )
    try:
        status = app.scan_status()
        assert status["running"] is True
        assert status["processed"] == 7
        assert status["total"] == 20
        assert status["frequency_hz"] == 98_800_000
        assert status["band"] == "FM"
        assert status["phase"] == "sampling"
        elapsed_s = status["elapsed_s"]
        assert isinstance(elapsed_s, (int, float))
        assert elapsed_s > 0
    finally:
        with app.scan_progress_lock:
            app.scan_progress.clear()
            app.scan_progress.update(original)


def test_viability_uses_band_specific_rf_thresholds_and_rejects_silence() -> None:
    assert app._is_viable("FM", signal_db=6, audio_dbfs=-20)
    assert not app._is_viable("FM", signal_db=5, audio_dbfs=-20)
    assert app._is_viable("AM", signal_db=10, audio_dbfs=-20)
    assert app._is_viable("SW", signal_db=10, audio_dbfs=-20)
    assert not app._is_viable("SW", signal_db=9, audio_dbfs=-20)
    assert not app._is_viable("FM", signal_db=30, audio_dbfs=-120)


def test_scan_targets_dedupe_conflicting_band_labels(tmp_path) -> None:
    stations_path = tmp_path / "stations.db"
    with sqlite3.connect(stations_path) as conn:
        conn.execute(
            "CREATE TABLE stations (freq_hz INTEGER, band TEXT, name TEXT, country TEXT, "
            "mode TEXT, time_range TEXT, days_code TEXT, language TEXT, source TEXT)"
        )
        conn.executemany(
            "INSERT INTO stations VALUES (963000, ?, ?, 'GB', 'AM', '', '', '', 'test')",
            [("SW", "Schedule label"), ("AM", "Broadcast label")],
        )
        conn.execute(
            "INSERT INTO stations VALUES "
            "(100000000, 'FM', 'FM target', 'US', 'FM', '', '', '', 'test')"
        )

    original_path = app.DB_PATH
    try:
        app.DB_PATH = stations_path
        assert app.list_scan_targets() == [
            {"freq_hz": 963_000, "band": "AM"},
            {"freq_hz": 100_000_000, "band": "FM"},
        ]
    finally:
        app.DB_PATH = original_path


def test_signal_store_migrates_existing_measurements_with_unknown_audio(tmp_path) -> None:
    signal_path = tmp_path / "legacy-signal.db"
    with sqlite3.connect(signal_path) as conn:
        conn.execute(
            "CREATE TABLE signal_measurements (freq_hz INTEGER PRIMARY KEY, "
            "signal_db INTEGER NOT NULL, noise_dbfs INTEGER NOT NULL, measured_at REAL NOT NULL)"
        )
        conn.execute("INSERT INTO signal_measurements VALUES (963000, 13, -50, 1.0)")

    original_path = app.SIGNAL_DB_PATH
    original_cache = app.signal_cache.copy()
    try:
        app.SIGNAL_DB_PATH = signal_path
        app.signal_cache.clear()
        app._init_signal_store()
        with sqlite3.connect(signal_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(signal_measurements)")}
        assert "audio_dbfs" in columns
        assert app.signal_cache[963_000]["audio_dbfs"] is None
    finally:
        app.SIGNAL_DB_PATH = original_path
        app.signal_cache.clear()
        app.signal_cache.update(original_cache)
