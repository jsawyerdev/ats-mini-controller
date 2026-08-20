"""Build a normalized station database from EiBi (global shortwave) and
optional regulator broadcast-license exports (FCC/Ofcom/ISED-style CSVs).

Normalized schema (one row per broadcast):
    freq_hz     int   - carrier frequency in Hz
    band        str   - "SW" | "AM" | "FM"
    name        str   - station/service name
    country     str   - ITU/ISO-ish country code or name, as given by the source
    mode        str   - "AM" for SW/MW, "FM" for VHF FM broadcast
    time_range  str   - "HHMM-HHMM" UTC, only meaningful for SW ("" = always on)
    days_code   str   - EiBi day code (Mo/Tu/../Su, digit form, or blank = daily)
    language    str   - language code, only meaningful for SW
    source      str   - "eibi" | "fcc" | "ofcom" | "ised" | <custom source name>

Author: James Sawyer / JSLabs - https://labs.jamessawyer.co.uk/monitoring/
"""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

_COLUMNS = (
    "freq_hz",
    "band",
    "name",
    "country",
    "mode",
    "time_range",
    "days_code",
    "language",
    "source",
)


def load_eibi(path: Path) -> list[dict]:
    """Parse an EiBi sked-*.csv file (semicolon-separated, 11 fields).

    Format: https://www.eibispace.de/dx/README.TXT
    kHz;Time(UTC);Days;ITU;Station;Lng;Target;Remarks;P;Start;Stop;
    """
    rows: list[dict] = []
    with path.open(encoding="latin-1") as f:
        reader = csv.reader(f, delimiter=";")
        next(reader, None)  # header
        for parts in reader:
            if len(parts) < 8:
                continue
            khz, time_range, days, itu, station, lang, _target, remarks = parts[:8]
            try:
                freq_hz = round(float(khz) * 1000)
            except ValueError:
                continue
            if not station.strip():
                continue
            mode = "USB" if "USB" in remarks else ("LSB" if "LSB" in remarks else "AM")
            rows.append(
                {
                    "freq_hz": freq_hz,
                    "band": "SW",
                    "name": station.strip(),
                    "country": itu.strip(),
                    "mode": mode,
                    "time_range": time_range.strip(),
                    "days_code": days.strip(),
                    "language": lang.strip(),
                    "source": "eibi",
                }
            )
    return rows


# Column names (lowercased) commonly used across FCC/Ofcom/ISED broadcast
# license exports. Regulator export formats vary and can change; this sniffs
# a best-effort match rather than assuming one fixed schema.
_FREQ_KEYS = ("frequency", "freq", "channel_freq", "freq_mhz", "freq_khz")
_NAME_KEYS = ("callsign", "call_sign", "station_name", "station", "name", "service_name")
_COUNTRY_KEYS = ("country", "state", "province", "region", "area", "city")


def load_fcc_pipe(path: Path, band: str) -> list[dict]:
    """Parse the legacy FCC fmq/amq CGI text output: pipe-delimited, no
    header row, one row per authorization (station + booster/translator).

    Layout: |CALLSIGN|FREQ unit|SERVICE|...|CITY|STATE|COUNTRY|... with a
    varying number of trailing technical columns, so the CITY/STATE/COUNTRY
    triplet is located by finding the COUNTRY field (US or CA) rather than
    a fixed index.
    """
    rows: list[dict] = []
    with path.open(encoding="latin-1", errors="replace") as f:
        for line in f:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 13:
                continue
            callsign, freq_raw = parts[1], parts[2]
            country_idx = next(
                (i for i, p in enumerate(parts) if p in ("US", "CA") and i >= 8), None
            )
            if country_idx is None or not callsign or not freq_raw:
                continue
            city, state, country = parts[country_idx - 2], parts[country_idx - 1], parts[country_idx]
            try:
                freq_val = float(freq_raw.split()[0])
            except (ValueError, IndexError):
                continue
            freq_hz = round(freq_val * (1_000_000 if band == "FM" else 1_000))
            rows.append(
                {
                    "freq_hz": freq_hz,
                    "band": band,
                    "name": f"{callsign} ({city}, {state})",
                    "country": country,
                    "mode": "FM" if band == "FM" else "AM",
                    "time_range": "",
                    "days_code": "",
                    "language": "",
                    "source": "fcc",
                }
            )
    return rows


