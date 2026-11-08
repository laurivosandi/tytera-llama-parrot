# SPDX-License-Identifier: GPL-3.0-or-later
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FW = ROOT / 'firmware'


class CodeplugTests(unittest.TestCase):
    def test_native_readers_with_sanitizers(self):
        with tempfile.TemporaryDirectory(prefix='tyt-cps-') as temp:
            directory = Path(temp)
            (directory / 'hwconfig.h').write_text('/* host hardware stub */\n')
            for target in ('MD3x0', 'MDUV3x0'):
                with self.subTest(target=target):
                    binary = directory / target
                    subprocess.run([
                        'cc', '-std=c99', '-D_DEFAULT_SOURCE', '-DCONFIG_TYT_CPS',
                        '-DPLATFORM_' + target, '-Wall', '-Wextra', '-Werror',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-ffunction-sections', '-fdata-sections', '-Wl,--gc-sections',
                        '-I' + str(directory), '-I' + str(FW / 'openrtx/include'),
                        str(ROOT / 'tests/codeplug_tyt.c'),
                        str(FW / 'platform/drivers/CPS/cps_io_native_tyt.c'),
                        str(FW / 'openrtx/src/core/cps.c'),
                        str(FW / 'openrtx/src/core/state.c'),
                        '-pthread', '-o', str(binary),
                    ], check=True)
                    result = subprocess.run([str(binary)], check=False,
                        env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'},
                        capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn('validation and fallback passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
