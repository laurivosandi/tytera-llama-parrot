# USB audio and serial bring-up

The two-patch OpenRTX example implements native-codeplug validation and recovery,
plus opt-in USB audio/control for MD-380/390 (`md3x0`) and MD-UV380/390 (`mduv3x0`).
**No-mod FM RX and TX over USB are confirmed working on the user's dual-band
radio**, including received speech feeding the Siil Udus bot and spoken replies
transmitted back. See [confirmed dual-band example](confirmed-dualband.md).

Both targets build and have host tests. Single-band hardware, calibrated RF
measurements, long-duration reliability, and the newest physical-knob event
reporting are not covered by that confirmation.

## Source and builds

OpenRTX base: `d5456734b94057b1b30624922a48dbfeb8853b29`.
TinyUSB: `c9e8ac5f4022030d8d66d76f042b3615f234bd61` (pinned in the patch).
The local `firmware/` checkout contains the applied changes. The parent repository
keeps `patches/openrtx-codeplug-recovery.patch` followed by
`patches/openrtx-usb-audio.patch` (see `patches/series`); the checkout,
downloaded dependencies, compiler binaries, and generated images are ignored.

```bash
# Restore the pinned checkout and apply both patches in order, if needed:
./scripts/prepare-firmware.sh
# Use the existing workspace-local compiler and dependencies:
./scripts/build-firmware.sh
# Verify the firmware protocol and FM state machine on the host:
python3 -m unittest discover -s tests -v
```

Build requirements: Miosix GCC 9.2.0-mp3.2, Meson, Ninja, CMake, a native C/C++
compiler, pkg-config, and libusb development files. This workspace has the compiler
and downloaded host packages in `toolchains/`. The Miosix installer was extracted
with `--noexec --keep`; it was not installed system-wide. On a fresh machine,
extract the installer in `toolchains/` and install libusb development files or
provide an equivalent local pkg-config environment.

The build script disables radio_tool's optional BLE dependency, builds the USB
packaging tool, and selects `-Dusb_audio=true`. Default builds retain the legacy
USB stack. Other radio targets also retain their existing USB implementations.
The new `radio_setTxAudioSource` interface is implemented by the two TYT families.

Generated files:

| File | Purpose |
|---|---|
| `artifacts/openrtx_md3x0_usb.bin` | TYT-wrapped MD-380/390 firmware |
| `artifacts/openrtx_mduv3x0_usb.bin` | TYT-wrapped MD-UV380/390 firmware |
| `artifacts/openrtx_*_usb.elf` | Symbols and loadable sections for debugging |
| `artifacts/SHA256SUMS` | Checksums of these build artifacts |

After further source edits, regenerate both distributable patches with
`python3 scripts/export-firmware-patch.py`. This does not stage or commit files.

## Implemented behavior

### Radio reply character

`scripts/radio-reply.py` (Siil Udus) keeps capture open and writes one WAV per
incoming transmission, starting at the first above-threshold audio block. The
clip closes after `--silence` (default 0.8 seconds) of quiet or `--seconds`
(default and maximum 30 seconds). The closed clip immediately becomes eligible
for LLM processing; completed clips received during inference are queued
separately. At the time limit, the remaining transmission is ignored until quiet
before rearming, so it does not create repeated replies to the same transmission.
Activity and channel availability use squelch-gated PCM, not a hardware carrier
signal. Keep USB squelch enabled; silence inside an open carrier can still look
like the end of a transmission.

Gemma replies in the caller's language (Estonian or English), and Piper selects
`et_EE-news-medium` or `en_US-lessac-medium`. Speech is pitch-shifted, normalized
to -16 LUFS with a -1.5 dB true-peak target, then scaled by `--gain` (default 1).
Before PTT, the bot waits for fresh capture samples and the configured quiet
interval. Capture continues during processing and waiting, but samples during
TX and a 300 ms settling guard are excluded. Receiver key-up buffering and PTT
cleanup reuse the announcement client.

The local dashboard at `http://127.0.0.1:8765` uses SSE for status and conversation
updates. It shows caller bubbles on the left and Siil on the right, with playback,
downloads, pause/resume, and a manual reply trigger. `--web-port 0` disables it.
Pause suppresses new processing/transmissions, but does not interrupt an already
keyed reply; capture continues. No browser action enables RF without `--transmit`.

