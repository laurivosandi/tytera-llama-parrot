# Plan

> Historical roadmap / proposed design, not an implementation checklist.
> Current behavior and bench status: [README](../README.md),
> [working dual-band example](confirmed-dualband.md), and [USB bring-up](usb-bringup.md).


Base: **an OpenRTX fork (C)**. The design is in [design.md](design.md) and the hardware facts are in
[findings.md](findings.md). A Rust rewrite is deferred ([rust-plan.md](rust-plan.md)). The design
is written so it carries over.

Principles:
- ship something useful early (USB audio)
- everything works on **unmodified** radios. Hardware mods are an optional tier.
- garbage in flash must never crash the radio

## Phase 0: Bring-up

- [ ] Fork OpenRTX. Build for `md3x0` and `mduv3x0`, then flash and boot on one MD-390 and one MD-UV390.
- [ ] Record the hardware revision of every radio in the fleet (board markings, TYT firmware version,
      VHF/UHF). Later production runs changed parts.
- [ ] Back up the original firmware, the vendor codeplug and **calibration data** of every radio
      (calibration lives in SPI flash).
- [ ] Set up SWD debugging on one radio of each type (`MDx_ENABLE_SWD`).
- [ ] Confirm the USB wiring (OTG_FS on PA11/PA12) and the existing CDC (`rtxlink`) enumeration on Linux.
- [ ] Decide on a dev unit for USB RX work. Unmodified UV390s can't feed RX audio to the MCU, so
      either use an MD-390 (band-limited 2T/5T path) or apply the RF→MCU mod to one UV390.
- [ ] Capture baseline SPI/I²C traffic on stock OpenRTX for later golden comparison.

## Phase 1: USB audio + control (no mods)

Implementation update: an opt-in UAC1 + CDC patch, FM audio routing, framed CTRL
PTT/status and a host CLI now build for both targets. See [USB bring-up](usb-bringup.md).
Dual-band USB FM RX/TX has since been confirmed working; the checklist below includes the
remaining daemon, controls and interoperability work.

