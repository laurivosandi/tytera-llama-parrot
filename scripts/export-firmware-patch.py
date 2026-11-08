#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Export codeplug recovery first, then USB changes, without touching the real index."""
import io
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile

root = Path(__file__).resolve().parents[1]
firmware = root / 'firmware'


def git(*args, cwd=firmware):
    return subprocess.check_output(['git', *args], cwd=cwd)


def changes(directory):
    patch = git('diff', '--binary', cwd=directory)
    for item in git('ls-files', '--others', '--exclude-standard', '-z', cwd=directory).split(b'\0'):
        if not item:
            continue
        result = subprocess.run(['git', 'diff', '--no-index', '--binary', '/dev/null', item.decode()],
                                cwd=directory, stdout=subprocess.PIPE, check=False)
        if result.returncode not in (0, 1):
            raise RuntimeError('Could not export '+item.decode())
        patch += result.stdout
    return patch


new = [p.decode() for p in git('ls-files', '--others', '--exclude-standard', '-z').split(b'\0') if p]
for name in new:
    if not name.startswith(('openrtx/', 'platform/')):
        raise SystemExit(f'Review unexpected untracked file before exporting: {name}')
modified = [p.decode() for p in git('diff', '--name-only', 'HEAD', '-z').split(b'\0') if p]
with tempfile.TemporaryDirectory(prefix='tyt-patch-series-') as temp:
    directory = Path(temp)
    with tarfile.open(fileobj=io.BytesIO(git('archive', 'HEAD'))) as archive:
        archive.extractall(directory, filter='data')
    git('init', '-q', cwd=directory)
    git('add', '-A', cwd=directory)

    # Codeplug-only intermediate tree. Shared boot/build files are split here,
    # so this patch builds without USB changes or TinyUSB.
    for name in ('openrtx/include/interfaces/cps_io.h', 'openrtx/src/core/state.c',
                 'platform/drivers/CPS/cps_io_native_tyt.c'):
        shutil.copy2(firmware/name, directory/name)
    name = 'openrtx/src/core/openrtx.c'
    text = (firmware/name).read_text()
    text = re.sub(r'^[ \t]*#ifdef CONFIG_USB_AUDIO\n.*?^[ \t]*#endif\n', '', text,
                  flags=re.MULTILINE | re.DOTALL)
    (directory/name).write_text(text)
    name = 'meson.build'
    text = (directory/name).read_text()
    for family in ('MD3x0', 'MDUV3x0'):
        text = text.replace(f'cps_io_native_{family}.c', 'cps_io_native_tyt.c')
        text = text.replace("{'PLATFORM_"+family+"':", "{'CONFIG_TYT_CPS': '', 'PLATFORM_"+family+"':")
    (directory/name).write_text(text)
    recovery = changes(directory)
    git('add', '-A', cwd=directory)

    # USB is the delta from that intermediate tree to the actual working tree.
    for name in sorted(set(modified+new)):
        source, destination = firmware/name, directory/name
        if source.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
        elif destination.exists():
            destination.unlink()
    usb = changes(directory)

patches = root/'patches'
patches.mkdir(exist_ok=True)
(patches/'openrtx-codeplug-recovery.patch').write_bytes(recovery)
(patches/'openrtx-usb-audio.patch').write_bytes(usb)
(patches/'series').write_text('openrtx-codeplug-recovery.patch\nopenrtx-usb-audio.patch\n')
print('Exported codeplug recovery, then USB audio/control (see patches/series)')
