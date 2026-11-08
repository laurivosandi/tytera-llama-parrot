# Hardware findings

Current result (2026-11-08): **unmodified dual-band FM RX and TX over USB work**
in the user's setup. The working example, observed behavior and remaining gaps
are recorded in [confirmed-dualband.md](confirmed-dualband.md).

Status markers: **[bench]** = user-confirmed on the dual-band radio;
**[verified]** = confirmed from OpenRTX source or the datasheet; **[docs]** = from
OpenRTX documentation; **[unverified]** = from memory or inference, check before relying on it.

## Platform

| | MD-380 / MD-390 | MD-UV380 / MD-UV390 |
|---|---|---|
| MCU | STM32F405VGT6, 168 MHz Cortex-M4F, 1 MB flash, 192 KB RAM | same |
| DMR baseband | HR_C5000 **[verified]** | HR_C6000 **[verified]** |
| RF | discrete RF; FM via C5000 analog FM mode **[docs]** | AT1846S transceiver **[verified]** |
| GPS | MD-390 only (UART NMEA) | UV390 variants (not in our fleet) |
| SPI flash | 16 MB on 390 variants **[unverified]** | same |

- USB D+/D- go straight to the STM32 OTG_FS port through the K-plug programming cable **[unverified, very likely]**.
- The TYT DFU bootloader is at 0x08000000 and application firmware at 0x0800C000. These addresses come from the firmware layout; recovery behavior has not been exhaustively tested.
- For development, use SWD via the test pads. OpenRTX has `MDx_ENABLE_SWD`, which frees the SWD pins (they are shared with `MIC_PWR`) **[verified]**.

## Audio / signal paths (no mods)

From `platform/drivers/audio/audio_MDx.cpp` and the pinmaps **[verified]**:

| Path | MD-380/390 | MD-UV380/390 |
|---|---|---|
| MCU → RF (TX modulation) | STM32 PWM, TIM3 CH3 | STM32 PWM, TIM3 CH3 |
| MCU → speaker | STM32 PWM | C6000 DAC FIFO over SPI ("OpenMusic" mode, `Cx000_dac`) |
| Mic → MCU | ADC2 ch3. In practice the VOX line, heavily filtered **[docs]** | ADC2 ch3 |
| RF → MCU | ADC2 ch13 = **PC3** (`AIN_RTX`). Fed by the 2T/5T selcall path through a narrow band-pass and a low-pass filter (C556) **[docs]** | **PC3 not connected** to any RX signal without the mod **[docs]** |

What this means:
- **TX from the host works on all models without mods** (USB audio → PWM → modulator).
- **FM RX audio to the host without mods:** MD-380/390 gets band-limited audio only (fine for AFSK 1200/2200 Hz tones, poor for voice or 4FSK). On the UV380/390, the implemented C6000 RX RAM path supplies 8 kHz mono PCM without the PC3 modification **[bench]**.
- **DMR audio is a separate future path.** The current patch implements FM USB audio; it does not provide a DMR vocoder or DMR USB audio.

### Scope of the no-mod result

