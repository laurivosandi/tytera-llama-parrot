# OpenRTX patch series

Base revision: `d5456734b94057b1b30624922a48dbfeb8853b29` from
<https://github.com/OpenRTX/OpenRTX>.

Apply the files in `series` order:

1. `openrtx-codeplug-recovery.patch` — validated TYT codeplug parsing and
   RAM-only recovery. PMR 01–16 on UHF-capable radios; 145.500 MHz FM CALL on
   VHF-only radios, with RX/TX enabled at the 1 W setting. Original flash is
   preserved. This patch also builds independently with the legacy USB stack.
2. `openrtx-usb-audio.patch` — composite USB FM audio and serial control, ALSA
   controls, dual-band C6000 receive RAM capture, and physical channel reports.
   Requires patch 1; it is not a standalone patch against the base revision.

From this repository, `./scripts/prepare-firmware.sh` prepares the checkout and
applies the series. Re-running it leaves an already-patched checkout alone.
`./scripts/build-firmware.sh` builds both TYT families with USB enabled.

The patches reproduce the modified firmware sources exactly in the host tests.
Both the standalone codeplug patch and the combined series have been cross-built
for MD-380/390 and MD-UV380/390. No-mod FM RX/TX has been confirmed on the user's
dual-band radio; see [the working example](../docs/confirmed-dualband.md) for the
scope of that hardware confirmation and features still awaiting validation.

After firmware edits, regenerate both files with
`python3 scripts/export-firmware-patch.py`. The exporter uses a temporary index,
so it does not stage or commit anything in the real OpenRTX checkout.

The patches retain upstream copyright/SPDX notices. Project modifications are
GPL-3.0-or-later; see [LICENSE](../LICENSE).