def load_regulator_csv(path: Path, source: str, band: str) -> list[dict]:
    """Generic loader for a manually downloaded regulator export (FCC AM/FM
    query, Ofcom transmitter list, ISED broadcast database, etc).

    Sniffs the header for a frequency and a name column since exact export
    schemas differ by regulator and change over time; unmatched columns are
    ignored rather than guessed.
    """
    rows: list[dict] = []
    with path.open(encoding="utf-8-sig", errors="replace") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            return rows
        lower_map = {name.lower().strip(): name for name in reader.fieldnames}

        def find(keys: tuple[str, ...]) -> str | None:
            for key in keys:
                for lower_name, orig_name in lower_map.items():
                    if key in lower_name:
                        return orig_name
            return None

        freq_col = find(_FREQ_KEYS)
        name_col = find(_NAME_KEYS)
        country_col = find(_COUNTRY_KEYS)
        if freq_col is None or name_col is None:
            raise ValueError(
                f"{path}: could not find frequency/name columns in header {reader.fieldnames}"
            )

        for row in reader:
            raw_freq = (row.get(freq_col) or "").strip().replace(",", "")
            if not raw_freq:
                continue
            try:
                freq_val = float(raw_freq)
            except ValueError:
                continue
            # Regulator exports usually give FM in MHz and AM in kHz.
            if band == "FM":
                freq_hz = round(freq_val * 1_000_000)
            else:
                freq_hz = round(freq_val * 1_000)
            rows.append(
                {
                    "freq_hz": freq_hz,
                    "band": band,
                    "name": (row.get(name_col) or "").strip(),
                    "country": (row.get(country_col) or "").strip() if country_col else "",
                    "mode": "FM" if band == "FM" else "AM",
                    "time_range": "",
                    "days_code": "",
                    "language": "",
                    "source": source,
                }
            )
    return rows


_TEXT_COLUMNS = ("band", "name", "country", "mode", "time_range", "days_code", "language", "source")


def write_sqlite(rows: list[dict], db_path: Path) -> None:
    db_path.unlink(missing_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        f"""
        CREATE TABLE stations (
            {", ".join(f"{c} TEXT" if c in _TEXT_COLUMNS else f"{c} INTEGER" for c in _COLUMNS)}
        )
        """
    )
    conn.executemany(
        f"INSERT INTO stations ({', '.join(_COLUMNS)}) VALUES ({', '.join('?' for _ in _COLUMNS)})",
        [tuple(row[c] for c in _COLUMNS) for row in rows],
    )
    conn.execute("CREATE INDEX idx_freq ON stations(freq_hz)")
    conn.execute("CREATE INDEX idx_band ON stations(band)")
    conn.commit()
    conn.close()


def build(
    eibi_path: Path,
    db_path: Path,
    regulator_files: dict[str, tuple[Path, str]] | None = None,
    fcc_files: dict[str, Path] | None = None,
) -> int:
    """regulator_files: {source_name: (path, band)} for generic CSV exports.
    fcc_files: {"FM": path, "AM": path} for the legacy fmq/amq pipe format.
    """
    rows = load_eibi(eibi_path)
    for source, (path, band) in (regulator_files or {}).items():
        rows.extend(load_regulator_csv(path, source, band))
    for band, path in (fcc_files or {}).items():
        rows.extend(load_fcc_pipe(path, band))
    write_sqlite(rows, db_path)
    return len(rows)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eibi", type=Path, default=Path("data/eibi_sked.csv"))
    parser.add_argument("--db", type=Path, default=Path("data/stations.db"))
    parser.add_argument(
        "--regulator",
        action="append",
        default=[],
        metavar="SOURCE:BAND:PATH",
        help="e.g. ofcom_fm:FM:data/ofcom_fm.csv (repeatable)",
    )
    parser.add_argument("--fcc-fm", type=Path, help="path to fmq CGI text output")
    parser.add_argument("--fcc-am", type=Path, help="path to amq CGI text output")
    args = parser.parse_args()

    regulator_files: dict[str, tuple[Path, str]] = {}
    for spec in args.regulator:
        source, band, path = spec.split(":", 2)
        regulator_files[source] = (Path(path), band)

    fcc_files: dict[str, Path] = {}
    if args.fcc_fm:
        fcc_files["FM"] = args.fcc_fm
    if args.fcc_am:
        fcc_files["AM"] = args.fcc_am

    count = build(args.eibi, args.db, regulator_files, fcc_files)
    print(f"Wrote {count} stations to {args.db}")
