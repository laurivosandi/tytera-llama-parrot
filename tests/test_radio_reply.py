# SPDX-License-Identifier: GPL-3.0-or-later
import importlib.util
import io
import json
import math
from pathlib import Path
import struct
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave

spec = importlib.util.spec_from_file_location('radio_reply', Path(__file__).resolve().parents[1]/'scripts/radio-reply.py')
reply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reply)


def tone(duration=1, frequency=500, rate=8000):
    data = io.BytesIO()
    with wave.open(data, 'wb') as wav:
        wav.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        wav.writeframes(b''.join(struct.pack('<h', int(5000*math.sin(2*math.pi*frequency*i/rate)))
                                 for i in range(int(rate*duration))))
    return data.getvalue()


class ReplyTests(unittest.TestCase):
    def test_endpoint_detector_prefix_silence_and_no_audio(self):
        detector = reply.Utterance(80, 0.3)
        quiet = bytes(1600)
        loud = struct.pack('<800h', *([1000]*800))
        for _ in range(10):
            self.assertFalse(detector.feed(quiet))
        self.assertFalse(detector.started)
        self.assertFalse(detector.feed(loud))
        self.assertEqual(detector.data, quiet*2+loud)
        self.assertFalse(detector.feed(quiet))
        self.assertFalse(detector.feed(quiet))
        self.assertTrue(detector.feed(quiet))

    def test_direct_audio_payload_and_duration_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'received.wav'
            audio = tone(0.2)
            path.write_bytes(audio)
            content = reply.audio_content(path)
            self.assertEqual(content[1]['type'], 'input_audio')
            self.assertEqual(content[1]['input_audio']['format'], 'wav')
            self.assertEqual(reply.base64.b64decode(content[1]['input_audio']['data']), audio)
            path.write_bytes(tone(30.1))
            with self.assertRaisesRegex(ValueError, '30 seconds'):
                reply.audio_content(path)

    def test_chipmunk_pitch_preserves_duration(self):
        shifted = reply.chipmunk(tone(), 1.5)
        with wave.open(io.BytesIO(shifted), 'rb') as wav:
            self.assertEqual(wav.getframerate(), 48000)
            self.assertAlmostEqual(wav.getnframes()/48000, 1, delta=0.08)
            samples = struct.unpack('<'+str(wav.getnframes())+'h', wav.readframes(wav.getnframes()))
        middle = samples[4800:-4800]
        crossings = sum(a <= 0 < b for a,b in zip(middle, middle[1:]))
        frequency = crossings*48000/len(middle)
        self.assertAlmostEqual(frequency, 750, delta=10)

    def test_remote_estonian_transcription_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'received.wav'
            path.write_bytes(tone(0.2))
            args = SimpleNamespace(asr_model='Systran/faster-whisper-small',
                asr_url='http://test/v1/audio/transcriptions', timeout=10, language='et')
            def respond(request, timeout):
                self.assertEqual(timeout, 10)
                self.assertIn(b'name="language"\r\n\r\net\r\n', request.data)
                self.assertIn(b'RIFF', request.data)
                self.assertEqual(request.get_header('Authorization'), 'Bearer test-key')
                return io.BytesIO(json.dumps({'text':'Tere, siil!'}).encode())
            with patch.dict(reply.os.environ, {'ASR_API_KEY':'test-key'}), \
                 patch.object(reply.urllib.request, 'urlopen', side_effect=respond):
                self.assertEqual(reply.transcribe(path,args), 'Tere, siil!')


