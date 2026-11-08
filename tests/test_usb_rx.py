# SPDX-License-Identifier: GPL-3.0-or-later
"""Host checks for native USB capture descriptors and half-buffer ownership."""
import ctypes as C
from pathlib import Path
import subprocess
import tempfile
import unittest

FW = Path(__file__).resolve().parents[1] / 'firmware'
USB = FW / 'platform/drivers/USB/tyt'


class Stream(C.Structure):
    _fields_ = [('dropped', C.c_uint32), ('underruns', C.c_uint32),
                ('blocks', C.c_uint32), ('last', C.c_uint32),
                ('markers', C.c_uint8 * 16),
                *[(name, C.c_uint32) for name in
                  ('early', 'ambiguous', 'unsynced', 'torn', 'io', 'full')],
                ('next', C.c_int)]


class ReceiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        library = Path(cls.temp.name) / 'receive.so'
        subprocess.run(['cc', '-std=c99', '-Wall', '-Wextra', '-Werror',
                        '-shared', '-fPIC', '-DPLATFORM_MDUV3x0',
                        '-I'+str(USB), '-I'+str(FW/'subprojects/tinyusb/src'),
                        str(USB/'usb_rx.c'), str(USB/'usb_descriptors.c'),
                        '-o', str(library)], check=True)
        cls.lib = C.CDLL(str(library))
        for name in ('usbRx_init', 'usbRx_select'):
            getattr(cls.lib, name).argtypes = [C.POINTER(Stream), C.c_void_p,
                                               C.c_uint32]
        cls.lib.tud_descriptor_configuration_cb.argtypes = [C.c_uint8]
        cls.lib.tud_descriptor_configuration_cb.restype = C.POINTER(C.c_uint8)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_buffer_ownership_silence_lost_events_and_rollover(self):
        state = Stream()
        marker = bytearray(16)
        now = 0xfffffff0
        self.lib.usbRx_init(C.byref(state), bytes(marker), now)
        def event(delta=32):
            nonlocal now
            now = (now + delta) & 0xffffffff
            return self.lib.usbRx_select(C.byref(state), bytes(marker), now)
        self.assertEqual(event(), -1)  # Can't establish ownership from silence.
        marker[0] = 1
        self.assertEqual(event(), 0)
        marker[8] = 1
        self.assertEqual(event(), 1)
        self.assertEqual(event(), 0)  # Identical periodic data uses known phase.
        self.assertEqual(event(), 1)
        marker[0] = marker[8] = 2
        self.assertEqual(event(), -1)  # Both changed: don't publish stale order.
        marker[0] = 3
        self.assertEqual(event(), 0)
        marker[8] = 3
        self.assertEqual(event(64), 1)  # Unique fresh half recovers after gap.
        self.assertEqual(event(64), -1)  # No change after gap cannot use phase.
        marker[0] = 4
        self.assertEqual(event(), 0)
        marker[8] = 4
        baseline = bytes(state.markers)
        last = state.last
        self.assertEqual(event(1), -1)
        self.assertEqual(bytes(state.markers), baseline)
        self.assertEqual(state.last, last)
        self.assertEqual(event(31), 1)  # Early IRQ didn't swallow new samples.
        self.assertEqual(state.dropped, 4)
        self.assertEqual((state.early, state.ambiguous, state.unsynced), (1, 1, 2))

    def test_dual_band_capture_8k_playback_48k(self):
        pointer = self.lib.tud_descriptor_configuration_cb(0)
        blob = bytes(pointer[:pointer[2] | pointer[3] << 8])
        descriptors = []
        offset = 0
        while offset < len(blob):
            size = blob[offset]
            self.assertGreaterEqual(size, 2)
            descriptors.append(blob[offset:offset+size])
            offset += size
        formats = [d for d in descriptors
                   if len(d) == 11 and d[1:4] == b'\x24\x02\x01']
        self.assertEqual([int.from_bytes(d[8:11], 'little') for d in formats],
                         [48000, 8000])
        endpoints = {d[2]: d for d in descriptors if d[1] == 5}
        self.assertEqual(int.from_bytes(endpoints[1][4:6], 'little'), 98)
        self.assertEqual(int.from_bytes(endpoints[0x81][4:6], 'little'), 18)
        self.assertEqual(len(endpoints), 5)
