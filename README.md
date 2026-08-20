# ATS Mini Controller

A local web app that controls an [ATS Mini](https://esp32-si4732.github.io/ats-mini/) (ESP32-S3 + Si4732 shortwave/AM/FM receiver) over USB, with a searchable 50,000+ station database, live signal scanning, and an automatic Morse code (CW) decoder.

Author: **James Sawyer / JSLabs** — [labs.jamessawyer.co.uk/monitoring](https://labs.jamessawyer.co.uk/monitoring/)

## What it does

- **Station browser**: 50,000+ shortwave, AM, and FM stations from EiBi (global shortwave schedules), the FCC (US/Canada AM+FM), and Ofcom (UK AM+FM). Search, filter by band, and see which shortwave broadcasts are on air right now based on your local time.
- **Click to tune**: click any station in the browser and the physical radio retunes to it over USB, in under a second for most band changes.
- **RSSI signal scanning**: measures real signal strength on a batch of stations and flags which ones are actually receivable right now, not just licensed to exist. Results persist across restarts.
- **CW (Morse code) monitor**: cycles known CW-heavy frequencies (propagation beacons, ham calling frequencies), locks onto whichever one has real keying, and decodes it live in the browser — timestamped, frequency-tagged, scrolling — until the signal stops or fades below detection.
- **Audio passthrough**: routes the radio's audio (via a USB sound interface connected to its headphone output) straight to your computer's speakers.

## Hardware required

- An ATS Mini running firmware **v2.34 or later**, with **Settings → USB Port → Ad hoc** enabled (remote control is off by default).
- USB-C cable (the same one used for charging/flashing carries the serial control link).
- Optional, for CW decoding and audio passthrough: a USB audio interface with a line/mic input, cabled from the ATS Mini's headphone jack into the interface's **input** jack (not its output jack — cheap adapters often have both on the same unit).

## Setup

```bash
pip install -r requirements.txt
./start.sh
```

`start.sh` clears any stale server process, warns if the radio isn't plugged in, starts the backend, and opens `http://127.0.0.1:8731` in your browser. The ATS Mini's serial port is auto-detected by USB vendor ID, so it works regardless of which `/dev/cu.usbmodemNNNN` path macOS assigns it.

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

The firmware's serial protocol has no direct frequency-jump command — only relative "next band" / "next mode" stepping — so retuning across very different bands can take a few seconds and passes through intermediate bands visibly on the radio's own screen. That's a hardware/firmware limit, not something this app can fully hide.

Decoding works by capturing audio, finding the CW tone's frequency (unknown in advance — it depends on the receiver's BFO offset), isolating it with a bandpass filter, extracting the on/off keying envelope, and converting the timing into dots, dashes, and text. No PyPI package does audio-to-Morse decoding well, so this part (`cw_decoder.py`) is a small local implementation on top of numpy/scipy, validated against synthetic Morse test signals in `test_cw_decoder.py`.

## Known limitations

- This firmware's band list has two entries both named `15M` (a shortwave broadcast meter band and the 15m ham band). Band selection matches by name, so a target using that name can land on the wrong one. `20M`/`17M`/`10M`/`30M` don't collide with any broadcast band.
- Audio passthrough plays through your computer's *default output device*, not inside the browser tab.
- CW decoding needs a genuinely strong, clean signal. A stock whip antenna indoors will often not be enough for weak DX or beacon traffic even when everything else is working correctly.
- RSSI scanning and CW listening are one-user-at-a-time operations: a manual tune always interrupts a running scan or the CW monitor.

## Disclaimers

- Not affiliated with, endorsed by, or supported by the ATS Mini / esp32-si4732 project, PU2CLR, or Espressif. Firmware links point to their official sources.
- Flashing firmware carries a real risk of bricking the device if the wrong build variant is used (this project's own testing hit exactly that — recovered by reflashing the correct variant, documented as a reminder to check flash/PSRAM type before flashing, not as a guarantee it can't happen to you).
- Provided as-is, no warranty — see [LICENSE](LICENSE).
- Receiving shortwave, amateur, and broadcast radio is legal in most jurisdictions for listening purposes, but rules vary by country. Check your local regulations, particularly around recording or publishing intercepted communications.
- The CW decoder is a best-effort tool, not a certified or professionally audited instrument. Don't rely on it for anything safety-critical.

## License

MIT — see [LICENSE](LICENSE).