class TransmissionTests(unittest.TestCase):
    def block(self, value=1000):
        return struct.pack('<800h', *([value]*800))

    def test_separate_transmissions_queue_without_overlap(self):
        w = reply.bot.TransmissionClips(30, 0.2, 80)
        for _ in range(100):
            w.feed(self.block(0))
        for value in (1000, 2000):
            for _ in range(5):
                w.feed(self.block(value))
            w.feed(self.block(0))
            w.feed(self.block(0))
        first, second = w.take(), w.take()
        self.assertEqual(first['pcm'], self.block(1000)*5+self.block(0)*2)
        self.assertEqual(second['pcm'], self.block(2000)*5+self.block(0)*2)
        self.assertEqual(first['start_sample'], 100*800)
        self.assertEqual(second['start_sample'], 107*800)
        self.assertIsNone(w.take())

    def test_time_limit_keeps_start_and_waits_for_end_before_rearming(self):
        w = reply.bot.TransmissionClips(1, 0.2, 80)
        for _ in range(35):
            w.feed(self.block())
        clip = w.take()
        self.assertEqual(len(clip['pcm']), 8000*2)
        self.assertEqual(clip['start_sample'], 0)
        self.assertEqual(clip['end_reason'], 'limit')
        self.assertIsNone(w.take())
        self.assertFalse(w.channel_clear())
        for _ in range(2):
            w.feed(self.block(0))
        self.assertTrue(w.channel_clear())
        w.feed(self.block(2000))
        self.assertFalse(w.channel_clear())
        for _ in range(2):
            w.feed(self.block(0))
        self.assertEqual(w.take()['pcm'], self.block(2000)+self.block(0)*2)

    def test_tx_is_not_recorded_and_stale_capture_is_not_clear(self):
        w = reply.bot.TransmissionClips(1, 0.2, 80)
        for _ in range(20):
            w.feed(self.block(), muted=True)
        self.assertIsNone(w.take(True))
        for _ in range(2):
            w.feed(self.block(0))
        self.assertTrue(w.channel_clear())
        w.last_sample_time -= 1
        self.assertFalse(w.channel_clear())

    def test_threshold_and_silence_are_sample_driven(self):
        w = reply.bot.TransmissionClips(30, 0.3, 80)
        w.feed(self.block(20))
        self.assertIsNone(w.take(True))
        w.feed(self.block(200))
        for _ in range(2):
            w.feed(self.block(0))
        self.assertIsNone(w.take())
        w.feed(self.block(0))
        self.assertIsNotNone(w.take())

    def test_wav_exists_at_start_and_is_closed_before_queued(self):
        with tempfile.TemporaryDirectory() as folder:
            capture = object.__new__(reply.bot.ContinuousCapture)
            capture.state = reply.bot.BotState(Path(folder))
            capture.writer = None
            capture.directory = None
            w = reply.bot.TransmissionClips(1, 0.2, 80, capture.clip_event)
            w.feed(self.block(1500))
            path = capture.directory/'received.wav'
            self.assertTrue(path.exists())
            self.assertIsNotNone(capture.writer)
            self.assertIsNone(w.take())
            for _ in range(2):
                w.feed(self.block(0))
            clip = w.take()
            self.assertEqual(clip['directory'], str(capture.directory))
            self.assertIsNone(capture.writer)
            with wave.open(str(path), 'rb') as wav:
                self.assertEqual(wav.getnframes(), 2400)
                self.assertEqual(wav.readframes(800), self.block(1500))

    def test_model_preserves_caller_language(self):
        args = SimpleNamespace(model='gemma-audio', gemma_url='http://test/v1', timeout=10)
        def respond(request, timeout):
            payload = json.loads(request.data)
            self.assertIn('English for English', payload['messages'][0]['content'])
            self.assertIn('language', payload['response_format']['schema']['required'])
            return io.BytesIO(json.dumps({'choices': [{'message': {'content': json.dumps({
                'transcript': 'Hello hedgehog', 'reply': 'What now?', 'language': 'en'})}}]}).encode())
        with patch.dict(reply.os.environ, {'GEMMA_API_KEY': 'test'}), \
             patch.object(reply.urllib.request, 'urlopen', side_effect=respond):
            result = reply.gemma_reply([{'type':'input_audio'}], args)
            self.assertEqual(result['language'], 'en')
            self.assertEqual(result['transcript'], 'Hello hedgehog')


class ChannelEventTests(unittest.TestCase):
    def test_channel_report_including_vfo_and_spaced_names(self):
        self.assertEqual(reply.bot.parse_channel('CHANNEL index=2 rx=446031250 tx=446031250 name=PMR 3'),
                         {'index': 2, 'rx': 446031250, 'tx': 446031250, 'name': 'PMR 3'})
        self.assertEqual(reply.bot.parse_channel('CHANNEL index=-1 rx=145500000 tx=145500000 name=VFO')['index'], -1)
        with self.assertRaises(ValueError):
            reply.bot.parse_channel('OK')

    def test_channel_event_does_not_consume_ptt_reply(self):
        usb = reply.announce.USB
        radio = object.__new__(usb.Radio)
        radio.fd = 123
        radio.pending = bytearray()
        radio.lock = usb.threading.RLock()
        radio.events = usb.collections.deque(maxlen=64)
        event = 'CHANNEL index=0 rx=446006250 tx=446006250 name=PMR 1'
        wire = usb.encode(b'\x02'+event.encode())+usb.encode(b'\x01OK')
        with patch.object(usb.select, 'select', side_effect=lambda r,w,e,t: (r,w,e)), \
             patch.object(usb.os, 'write', side_effect=lambda fd,data: len(data)), \
             patch.object(usb.os, 'read', return_value=wire):
            self.assertEqual(radio.command('PTT 1'), 'OK')
        with patch.object(usb.select, 'select', return_value=([],[],[])):
            self.assertEqual(radio.poll_events(), [event])
            self.assertEqual(radio.poll_events(), [])