The C6000 receive path makes the RF → MCU analog modification unnecessary for
**FM voice over USB on the tested dual-band radio**. PC-generated TX audio also
bypasses the microphone path. This does not establish no-mod M17 or 9600-baud
reception, or remove the microphone-path requirement for standalone M17.
See [the comparison with OpenRTX’s modification guide](confirmed-dualband.md#what-this-changes-about-openrtxs-hardware-modifications).

### OpenRTX M17 mods (for reference) **[docs]**

Source: <https://openrtx.org/#/M17/m17?id=hardware-modifications>

- **MD-380/390 ("Easy"):**
  - RF→MCU: remove R150, wire the R150 pad (2T/5T) to U102 pin 1 (demodulator output), remove C556 (RF side).
  - Mic→MCU: remove EC151 and D102.
- **MD-UV380/390 ("Medium"):** RF→MCU: wire C6000 `ADC_IVINP` to STM32 `PC3` (opposite sides of the PCB, needs a groove filed in the heat sink). Mic→MCU is also a mod.
- **If voice comes over USB, the Mic→MCU mod is unnecessary.** The direct RF→MCU path remains relevant for M17 RX / 9600 baud. The C6000 RAM path now supplies FM voice without it; it is not a raw discriminator stream.

OpenRTX mode status: M17 RX/TX on the MD-380 UHF and UV380. On the MD-380 VHF, RX only. DMR and APRS are listed as "future" ([dev status](https://openrtx.org/#/dev_status?id=modes)).

## USB constraints

- The STM32F405 OTG_FS has **EP0 + 3 IN + 3 OUT endpoints** and a 1.25 KB FIFO **[unverified: check RM0090]**.
- UAC (audio) needs 1 iso IN + 1 iso OUT. CDC-ACM needs bulk IN + bulk OUT + interrupt IN.
  **Audio + one CDC-ACM uses everything.** No room for a second CDC, CDC-ECM/NCM ethernet, or a UAC feedback endpoint.
- Consequences:
  - Use an adaptive/synchronous OUT endpoint and resample on the device to absorb clock drift. Make the IN endpoint asynchronous (the device picks the packet size).
  - Multiplex control, KISS and GPS NMEA over one CDC channel and demultiplex them in a host daemon, or switch USB configurations at runtime.
  - For networking, use KISS over CDC plus `tncattach`/`kissattach` on the host instead of a USB ethernet function.
- The existing OpenRTX USB stack is the old ST OTG library with CDC only (`platform/mcu/STM32F4xx/drivers/usb/`, `platform/drivers/USB/usb_MDx.cpp`) **[verified]**. The opt-in TYT build now replaces it with TinyUSB UAC1 + CDC without a feedback endpoint; default builds retain the original stack.
- Ready-made UAC classes that need an explicit feedback endpoint don't fit. Example: embassy-usb's `uac1::speaker` (Rust); its UAC1 source/microphone class was an unmerged PR (#5600) when checked **[docs]**.

## HR_C5000 / HR_C6000 baseband

The OpenRTX driver is `platform/drivers/baseband/HR_Cx000.*` and `HR_C6000*`, shared between both chips **[verified]**.

Blocks:
1. **RF-side modem.** RX ADC (`ADC_IVINP/N`, pins 70/71) accepts IF, baseband I/Q or discriminator input. On the UV380 it gets AT1846S demod output. TX uses two-point modulation DACs with calibration offsets (`setModOffset`, reverse-engineered from TYT firmware).
2. **DMR L1/L2 engine.**
   - 4FSK at 4800 symbols/s, TDMA slot timing with interrupts, BS/MS sync detection.
   - Color code, address filters, Tier I/II, basic-privacy encryption.
   - The MCU exchanges burst payloads through TX/RX data registers (SPI op modes `DATA` / `DATA_R`).
3. **Vocoder interface.** Pins 34–37, SPI or I2S, master or slave. The chip expects an external AMBE vocoder (HR_V3000 in the datasheet). On TYT radios the STM32 plays that role in software. On the MD-380 this is `V_CS` = PB12 (SPI2/I2S2) **[verified]**. On the UV380 the pin isn't defined in OpenRTX **[unverified]**.
4. **Audio codec.**
   - Mic preamp + ADC and DAC + lineout.
   - Input gain: C6000 −12…+36 dB, C5000 −34.5…+12 dB.
   - I2S slave.
   - "OpenMusic" DAC FIFO over SPI (64-byte chunks).
5. **Analog FM helpers.** FM BPF (12.5/25 kHz), CTCSS encode/decode, tone generator, DTMF.

Control: SPI with op modes AUX/DATA/SOUND/CONFIG/DATA_R.
- UV380: bit-banged on PE2–PE5, sleep on PE6.
- MD-380: PE2/PC13/PE5/PE3, and MOSI is shared with the PLL data line.

The init sequences are copied from TYT firmware and contain many undocumented registers.

### Software-only FM receive discovery (2026-11-07)

The local translated C6000 manual's RX RAM table documents two 512-byte FM
buffers at `0x030–0x22f` and `0x230–0x42f`. These are accessible through the
existing user SPI port (DATA mode, extended addresses). This is a more concrete
candidate than looking for an undocumented analog routing trick.

[RadioFoundry's C6000 bench notes](https://radiofoundry.org/docs/spec/modem-hrc6000)
report 8 kHz big-endian 16-bit FM receive samples in these buffers on an
AT1846S-based board, 256 samples per half every 32 ms, with status `0x82` bit 1
after unmasking through `0x81`. The samples reportedly follow the `0x34` filters
and de-emphasis but are not squelch gated. These are another project's reported
measurements that informed the experiment. The subsequent dual-band bench result below confirms usable voice from this path; it does not independently verify every register/filter claim.

The new read-only `RXPROBE 00` through `RXPROBE 15` commands capture 64-byte
chunks from both halves, with device start/end ticks, mode and IRQ mask/status.
`scripts/probe-fm-rx.py` saves the results as JSONL and compares byte order and
changes between snapshots. These are live snapshots, potentially torn; they do
not yet form a continuous audio stream. No IRQ mask/acknowledgement or routing
registers are changed.

The initial read-only snapshots produced recognizable but chopped voice. The
current implementation continuously captures the alternating halves, converts
big-endian PCM to little-endian PCM, and exposes native 8 kHz USB capture. No host
upsampling to 48 kHz is required for RX. FM capture restores the saved IRQ mask
when stopped; mode transitions gate it away from M17.

### Datasheet findings **[verified, from translated datasheet]**

- **Three operating layers.** Layer 1 is "physical layer only": filtering plus 4FSK mod/demod, with the MCU doing all coding. It has a dedicated "physical layer works alone" RX interrupt (meant for BER testing). Layer 2 adds FEC and interleaving. Layer 3 is the full DMR protocol.
- **Soft information per received burst:** 264 bits × 6-bit soft values at `0xA8–0x1AF` (slot 0) and `0x1C8–0x2CF` (slot 1).
- **The symbol ↔ deviation mapping is configurable** (`0x01[7]`, datasheet table 4.6).
- `TEST_MODE` is a hardware pin (pin 38). Register `0x12` bit 6 enables a two-point modulation test square-wave output.
- Open question: is Layer 1 still tied to 30 ms DMR bursts and sync patterns? It probably is, which would make continuous M17 framing hard.

## OpenRTX codeplug and UI issues

### Codeplug reader **[verified]**

In the **upstream baseline**, `platform/drivers/CPS/cps_io_native_MDUV3x0.c` (the MD3x0 reader has the same structure) has these issues. The separate recovery patch replaces both TYT readers with validated `cps_io_native_tyt.c`; calibration validation remains outside its scope:
- The vendor codeplug is parsed straight from SPI flash **on every channel load**. There is no
  magic, version or CRC anywhere.
- The empty test is `wcslen((wchar_t*)name) == 0`. Names are UTF-16, `wchar_t` is 4 bytes on
  arm-none-eabi, and the build has no `-fshort-wchar`. So **erased slots (0xFFFF) read as valid
  channels/zones/contacts**, and full-length 16-character names make `wcslen` read past the struct.
- `channel_mode` (a 2-bit field), color code, timeslot, and contact/scan indices are copied unvalidated.
- BCD frequencies are converted without checking BCD validity. Band limits are checked only in one
  UI path (`_ui_channel_valid()` in `ui/default/ui.c`).
- Zone member indices are used as-is (`member - 1`, ext array indexed by `ch_pos - 16`). Bounds
  safety depends on the caller.
- Calibration data is read from the same flash without validation.

### UI

- `ui/default/ui.c` is 2790 lines (126 `case` labels) and `ui_menu.c` 1363 lines: one state machine
  mixing input, state changes and codeplug access **[verified]**.
- From use:
  - VFO knob tuning doesn't snap to the NFM channel raster (can't land on 12.5 kHz / PMR446
    frequencies).
  - Channel/zone selection isn't discoverable.
- OpenRTX supports separate UI implementations (`ui/default`, `ui/module17`) **[verified]**.

## Datasheets & references

- **HR_C6000** (leaked, translated): [OpenGD77 docs/data_sheets/C6000](https://github.com/open-ham/OpenGD77/tree/main/docs/data_sheets/C6000)
  - Download the named PDFs from the linked upstream directory. Local `docs/datasheets/*.pdf` copies are ignored and not redistributed in this repository.
  - `HR_C6000_updated_chinese_translated_english.pdf`: the most complete version, start here
  - `HR_C6000_updated_chinese.pdf`: the original Chinese
  - `HR-C6000 Registers_G4EML.docx`, `HRC6000_VK3KYY.docx`: register notes from driver authors
  - The same repo has the AT1846S datasheet + programming guide, the HR_C7000 manuals, and the ETSI DMR spec
- **HR_C5000** (English): <https://www.qsl.net/dl4yhf/RT3/HR_C5000_english.html>
- **MD380 hardware notes** (DL4YHF): <https://qsl.net/d/dl4yhf/RT3/md380_hw.html>
- **OpenRTX:** <https://github.com/OpenRTX/OpenRTX>, docs at <https://openrtx.org>
- **OpenGD77 / OpenMDUV380:** DMR implementation with AMBE taken from TYT firmware. Check the license before reusing code.
- **md380tools:** reverse engineering of TYT firmware (C5000 registers, AMBE emulation, firmware wrapping)

### FM RX bench progress (2026-11-07)

The user's `RXREGS` result was fast `30,00,84,49`, fast extended `b0,80,04,c9`,
slow `b0,80,04,c9`, slow extended `b0,80,04,c9` for CONFIG `01,10,81,e0`.
Ordinary fast reads corrupted bit 7; extended addressing returned expected data.
The read-only tone capture contained 16 complete snapshots and changing samples.
Big-endian signed PCM had adjacent-sample correlation 0.812 versus 0.002 for
little-endian. The user recognized audio in the 8 kHz WAV; snapshot gaps chopped it.

An intermediate image used fast extended burst reads and data-ready acknowledgements,
native 8 kHz USB capture, conservative half-buffer selection and error counters.
It stalled as described below; the final working implementation uses the slower SPI transport. USB squelch is
an independent RSSI gate selectable as Off or 1..15; it emits zero PCM when closed.

### Continuous capture stall investigation

With FM-ready enabled, the user observed 142 additional events and drops with
no new accepted blocks (253 total). This isolates failure to block acceptance;
it is not lack of host consumption or only squelch. The old aggregate dropped
counter cannot distinguish timing rejection from buffer overwrite or bus errors.
Audit found that early notifications consumed the comparison baseline even when
no block was delivered; the selector now preserves it. A uniquely changed half
can recover after a scheduling gap. Capture now uses the verified slow transport:
the datasheet section 4.8 specifies maximum 4 MHz U_SPI, while the existing target
fast transport documents 6.46 MHz. Correct reads of four extended registers did
not validate sustained burst transfers at that rate. RXDROP now records separate
failure reasons. Subsequent user tests confirmed continuous received speech and
radio-bot replies on the dual-band unit. Long-running loss/latency measurements
and validation on other hardware revisions remain outstanding.
