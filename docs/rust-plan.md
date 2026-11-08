# Rust rewrite plan (deferred)

> Historical roadmap / proposed design, not an implementation checklist.
> Current behavior and bench status: [README](../README.md),
> [working dual-band example](confirmed-dualband.md), and [USB bring-up](usb-bringup.md).


**Status: deferred.** The current implementation is an OpenRTX fork in C ([plan.md](plan.md)).
The design in [design.md](design.md) (codeplug format, validation, PMR fallback, CHIRP, USB
protocol, UI, TX safety) doesn't depend on the language and carries over unchanged. This document
covers only what would be specific to a Rust (embassy, `no_std`) firmware.

## Why it might be worth it later

- Memory safety for parsing untrusted flash and USB data. The OpenRTX codeplug bugs
  ([findings.md](findings.md#openrtx-codeplug-and-ui-issues)) are exactly this class of bug.
- Type-enforced "parse once, then trust": a `ValidChannel` can only be built by the validator.
- One `no_std` crate shared by firmware, host tool, fuzzers and CHIRP test fixtures.
- Cheap host fuzzing (`cargo fuzz`), probe-rs + defmt/RTT, and embassy async for USB, audio DMA,
  SPI, GPS and UI tasks.
- Our scope (USB modem, few models) is narrower than OpenRTX's, which keeps a rewrite viable.

Licensing: drivers ported by reading OpenRTX (GPLv3) are derivative works, so the project stays **GPLv3**.

## Workspace layout

```
crates/codeplug   no_std: schema, validation, defaults, (de)serialization
crates/proto      no_std: USB framing (COBS + CRC), channel mux, commands
crates/dsp        no_std: resampler, AFSK1200, later M17 / Codec2 glue
crates/drivers    no_std: at1846s, hr_cx000, display, w25q, keypad, gps
crates/board      per-model pin maps + init (features: md3x0, mduv3x0)
firmware/         embassy application (thumbv7em-none-eabihf)
host/             Linux CLI + daemon
chirp/            CHIRP driver module
```

## Embassy tasks

| Task | Responsibility |
|---|---|
| `usb` | OTG_FS composite: custom UAC1 class + CDC-ACM |
| `audio` | DMA ADC capture (PC3), PWM playback (TIM3 CH3), resampling |
| `radio` | AT1846S / PLL + HR_Cx000, PTT, TX timeout, squelch, RSSI |
| `ui` | input events → controller → view. The only owner of the display. |
| `storage` | codeplug store (A/B, CRC) |
| `gps` | UART NMEA (MD-390) |
| `power` | battery, backlight |

Memory map: TYT bootloader at `0x08000000–0x0800BFFF` (never touched), firmware linked at
`0x0800C000` (`memory.x`), then wrapped for the TYT bootloader with existing tools.

## Rust-specific findings

| Area | Situation |
|---|---|
| STM32F405 (OTG_FS, DMA, ADC, timers) | ✅ embassy-stm32 |
| Display, keypad, SPI flash, AT1846S | Port from OpenRTX (small register sequences) |
| C5000/C6000 init, two-point modulation, calibration | Port from OpenRTX. Magic numbers copied from TYT firmware; the main risk. |
| MD-380 discrete RF (bit-banged PLL, MOSI shared with C5000) | Port from OpenRTX, fiddly |
| USB audio | embassy-usb `uac1::speaker` needs an explicit feedback endpoint (no endpoint left for it); the UAC1 source/microphone class was unmerged at the time (embassy PR #5600). **Write a custom UAC1 class** (adaptive OUT, async IN). |
| M17, Codec2 | FFI to the existing C first, port later |
| AMBE (code taken from TYT firmware) | Called through function pointers, same as from C |

## Spike (go/no-go, 1–2 weeks, MD-UV390)

1. Boot through the TYT bootloader. defmt over RTT via probe-rs.
2. Display, backlight, keypad, knob, PTT.
3. USB composite (custom UAC1 + CDC-ACM) enumerates on Linux. Audio plays via the C6000 DAC FIFO.
4. AT1846S + C6000 FM RX/TX on a calibrated frequency, verified on an SDR.

If step 4 is a slog, stay on C. A middle ground: compile the codeplug and protocol code as a Rust
staticlib with a C ABI and link it into the OpenRTX fork.

## When to revisit

- After phase 2 of [plan.md](plan.md), once the codeplug format and protocol are stable. They're the
  best candidates for a first Rust staticlib.
- Or earlier, if the OpenRTX codebase becomes the bottleneck (UI rework, USB stack replacement).

Sources:
- [embassy-usb UAC1](https://docs.rs/crate/embassy-usb/latest/source/src/class/uac1/mod.rs)
- [embassy PR #5600](https://github.com/embassy-rs/embassy/pull/5600)
