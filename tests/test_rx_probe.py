# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch, Mock
import tempfile
import contextlib
import io

spec = importlib.util.spec_from_file_location(
    'probe', Path(__file__).resolve().parents[1] / 'scripts/probe-fm-rx.py')
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_reply_bounds_and_clock_wrap(self):
        payload = bytes(range(64)).hex()
        record = probe.parse_reply(f'RXPROBE 15 4294967295 2 80 04 02 {payload}', 15)
        self.assertEqual(record['address'], 0x3f0)
        self.assertEqual(record['data'], payload)
        for mode in ('80', '81', 'f3', 'ff'):
            record = probe.parse_reply(
                f'RXPROBE 15 0 1 {mode} 04 02 {payload}', 15)
            self.assertEqual(record['mode'], int(mode, 16))
        for reply in [f'RXPROBE 14 0 1 80 04 02 {payload}',
                      f'RXPROBE 15 -1 1 80 04 02 {payload}',
                      f'RXPROBE 15 0 1 80 104 02 {payload}',
                      f'RXPROBE 15 0 1 00 04 02 {payload}',
                      f'RXPROBE 15 0 1 80 04 02 {payload[:-2]}',
                      'ERR probe requires idle FM reception']:
            with self.assertRaises(ValueError):
                probe.parse_reply(reply, 15)

    def test_register_diagnostic_and_failed_probe_create_no_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'capture.jsonl'
            for registers_only in (True, False):
                radio = Mock()
                radio.command.side_effect = [
                    'v=1', 'RXREGS order=01,10,81,e0 fast=00,00,00,00',
                    'ERR probe requires FM mode: reg10=00 (bit7 must be set)']
                argv = ['probe-fm-rx.py', '--output', str(output)]
                if registers_only:
                    argv.append('--registers-only')
                with patch.object(probe.sys, 'argv', argv), \
                     patch.object(probe.usb, 'Radio', return_value=radio), \
                     contextlib.redirect_stdout(io.StringIO()):
                    if registers_only:
                        probe.main()
                    else:
                        with self.assertRaises(ValueError):
                            probe.main()
                self.assertFalse(output.exists())
                radio.close.assert_called_once()
                self.assertEqual(radio.command.call_count,
                                 2 if registers_only else 3)

    def test_byte_order_statistics(self):
        data = struct.pack('>hhhh', -30000, 30000, -100, 100)
        stats = probe.summarize(data)
        self.assertEqual(stats['big_endian'],
                         dict(min=-30000, max=30000, mean=0))
        self.assertNotEqual(stats['big_endian'], stats['little_endian'])
