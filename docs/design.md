# Design

> Historical roadmap / proposed design, not an implementation checklist.
> Current behavior and bench status: [README](../README.md),
> [working dual-band example](confirmed-dualband.md), and [USB bring-up](usb-bringup.md).


This design doesn't depend on the implementation language. It applies to the current OpenRTX-based (C)
implementation and carries over to a possible later Rust rewrite ([rust-plan.md](rust-plan.md), deferred).

The problems this design fixes are documented in [findings.md](findings.md#openrtx-codeplug-and-ui-issues).

## Principles

- **Garbage in flash must never crash the radio or make it transmit somewhere unexpected.**
- **Parse once, then trust.** Untrusted data (flash, USB) passes through one validation layer.
  Everything after that layer uses validated structures only.
- **Never destroy data automatically.** Corrupt data stays in place for recovery. Overwriting it is
  an explicit user action.
- **No Windows needed.** All configuration works from Linux, through CHIRP or our own CLI.

## Ownership rules

- Only the **storage** module touches the codeplug region of SPI flash. Other code gets validated
  channel, zone and contact structures.
- Only the **radio** module touches RF chips. It takes typed requests (tune to a validated
  frequency, PTT on/off).
- The **UI** never reads flash and never pokes hardware. It works on the model (validated store +
  radio state) through events.

## TX safety

TX is allowed only when all of these hold:
- the calibration data passed validation
- the frequency is inside this radio's hardware band limits
- the channel is not marked RX-only

There is also a hard TX timeout regardless of the PTT source (key, USB command, VOX-on-USB).

Calibration data:
- read-only to us
- backed up to the host before anything else
- range-checked field by field. Invalid calibration means RX-only plus an on-screen warning.

## Codeplug

### Format

Our own format, stored in a region of SPI flash separate from the vendor codeplug (so the vendor
data stays importable). It is a fixed-layout binary image, so CHIRP's clone mode (download image →
edit → upload) maps onto it directly.

```
Header      magic "TYTX", schema_version, image_len, seq_no, crc32(header)
Settings    radio-wide settings (callsign, DMR ID, squelch, VFO steps, power-on screen, ...), crc16
Channels    N × fixed-size record: flags, name[16] (UTF-8, NUL-padded), rx_hz u32, tx_hz u32 /
            offset, mode (FM/NFM/DMR/M17), power, tone mode, CTCSS/DCS rx/tx, DMR CC/TS/contact,
            rx_only, scan, crc16
Zones       M × record: name, channel index list, crc16
Contacts    K × record (DMR/M17), crc16
```

- Each record has a `flags` byte: `0x01` = present. **0x00 and 0xFF both mean "no record".**
  Erased and zeroed flash are both empty, never garbage.
- Little-endian integers, no bitfields, no casting packed structs over flash contents. Parsing is
  explicit, field by field.
- **A/B copies** with `seq_no`. Writes go to the inactive copy and are committed by writing its
  header last. On boot, use the newest copy whose header CRC is valid. A power cut mid-write falls
  back to the last good copy.

### Validation (one function, one place)

Every record is checked once, on load or on write:
- Frequencies inside this radio's band limits (from hardware info / calibration), and aligned to
  the 6.25 kHz grid (5 kHz on VHF).
- Enums (mode, power, tone mode) must be known values. CTCSS must be in the standard table, DCS in
  the standard code list.
- DMR: color code 0–15, timeslot 1–2, contact index must exist.
- Names: valid UTF-8, NUL-terminated, mapped to printable display glyphs.
- Cross-references: zone → channel and channel → contact must point to present, valid records.

Records that fail are **skipped and reported** (record index + reason) on the USB log channel and on
a UI status screen. They are never fatal.

The validator and parser are fuzzed on the host (OpenRTX builds for Linux, so the same C code runs
under libFuzzer/AFL). The corpus includes real dumps from every radio in the fleet, plus erased
(0xFF) and zeroed images.

### Corruption fallback: PMR446 defaults

Implementation override: the user explicitly requested TX/RX on PMR fallback
channels. The current build uses the 1 W setting and permits PTT, while retaining
RAM-only recovery and the TX timeout. The RX-only defaults below describe the
original design. VHF-only recovery now uses 145.500 MHz FM calling with RX/TX at 1 W.

- **Whole store invalid** (no valid header in either copy), or **zero valid channels** after validation:
  - Load a built-in default channel set **in RAM**.
  - Show "Codeplug invalid – defaults loaded" on boot.
  - **Do not overwrite flash automatically.** The corrupt image stays readable over USB for recovery.
    Writing the defaults is an explicit user action (menu or CLI).
- **Default set (UHF-capable radios), PMR446, 16 channels, NFM 12.5 kHz:**
  - channel *n* = 446.00625 MHz + (*n* − 1) × 12.5 kHz, *n* = 1…16 (446.00625 … 446.19375 MHz)
  - no CTCSS
  - **RX-only by default.** PMR446 legally requires ≤ 0.5 W ERP and a fixed, non-removable antenna.
    These radios have a detachable antenna and their lowest power is about 1 W, so transmitting
    there isn't licence-exempt. The user can enable TX per channel (e.g. for licensed use).
