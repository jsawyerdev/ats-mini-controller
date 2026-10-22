"""Boundary tests for API request validation and health reporting."""

from __future__ import annotations

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
