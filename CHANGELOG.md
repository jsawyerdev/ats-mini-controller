# Changelog

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
