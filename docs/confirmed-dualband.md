# Working example: unmodified dual-band TYT USB FM radio

Status recorded on 2026-11-08 from the user's bench tests. This is a working
firmware example, not just a proposed signal path. The patch series and host
scripts reproduce the setup; the recordings and downloaded tools stay local.

## Confirmed behavior

The test radio is from the MD-UV380/390 dual-band family, described by the user
as an MD-UV390. Its factory identity exposed over USB is `Tytera MD-UV380`.
No RF-to-MCU hardware modification was made. Do not infer a board revision or
compatibility with every UV390 Plus variant from this result.

| Observation | Evidence |
|---|---|
| Composite USB audio and serial enumerate on Linux | User supplied ALSA and PipeWire device listings and successful `/dev/ttyACM0` commands |
| Received FM voice reaches the PC without the PC3 wire mod | User recognized recorded voice, then ran continuous radio receive into the voice bot |
| USB capture is native 8 kHz mono S16_LE | User's PipeWire source listing reports `s16le 1ch 8000Hz` |
| USB playback is 48 kHz mono S16_LE | User's PipeWire sink listing reports `s16le 1ch 48000Hz` |
| PC audio transmits through the radio | User heard synthesized replies on the receiving radio, including subsequent level adjustments |
| Two-way FM voice application works | User confirmed the bot worked and provided caller transcriptions/replies from live radio use |

The hardware confirmation covers the core no-mod FM RX/TX path. The latest
physical-knob event reporting and VHF-only calling-channel fallback have been
built and host-tested, but have not separately been confirmed on the radio.
Single-band RX, RF power/deviation/spectral measurements, bit-error performance,
M17 reception through this new path, and long-duration soak tests remain open.

## Why no RF receive wire is needed for this example

The dual-band radio already feeds its demodulated signal into the C6000. In FM
mode, C6000 RX RAM exposes two alternating 512-byte blocks at `0x030–0x22f` and
`0x230–0x42f`: 256 big-endian signed 16-bit samples per half, at 8 kHz (32 ms).
The patch reads these over SPI and sends little-endian PCM through USB capture.
It does not connect or emulate the missing analog signal at STM32 PC3.

Initial read-only snapshots gave recognizable but chopped audio. Fast register
readback also lost bit 7: `RXREGS` returned `30,00,84,49` for ordinary fast reads,
versus `b0,80,04,c9` with extended/slow reads. The continuous implementation had
to enable the FM-ready IRQ, preserve ownership state on early notifications,
and use the slower, bounded SPI transport. Working voice reception followed
those corrections. See [findings](findings.md) for the investigation and sources.

### What this changes about OpenRTX's hardware modifications