Whisper CPU, Gemma Vulkan and Piper are configured for `ms-s1` in ee-west-2.
The laptop only handles audio, HTTP, and the pitch effect. Python standard library,
`parec`, `paplay`, `pactl`, `ffmpeg`, and `kubectl` (for the Piper token) are needed;
no local Whisper package is required. No local Python speech-model dependencies are required.

```bash
# Generate a reply WAV without recording or RF transmission:
python3 scripts/radio-reply.py --text 'Siil, miks sa nii pahur oled?'
# Receive one utterance, then transmit the answer:
python3 scripts/radio-reply.py --transmit
# Keep taking turns until Ctrl-C:
python3 scripts/radio-reply.py --transmit --loop
```

The input is autodetected when exactly one Tytera source is present. Use `--source`
and `--sink` to choose among multiple radios; names come from `pactl list short
sources` and `pactl list short sinks`. Keep PTT Mode Triggered and Radio playback
unmuted. Use USB squelch to avoid treating continuous hiss as speech. `--threshold`
sets PCM RMS activity detection, `--silence` changes the end-of-turn gap, and
`--pitch` (default 1.5) controls the chipmunk effect. Raw recording, transcript,
reply text and WAV are retained under UTC timestamped directories such as
`artifacts/radio-replies/2026-11-07T20-15-30.123456Z/`: `received.wav`,
`transcript.txt`, `reply.txt`, `reply.wav`, and `turn.json` (timing and state).
Each directory is created when recording starts. Existing `reply-*` archives
also appear in the dashboard.

Endpoints are configurable: `ASR_URL` / `--asr-url` defaults to
`http://[fd10:0:0:fd00::84]:8000/v1/audio/transcriptions`; `GEMMA_URL` / `--gemma-url`
defaults to `http://10.253.0.81:8080/v1`; `TTS_URL` / `--tts-url` defaults to
`http://10.253.0.83:8080/v1/audio/speech`. ASR shares the existing llama API secret;
`ASR_API_KEY` overrides it. Gemma uses `GEMMA_API_KEY` or the private key file
`~/.config/opencode/gemma-api-key`. Piper uses `TTS_API_KEY` or its existing
Kubernetes secret. Keys are not saved in the output artifacts.

### Named RF channels and TX power

The playback mixer now exposes `RF Channel Playback Source` and
`TX Power Playback Source`:

```bash
amixer -c N cget name='RF Channel Playback Source'
amixer -c N cset name='RF Channel Playback Source' 'PMR 1'
amixer -c N cset name='TX Power Playback Source' Channel
# Override only USB-fed FM transmissions, without editing the codeplug:
amixer -c N cset name='TX Power Playback Source' '1 W'
```

Use the exact channel item name printed by `cget`. Names are read from the first
127 global codeplug entries at boot; the full 16-channel PMR fallback fits.
The `Radio` item keeps current settings. Selection loads the full codeplug entry
through the UI, including frequencies, mode, bandwidth, tones, RX-only and power.
It exits the bank filter and enters memory mode; requests wait until PTT is
released. Invalid/out-of-band channels are rejected. Changing the codeplug requires
a reboot to refresh USB names; manual front-panel changes are not pushed as ALSA
control notifications. The selector reports the last requested USB selection.

TX Power offers Channel (default) and 1 W through 5 W. Overrides apply on the
next USB-fed FM key-down, preserve TX-disable and the hardware calibration/clamp,
and do not change physical microphone TX, M17, or saved channel power. The labels
are requested calibrated power, not measured RF output.

### Native dual-band FM capture and USB squelch

MD-UV380/390 now has an experimental continuous FM receive path at native
8 kHz mono S16_LE. Playback stays at 48 kHz. The host can resample capture if
needed; firmware does not upsample it. MD-380/390 ADC capture stays at 48 kHz.

