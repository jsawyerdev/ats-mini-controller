# Changelog

## 2.4.0 - 2026-10-22

### Fixed

- Audible-station ranking now requires demodulated audio as well as RF energy, preventing silent
  carriers and receiver spurs from being presented as the strongest listenable station.
- Recovers the RTL-SDR after a failed direct-sampling/normal-tuner mode transition instead of
  leaving the audio stream stalled.
- Full-scan targets are now unique by frequency even when source records disagree on the band.

### Added

- Added live per-frequency scan progress.
- Manual tuning during a scan now asks before stopping the scan; declining leaves it running.

## 2.3.0 - 2026-10-22

### Added

- Added a stoppable **Scan all (filtered)** workflow that checks every unique frequency matching
  the current text, band, and country filters in bounded batches, updating signal strength and
  last-checked timestamps as it progresses.

## 2.2.0 - 2026-10-22

### Added

- Added sortable last-checked timestamps backed by the existing persisted measurement time.
- Added an explicit country filter and local Unicode country flags without a runtime CDN.
- Added a documented audit of the bundled station database's coverage and known gaps.

### Fixed

- Normalized Ofcom records to the United Kingdom instead of presenting their service area as a
  country.

## 2.1.0 - 2026-10-22

### Added

- Added server-side sorting for frequency, band, name, country, language, and signal columns.
  Signal sorting ranks all filtered scanned stations numerically and always leaves unscanned
  stations last.

## 2.0.1 - 2026-10-22

### Fixed

- Restored the documented INFO production log level instead of forcing high-volume SDR sample
  diagnostics through Docker Compose.

## 2.0.0 - 2026-10-22

### Changed

- Replaced ATS Mini serial control and external audio capture with direct RTL-SDR tuning,
  demodulation, browser audio streaming, and CW monitoring.
- Replaced receiver RSSI ranking with a noise-relative spectral score. The scanner now rejects
  the RTL-SDR centre spike, smooths multiple windows, and only identifies stations at least 6 dB
  above nearby spectrum as clear signals.
- Renamed the scan API fields from `rssi` and `snr` to `signal_db` and `noise_dbfs`. Measurements
  now use `data/signal_cache.db`; incompatible legacy RSSI measurements are intentionally ignored.
- Added strict request bounds, a radio-aware health endpoint, and application lifespan cleanup.
- Updated and pinned the Python runtime stack, including the maintained RTL-SDR wrapper and
  bundled native library.
- Hardened the container to run as a non-root user with a read-only root filesystem, no Linux
  capabilities, and no-new-privileges.

### Added

- Deterministic signal-estimator regression tests covering noise, DC offsets, in-channel
  carriers, and adjacent-channel rejection.
- API boundary and health-state tests.

### Deployment notes

- This is a breaking hardware and API release. It requires an RTL2832U-based SDR dongle and does
  not support the ATS Mini serial protocol.
- Port 8731 has no application authentication and must remain on a trusted network or behind an
  authenticated reverse proxy.
