# Bundled station database audit

Audited 2026-10-22 against `data/stations.db`.

## Observed coverage

- 50,026 records across 2,312 unique frequencies.
- 9,391 EiBi schedule records covering 1,994 LF/shortwave frequencies.
- 38,445 FCC records covering 120 AM and 100 FM frequencies in the US and Canada.
- 2,190 Ofcom records covering 40 AM and 202 FM frequencies in the UK.
- 97 EiBi country/territory codes, two FCC country codes, and UK Ofcom coverage.

Multiple records on one frequency are expected: schedules, transmitters, translators, and
coverage areas can share a carrier. Scanning therefore measures each unique frequency once.

## Confirmed gaps and limitations

- Local AM/FM listings are not global. Outside the US, Canada, and UK, the database only has
  EiBi schedule data; it does not include other national regulator datasets.
- DAB, HD Radio, DRM metadata, internet streams, transmitter power, coordinates, antenna pattern,
  and distance from the receiver are not represented.
- The original Ofcom import stored its `Area` field in `country`. The API now correctly normalizes
  those rows to `GB`, but the area is not separately exposed by the current schema.
- EiBi uses ITU-style country codes rather than ISO alpha-2 codes. The UI shows a globe for those
  codes instead of guessing a potentially wrong national flag.
- 46 EiBi records are below 100 kHz. They are valid VLF/LF time, navigation, and military entries,
  but are grouped under the historical `SW` label and may be outside the useful range of a given
  dongle and antenna.
- Ten Ofcom records have no area value. Station names and frequencies are present.
- Language is intentionally absent from regulator AM/FM records and absent from 340 EiBi rows.
- The database does not record when each upstream dataset was downloaded, so source freshness
  cannot be proven from the SQLite file alone.

## Country presentation

The country filter normalizes FCC `US`/`CA` and all Ofcom rows to `GB`. These ISO alpha-2 values
render as local Unicode flags. EiBi codes remain exactly as supplied and use a neutral globe;
this avoids a CDN dependency and avoids falsely mapping ITU codes to ISO flags.