```bash
# Reflash the dual-band image in firmware-update mode, then reboot:
sudo firmware/subprojects/radio_tool/radio_tool -d 0 -f -i artifacts/openrtx_mduv3x0_usb.bin
arecord -l
# Replace N with the radio's current card number:
amixer -c N cget name='Squelch Capture Source'
amixer -c N cset name='Squelch Capture Source' 5
arecord -D hw:N,0 -f S16_LE -c 1 -r 8000 -d 20 artifacts/fm-continuous.wav
python3 scripts/tyt-usb.py /dev/ttyACM0 rxstat
# Raw unsquelched audio:
amixer -c N cset name='Squelch Capture Source' Off
```

`Squelch Capture Source` is a standard USB Audio selector with named choices
Off and 1..15. It controls USB audio independently of the speaker's squelch,
using OpenRTX's RSSI threshold mapping and one dB hysteresis. Off is the
power-on default; no settings are written to flash. It is an RSSI gate and
does not implement CTCSS tone filtering. Closed squelch supplies exact-zero
PCM while capture stays running. Applications can detect zero audio, but that
alone cannot distinguish squelch from another source of silence. Buffered audio
adds latency to gate changes.

The C6000 path temporarily enables FM data-ready in the interrupt mask,
restores the prior mask on stop/disconnect/TX/mode exit, acknowledges only that
interrupt bit, and
reads a whole 512-byte half in one extended-address SPI transaction using the
slower diagnostic transport. This avoids the normal 6.46 MHz transport, above
the manual's 4 MHz limit, without changing normal radio or M17 SPI timing. Changes
in each half's trailing samples establish ownership; after synchronization,
identical buffers use expected alternation. Ambiguous or missed events are
counted and discarded. A second marker read checks for overwrite during copying.
Native asynchronous USB packets accommodate clock drift, with about 48 ms of
initial silence buffering. `rxstat` reports accepted blocks, dropped/uncertain
blocks, starvation ticks (`underruns`), ready events, and queued samples.
`python3 scripts/tyt-usb.py /dev/ttyACM0 rxdrop` separates early notifications,
ambiguous ownership, loss of synchronization, changed-during-copy data (`torn`,
also includes burst read errors), marker read errors (`io`), and full USB FIFO.
Early notifications do not advance the marker baseline; a uniquely changed
half can re-establish synchronization after a scheduling gap. Constant data before
ownership is established may delay synchronization.

Capture is enabled only by the FM receive FSM. It stops and flushes queued
samples before TX or changing modes, including M17. The normal SPI transport,
TX audio, and M17 modulation/demodulation are unchanged. Diagnostic snapshots
are rejected while continuous capture is running. This new continuous path and
mixer selector have passed host tests and both firmware builds; radio verification
of buffer ordering, continuity and mixer enumeration is still required.

### Dual-band C6000 receive probe

The experimental `scripts/probe-fm-rx.py` reads the two documented FM receive
RAM buffers through CDC. It does not change routing, acknowledge interrupts,
key TX or activate ALSA capture. Stop any ALSA capture before probing.
The firmware rejects the probe outside reception or while USB playback is active.

Flash the latest dual-band image, select an FM channel, stop USB playback and
receive a known steady tone from another radio or signal generator. Then run:

```bash
python3 scripts/probe-fm-rx.py --port /dev/ttyACM0 \
  --snapshots 16 --output artifacts/fm-rx-tone.jsonl
```

Repeat without the tone, using a different output filename. The script refuses
to overwrite existing evidence. Each JSONL record contains one 64-byte chunk,
its address, device start/end milliseconds, host timestamp, mode, IRQ mask/status
and raw bytes in hex. A full snapshot covers `0x030..0x42f` in 16 commands.
The terminal shows per-half big/little-endian signed 16-bit statistics and how
many bytes changed since the previous snapshot. These statistics do not prove
sample format or audio continuity. Zero/stale buffers are a useful negative result.

If register `0x10` reads zero, compare the read transports before interpreting
RAM. This command creates no capture file:

```bash
python3 scripts/probe-fm-rx.py --port /dev/ttyACM0 --registers-only
```

`RXREGS` reports CONFIG addresses `01,10,81,e0` in that order, using the normal
fast transport and a diagnostic transport with 1-microsecond settling delays,
each with ordinary and extended addressing. FM initialization writes
`b0,80,04,c9` respectively (other operations may subsequently change values).
The RAM probe now uses the slower diagnostic transport. Normal radio and TX
SPI timing remain unchanged. The two transports share the same bus mutex.
A zero mode read does not establish that the radio is actually in digital mode;
it may indicate unsuccessful readback. The timing experiment is not yet verified
on hardware.

