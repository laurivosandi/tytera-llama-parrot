# SPDX-License-Identifier: GPL-3.0-or-later
"""Host verification of firmware wire format, descriptors and PTT policy."""
import ctypes as C
import importlib.util
from pathlib import Path
import random
import os
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
FW = ROOT / 'firmware'
spec = importlib.util.spec_from_file_location('tyt_usb', ROOT / 'scripts/tyt-usb.py')
client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(client)


class Control(C.Structure):
    _fields_ = [('lease', C.c_uint32), ('start', C.c_uint32),
                ('requested', C.c_bool), ('blocked', C.c_bool)]


class Mixer(C.Structure):
    _fields_ = [('start', C.c_uint32), ('mode', C.c_uint8),
                ('muted', C.c_bool), ('requested', C.c_bool),
                ('blocked', C.c_bool), ('squelch', C.c_uint8), ('power', C.c_uint8),
                ('squelchOpen', C.c_bool), ('channelCount', C.c_uint8),
                ('channel', C.c_uint8), ('channelPending', C.c_bool)]


class USBTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='tyt-usb-')
        lib = Path(cls.temp.name) / 'usb.so'
        subprocess.run(['cc', '-std=c99', '-shared', '-fPIC', '-O1', '-g',
                        '-Wall', '-Wextra', '-Werror',
                        '-I'+str(FW/'openrtx/include'),
                        '-I'+str(FW/'platform/drivers/USB/tyt'),
                        '-I'+str(FW/'subprojects/tinyusb/src'),
                        str(FW/'openrtx/src/core/usb_frame.c'),
                        str(FW/'openrtx/src/core/usb_control.c'),
                        str(FW/'openrtx/src/core/crc.c'),
                        str(FW/'platform/drivers/USB/tyt/usb_descriptors.c'),
                        str(FW/'platform/drivers/USB/tyt/usb_mixer.c'),
                        '-o', str(lib)], check=True)
        cls.lib = C.CDLL(str(lib))
        cls.lib.usbMixer_control.argtypes = [C.POINTER(Mixer), C.c_uint16,
            C.c_uint16, C.c_uint16, C.c_bool, C.POINTER(C.c_uint8)]
        cls.lib.usbMixer_squelch.argtypes = [C.POINTER(Mixer), C.c_int32]
        cls.lib.usbMixer_tick.argtypes = [C.POINTER(Mixer), C.c_uint32,
                                        C.c_bool, C.c_bool]
        for name in ['usbFrame_encode', 'usbFrame_decode']:
            fn = getattr(cls.lib, name)
            fn.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t]
            fn.restype = C.c_size_t
        cls.lib.usbControl_tick.argtypes = [C.POINTER(Control), C.c_uint32, C.c_bool]
        cls.lib.usbControl_command.argtypes = [C.POINTER(Control), C.c_uint32,
                                              C.c_bool, C.c_void_p, C.c_size_t]
        cls.lib.usbControl_command.restype = C.c_char_p
        cls.lib.tud_descriptor_configuration_cb.argtypes = [C.c_uint8]
        cls.lib.tud_descriptor_configuration_cb.restype = C.POINTER(C.c_uint8)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_fm_routing_and_rf_shutdown(self):
        directory = Path(self.temp.name)
        (directory/'hwconfig.h').write_text('/* host hardware stub */\n')
        executable = directory/'fm_usb'
        subprocess.run(['c++', '-std=c++14', '-DCONFIG_USB_AUDIO',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer',
                        '-I'+str(directory), '-I'+str(FW/'openrtx/include'),
                        str(ROOT/'tests/fm_usb.cpp'),
                        str(FW/'openrtx/src/rtx/OpMode_FM.cpp'),
                        '-o', str(executable)], check=True)
        # LeakSanitizer cannot operate under this sandbox's ptrace monitor.
        # Address/undefined behavior instrumentation remains enabled.
        subprocess.run([str(executable)], check=True,
                       env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})

    def test_wire_roundtrip_and_independent_python_implementation(self):
        rng = random.Random(173)
        for size in range(1, 241):
            for message in [bytes(size), b'\xff'*size, rng.randbytes(size)]:
                wire = C.create_string_buffer(244)
                count = self.lib.usbFrame_encode(message, size, wire, 244)
                self.assertEqual(wire.raw[:count], client.encode(message))
                self.assertEqual(client.decode(wire.raw[:count-1]), message)
                decoded = C.create_string_buffer(240)
                length = self.lib.usbFrame_decode(wire, count-1, decoded, 240)
                self.assertEqual(decoded.raw[:length], message)

    def test_bad_crc_and_bounded_buffers(self):
        message = b'\x01PTT 1'
        wire = bytearray(client.encode(message)[:-1])
        wire[2] ^= 1
        out = C.create_string_buffer(b'Z'*256)
        self.assertEqual(self.lib.usbFrame_decode(bytes(wire), len(wire), out, 240), 0)
        self.assertEqual(out.raw[:256], b'Z'*256)
        for wire in [b'', b'\0'*10, b'\x08abc', b'\xff'*244, b'\x01'*244]:
            self.assertEqual(self.lib.usbFrame_decode(wire, len(wire), out, 240), 0)
        valid = client.encode(message)[:-1]
        self.assertEqual(self.lib.usbFrame_decode(valid, len(valid), out, 1), 0)
        self.assertEqual(self.lib.usbFrame_encode(b'X'*241, 241, out, 256), 0)
        self.assertEqual(self.lib.usbFrame_encode(message, len(message), out, 2), 0)
        self.assertEqual(out.raw[:256], b'Z'*256)

    def test_malformed_frame_fuzz(self):
        rng = random.Random(75)
        for _ in range(10000):
            wire = rng.randbytes(rng.randrange(300))
            out = C.create_string_buffer(256)
            count = self.lib.usbFrame_decode(wire, len(wire), out, 240)
            try:
                message = client.decode(wire)
            except ValueError:
                self.assertEqual(count, 0)
            else:
                self.assertEqual(out.raw[:count], message)

    def command(self, state, now, command, live=True):
        return self.lib.usbControl_command(C.byref(state), now, live, command, len(command))

    def test_ptt_lease_disconnect_and_malformed_commands(self):
        state = Control()
        self.assertTrue(self.command(state, 10, b'PTT 1', False).startswith(b'ERR'))
        self.assertFalse(state.requested)
        self.command(state, 10, b'PTT 1')
        self.assertTrue(state.requested)
        self.lib.usbControl_tick(C.byref(state), 1009, True)
        self.assertTrue(state.requested)
        self.lib.usbControl_tick(C.byref(state), 1010, True)
        self.assertFalse(state.requested)
        self.command(state, 2000, b'PTT 1')
        for bad in [b'PTT 1\0', b'PTT 1garbage', b'PTT', b'PTT 2', b'']:
            self.assertEqual(self.command(state, 2001, bad), b'ERR unsupported')
            self.assertEqual(state.lease, 2000)
        self.lib.usbControl_tick(C.byref(state), 2002, False)
        self.assertFalse(state.requested)

    def test_hard_timeout_cannot_be_extended_by_keepalives(self):
        state = Control()
        for now in range(0, 60000, 250):
            self.command(state, now, b'PTT 1')
            self.assertTrue(state.requested)
            self.assertEqual(state.start, 0)
        self.command(state, 60000, b'PTT 1')
        self.assertTrue(state.blocked)
        self.assertFalse(state.requested)
        self.command(state, 61000, b'PTT 1')
        self.assertFalse(state.requested)
        self.command(state, 61001, b'PTT 0')
        self.command(state, 61002, b'PTT 1')
        self.assertTrue(state.requested)

    def test_clock_rollover(self):
        state = Control()
        start = 0xfffffff0
        self.command(state, start, b'PTT 1')
        self.lib.usbControl_tick(C.byref(state), (start+999) & 0xffffffff, True)
        self.assertTrue(state.requested)
        self.lib.usbControl_tick(C.byref(state), (start+1000) & 0xffffffff, True)
        self.assertFalse(state.requested)

    def test_usb_model_identity(self):
        self.lib.usbDescriptors_setModel.argtypes = [C.c_char_p, C.c_size_t]
        self.lib.tud_descriptor_string_cb.argtypes = [C.c_uint8, C.c_uint16]
        self.lib.tud_descriptor_string_cb.restype = C.POINTER(C.c_uint16)
        def string(index):
            data = self.lib.tud_descriptor_string_cb(index, 0x409)
            return ''.join(chr(data[i]) for i in range(1, (data[0] & 255)//2))
        self.assertEqual(string(1), 'TYT (Tytera)')
        self.lib.usbDescriptors_setModel(b'MDUV390   ', 10)
        self.assertEqual(string(2), 'Tytera MDUV390')
        for invalid in [b'', b'\xff'*10, b'A'*33, b'   ']:
            self.lib.usbDescriptors_setModel(invalid, len(invalid))
            self.assertEqual(string(2), 'Tytera MDUV390')
        self.assertEqual([string(i) for i in (6, 7, 8)],
                         ['Triggered', 'Always', 'PTT Mode'])

    def test_mixer_controls_and_automatic_ptt(self):
        state = Mixer(mode=1)
        def control(entity, selector, value, write=True, length=1):
            data = C.c_uint8(value)
            result = self.lib.usbMixer_control(C.byref(state), entity << 8,
                selector, length, write, C.byref(data))
            return result, data.value
        def tick(now, playing=True, live=True):
            self.lib.usbMixer_tick(C.byref(state), now, playing, live)
        tick(0)
        self.assertFalse(state.requested)  # Triggered stays manual.
        self.assertEqual(control(7, 0, 0, False), (0, 1))
        for args in [(7, 0, 0), (7, 0, 3), (8, 256, 2),
                     (8, 257, 1), (7, 256, 2), (9, 0, 1)]:
            self.assertLess(control(*args)[0], 0)
        self.assertLess(control(7, 0, 2, length=2)[0], 0)
        self.assertEqual(state.mode, 1)
        self.assertEqual(control(5, 0, 0, False), (0, 1))
        self.assertEqual(control(6, 0, 0, False), (0, 1))
        self.assertEqual(control(7, 0, 2), (0, 2))
        tick(10, live=False)
        self.assertFalse(state.requested)
        tick(20)
        self.assertTrue(state.requested)  # No CDC connection needed.
        tick(60019)
        self.assertTrue(state.requested)
        tick(60020)
        self.assertTrue(state.blocked)
        self.assertFalse(state.requested)
        tick(61000)
        self.assertFalse(state.requested)
        tick(61001, playing=False)
        tick(61002)
        self.assertTrue(state.requested)
        self.assertEqual(control(8, 256, 1), (0, 1))
        tick(61003)
        self.assertFalse(state.requested)
        control(8, 256, 0)
        tick(61004)
        self.assertTrue(state.requested)
        tick(61005, live=False)
        tick(61006)
        self.assertTrue(state.blocked)  # Packet loss requires deliberate rearm.
        self.assertFalse(state.requested)
        control(7, 0, 1)
        tick(61007)
        self.assertFalse(state.requested)
        control(7, 0, 2)
        tick(0xfffffff0)
        tick((0xfffffff0 + 59999) & 0xffffffff)
        self.assertTrue(state.requested)
        tick((0xfffffff0 + 60000) & 0xffffffff)
        self.assertFalse(state.requested)

    def test_squelch_selector_and_hysteresis(self):
        state = Mixer(mode=1)
        value = C.c_uint8()
        def control(write=False, selected=0):
            value.value = selected
            result = self.lib.usbMixer_control(C.byref(state), 26 << 8,
                0, 1, write, C.byref(value))
            return result, value.value
        self.assertEqual(control(), (0, 1))  # Off maps to UAC pin 1.
        self.lib.usbMixer_squelch(C.byref(state), -150)
        self.assertTrue(state.squelchOpen)
        for invalid in (0, 17, 255):
            self.assertLess(control(True, invalid)[0], 0)
            self.assertEqual(state.squelch, 0)
        for level in range(1, 16):
            self.assertEqual(control(True, level + 1), (0, level + 1))
            threshold = -127 + level * 66 // 15
            self.lib.usbMixer_squelch(C.byref(state), threshold + 1)
            self.assertFalse(state.squelchOpen)
            self.lib.usbMixer_squelch(C.byref(state), threshold + 2)
            self.assertTrue(state.squelchOpen)
            control(True, level + 1)  # Repeated host SET preserves hysteresis.
            self.lib.usbMixer_squelch(C.byref(state), threshold - 1)
            self.assertTrue(state.squelchOpen)
            self.lib.usbMixer_squelch(C.byref(state), threshold - 2)
            self.assertFalse(state.squelchOpen)
        control(True, 1)
        self.lib.usbMixer_squelch(C.byref(state), -150)
        self.assertTrue(state.squelchOpen)

    def test_power_and_channel_selectors(self):
        state = Mixer(mode=1, channelCount=16, channel=1)
        data = C.c_uint8()
        def control(entity, value, write=True):
            data.value = value
            result = self.lib.usbMixer_control(C.byref(state), entity << 8,
                0, 1, write, C.byref(data))
            return result, data.value
        self.assertEqual(control(33, 0, False), (0, 1))
        for pin in range(1, 7):
            self.assertEqual(control(33, pin), (0, pin))
            self.assertEqual(state.power, pin - 1)
        self.assertLess(control(33, 7)[0], 0)
        self.assertLess(control(33, 0)[0], 0)
        self.assertEqual(control(162, 17), (0, 17))
        self.assertTrue(state.channelPending)
        self.assertEqual(control(162, 0, False), (0, 17))
        self.assertLess(control(162, 18)[0], 0)
        self.assertEqual(control(162, 1), (0, 1))
        self.assertFalse(state.channelPending)
        self.assertEqual(state.mode, 1)
        self.assertFalse(state.requested)

    def test_dynamic_codeplug_descriptor_names(self):
        fn = self.lib.usbDescriptors_setChannels
        fn.argtypes = [C.c_void_p, C.c_size_t]
        labels = C.create_string_buffer(b'PMR1' + bytes(29) + b'PMR2' + bytes(29))
        self.assertEqual(fn(labels, 2), 0)
        try:
            pointer = self.lib.tud_descriptor_configuration_cb(0)
            length = pointer[2] | pointer[3] << 8
            blob = bytes(pointer[:length])
            descriptors = []
            offset = 0
            while offset < length:
                size = blob[offset]
                self.assertGreaterEqual(size, 2)
                descriptors.append(blob[offset:offset+size])
                offset += size
            selector = next(d for d in descriptors
                            if d[1:4] == bytes([0x24, 5, 162]))
            self.assertEqual(selector, bytes([9, 0x24, 5, 162, 3, 34, 35, 36, 162]))
            self.lib.tud_descriptor_string_cb.argtypes = [C.c_uint8, C.c_uint16]
            self.lib.tud_descriptor_string_cb.restype = C.POINTER(C.c_uint16)
            for index, expected in [(35, 'PMR1'), (36, 'PMR2'), (162, 'RF Channel')]:
                ptr = self.lib.tud_descriptor_string_cb(index, 0x409)
                value = ''.join(chr(ptr[i]) for i in range(1, (ptr[0] & 255)//2))
                self.assertEqual(value, expected)
            self.assertLess(fn(labels, 128), 0)
        finally:
            self.assertEqual(fn(None, 0), 0)

    def test_composite_descriptor_graph_and_endpoint_budget(self):
        pointer = self.lib.tud_descriptor_configuration_cb(0)
        length = pointer[2] | pointer[3] << 8
        blob = bytes(pointer[:length])
        self.assertEqual(blob[4], 5)
        descriptors = []
        offset = 0
        while offset < length:
            size = blob[offset]
            self.assertGreaterEqual(size, 2)
            self.assertLessEqual(offset+size, length)
            descriptors.append(blob[offset:offset+size])
            offset += size
        self.assertEqual(offset, length)
        interfaces = [d for d in descriptors if d[1] == 4]
        self.assertEqual([(d[2], d[3], d[4]) for d in interfaces],
                         [(0,0,0), (1,0,0), (1,1,1), (2,0,0), (2,1,1),
                          (3,0,1), (4,0,2)])
        eps = [d for d in descriptors if d[1] == 5]
        self.assertEqual({d[2] for d in eps}, {1, 0x81, 2, 0x82, 0x83})
        self.assertEqual(sum(bool(d[2]&128) for d in eps), 3)
        self.assertTrue(all(d[2]&15 <= 3 for d in eps))
        self.assertEqual(eps[0][3], 9)  # adaptive OUT
        self.assertEqual(eps[1][3], 5)  # asynchronous IN
        for ep in eps[:2]:
            self.assertEqual(ep[4:6], b'\x62\0')  # up to 49 mono samples
            self.assertEqual(ep[6:], b'\x01\0\0')  # 1 ms, no feedback
        formats = [d for d in descriptors if d[1:4] == b'\x24\x02\x01' and len(d)==11]
        self.assertEqual(len(formats), 2)
        for fmt in formats:
            self.assertEqual(fmt[4:8], bytes([1, 2, 16, 1]))
            self.assertEqual(int.from_bytes(fmt[8:11], 'little'), 48000)
        header = next(d for d in descriptors if d[1:3] == b'\x24\x01' and len(d)==10)
        self.assertEqual(int.from_bytes(header[5:7], 'little'), 285)
        self.assertEqual(header[7:], bytes([2, 1, 2]))
        units = {d[3]: d for d in descriptors
                 if d[1] == 0x24 and d[2] in (5, 6)}
        self.assertEqual(units[7], bytes([8, 0x24, 5, 7, 2, 5, 6, 8]))
        self.assertEqual(units[8], bytes([9, 0x24, 6, 8, 7, 1, 1, 0, 9]))
        self.assertEqual(units[5][4:], bytes([1, 1, 6]))
        self.assertEqual(units[6][4:], bytes([1, 1, 7]))
        self.assertEqual(units[26], bytes([22, 0x24, 5, 26, 16] +
                                          list(range(10, 26)) + [26]))
        for entity in range(10, 26):
            self.assertEqual(units[entity],
                             bytes([7, 0x24, 5, entity, 1, 3, entity]))
        self.assertFalse(self.lib.tud_descriptor_configuration_cb(1))


if __name__ == '__main__':
    unittest.main()