See [design.md § USB](design.md#usb).

- [ ] Replace OpenRTX's ST OTG library with TinyUSB. Composite device: UAC1 mono 16-bit 48 kHz
      (IN + OUT, no feedback endpoint) + one CDC-ACM.
- [ ] TX path: USB OUT → resampler → PWM (TIM3 CH3) → modulator. Works on all models.
- [ ] RX path: ADC2 ch13 (PC3) → USB IN.
  - MD-390: the band-limited 2T/5T path. Characterize its frequency response; good enough for AFSK1200.
  - UV390: silence without the mod. Report this clearly via `CTRL` status.
- [ ] Local monitoring: RX audio still goes to the speaker. On the UV390 that is the C6000 DAC FIFO.
- [ ] Framed CDC protocol with channels `CTRL` / `PROG` / `KISS` / `NMEA` / `LOG`.
- [ ] PTT via `CTRL`, plus an optional VOX-on-USB-audio mode. Hard TX timeout.
- [ ] Host daemon: demultiplexes CDC into PTYs, provides Hamlib-compatible control, feeds gpsd
      (MD-390 NMEA).
- [ ] Interop testing with WSJT-X, fldigi and Direwolf (TX on all models; RX on the MD-390 or a modded UV390).

Deliverable: an unmodified radio shows up as an ALSA card plus a serial port.

## Phase 2: Codeplug hardening + CHIRP (no mods)

Implementation update: MD3x0 and MDUV3x0 now use a shared validated native
reader, compact valid channel/bank lists, and automatic PMR fallback
when no channels survive. At the user’s request, the current PMR fallback enables
TX/RX at the 1 W setting (overriding the original RX-only plan). Recovery starts
in memory mode and preserves flash,
including the old saved VFO. VHF-only fallback is now 145.500 MHz FM calling, RX/TX at 1 W.
Host corruption tests pass for both models; on-radio verification remains pending.
The custom store, calibration validation and CHIRP work below are still outstanding.

See [design.md § Codeplug](design.md#codeplug).

- [ ] **Quick fix first:** harden the existing vendor codeplug readers (`cps_io_native_MD3x0.c`,
      `cps_io_native_MDUV3x0.c`):
  - fix the empty-slot test (UTF-16 vs 4-byte `wchar_t`; treat 0xFFFF / 0x0000 as empty)
  - validate mode, color code, timeslot and indices
  - validate BCD, and check band limits in the reader itself, not only in one UI path
- [ ] Validate calibration data. Invalid calibration means RX-only plus a warning.
- [ ] PMR446 RX-only fallback (VHF units: 2 m RX-only set) when nothing valid is left. Kept in RAM;
      flash isn't touched.
- [ ] Our own codeplug store: fixed-layout image, per-record CRC, A/B copies, one validator.
- [ ] Fuzz the parser and validator on the host (OpenRTX Linux build + libFuzzer).
- [ ] `PROG` channel commands: `IDENTIFY` / `READ_BLOCK` / `WRITE_BLOCK` / `COMMIT` (validated on the
      device, with per-record errors).
- [ ] CHIRP driver module (clone mode), plus a CLI with CHIRP CSV import/export.
- [ ] Optional: one-time vendor codeplug import through the validator.

## Phase 3: UI

See [design.md § On-radio UI](design.md#on-radio-ui).

- [ ] New UI next to `ui/default` (model / controller / view).
- [ ] CH/VFO modes always visible. In CH mode the knob skips invalid slots; zone selection gets a
      dedicated key.
- [ ] VFO steps 6.25 / 12.5 / 25 kHz (VHF: 5 / 12.5 / 25 kHz) with snapping to the step raster,
      including keypad entry.
- [ ] Save VFO to channel. Status screens for codeplug, USB, GPS, battery and calibration.

## Phase 4: DMR on the radio (no mods)

The C5000/C6000 does 4FSK, timing and sync. The MCU only handles payloads.

- [ ] DMR stack: C5000 (MD-390) first, because md380tools documents it best; then C6000 (UV390).
      Use the OpenGD77 C6000 code as a reference (check its license).
- [ ] AMBE+2 on the MCU, using code taken from TYT firmware (supplied by the user at build/flash time).
- [ ] **USB audio in DMR mode:** decoded PCM → USB IN; USB OUT → AMBE → baseband. This gives
      full-duplex USB audio on **all** models with no mods.
- [ ] DMR SMS and standard packet data (rate ½ / ¾ / 1 ≈ 1.6 / 2.4 / 3.2 kbit/s).
- [ ] **Custom data over DMR bursts:** KISS frames in 216-bit payloads (~3.6 kbit/s raw per slot),
      exposed on the `KISS` channel, so `tncattach` on the host gives an IP interface.
- [ ] Experimental: Codec2 (3200, or 1600 + FEC) inside DMR voice bursts.

## Phase 5: On-radio analog modems (no mods)

- [ ] AFSK1200 AX.25 modem on the MCU, exposed as KISS.
  - TX: all models
  - RX: MD-390, over the 2T/5T path
- [ ] APRS beacons using GPS (MD-390). The UV390 can do TX-only beacons with a fixed position.
- [ ] DTMF / selcall (the C6000 has built-in DTMF; the MD-380's 2T/5T path was designed for this).

## Phase 6: C6000/C5000 research (could remove the need for mods)

Goal: get raw received audio or symbols out of the baseband chip without touching hardware.

- [ ] Dump the register state TYT firmware uses in each mode (md380tools / SWD).
- [ ] Peek/poke baseband registers over `CTRL`.
- [ ] Sweep codec routing (`0xE0`–`0xE5`) and vocoder routing (`0x06`) while injecting a known RF tone.
      Use a logic analyzer on the vocoder/I2S pins to see whether the `ADC_IVINP` signal can reach
      the MCU. **If yes: FM capture and M17 RX on the UV390 with no mods.**
- [ ] Evaluate Layer-1 ("physical layer only") mode and the 6-bit soft-bit buffers for non-DMR framing.
- [ ] Check whether the deviation mapping (`0x01[7]`) can be tuned to M17's ±800/±2400 Hz.

## Phase 7: Optional hardware-mod tier

Only for radios where it's worth it. Skip the mic mod, since voice comes over USB.

- [ ] RF→MCU mod (MD-390: R150 + wire + C556; UV390: `ADC_IVINP` → PC3).
- [ ] Unlocks: full-bandwidth FM capture over USB, M17 RX (OpenRTX already has the stack),
      9600 G3RUH, and a raw-discriminator "SDR-lite" capture mode for host-side modems.
- [ ] M17 packet data exposed as KISS.

## Open questions

- Exact OTG_FS endpoint/FIFO limits (RM0090), and whether to run audio at 48 kHz or 8/16 kHz internally.
- Whether every radio in the fleet is the same hardware revision.
- The license of the OpenGD77 code (for reusing the DMR stack).
- Whether Layer-1 mode in the C6000 is usable outside DMR burst timing.
- The 2 m RX-only fallback channel list for VHF-only units.
- When to revisit the Rust rewrite (see [rust-plan.md](rust-plan.md)).