The snapshot is not atomic: the chip may overwrite RAM during a read, and mode
or PTT may change after the initial check. Individual SPI reads use the existing
bus lock. No chip registers are written. Establish buffer ownership, freshness,
rate and format when validating the experimental continuous ALSA receive stream.

### ALSA PTT selector and mute

The USB Audio 1.0 selector exposes `Triggered` (power-on default) and `Always`.
In Always mode, unmuted playback packets key FM TX, including silent samples.
Opening a handle without starting playback does not key TX. Stopping playback,
muting, disconnecting or losing packets for 100 ms releases automatic PTT.
The 60-second timeout and `txDisable` still apply. After timeout or packet loss,
stop/start playback, mute/unmute, or switch modes to rearm. CDC `PTT 0` controls
only the manual request; use mute or stop playback to stop automatic TX.
Physical PTT remains independent. These mixer settings are not saved in flash.

After flashing, reconnect USB, find the card with `aplay -l`, and inspect controls:

```bash
amixer -c N controls
amixer -c N cget name='PTT Mode Playback Source'
amixer -c N cset name='PTT Mode Playback Source' Always
amixer -c N cset name='Radio Playback Switch' on
aplay -D plughw:N,0 artifacts/time-announcement.wav
# Release USB PTT through the standard playback mute:
amixer -c N cset name='Radio Playback Switch' off
# Restore the existing CDC/physical PTT workflow:
amixer -c N cset name='PTT Mode Playback Source' Triggered
amixer -c N cset name='Radio Playback Switch' on
```

Linux adds the selector's `Playback Source` suffix ([ALSA USB mixer parser](https://github.com/torvalds/linux/blob/master/sound/usb/mixer.c)).
The choices are also available in `alsamixer -c N` (F5 to show all controls).
Always mode follows the USB stream, so a desktop audio server holding it open
with silence can keep TX keyed until timeout. Direct ALSA playback gives explicit
stream lifetime. The controls still need enumeration and RF testing on hardware.

USB manufacturer is `TYT (Tytera)`; product is `Tytera` plus the valid factory
model name, with a model-family fallback. The serial remains the 24-hex-digit
STM32 unique ID, allowing multiple radios to be distinguished.

Dual-band flashing from the workspace, with the radio in firmware-update mode:

```bash
sudo firmware/subprojects/radio_tool/radio_tool -d 0 -f -i artifacts/openrtx_mduv3x0_usb.bin
```

### Estonian time announcements

`scripts/announce-time.py` rounds the current Europe/Tallinn time to the nearest
half-hour and synthesizes the current date and time with the local Piper neural
speech service. For example, on 7 November 2026 at 15:15–15:44 it produces
“Täna on seitsmes november 2026. Kell on pool neli.” Day ordinals are spelled
out for TTS pronunciation. Ties round forward. This is a
one-shot command; it does not install a repeating schedule.

The existing `../kube/llm/templates/tts.yaml` deployment is configured in
`../kube/values.ee-west-2.yaml` with `et_EE-news-medium` (four speakers; default
Albert). It exposes `http://10.253.0.83:8080/v1/audio/speech`. The client reads
`TTS_API_KEY` or obtains the existing `llm/piper-tts-api-token` Secret using the
`admin@ee-west-2.codemowers.io` context. Credentials are not cached. Override the
endpoint with `TTS_URL` or `--tts-url`.

Requirements: Python 3 with timezone data, `ffmpeg`, `aplay` (alsa-utils), and
`kubectl` if using the cluster credential lookup. No Python packages are needed.

```bash
# Check wording without accessing TTS or the radio:
python3 scripts/announce-time.py --at 15:30 --print-text

# Generate a WAV without transmitting (default behavior):
python3 scripts/announce-time.py

# Find the radio's ALSA card, then announce on its currently selected FM channel:
aplay -l
python3 scripts/announce-time.py --transmit --device plughw:N,0 --port /dev/ttyACM0

# Announce a fixed phrase instead:
python3 scripts/announce-time.py --text 'Kell on pool neli.' --transmit --device plughw:N,0
```

