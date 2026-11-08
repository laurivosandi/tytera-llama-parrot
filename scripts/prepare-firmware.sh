#!/usr/bin/env bash
# SPDX-License-Identifier: GPL-3.0-or-later
set -euo pipefail
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
revision=d5456734b94057b1b30624922a48dbfeb8853b29
if [[ ! -d "$root/firmware" ]]; then
    git clone https://github.com/OpenRTX/OpenRTX.git "$root/firmware"
    git -C "$root/firmware" checkout --detach "$revision"
fi
if [[ $(git -C "$root/firmware" rev-parse HEAD) != "$revision" ]]; then
    echo "Expected OpenRTX $revision; leaving the existing checkout untouched." >&2
    exit 1
fi
recovery="$root/patches/openrtx-codeplug-recovery.patch"
usb="$root/patches/openrtx-usb-audio.patch"
# Shared boot/build files mean the first patch cannot be reverse-checked after
# the second one. Check the final patch before considering the first patch.
if git -C "$root/firmware" apply --reverse --check "$usb" 2>/dev/null; then
    echo 'USB patch is already applied; leaving the checkout untouched.'
    exit 0
fi
if git -C "$root/firmware" apply --reverse --check "$recovery" 2>/dev/null; then
    echo 'Codeplug recovery patch is already applied.'
else
    git -C "$root/firmware" apply --check "$recovery"
    git -C "$root/firmware" apply "$recovery"
fi
git -C "$root/firmware" apply --check "$usb"
git -C "$root/firmware" apply "$usb"
echo 'Applied codeplug recovery and USB audio/control patches.'