[OpenRTX's dual-band modification guide](https://openrtx.org/#/M17/mduv380_mods)
is explicitly a guide to running **M17**, and lists two missing analog paths:
RF → MCU and microphone → MCU. Our result has a narrower scope:

- For **FM voice received over USB**, the RF → MCU wire is unnecessary on the
  tested radio: C6000 RAM supplies the samples through SPI instead.
- For **PC-generated FM transmit audio**, the microphone modification is also
  unnecessary because the PC supplies the audio; the existing MCU → RF path works.
- This does **not** prove that either modification is obsolete for standalone
  M17 operation with the radio microphone, or that the 8 kHz FM voice buffer is
  suitable for M17/9600-baud demodulation. We have not demonstrated those modes.

The appropriate claim is “no hardware modifications needed for dual-band FM USB
voice,” not “OpenRTX no longer needs M17 hardware modifications.”

## Reproduce the firmware

From the repository root, install Git, Python 3.12+, a native C/C++ compiler,
Meson, Ninja, CMake, pkg-config and libusb development headers. Use the tested
Miosix GCC **9.2.0-mp3.2** toolchain (not a generic ARM GCC).

Download the matching Linux installer from the
[Miosix toolchain page](https://miosix.org/wiki/index.php?title=Miosix_Toolchain).
Place it in `toolchains/`, verify its published checksum, and extract locally:

```bash
cd toolchains
printf '%s\n' 'd71fdac28a6fc4a4c0b41e90fb46c3722805b04e1ddb355d0da2f1b67e50ef3e  MiosixToolchainInstaller9.2.0mp3.2.run' | sha256sum -c -
sh MiosixToolchainInstaller9.2.0mp3.2.run --noexec --keep
cd ..
source toolchains/env.sh
arm-miosix-eabi-gcc --version
./scripts/prepare-firmware.sh
./scripts/build-firmware.sh
python3 -m unittest discover -s tests -v
```

The preparation script checks out upstream
`d5456734b94057b1b30624922a48dbfeb8853b29` and applies, in order:

1. `patches/openrtx-codeplug-recovery.patch` — validated native codeplug parsing
   and RAM-only recovery. This patch can also be used without USB changes.
2. `patches/openrtx-usb-audio.patch` — FM USB audio, framed serial control,
   diagnostics, mixer selectors and channel-change reports.

TinyUSB is pinned by the patch to
`c9e8ac5f4022030d8d66d76f042b3615f234bd61`. Meson fetches missing dependencies
on the first build. Images go to `artifacts/`, along with ELF files and hashes.

With the dual-band radio in its firmware-update mode:

```bash
sudo firmware/subprojects/radio_tool/radio_tool -d 0 -f \
  -i artifacts/openrtx_mduv3x0_usb.bin
```

Reboot into OpenRTX. Do not use the single-band image on the dual-band radio.
If the native codeplug is unusable, UHF-capable units get PMR 01–16 in RAM;
VHF-only units get `FM CALL` at 145.500 MHz. Both recovery sets enable RX/TX at
the 1 W setting. Flash codeplug/settings are preserved.

## Exercise USB audio

Choose an FM channel on the radio and identify the host devices:

```bash
python3 scripts/tyt-usb.py /dev/ttyACM0 status
python3 scripts/tyt-usb.py /dev/ttyACM0 rxstat
pactl list short sources
pactl list short sinks
cat /proc/asound/cards
```

The radio source name starts with `alsa_input.usb-TYT__Tytera__`; its playback
sink starts with `alsa_output.usb-TYT__Tytera__`. Use the names from your machine,
not the USB instance numbers or serial number from someone else's listing.

For listening through laptop speakers, set `radio_source` and `speaker_sink` to
the matching names from those listings, then run:

```bash
pactl load-module module-loopback source="$radio_source" sink="$speaker_sink" latency_msec=100
```

This prints a module ID. Remove that loopback afterward using
`pactl unload-module ID`. Choose the laptop speaker sink, not the radio TX sink.
Prefer PulseAudio/PipeWire playback when it already owns the ALSA device; direct
`aplay` access can fail with “Device or resource busy.”

In `alsamixer`, choose the radio card. PTT Mode should be **Triggered** for the
bot, playback unmuted, and USB capture squelch enabled so a closed channel emits
zeros. `RF Channel` selects codeplug entries, and `TX Power` selects the channel
setting or a calibrated power request. These controls do not rewrite the codeplug.

## Optional Siil Udus example

The firmware works independently of the AI services. The bot additionally needs
`parec`, `paplay`, `pactl`, `ffmpeg` and reachable model services:

- Gemma 4 E4B with audio input and the matching projector, using a llama.cpp
  OpenAI-compatible endpoint. Set `GEMMA_AUDIO_URL` and `GEMMA_API_KEY`.
- Piper's HTTP speech endpoint with Estonian and English voices. Set `TTS_URL`
  and `TTS_API_KEY`. The default voice names are `et_EE-news-medium` and
  `en_US-lessac-medium`.
- Alternatively, omit `--direct-audio` and provide Whisper (`ASR_URL`) plus
  a text model (`GEMMA_URL`).

The defaults refer to the author's private deployment on `ms-s1`; those endpoints
and the sibling Kubernetes repository are not required or supplied for a fresh
firmware checkout. Configure equivalent services on your own network. No API keys
are stored here. With the services configured:

```bash
python3 scripts/radio-reply.py --direct-audio --transmit --loop
```

Open `http://127.0.0.1:8765`. Each transmission gets its own timestamped WAV;
only the completed clip is sent to the model. The bot waits for quiet before
replying. “Quiet” is inferred from squelch-gated audio, not a hardware carrier
indicator. Radio reception is unavailable during its own transmission.