Replace `N` with the radio's card number. Generated audio is saved as
`artifacts/time-announcement.wav`; speech is cached in `artifacts/tts-cache/`.
The client converts speech to 48 kHz mono S16_LE, applies gain 0.5 (adjustable
with `--gain`), reserves 1.5 seconds for key-up, then adds a receiver buffer of
two seconds (adjustable with `--receiver-delay`, 0–5) and half a second of tail,
renews USB PTT during playback, and releases PTT on completion or error. It
requires a PTT acknowledgement within the key-up budget, preserving the full
receiver buffer before speech, and enforces a
55-second playback deadline. PTT acknowledgement means the firmware accepted
the request; channel TX restrictions still apply. Generated WAVs and host tests
do not verify RF output or audio deviation.

### Firmware

- One UAC1 function: mono signed 16-bit little-endian PCM; playback at 48 kHz and
  capture, alongside one CDC-ACM serial port.
- Audio endpoints `01/81`, CDC bulk `02/82`, CDC notification `83`. This uses three
  IN and two OUT endpoints beyond EP0. The remaining OUT endpoint cannot provide
  the IN feedback endpoint an asynchronous speaker would require.
- Playback is adaptive without feedback. A bounded linear resampler consumes
  479/480/481 input samples per 480-sample DMA block to control FIFO occupancy.
  Capture uses TinyUSB's IN flow control, allowing 47/48/49 samples per USB frame.
- FM host-to-radio audio uses TIM3 PWM and selects the baseband chip's MCU line
  input. Host PCM is sent without FM pre-emphasis; microphone TX retains it.
  Input level and deviation need bench calibration.
- MD-380/390 FM capture uses ADC2 channel 13, converting right-aligned unsigned
  12-bit samples to signed PCM. It is the existing band-limited RF tap. Capture
  coexists with the local speaker and is unsquelched.
- Unmodified MD-UV380/390 has experimental native 8 kHz C6000 capture.
  Capture returns silence outside FM reception, including during TX.
- In default Triggered mode, streaming alone does not key RF. In FM, CDC PTT
  or physical PTT can key it. Always mode also keys on unmuted USB playback.
  If playback is active at key-down, that transmission uses USB audio; otherwise
  physical PTT uses the microphone. The source stays fixed until unkeying.
- CDC PTT requires DTR and unmuted live playback. `PTT 1` renews a one-second lease.
  Playback stopping, missing audio packets for 100 ms, dropping DTR, disconnect,
  suspend, or lease expiry releases the USB request. Physical PTT is an independent
  request; loss of playback during USB-sourced TX nevertheless unkeys RF and
  requires PTT release before microphone TX can resume.
- A 60-second continuous FM TX limit applies to all PTT sources in this build.
  USB keepalives cannot extend it; `PTT 0` resets the USB timeout latch. Local PTT
  must be released to reset the FM timeout latch. Existing `txDisable` remains
  authoritative. DMA-start/service failure also prevents or terminates USB TX.

This is initial audio/control bring-up. It does not implement frequency control,
Hamlib, a PTY daemon, VOX, PROG/CHIRP, KISS modulation, GPS forwarding, DMR audio,
or calibration validation and the custom codeplug store described in `design.md`.
Native TYT codeplug validation is implemented as described below.

## Invalid-codeplug recovery

Both TYT builds now validate native channel records at boot. Names are read as
bounded UTF-16 rather than native `wchar_t`; decoded names always terminate.
Checks cover empty/erased names, malformed UTF-16, mode and bandwidth, BCD
frequencies and hardware bands, list-index bounds, UV power selection, DMR
slots and contact references, and SPI read failures. Unknown tones remain
disabled, as in the previous reader; DCS decoding is not added by this change.
The vendor format has no record CRC, so validation cannot identify corruption
that happens to form a structurally valid record.

Valid channels are presented as a compact list, preserving their original order.
Empty/bad slots no longer strand the channel knob. Banks are similarly filtered;
invalid members are removed and remaining references are translated to the
compact channel list. Flash records and their indices are not rewritten. Contact
reads are bounded, and vendor contact references/types are converted to the API's
zero-based representation.

