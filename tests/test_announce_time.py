# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
from datetime import datetime
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import wave

SPEC = importlib.util.spec_from_file_location(
    'announce_time', Path(__file__).resolve().parents[1] / 'scripts/announce-time.py')
client = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(client)


def sample():
    data = io.BytesIO()
    with wave.open(data, 'wb') as writer:
        writer.setparams((1, 2, 22050, 0, 'NONE', 'not compressed'))
        writer.writeframes(b'\x00\x10' * 22050)
    return data.getvalue()


class AnnouncementTests(unittest.TestCase):
    def test_rounding_and_midnight(self):
        cases = {'15:14': 'Kell on kolm.', '15:15': 'Kell on pool neli.',
                 '15:30': 'Kell on pool neli.', '15:44': 'Kell on pool neli.',
                 '15:45': 'Kell on neli.', '23:30': 'Kell on pool kaksteist.',
                 '23:45': 'Kell on kaksteist.', '00:15': 'Kell on pool üks.'}
        for clock, expected in cases.items():
            with self.subTest(clock=clock):
                now = datetime.strptime('2026-11-07 ' + clock, '%Y-%m-%d %H:%M')
                self.assertEqual(client.announcement(now), 'Täna on seitsmes november 2026. ' + expected)

    def test_resampling_and_keyup_silence(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'test.wav'
            client.prepare_audio(sample(), output, 0.5)
            with wave.open(str(output), 'rb') as reader:
                self.assertEqual((reader.getnchannels(), reader.getsampwidth(), reader.getframerate()),
                                 (1, 2, 48000))
                self.assertEqual(reader.getnframes(), 240000)
                self.assertEqual(reader.readframes(168000), bytes(336000))
                self.assertNotEqual(reader.readframes(48000), bytes(96000))
                self.assertEqual(reader.readframes(24000), bytes(48000))

    def test_speech_cached_without_reusing_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(tts_url='http://test/v1/audio/speech',
                                      voice='et_EE-news-medium', cache_dir=Path(directory))
            response = MagicMock()
            response.__enter__.return_value.read.return_value = sample()
            with patch.dict(client.os.environ, {'TTS_API_KEY': 'test-token'}), \
                 patch.object(client.urllib.request, 'urlopen', return_value=response) as request:
                first = client.synthesize('Kell on pool neli.', args)
                second = client.synthesize('Kell on pool neli.', args)
                self.assertEqual(first, second)
                self.assertEqual(request.call_count, 1)
                self.assertEqual(request.call_args.args[0].get_header('Authorization'),
                                 'Bearer test-token')

    def test_failure_releases_ptt_and_stops_playback(self):
        radio = MagicMock()
        radio.command.side_effect = ['OK', 'ERR rejected', 'OK']
        player = MagicMock()
        player.poll.return_value = None
        with patch.object(client.USB, 'Radio', return_value=radio), \
             patch.object(client.subprocess, 'Popen', return_value=player):
            with self.assertRaisesRegex(RuntimeError, 'rejected'):
                client.transmit('test.wav', 'null', '/dev/test')
        self.assertEqual(radio.command.call_args.args[0], 'PTT 0')
        radio.close.assert_called_once()
        player.terminate.assert_called_once()

    def test_playback_startup_retries_then_renews(self):
        radio = MagicMock()
        radio.command.side_effect = ['OK', 'ERR playback inactive or timeout; PTT 0 to reset',
                                     'OK requested', 'OK requested', 'OK']
        player = MagicMock()
        player.poll.side_effect = [None, None, None, 0, 0]
        player.returncode = 0
        with patch.object(client.USB, 'Radio', return_value=radio), \
             patch.object(client.subprocess, 'Popen', return_value=player), \
             patch.object(client.time, 'sleep'):
            client.transmit('test.wav', 'null', '/dev/test')
        self.assertEqual([c.args[0] for c in radio.command.call_args_list],
                         ['PTT 0', 'PTT 1', 'PTT 1', 'PTT 1', 'PTT 0'])
        player.terminate.assert_not_called()

    def test_late_keyup_aborts_before_receiver_buffer_is_used(self):
        radio = MagicMock()
        radio.command.return_value = 'OK'
        player = MagicMock()
        player.poll.return_value = None
        with patch.object(client.USB, 'Radio', return_value=radio), \
             patch.object(client.subprocess, 'Popen', return_value=player), \
             patch.object(client.time, 'monotonic', side_effect=[0, 0.1, 1.6]):
            with self.assertRaisesRegex(TimeoutError, 'receiver buffer'):
                client.transmit('test.wav', 'null', '/dev/test')
        self.assertEqual(radio.command.call_args.args[0], 'PTT 0')
        player.terminate.assert_called_once()


if __name__ == '__main__':
    unittest.main()
