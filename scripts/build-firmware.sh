#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
source "$root/toolchains/env.sh"
command -v arm-miosix-eabi-gcc >/dev/null
# Optional workspace-local libusb headers, extracted from the distribution package.
if [[ -d "$root/toolchains/host/usr/lib/x86_64-linux-gnu/pkgconfig" ]]; then
    export PKG_CONFIG_PATH="$root/toolchains/host/usr/lib/x86_64-linux-gnu/pkgconfig${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
    export PKG_CONFIG_SYSROOT_DIR="$root/toolchains/host"
fi
cd "$root/firmware"
for dependency in codec2 XPowersLib radio_tool; do
    if [[ ! -d "subprojects/$dependency" ]]; then
        meson subprojects download "$dependency"
    fi
done
# These radios use USB; the optional BLE transport need not be downloaded.
cmake -S subprojects/radio_tool -B subprojects/radio_tool -DBUILD_BLE=OFF
cmake --build subprojects/radio_tool -j 4
export PATH="$PWD/subprojects/radio_tool:$PATH"
if [[ -f build_usb/build.ninja ]]; then
    meson setup --reconfigure build_usb -Dusb_audio=true
else
    meson setup --cross-file cross_cm4.txt build_usb -Dusb_audio=true
fi
meson compile -C build_usb openrtx_md3x0_wrap openrtx_mduv3x0_wrap
mkdir -p "$root/artifacts"
for target in md3x0 mduv3x0; do
    cp "build_usb/openrtx_${target}_wrap" "$root/artifacts/openrtx_${target}_usb.bin"
    cp "build_usb/openrtx_${target}" "$root/artifacts/openrtx_${target}_usb.elf"
done
cd "$root/artifacts"
sha256sum *.bin *.elf > SHA256SUMS