If no valid channels remain (or the index allocation fails), UHF-capable radios
receive 16 RAM-only channels named `PMR 01` through `PMR 16`, from
446.00625 to 446.19375 MHz in 12.5 kHz steps. Each is FM/NFM, simplex TX/RX at the 1 W power setting,
and without tones. The bank is named `PMR TX/RX`. Startup selects PMR 1
in memory mode, replacing the saved VFO only in RAM. Use the channel knob or
up/down arrows to select another PMR channel.

At the user’s explicit request, PMR recovery channels permit physical or USB PTT.
The existing TX timeout and USB keepalive requirements still apply. The 1 W value
is a configured setting; actual RF output has not been measured. Recovery still
suppresses the usual settings/VFO save at shutdown. No codeplug, settings,
or calibration data is erased or overwritten by recovery. The defaults are
regenerated on every boot until a usable codeplug is restored. A VHF-only radio
uses a single `FM CALL` channel at 145.500 MHz, with RX/TX enabled at 1 W,
instead of unsupported PMR446 tuning;
unusable hardware-band information fails initialization rather than guessing.

Codeplug tests run the real reader for both models under AddressSanitizer/UBSan
with mocked flash. They cover erased/zero/random data, malformed records,
partial recovery, zone remapping including UV extension members, out-of-range
indices, I/O failures, DMR contact validation, VHF-only hardware, and unchanged
flash contents. They also run the real shutdown persistence function and verify
that it does not save anything during fallback.

## Serial wire format

The port uses binary frames, not a text terminal. Line coding is accepted by CDC;
115200 8N1 is the host client's default but does not control USB transfer speed.

```
COBS(channel:u8 + payload + crc16_le) + 00
```

CRC is CRC-16/XMODEM: polynomial `0x1021`, initial value zero, no reflection,
no final XOR. It covers the channel and payload. A decoded message is at most
240 bytes including the channel, excluding the two-byte CRC. An encoded frame
is at most 244 bytes including the zero delimiter. Oversized input is discarded
until the next delimiter; malformed COBS and bad CRC frames have no effect.

Channel assignments: CTRL=1, PROG=2, KISS=3, NMEA=4, LOG=5. Only CTRL and outbound
LOG are implemented. Unsupported requests return `ERR unsupported` on their
channel. Raw XMODEM is unavailable in the opt-in build. Existing firmware stdout
is framed as LOG and may be dropped if the host cannot keep up.

CTRL payloads are exact ASCII bytes without a newline or NUL. The UV-only
`RXPROBE NN` diagnostic accepts two decimal digits 00..15 and returns
`RXPROBE NN start_tick end_tick mode_hex irq_mask_hex irq_status_hex data_hex`
(64 raw bytes), or an `ERR` reply. Other commands:

| Request | Reply / effect |
|---|---|
| `STATUS` | Version, sample rate, RX capability, stream flags, PTT request, timeout latch |
| `PTT 1` | `OK requested` when DTR and playback are live; renew every 250–500 ms |
| `PTT 0` | `OK`; release request and clear USB timeout latch |

`OK requested` acknowledges the request, not RF transmission. FM mode and channel
TX permission still decide whether it is acted on. Stream flags describe host
alternate settings; `rx` describes the hardware capability.

## Bench verification

Use the backed-up development radio and the appropriate model image. Firmware,
codeplug and calibration backup plus revision recording remain required bring-up
steps from `plan.md`. No flashing is performed by the build scripts.

After flashing and booting, inspect the programming-cable connection:

```bash
lsusb -v -d 0483:5740
aplay -l
arecord -l
python3 scripts/tyt-usb.py /dev/ttyACM0 status
```

The development build retains OpenRTX's `0483:5740` identity and uses the STM32
unique ID as its serial number. This is not a new USB VID/PID allocation. A
shipping product needs its own allocation.

Use the ALSA card number shown above in place of `N`. Capture should work without
PTT. Check capture against a known injected signal. Use 8000 for UV390 and
48000 for MD-390:

```bash
arecord -D hw:N,0 -f S16_LE -c 1 -r 48000 -d 10 rx.wav
```