- **VHF-only units** (single-band VHF MD-390) can't reach 446 MHz. Fall back to a small RX-only set
  of common amateur 2 m simplex/calling frequencies (exact list TBD) plus an empty VFO.
- Partial corruption (some records bad) only drops the bad records. Defaults apply only when nothing
  usable is left.
- **Legacy path:** while the vendor codeplug reader is still in use, it gets the same treatment.
  Invalid vendor records are skipped, and the PMR fallback applies when nothing valid is left.

### Programming without Windows

1. **CHIRP driver:** a Python driver module for CHIRP (Linux-native).
   - Load it via CHIRP's developer "Load Module"; aim to upstream once stable.
   - Clone mode over the USB serial port, using the `PROG` channel of the USB protocol (below).
     Commands: `IDENTIFY`, `READ_BLOCK`, `WRITE_BLOCK`, `COMMIT`.
   - The memory map is described with CHIRP's `bitwise`, generated from the same layout definition
     the firmware uses, so the two can't drift.
   - Standard CHIRP fields: freq, duplex/offset, mode, power, tone mode, CTCSS/DCS, name.
     DMR/M17 extras go in CHIRP's per-memory "extra" settings.
   - **The device validates on `COMMIT`.** An invalid image is rejected with per-record errors, and
     the previous copy stays active (A/B).
2. **CLI:** `tyt codeplug {read,write,validate,import-csv,export-csv,defaults}`, using CHIRP's CSV
   format so channel lists move freely between CHIRP, the CLI and other radios.
3. **Vendor codeplug import (optional, later):** a one-time conversion of an existing TYT codeplug
   (e.g. one made with editcp / dmrconfig on Linux) through the same validator.

## USB

- One configuration, composite device: **UAC1 mono 16-bit 48 kHz (IN + OUT) + one CDC-ACM**.
  This uses all 3 non-control IN endpoints and 2 of the 3 OUT endpoints of OTG_FS.
- **No UAC feedback endpoint**, because there's no endpoint left for it:
  - OUT is adaptive/synchronous: the device absorbs clock drift with a fractional resampler.
  - IN is asynchronous: the device chooses 47/48/49 samples per frame.
- Stack: TinyUSB (STM32F4 OTG_FS, UAC + CDC composite), configured without a feedback endpoint,
  replacing OpenRTX's old ST OTG library. Ready-made class implementations that require explicit
  feedback (e.g. embassy-usb's `uac1::speaker`) don't fit.
- **The CDC port carries framed messages** (COBS + CRC) with a channel ID:
  - `CTRL`: commands (PTT, frequency, power, squelch, mode, RSSI, status), Hamlib-compatible on the host
  - `PROG`: codeplug read/write/commit (CHIRP, CLI)
  - `KISS`: TNC data
  - `NMEA`: GPS (MD-390)
  - `LOG`: diagnostics, validation reports
- **Host daemon:** opens the CDC port and demultiplexes it into PTYs
  (`/run/tyt/<serial>/{ctrl,kiss,nmea,prog}`). It feeds gpsd and exposes PROG to CHIRP and the CLI.

## On-radio UI

Structure: model / controller / view.
- **model:** validated store + radio state
- **controller:** small per-screen handlers fed by input events
- **view:** pure draw functions, no state changes, no flash access

OpenRTX supports separate UI implementations (`ui/default`, `ui/module17`), so a new UI can live
next to the default one during development.

### Behaviour

- **Two clear modes, always shown on screen: `CH` (memory) and `VFO`.** One key toggles them.
- **Channel mode:**
  - Knob = next/previous **valid** channel in the current zone (empty or invalid slots are skipped).
  - Zone = a dedicated key, then knob to pick, then confirm. Zone name and channel number always visible.
- **VFO mode:**
  - Knob tunes by the **current step**. Steps: 6.25 / 12.5 / 25 kHz (UHF), 5 / 12.5 / 25 kHz (VHF).
  - The frequency **always snaps to the step raster**, including after direct keypad entry.
    12.5 kHz with a 6.25 kHz offset lines up with PMR446 and NFM channelization.
  - Step changes with a key + knob. The step is shown next to the frequency.
- **Direct keypad frequency entry**, validated against band limits as you type.
- **"Save VFO to channel"** from the VFO screen.
- **Status screens:**
  - codeplug status (valid / skipped records / defaults loaded)
  - USB state (enumerated, audio streaming, PTT source)
  - GPS fix, battery, calibration status

## Testing

- **Host:** unit tests + fuzzing for the codeplug parser/validator and the USB framing.
- **Golden comparison:** capture SPI/I²C init traffic with a logic analyzer on stock OpenRTX, then
  compare after our changes.
- **RF:** SDR checks per radio (frequency error, deviation, TX power, spurious emissions) on the
  first TX of every build that touches radio or driver code.
