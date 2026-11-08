# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline checks of the documented fresh-checkout preparation workflow."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PatchSeriesTests(unittest.TestCase):
    def workspace(self, directory):
        root = Path(directory)
        shutil.copytree(ROOT/'patches', root/'patches')
        (root/'scripts').mkdir()
        shutil.copy2(ROOT/'scripts/prepare-firmware.sh', root/'scripts/prepare-firmware.sh')
        subprocess.run(['git', 'clone', '--quiet', '--shared', '--no-checkout',
                        str(ROOT/'firmware'), str(root/'firmware')], check=True)
        subprocess.run(['git', '-C', str(root/'firmware'), 'checkout', '--quiet', '--detach',
                        'd5456734b94057b1b30624922a48dbfeb8853b29'], check=True)
        return root

    def prepare(self, root):
        return subprocess.run(['bash', str(root/'scripts/prepare-firmware.sh')],
                              capture_output=True, text=True)

    def test_clean_checkout_then_idempotent_reuse(self):
        with tempfile.TemporaryDirectory(prefix='tyt-prepare-') as temp:
            root = self.workspace(temp)
            first = self.prepare(root)
            self.assertEqual(first.returncode, 0, first.stderr)
            paths = subprocess.check_output(['git', '-C', str(ROOT/'firmware'),
                'diff', '--name-only', 'HEAD'], text=True).splitlines()
            paths += subprocess.check_output(['git', '-C', str(ROOT/'firmware'),
                'ls-files', '--others', '--exclude-standard'], text=True).splitlines()
            for name in paths:
                self.assertEqual((root/'firmware'/name).read_bytes(),
                                 (ROOT/'firmware'/name).read_bytes(), name)
            second = self.prepare(root)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn('already applied', second.stdout)

    def test_codeplug_only_checkout_can_add_usb(self):
        with tempfile.TemporaryDirectory(prefix='tyt-prepare-') as temp:
            root = self.workspace(temp)
            subprocess.run(['git', '-C', str(root/'firmware'), 'apply',
                str(root/'patches/openrtx-codeplug-recovery.patch')], check=True)
            result = self.prepare(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('Codeplug recovery patch is already applied', result.stdout)

    def test_conflicting_checkout_is_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix='tyt-prepare-') as temp:
            root = self.workspace(temp)
            target = root/'firmware/meson.build'
            target.write_text('local work that must remain intact\n')
            result = self.prepare(root)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(target.read_text(), 'local work that must remain intact\n')
