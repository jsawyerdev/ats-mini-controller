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
            [(90_000_000, "Unscanned"), (91_000_000, "Weak"), (92_000_000, "Strong")],
        )
        conn.execute(
            "INSERT INTO stations VALUES "
            "(93000000, 'FM', 'Ofcom', 'London', 'FM', '', '', '', 'ofcom_fm')"
        )
    with sqlite3.connect(signal_path) as conn:
        conn.execute(
            "CREATE TABLE signal_measurements (freq_hz INTEGER PRIMARY KEY, signal_db INTEGER, "
            "noise_dbfs INTEGER, measured_at REAL)"
        )
        conn.executemany(
            "INSERT INTO signal_measurements VALUES (?, ?, -50, 1.0)",
            [(91_000_000, 7), (92_000_000, 21)],
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
    finally:
        app.DB_PATH = original_db_path
        app.SIGNAL_DB_PATH = original_signal_path

    assert [station["name"] for station in descending] == [
        "Strong", "Weak", "Unscanned", "Ofcom"
    ]
    assert [station["name"] for station in ascending] == [
        "Weak", "Strong", "Unscanned", "Ofcom"
    ]
    assert [(station["name"], station["country"]) for station in uk_stations] == [
        ("Unscanned", "GB"), ("Weak", "GB"), ("Strong", "GB"), ("Ofcom", "GB")
    ]
    assert countries == [{"code": "GB", "name": "United Kingdom"}]