For TX, select a permitted FM channel and connect a dummy load/measurement setup.
Start a sufficiently long, low-level mono 48 kHz test WAV in one terminal, then
hold USB PTT from another:

```bash
aplay -D hw:N,0 test-mono-48k.wav
python3 scripts/tyt-usb.py /dev/ttyACM0 ptt --seconds 5
```

Check modulation and deviation on both models before raising PCM level. Then test
concurrent capture/playback/control, stopping playback while keyed, killing the
client, unplugging USB, suspend/resume, repeated alternate-setting changes, changing
FM/M17 modes, and RX-only channels. Verify the one-second lease and 60-second hard
limit with RF measurement. Monitor for FIFO glitches and long-term crystal drift.

## Verification performed

- USB-enabled and legacy firmware compiled and linked for both targets.
- Both USB firmware images wrapped successfully with `radio_tool`.
- Host tests cover the descriptor graph, sample formats and endpoint budget;
  independent Python/C framing agreement for all payload sizes; malformed input
  and 10,000 randomized frames; PTT expiry, keepalive hard limit and timer wrap.
- The real FM state machine is exercised with mock hardware under AddressSanitizer
  and UBSan: TX-disable, source selection, playback loss, DMA failures, local PTT
  timeout/rearm, and USB lease loss. LeakSanitizer alone is disabled because the
  execution sandbox uses ptrace.
- Hardware enumeration, ALSA streaming, RF modulation, and on-device timing have
  not been verified.

### Direct audio trial with Gemma E4B

The separate `llama-gemma-4-e4b-audio` deployment on ms-s1 accepts recorded
speech directly; Piper still generates the chipmunk reply. Models are cached
in `/var/lib/llama/models`. The original CPU Whisper service remains running.

```sh
python3 scripts/radio-reply.py --direct-audio --input-wav artifacts/radio-replies/reply-kabo14lc/received.wav
python3 scripts/radio-reply.py --direct-audio --port /dev/ttyACM0 --transmit --loop
```

Direct audio limits live turns to 30 seconds and rejects longer input files.
Its endpoint defaults to `http://[fd10:0:0:fd00::85]:8080/v1`; override with
`--audio-url` or `GEMMA_AUDIO_URL`. Omit `--direct-audio` to use Whisper again.
Direct mode logs Gemma’s transcription as `Heard` and saves it in
`transcript.txt`; only the reply is spoken. Both fields come from one audio request. Quality on Estonian
radio recordings must be compared experimentally.

### Physical knob channel reporting

Updated firmware publishes the actual UI channel (including bank-to-global index
translation) to USB. UAC `RF Channel` GET_CUR follows the radio, but ordinary ALSA
mixer UIs may cache the last value: no spare IN endpoint exists for an AudioControl
interrupt notification. For live PC updates, CDC `EVENTS 1` subscribes to channel
changes on framed channel 2; command replies remain on framed channel 1. `CHANNEL`
returns the current state immediately; `EVENTS 0` unsubscribes. USB reset or DTR
close unsubscribes. Reports include the zero-based global index (-1 for VFO),
RX/TX frequencies in Hz, and channel name.

```sh
python3 scripts/tyt-usb.py /dev/ttyACM0 channel
python3 scripts/tyt-usb.py /dev/ttyACM0 watch
```

Run the standalone watcher only when the bot is stopped. Siil Udus subscribes
itself and shares one serial object between the event listener and PTT commands.
The web UI shows channel name/frequency through SSE. On older firmware the bot
continues without channel reporting and displays the reason.

### Patch series

The firmware changes are exported as two ordered patches against the documented
upstream base:

1. `patches/openrtx-codeplug-recovery.patch`: standalone native codeplug validation,
   boot fallback, and preservation of flash settings while in recovery. UHF-capable
   radios get PMR 01–16; VHF-only radios get 145.500 MHz `FM CALL`. Both defaults
   enable RX/TX at 1 W, in RAM only.
2. `patches/openrtx-usb-audio.patch`: USB audio, serial control, ALSA selectors and
   physical channel reporting, applied after the codeplug patch.

`patches/series` records the order. `python3 scripts/export-firmware-patch.py`
regenerates both without changing the firmware Git index. Do not apply the patches
again to the already modified working tree.
