# TYT MD-380/390 & MD-UV380/390 alternative firmware

A working OpenRTX patch example for **FM receive and transmit over USB on an
unmodified dual-band Tytera handheld**. Linux sees a sound card and a serial
control port. The optional **Siil Udus** bot listens, answers over radio, and
shows conversations in a local web UI.

Confirmed on the user's MD-UV380/390-family radio; single-band MD-380/390 builds
are included but are not claimed to have the same hardware verification.
Start with the [confirmed dual-band example](docs/confirmed-dualband.md) for
observations, prerequisites, build/flash commands and the limits of the result.

## Documents

- [docs/confirmed-dualband.md](docs/confirmed-dualband.md): working example and confirmed hardware observations
- [docs/findings.md](docs/findings.md): hardware facts, signal paths, what works without mods, datasheet sources
- [docs/plan.md](docs/plan.md): phased plan: USB audio → codeplug hardening + CHIRP → UI → DMR → ...
- [docs/design.md](docs/design.md): language-independent design: validated codeplug, PMR fallback, CHIRP, USB protocol, UI, TX safety
- [docs/rust-plan.md](docs/rust-plan.md): Rust/embassy rewrite (**deferred**)

## Current status

Based on [OpenRTX](https://github.com/OpenRTX/OpenRTX) (GPLv3, C/C++), with
two firmware patches listed in [patches/series](patches/series):
[codeplug recovery](patches/openrtx-codeplug-recovery.patch), followed by
[USB audio/control](patches/openrtx-usb-audio.patch).

| Feature | Status |
|---|---|
| USB FM playback → radio TX | Implemented for both TYT families; exercised on the dual-band radio |
| USB FM RX → host, MD-UV380/390 | Working experimentally through C6000 receive RAM, 8 kHz mono, without hardware mods |
| USB FM RX → host, MD-380/390 | Implemented through the band-limited ADC path; separate hardware validation needed |
| USB serial control | Framed CDC commands for PTT, status, diagnostics, and channel-change events |
| ALSA controls | PTT mode, playback mute, capture squelch, TX power, and named codeplug channels |
| Physical channel knob → PC | Implemented; channel name/frequency pushed over serial to the bot UI; latest firmware needs hardware verification |
| Corrupt codeplug recovery | Validated native records; 16 TX/RX NFM PMR446 channels at 1 W in RAM, without erasing flash; VHF-only fallback is 145.500 MHz FM calling with RX/TX at 1 W |
| Siil Udus radio bot | Estonian/English audio understanding, grumpy hedgehog replies, chipmunk TTS, SSE web UI, timestamped recordings |

CHIRP integration, Hamlib, KISS/TNC, DMR USB audio, and custom DMR data remain
project goals, not delivered features of this patch. Existing OpenRTX M17 paths
are retained; USB FM audio does not add an M17 receive hardware path.

See [USB bring-up](docs/usb-bringup.md) for the implemented protocol, hardware
limitations, build details, and remaining validation. Planning documents describe
broader goals and should not be read as a list of working features.

## Build and flash

Install the toolchain and host dependencies described in the
[example](docs/confirmed-dualband.md#reproduce-the-firmware), then:

```bash
./scripts/prepare-firmware.sh
./scripts/build-firmware.sh
python3 -m unittest discover -s tests -v
```

Put the dual-band radio into firmware-update mode, then flash and reboot:

```bash
sudo firmware/subprojects/radio_tool/radio_tool -d 0 -f \
  -i artifacts/openrtx_mduv3x0_usb.bin
```

For the single-band MD-380/390, use `artifacts/openrtx_md3x0_usb.bin` instead.
Firmware has been flashed and exercised during development; each new build still
needs hardware verification. Host tests include corruption handling, USB framing, clip recording, and clean-checkout patch application.

## Siil Udus

```bash
python3 scripts/radio-reply.py --direct-audio --transmit --loop
```

Open **http://127.0.0.1:8765** for live SSE status, caller/Siil chat bubbles,
recording playback/downloads, and pause/resume controls. Requires `parec`, `paplay`,
`pactl`, `ffmpeg`, and access to the configured Gemma/Piper services. `kubectl`
provides the Piper token unless `TTS_API_KEY` is set. The services run on `ms-s1`;
CPU Whisper remains available by omitting `--direct-audio`.

Capture stays open during inference. Each incoming transmission starts its own
WAV and ends after 0.8 seconds of quiet or 30 seconds, then triggers the LLM on
that clip. Later transmissions are recorded separately. At the time limit,
capture waits for quiet before rearming. Replies wait for a quiet channel before
PTT. Detection uses squelch-gated audio rather than a hardware carrier signal;
keep USB squelch enabled. PTT mode must be **Triggered**, with playback unmuted.

Replies use the caller's language, the appropriate Estonian/English Piper voice,
a chipmunk pitch effect, and loudness normalization. Timestamped UTC directories
under `artifacts/radio-replies/` contain `received.wav`, `transcript.txt`,
`reply.txt`, `reply.wav`, and `turn.json`. Conversation history is displayed and
saved, but earlier turns are not automatically sent as model context.

Physical knob updates require the latest firmware. To inspect them with the bot
stopped:

```bash
python3 scripts/tyt-usb.py /dev/ttyACM0 watch
```

## Patch size

Measured on 2026-11-08 against upstream base `d5456734b94057b1b30624922a48dbfeb8853b29`:

- Codeplug patch: **5 files, 421 added lines, 14 removed lines**.
- USB patch: **33 files, 2,020 added lines, 61 removed lines**.
- Together: **36 unique files**, net **+2,366** lines (two shared files).
  These are Git patch lines, including comments and blanks. Host bot/UI/scripts
  and Kubernetes changes are outside the OpenRTX patch.
- Original tracked OpenRTX checkout: **146,861 source/build/config lines** using
  `cloc`, excluding blanks, comments, Markdown, text, reStructuredText and SVG.
  This includes bundled MCU headers, drivers and libraries, but not separately
  fetched dependencies or build output.
- The hardware-independent `openrtx/` core alone: **21,434 code lines**.

## Local development dependencies

Keep these in the workspace for subsequent builds:

- `firmware/`: OpenRTX checkout, including its adapted Miosix kernel in
  `firmware/lib/miosix-kernel/`.
- `firmware/subprojects/tinyusb/`: TinyUSB checkout for USB audio and serial work.
- `toolchains/arm-miosix-eabi/`: Miosix GCC 9.2.0-mp3.2, extracted locally without
  running the system installer. The downloaded installer is retained in `toolchains/`.

Activate the compiler in a Bash shell before configuring or building OpenRTX:

```bash
source toolchains/env.sh
arm-miosix-eabi-gcc --version
```

Apply to the upstream base in this order (the codeplug patch also works alone):

```bash
git -C firmware apply ../patches/openrtx-codeplug-recovery.patch
git -C firmware apply ../patches/openrtx-usb-audio.patch
```

The existing `firmware/` working tree already includes both; do not reapply them
there. These commands are for a clean checkout of the documented upstream base.

Regenerate both patches after firmware edits:

```bash
python3 scripts/export-firmware-patch.py
```

## Repository contents and license

Source, patches, host tests and documentation are included. `firmware/`, downloaded
toolchains/dependencies, local datasheet PDFs, generated binaries, audio recordings,
Python environments and local credentials are ignored. The sibling Kubernetes
repository is not part of this example.

Project code and firmware modifications are GPL-3.0-or-later; see [LICENSE](LICENSE)
and file SPDX notices. Upstream files retain their existing notices and licensing.
Third-party datasheets are linked from the findings rather than redistributed.
