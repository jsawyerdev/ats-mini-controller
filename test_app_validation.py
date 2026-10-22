"""Boundary tests for API request validation and health reporting."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import numpy as np
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
    assert app._mirror_frequency(15_245_200) == 13_554_800
    assert app._mirror_frequency(89_300_000) is None


def test_scan_request_bounds_work_and_duration() -> None:
    target = app.ScanTarget(freq_hz=100_000_000, band="FM")
    _assert_invalid(app.ScanRequest, stations=[])
    _assert_invalid(app.ScanRequest, stations=[target], settle_s=10.1)
    _assert_invalid(app.ScanRequest, stations=[target] * 201)
    _assert_invalid(app.PpmRequest, ppm=201)
    _assert_invalid(app.IqRecordingRequest, seconds=0.9)
    _assert_invalid(app.IqRecordingRequest, seconds=10.1)
    _assert_invalid(app.CalibrationRequest, reference_hz=0, band="FM")


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


def test_calibration_proposes_correction_and_restores_reference(monkeypatch) -> None:
    class FakeRadio:
        def __init__(self) -> None:
            self.frequency_hz = 0
            self.tunes = []

        def tune_to(self, frequency_hz: int, band: str) -> bool:
            self.frequency_hz = frequency_hz
            self.tunes.append((frequency_hz, band))
            return True

        def latest_status(self):
            return SimpleNamespace(frequency_hz=self.frequency_hz, ppm=0)

        def estimate_reference(self, expected_offset_hz: int) -> float:
            assert expected_offset_hz == 100_000
            return 100_100

    fake = FakeRadio()
    original_bridge = app.bridge
    try:
        app.bridge = fake
        monkeypatch.setattr(app.time, "sleep", lambda _: None)

        result = app.calibrate(
            app.CalibrationRequest(reference_hz=10_000_000, band="SW")
        )

        assert result["error_hz"] == 100
        assert result["proposed_ppm"] == -10
        assert fake.tunes == [(9_900_000, "SW"), (10_000_000, "SW")]
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
            "noise_dbfs INTEGER, audio_dbfs INTEGER, content_db INTEGER, measured_at REAL)"
        )
        conn.executemany(
            "INSERT INTO signal_measurements VALUES (?, ?, -50, -20, 10, 1.0)",
            [(91_000_000, 7), (92_000_000, 21)],
        )
        conn.execute(
            "INSERT INTO signal_measurements VALUES (94000000, 30, -50, -120, 10, 1.0)"
        )

    original_db_path = app.DB_PATH
    original_signal_path = app.SIGNAL_DB_PATH
    try:
        app.DB_PATH = stations_path
        app.SIGNAL_DB_PATH = signal_path
        descending = app.list_stations("", "", "", 10, 0, "signal", "desc")["stations"]
        ascending = app.list_stations("", "", "", 10, 0, "signal", "asc")["stations"]
        uk_stations = app.list_stations("", "", "GB", 10, 0, "frequency", "asc")[
            "stations"
        ]
        countries = app.list_countries()
        scan_targets = app.list_scan_targets()
    finally:
        app.DB_PATH = original_db_path
        app.SIGNAL_DB_PATH = original_signal_path

    assert [station["name"] for station in descending] == [
        "Strong",
        "Weak",
        "Silent spur",
        "Unscanned",
        "Ofcom",
    ]
    assert [station["name"] for station in ascending] == [
        "Weak",
        "Strong",
        "Silent spur",
        "Unscanned",
        "Ofcom",
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
    assert app._is_viable("FM", signal_db=6, audio_dbfs=-20, content_db=3)
    assert not app._is_viable("FM", signal_db=5, audio_dbfs=-20, content_db=20)
    assert app._is_viable("AM", signal_db=10, audio_dbfs=-20, content_db=3)
    assert app._is_viable("SW", signal_db=10, audio_dbfs=-20, content_db=3)
    assert not app._is_viable("SW", signal_db=9, audio_dbfs=-20, content_db=20)
    assert not app._is_viable("FM", signal_db=30, audio_dbfs=-120, content_db=20)
    assert not app._is_viable("FM", signal_db=30, audio_dbfs=-20, content_db=2)
    assert not app._is_viable("FM", signal_db=30, audio_dbfs=-20, content_db=None)


def test_scan_rejects_rf_candidate_when_audio_is_noise_like(
    tmp_path, monkeypatch
) -> None:
    class FakeRadio:
        def tune_to(self, frequency_hz: int, band: str) -> bool:
            self.frequency_hz = frequency_hz
            return True

        def latest_status(self):
            return SimpleNamespace(
                frequency_hz=self.frequency_hz,
                signal_db=18,
                noise_dbfs=-48,
                audio_dbfs=-42,
            )

        def capture_audio(self, seconds: float):
            assert seconds == app.CONTENT_CAPTURE_S
            return np.ones(72_000, dtype=np.float32)

    signal_path = tmp_path / "signal.db"
    original_bridge = app.bridge
    original_path = app.SIGNAL_DB_PATH
    original_cache = app.signal_cache.copy()
    try:
        app.bridge = FakeRadio()
        app.SIGNAL_DB_PATH = signal_path
        app.signal_cache.clear()
        app._init_signal_store()
        monkeypatch.setattr(app.time, "sleep", lambda _: None)
        monkeypatch.setattr(app, "audio_content_db", lambda *_: 0)

        result = app.scan(
            app.ScanRequest(stations=[app.ScanTarget(freq_hz=15_245_200, band="SW")])
        )

        assert result["results"][0]["content_db"] == 0
        assert result["results"][0]["viable"] is False
        with sqlite3.connect(signal_path) as conn:
            stored = conn.execute(
                "SELECT content_db FROM signal_measurements WHERE freq_hz = 15245200"
            ).fetchone()
        assert stored == (0,)
    finally:
        app.bridge = original_bridge
        app.SIGNAL_DB_PATH = original_path
        app.signal_cache.clear()
        app.signal_cache.update(original_cache)
        app.scan_cancel.clear()


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


def test_signal_store_migrates_existing_measurements_with_unknown_audio(
    tmp_path,
) -> None:
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
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(signal_measurements)")
            }
        assert "audio_dbfs" in columns
        assert "content_db" in columns
        assert app.signal_cache[963_000]["audio_dbfs"] is None
        assert app.signal_cache[963_000]["content_db"] is None
    finally:
        app.SIGNAL_DB_PATH = original_path
        app.signal_cache.clear()
        app.signal_cache.update(original_cache)
