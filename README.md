# RTL-SDR Station Browser

A web app that tunes a supported RTL-SDR USB dongle in software, with a searchable 50,000+ station database, live signal scanning, and an automatic Morse code (CW) decoder. Originally built around a physical [ATS Mini](https://esp32-si4732.github.io/ats-mini/) receiver controlled over USB serial; rewritten to demodulate AM/FM/CW itself from raw IQ samples, so no dedicated receiver hardware is needed.

Author: **James Sawyer / JSLabs** — [jamessawyer.co.uk](https://www.jamessawyer.co.uk/) | [labs.jamessawyer.co.uk](https://labs.jamessawyer.co.uk/)

![Station browser: 50,000+ stations, live meter, click to tune](docs/screenshots/stations.png)

![Searching and filtering, with on-air stations highlighted](docs/screenshots/search-bbc.png)

![CW Monitor tab scanning a beacon frequency live](docs/screenshots/cw-monitor.png)

## What it does

- **Station browser**: 50,000+ shortwave, AM, and FM records from EiBi (global shortwave schedules), the FCC (US/Canada AM+FM), and Ofcom (UK AM+FM). Search by name, country, or language; filter by band or country; and see which shortwave broadcasts are on air now in your local time.
- **Click to tune**: click any station in the browser and the SDR retunes instantly — a direct frequency set, not a multi-second band search.
- **Signal scanning**: checks all 2,312 unique catalogue frequencies, compares each channel with its nearby RF noise floor, then analyses RF candidates for structured audio so amplified hiss is not presented as useful listening. Results and last-checked times persist across restarts.
- **CW (Morse code) monitor**: cycles known CW-heavy frequencies (propagation beacons, ham calling frequencies), locks onto whichever one has real keying, and decodes it live in the browser — timestamped, frequency-tagged, scrolling — until the signal stops or fades below detection.
- **Live audio**: streams the demodulated audio to your browser over WebSocket and plays it via the Web Audio API — no physical audio cable needed, and it works from any machine on the network, not just whatever's plugged into the radio.
- **Receiver diagnostics**: displays a live bounded-bandwidth waterfall, warns about direct-sampling mirror frequencies, records short SigMF IQ captures, controls FM tuner gain, and provides operator-approved frequency calibration.

## Hardware required

- Any RTL2832U-based RTL-SDR USB dongle.
- FM broadcast (88-108 MHz) is within the tuner IC's normal range on every RTL-SDR dongle. AM/mediumwave (530-1700 kHz) and shortwave (1.6-30 MHz) are both below the tuner's PLL floor (~24-28 MHz) and are received via the RTL2832U's direct-sampling mode instead — a standard chip capability, but whether a given dongle's PCB actually wires that input to a usable antenna path varies by board. **Verify AM/SW reception against a real antenna and a known signal before relying on it**; this can't be confirmed from software alone.
- An antenna suited to the band you're targeting: the dongle's stock antenna is usually adequate for local FM, but shortwave/AM reception benefits substantially from a longer wire antenna.

The deployed Pi has an external antenna connected, but the dongle identifies only as a generic
`0bda:2838 RTL2838UHIDIR`. That does not prove the connector is routed to the Q-branch used for
HF. See the measured signal-path evidence and physical verification plan in
[HARDWARE.md](HARDWARE.md).

## Running it

The project ships a `Dockerfile` and `compose.yaml` — this is the intended way to run it, since it keeps the RTL-SDR driver stack (a blacklisted kernel DVB module, `librtlsdr`, udev permissions) isolated from the host's Python environment:

```bash
docker compose up -d --build
```

The container needs the RTL-SDR dongle passed through (`compose.yaml` already does this via `/dev/bus/usb`) and needs the **host's** kernel DVB drivers (`dvb_usb_rtl28xxu`, `rtl2832_sdr`, `rtl2832`) blacklisted first, since those claim the device before `librtlsdr` in the container ever gets a chance to. On Debian/Ubuntu:

```bash
sudo apt-get install -y rtl-sdr librtlsdr-dev
sudo tee /etc/modprobe.d/blacklist-rtlsdr.conf <<'EOF'
blacklist dvb_usb_rtl28xxu
blacklist rtl2832_sdr
blacklist rtl2832
blacklist rtl2830
blacklist r820t
EOF
sudo reboot
```

Then browse to `http://<host>:8731`.

The container runs as a non-root user with all Linux capabilities dropped, a
read-only root filesystem, and only `./data` writable. The API controls physical
radio state and has no application-level authentication, so expose port 8731 only
on a trusted LAN or behind an authenticated reverse proxy; do not publish it to
the internet.

### Running without Docker (local dev)

```bash
pip install -r requirements.txt
./start.sh
```

`start.sh` warns if no RTL-SDR dongle is detected, starts the backend, and opens `http://127.0.0.1:8731` in your browser. You'll still need the host-side kernel driver blacklist above, or the DVB module will grab the dongle before Python ever tries to open it.

## Rebuilding the station database

`data/stations.db` ships pre-built. To refresh it from source:

```bash
python3 stations_db.py --eibi data/eibi_sked.csv \
  --regulator ofcom_fm:FM:data/ofcom_fm.csv \
  --regulator ofcom_am:AM:data/ofcom_am.csv \
  --fcc-fm data/fcc_fm_raw.txt \
  --fcc-am data/fcc_am_raw.txt
```

Source files aren't included in this repo (the FCC dumps alone are ~23MB); download links are in `stations_db.py`'s docstrings and the EiBi/FCC/Ofcom sites directly.

## How the CW monitor works

Tuning is a direct frequency set with no band list to navigate, so retuning between candidate frequencies is effectively instant - unlike the ATS Mini's relative band-stepping this replaced, there's no visible multi-second hop through intermediate bands.

The radio bridge tunes `tone_hz` off the actual carrier, so a keyed CW signal beats against that offset and produces an audible tone in the demodulated audio exactly when the carrier is present. From there, decoding works by capturing that audio, finding the tone's frequency, isolating it with a bandpass filter, extracting the on/off keying envelope, and converting the timing into dots, dashes, and text. The decoder (`cw_decoder.py`) is a small local implementation on top of numpy/scipy, validated against synthetic Morse test signals in `test_cw_decoder.py`.

## User guide

### Browsing and tuning

The Stations tab shows the full database, 500 rows per page. Click a column heading to sort the complete filtered result set; **Signal** starts strongest-first and **Last checked** shows when that frequency was measured. Type in the search box to filter by name, country, or language, or use the country and band dropdowns for exact filtering. Country flags are rendered locally as Unicode, with a neutral globe for EiBi's non-ISO codes. Click any row and the SDR retunes instantly.

The bundled list's measured coverage and known omissions are recorded in
[DATABASE_AUDIT.md](DATABASE_AUDIT.md).

The **On air now** checkbox filters shortwave listings to schedules that are currently active, converted from the database's UTC times into your browser's local time zone automatically. AM and FM entries are broadcast-license data with no schedule, so they're always treated as on air.

The meter bar at the top is always live: current frequency, band/mode, signal above the nearby noise floor, absolute noise floor, volume, and an audio toggle.

### Signal scanning

Click **Scan signals (visible)** to measure up to 200 stations on the current page. The scanner compares in-channel spectral power with nearby spectrum captured at the same instant and rejects the dongle's centre-frequency DC spike. RF candidates then receive a 1.5-second audio-content check. The **Viable only (scanned)** checkbox keeps results with non-silent, structured audio at least 6 dB above noise on FM or 10 dB above noise on direct-sampled AM/SW. Flat broadband hiss is labelled **noise-like** instead of being promoted as a viable station. Measurements persist across restarts.

**Scan all stations** always walks all 2,312 unique catalogue frequencies in batches of 200.
Search, band, country, page, on-air, and viable-only filters never narrow a full scan. Identical
frequencies are measured once because multiple station records can describe the same RF target.
Completed batches remain saved, updating both **Signal** and **Last checked**.

The inline progress panel shows the current phase, band and frequency, completed and total
targets, percentage, elapsed time, estimated time remaining, and a progress bar. Candidate audio
analysis is shown as its own phase. The scan button
becomes a stop control while running. At the default 0.7-second settling time, a complete sweep
took about 27 minutes on the deployed Raspberry Pi before audio classification; each RF candidate
now adds a 1.5-second content capture, so the total depends on how many channels pass the RF gate.
Borderline audio is automatically extended by another three seconds; clearly flat noise and
clearly structured audio keep the fast path.

Tuning a station while scanning asks before it stops the sweep. A scan and uninterrupted listening
cannot run simultaneously on one dongle: scanning necessarily retunes the same RF front end. A
second RTL-SDR would be required for true simultaneous scan-and-listen operation.

### CW Monitor

The CW Monitor tab runs a background scan → lock → decode → release loop: it cycles a small set of known CW-heavy frequencies (propagation beacons with high duty cycles, ham calling frequencies), and the moment it detects real on/off keying it locks onto that frequency and decodes continuously, streaming the text live. When the signal goes quiet or fades below detection, that decode is pushed to the scrolling log with a timestamp, frequency, and WPM estimate, and scanning resumes.

**One-shot: listen here** does a single ~4 second capture-and-decode on whatever frequency the radio is currently tuned to, without touching the tuning — useful to check a specific frequency you've navigated to manually from the Stations tab.

A manual tune from the Stations tab always interrupts a running scan or CW hunt — background automation never fights you for control of the radio.

### Live audio

The **Audio** toggle in the meter bar opens a WebSocket to the server and streams the currently-demodulated audio to your browser, playing it via the Web Audio API. Because this is a network stream rather than a local hardware passthrough, it works from any device on the network, doesn't need exclusive access to anything, and can run alongside the CW monitor without contention - both just read the same underlying demodulated stream.

### Receiver controls and diagnostics

The live waterfall is a 512-bin display product generated from the existing IQ stream; the
browser never receives raw unbounded samples. FM tuner gain is selectable from verified R820T
gain steps. Direct-sampled AM/SW bypasses that tuner, so the UI explicitly reports that the gain
setting is inactive there.

**Estimate calibration** measures a user-supplied exact reference carrier 100 kHz away from the
receiver centre, proposes a bounded oscillator correction, and requires confirmation before
applying it. It does not choose a reference or silently recalibrate from catalogue labels.

**Record IQ** writes a five-second, 8-bit complex SigMF capture and metadata into
`data/recordings`. Requests are limited to 1–10 seconds, concurrent recordings are rejected,
retuning aborts the recording, and total recording storage is capped at 512 MiB. Files are not
served over the unauthenticated web interface; retrieve them from the Pi filesystem.

## Known limitations

- AM/shortwave reception depends on the RTL-SDR dongle's direct-sampling input actually being wired to the external antenna path. That routing is unverified on the deployed generic board.
- Direct sampling aliases signals around the 28.8 MHz ADC clock: `f` and `28.8 MHz - f` can represent the same RF energy. Without preselection, a database label is a schedule match, not station identification.
- Signal, noise, and audio-content figures are software estimates, not calibrated receiver measurements. Structured interference can still pass; listening and an identifier remain the final attribution checks.
- CW decoding needs a genuinely strong, clean signal. A stock whip antenna indoors will often not be enough for weak DX or beacon traffic even when everything else is working correctly.
- Signal scanning and CW listening are one-user-at-a-time operations on the radio itself: a manual tune always interrupts a running scan or the CW monitor.

## Disclaimers

- Not affiliated with, endorsed by, or supported by the RTL-SDR project, Realtek, or (for the station database) EiBi, the FCC, or Ofcom.
- Provided as-is, no warranty — see [LICENSE](LICENSE).
- Receiving shortwave, amateur, and broadcast radio is legal in most jurisdictions for listening purposes, but rules vary by country. Check your local regulations, particularly around recording or publishing intercepted communications.
- The CW decoder is a best-effort tool, not a certified or professionally audited instrument. Don't rely on it for anything safety-critical.

## Changelog

### Unreleased

### 2.8.3 - 2026-10-22

#### Fixed

- Excluded operator IQ recordings from Docker build contexts and image layers; captures remain
  only in the mounted persistent data directory.

### 2.8.2 - 2026-10-22

#### Fixed

- Stream waterfall rows over one server-sent event connection instead of generating four HTTP
  access-log entries per browser per second.
- Gain changes now use the same scan-interruption confirmation as calibration and IQ recording,
  preserving fixed-gain scan comparability.

### 2.8.1 - 2026-10-22

#### Fixed

- Apply oscillator correction only when its value changes; pyrtlsdr rejects reapplying unchanged
  zero PPM on the deployed dongle.
- Run initial device configuration inside the supervised receiver loop so a native-driver error
  is logged and retried instead of terminating the worker thread.

### 2.8.0 - 2026-10-22

#### Added

- Added a live 512-bin waterfall derived from the receiver worker without exposing raw IQ to the
  browser.
- Added selectable fixed FM tuner gain and operator-approved PPM calibration against an exact
  known carrier.
- Added bounded 1–10 second SigMF IQ recording with a 512 MiB storage quota and retune safety.
- Added explicit direct-sampling mirror frequencies and paired measurement strength in station
  results.

#### Changed

- Replaced per-chunk AM peak normalisation with stateful attack/release AGC.
- Borderline content measurements extend from 1.5 to 4.5 seconds while clear decisions retain the
  shorter scan path.

### 2.7.0 - 2026-10-22

#### Fixed

- Added a second-stage audio-content measurement for RF candidates so stationary broadband hiss
  is labelled noise-like and excluded from viable/strongest-target results.
- Existing cached measurements are deliberately unclassified until rescanned because they lack
  the new audio-content evidence.

#### Documentation

- Moved the complete changelog into this README so releases, setup, usage, limitations, and change
  history have one public source of truth.
- Updated the user guide to distinguish page-scoped and full-catalogue scans and document
  viability thresholds, persistent results, detailed progress, interruption behaviour, and the
  measured full-scan duration on Raspberry Pi.
- Added a hardware and signal-validity audit documenting the external antenna, generic USB
  identity, unverified Q-branch routing, measured direct-sampling aliases, software evidence,
  limitations, falsification criteria, and an ordered hardware isolation plan.

### 2.6.1 - 2026-10-22

#### Changed

- **Scan all stations** now always scans every unique frequency in the catalogue; search, band,
  country, page, on-air, and viable-only filters affect browsing but never narrow a full scan.

### 2.6.0 - 2026-10-22

#### Added

- Added an inline scan-progress panel showing the current band and frequency, scan phase,
  completed and total targets, percentage, elapsed time, estimated time remaining, and a progress
  bar for visible and full scans.

### 2.5.1 - 2026-10-22

#### Fixed

- Viability now uses hardware-calibrated RF thresholds: 6 dB for tuner-path FM and 10 dB for
  noisier direct-sampled AM/SW, with silent demodulation rejected in both cases.
- Removed the experimental spectral-flatness gate after live A/B validation showed that it could
  not distinguish an audible FM station from an empty channel.

### 2.5.0 - 2026-10-22

#### Fixed

- Viable-target detection now measures structure in demodulated audio rather than its normalized
  playback volume, which previously promoted amplified hiss as something worth listening to.
- Strongest-signal sorting and filtering require both a signal above the local RF floor and
  non-noise-like audio. Existing measurements remain unclassified until rescanned.
- Audio quality uses a rolling window long enough to distinguish programme structure from hiss.

### 2.4.0 - 2026-10-22

#### Fixed

- Audible-station ranking now requires demodulated audio as well as RF energy, preventing silent
  carriers and receiver spurs from being presented as the strongest listenable station.
- Recovers the RTL-SDR after a failed direct-sampling/normal-tuner mode transition instead of
  leaving the audio stream stalled.
- Full-scan targets are now unique by frequency even when source records disagree on the band.

#### Added

- Added live per-frequency scan progress.
- Manual tuning during a scan now asks before stopping the scan; declining leaves it running.

### 2.3.0 - 2026-10-22

#### Added

- Added a stoppable **Scan all (filtered)** workflow that checks every unique frequency matching
  the current text, band, and country filters in bounded batches, updating signal strength and
  last-checked timestamps as it progresses.

### 2.2.0 - 2026-10-22

#### Added

- Added sortable last-checked timestamps backed by the existing persisted measurement time.
- Added an explicit country filter and local Unicode country flags without a runtime CDN.
- Added a documented audit of the bundled station database's coverage and known gaps.

#### Fixed

- Normalized Ofcom records to the United Kingdom instead of presenting their service area as a
  country.

### 2.1.0 - 2026-10-22

#### Added

- Added server-side sorting for frequency, band, name, country, language, and signal columns.
  Signal sorting ranks all filtered scanned stations numerically and always leaves unscanned
  stations last.

### 2.0.1 - 2026-10-22

#### Fixed

- Restored the documented INFO production log level instead of forcing high-volume SDR sample
  diagnostics through Docker Compose.

### 2.0.0 - 2026-10-22

#### Changed

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

#### Added

- Deterministic signal-estimator regression tests covering noise, DC offsets, in-channel
  carriers, and adjacent-channel rejection.
- API boundary and health-state tests.

#### Deployment notes

- This is a breaking hardware and API release. It requires an RTL2832U-based SDR dongle and does
  not support the ATS Mini serial protocol.
- Port 8731 has no application authentication and must remain on a trusted network or behind an
  authenticated reverse proxy.

## License

MIT — see [LICENSE](LICENSE).
