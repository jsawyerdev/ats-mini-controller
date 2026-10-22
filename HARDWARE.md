# Receiver hardware and signal-validity audit

This document separates what the deployed receiver has demonstrated from what its USB identity,
station catalogue, and signal meter cannot establish. It reflects measurements made on
2026-10-22 with the Raspberry Pi deployment and its externally connected antenna.

## Verified configuration

| Layer | Observed fact | What it does not prove |
| --- | --- | --- |
| Host | Raspberry Pi 5 Model B Rev 1.0, Ubuntu kernel `7.0.0-1017-raspi` | Stable power, freedom from USB interference, or absence of throttling |
| USB device | `0bda:2838 Realtek RTL2838 DVB-T`, product `RTL2838UHIDIR`, serial `00000001` | Board vendor, tuner revision, input matching, filtering, or HF wiring |
| USB link | USB 2.0 high-speed (480 Mbit/s), 500 mA descriptor | Actual current consumption or analogue performance |
| Antenna | An external antenna is physically connected | That the connector reaches the RTL2832U Q ADC input selected by software |
| FM path | 89.3 MHz produced structured programme audio | Reception quality across the whole FM band |
| HF path | Q-branch direct sampling can be enabled and returns samples | That a catalogue station was received or that a measured peak is at its displayed frequency |

The dongle's descriptor is generic. The exact PCB and RF topology cannot be inferred from
`0bda:2838`; a board photo, model identification, schematic, or continuity measurement is needed
to verify how the external connector reaches the direct-sampling input.

## Signal paths

```text
FM (88-108 MHz)
external antenna -> board input -> R820T tuner/filter/gain -> RTL2832U ADC -> software FM demod

AM/SW (<28.8 MHz)
external antenna -> board routing UNKNOWN -> RTL2832U Q ADC -> software AM demod
                                      (R820T tuner and its gain are bypassed)
```

The application selects Q-branch direct sampling below 28.8 MHz. This is a software selection,
not evidence that the board manufacturer wired the antenna connector to that pin. Generic
RTL-SDR boards commonly require a hardware modification; purpose-built boards may include the
path and matching network.

## What the measurements show

The leading hypothesis was that the strongest shortwave rows were actual scheduled stations.
Two independent observations contradict it:

1. The selected 15.2452 MHz audio capture was stationary broadband hiss: spectral flatness
   0.982, only 0.08 dB of spectral structure, and no dominant speech/music components. A known
   received FM programme at 89.3 MHz measured 0.100 flatness and 9.99 dB of structure using the
   same analysis.
2. Direct-sampling peaks appeared in complementary pairs around the 28.8 MHz ADC clock. For
   example, 13.545 and 15.255 MHz both measured 16 dB, then 19/18 dB on repetition; 15.2452 and
   13.5548 MHz measured 17/18 dB, then 19/19 dB. Each pair sums to 28.8 MHz. This is the expected
   Nyquist image relationship, so the two displayed frequencies are not independent evidence of
   two transmitters.

The station database describes broadcasts scheduled on a frequency. It is not an identification
system. A row name must therefore be read as “a station may use this channel,” never “this audio
has been identified as that station.”

### Conclusions and confidence

- **High confidence:** the captured 15.2452 MHz target was noise, not intelligible programme
  audio. A recording containing stable speech, music, or a decoded station identifier would
  falsify this conclusion for that later capture.
- **High confidence:** the direct-sampled HF display contains mirror responses. Peaks that stop
  tracking at `28.8 MHz - f` under repeated controlled injection would falsify the mirror
  diagnosis.
- **Moderate confidence:** inadequate HF input routing, missing preselection, local interference,
  or some combination causes the poor target quality. The current evidence cannot rank those
  causes independently.
- **Unknown:** whether the external connector is routed to Q input, and whether the Pi was
  undervolted or throttled during measurements. Physical inspection and privileged host
  telemetry are still required.

The strongest counterargument is that a real HF broadcast also produces a Nyquist image in an
unfiltered direct-sampling receiver. That is correct: mirrored energy does not prove that the
original RF source is false. It does prove that the catalogue frequency and signal score alone
cannot identify which side of the Nyquist boundary contains the source. The noise-like audio is
the separate evidence that the selected result was not useful listening at the time measured.

## Software response

Scanning now uses two stages:

1. A fast RF gate compares in-channel power with nearby spectrum and rejects silent demodulation.
2. Only RF candidates receive a 1.5-second Welch spectral-flatness analysis. Flat broadband hiss
   is labelled `noise-like`; structured speech, music, or tones must exceed 3 dB of spectral
   content before the row is considered viable.

This deliberately classifies uncertain results as unverified. It does not attempt station
identification and cannot reject every intermittent interferer, digital signal, or structured
spur. Listening and station identification remain necessary for attribution.

## Hardware isolation plan

Run these checks in order because each separates competing causes:

1. Photograph and identify both sides of the dongle PCB. Verify continuity from the external
   connector to the RTL2832U Q-branch network; do not infer it from the USB product string.
2. Repeat the same-frequency capture with the antenna connected, disconnected, and the input
   terminated in 50 ohms. A peak unchanged under termination is generated after the antenna
   input or coupled locally.
3. Move the dongle away from the Pi on a shielded USB extension and add ferrites at both ends.
   A material drop identifies Pi/USB/common-mode coupling.
4. Inject a known, legal low-level test signal through attenuation and verify both its wanted
   frequency and `28.8 MHz - f` image. This quantifies image rejection and confirms routing.
5. Add band-selective filtering before the ADC: a low-pass/high-pass split or switched HF
   preselector, plus medium-wave rejection where local broadcast transmitters overload the ADC.
6. If reliable HF attribution is required, use an upconverter into the tuner path or an SDR with
   a purpose-designed HF front end, filtering, and documented dynamic range. Direct sampling on
   this unverified generic board is useful for experimentation, not calibrated identification.

## Primary references

- [pyrtlsdr direct-sampling API](https://pyrtlsdr.readthedocs.io/en/stable/rtlsdr.html)
- [RTL-SDR Blog V3 direct-sampling guide](https://www.rtl-sdr.com/rtl-sdr-blog-v-3-dongles-user-guide/comment-page-1/)
- [Generic RTL-SDR direct-sampling hardware modification](https://www.rtl-sdr.com/rtl-sdr-direct-sampling-mode/comment-page-1/)
