#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Siil Udus: radio conversations in Estonian and English, with a live web UI."""
import argparse
import base64
import uuid
import collections
import copy
from datetime import datetime, timezone
import shutil
import importlib.util
import json
import math
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import time
import urllib.request
import wave

spec = importlib.util.spec_from_file_location('announce', Path(__file__).with_name('announce-time.py'))
announce = importlib.util.module_from_spec(spec)
spec.loader.exec_module(announce)
PERSONA = '''You are Siil Udus, a grumpy but funny hedgehog. Everyone annoys you,
but you are not cruel. Reply in the caller's language: Estonian for Estonian,
English for English. Use natural, grammatical language, one to three short
sentences, at most 45 words. Occasionally use “Ärge lollitage niimoodi” in
Estonian, or “Don't be silly like that” in English, but usually omit catchphrases.
No stage directions, emoji or explanations of your role. Do not invent inaudible
words. If unclear, ask briefly for repetition. Audio contains one incoming transmission. Respond to that caller message. Transcribe what
you actually hear, without translating it or putting words in the caller's mouth.
Return JSON with transcript, reply, and language (et or en). The hedgehog persona
applies only to reply, never to transcript.'''
RATE = 8000

spec = importlib.util.spec_from_file_location('radio_bot', Path(__file__).with_name('radio_bot.py'))
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)


def stop_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class Utterance:
    """Bounded PCM endpoint detector; preserve 300 ms before the first sound."""
    def __init__(self, threshold, silence):
        self.threshold = threshold
        self.silence = silence
        self.prefix = collections.deque(maxlen=3)
        self.data = bytearray()
        self.started = False
        self.quiet = 0.0

    def feed(self, block):
        samples = struct.unpack('<' + 'h' * (len(block)//2), block)
        rms = math.sqrt(sum(x*x for x in samples) / len(samples))
        loud = rms >= self.threshold
        if not self.started:
            self.prefix.append(block)
            if not loud:
                return False
            self.started = True
            self.data.extend(b''.join(self.prefix))
        else:
            self.data.extend(block)
        self.quiet = 0.0 if loud else self.quiet + len(samples)/RATE
        return self.quiet >= self.silence


def record(source, output, seconds, silence, threshold):
    detector = Utterance(threshold, silence)
    process = subprocess.Popen(['parec', '--raw', '--format=s16le', '--channels=1',
                                '--rate=8000', '--latency-msec=100',
                                '--device='+source], stdout=subprocess.PIPE)
    deadline = time.monotonic() + seconds
    pending = bytearray()
    captured = 0
    try:
        while time.monotonic() < deadline and captured < int(seconds * RATE):
            if not select.select([process.stdout], [], [], min(0.2, max(0, deadline-time.monotonic())))[0]:
                if process.poll() is not None:
                    raise RuntimeError(f'parec exited ({process.returncode})')
                continue
            data = os.read(process.stdout.fileno(), 1600)
            if not data:
                raise RuntimeError('Radio recording ended unexpectedly')
            pending.extend(data)
            if len(pending) >= 1600:
                block = bytes(pending[:1600])
                del pending[:1600]
                captured += 800
                if detector.feed(block):
                    break
    finally:
        stop_process(process)
        process.stdout.close()
    if not detector.started:
        return False
    with wave.open(str(output), 'wb') as wav:
        wav.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        wav.writeframes(detector.data)
    return True


def discover_source():
    listing = subprocess.check_output(['pactl', 'list', 'short', 'sources'], text=True)
    names = [line.split()[1] for line in listing.splitlines()
             if len(line.split()) > 1 and 'alsa_input.usb-' in line
             and ('Tytera' in line or 'TYT' in line)]
    if len(names) != 1:
        raise ValueError('Use --source with the radio input name from pactl list short sources')
    return names[0]


def transcribe(path, args):
    with wave.open(str(path), 'rb') as wav:
        duration = wav.getnframes() / wav.getframerate()
        if not 0 < duration <= 60:
            raise ValueError('Input recording must be at most 60 seconds')
    audio = path.read_bytes()
    if len(audio) > 16 * 1024 * 1024:
        raise ValueError('Input WAV exceeds 16 MiB')
    token = os.environ.get('ASR_API_KEY') or os.environ.get('GEMMA_API_KEY') or args.gemma_key_file.expanduser().read_text().strip()
    boundary = 'tyt-' + uuid.uuid4().hex
    parts = []
    fields = [('model', args.asr_model), ('response_format', 'json')]
    if getattr(args, 'language', 'auto') != 'auto':
        fields.append(('language', args.language))
    for name, value in fields:
        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
                      f'\r\n\r\n{value}\r\n').encode())
    parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                  'filename="radio.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode())
    parts.extend([audio, f'\r\n--{boundary}--\r\n'.encode()])
    req = urllib.request.Request(args.asr_url, data=b''.join(parts), headers={
        'Authorization': 'Bearer '+token,
        'Content-Type': 'multipart/form-data; boundary='+boundary})
    with urllib.request.urlopen(req, timeout=args.timeout) as response:
        body = response.read(1024*1024+1)
    if len(body) > 1024*1024:
        raise ValueError('Transcription response is too large')
    return json.loads(body)['text'].strip()


def audio_content(path):
    with wave.open(str(path), 'rb') as wav:
        duration = wav.getnframes() / wav.getframerate()
    if not 0 < duration <= 30:
        raise ValueError('Direct Gemma audio requires a recording of at most 30 seconds')
    if path.stat().st_size > 16*1024*1024:
        raise ValueError('Audio input exceeds 16 MiB')
    return [
        {'type': 'text', 'text': 'Listen to this Estonian or English radio recording. Transcribe the caller and reply to their latest message in the same language. Return JSON with transcript, reply and language (et or en). Mark unclear words [unclear].'},
        {'type': 'input_audio', 'input_audio': {
            'data': base64.b64encode(path.read_bytes()).decode('ascii'), 'format': 'wav'}}]


def gemma_reply(text, args):
    token = os.environ.get('GEMMA_API_KEY') or args.gemma_key_file.expanduser().read_text().strip()
    def request(path, body=None):
        req = urllib.request.Request(args.gemma_url.rstrip('/') + path,
            data=None if body is None else json.dumps(body, ensure_ascii=False).encode(),
            headers={'Authorization': 'Bearer '+token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=args.timeout) as response:
            data = response.read(1024*1024+1)
        if len(data) > 1024*1024:
            raise ValueError('Gemma response is too large')
        return json.loads(data)
    model = args.model
    if not model:
        models = request('/models')['data']
        candidates = [m['id'] for m in models if 'gemma' in m['id'].lower()]
        if len(candidates) != 1:
            raise ValueError('Select Gemma explicitly with --model')
        model = candidates[0]
    direct_audio = isinstance(text, list)
    payload = {
        'model': model, 'messages': [{'role': 'system', 'content': PERSONA},
                                     {'role': 'user', 'content': text}],
        'max_tokens': 1024, 'temperature': 0.6, 'stream': False,
        'chat_template_kwargs': {'enable_thinking': False},
        'response_format': {'type': 'json_object', 'schema': {
            'type': 'object', 'properties': {
                'transcript': {'type': 'string'}, 'reply': {'type': 'string'},
                'language': {'type': 'string', 'enum': ['et', 'en']}},
            'required': ['transcript', 'reply', 'language'], 'additionalProperties': False}}}
    result = request('/chat/completions', payload)
    turn = json.loads(result['choices'][0]['message'].get('content', '').strip())
    if not isinstance(turn, dict) or not all(isinstance(turn.get(k), str)
                                           for k in ('transcript', 'reply', 'language')):
        raise ValueError('Gemma returned invalid transcript/reply fields')
    turn = {key: value.strip() for key, value in turn.items()}
    if not direct_audio:
        turn['transcript'] = text
    if (not turn['reply'] or '<think>' in turn['reply'] or len(turn['reply']) > 1500
            or turn['language'] not in ('et', 'en')):
        raise ValueError('Gemma returned no usable short spoken reply')
    return turn


def chipmunk(data, pitch):
    with tempfile.TemporaryDirectory(prefix='tyt-chipmunk-') as directory:
        source, output = Path(directory)/'in.wav', Path(directory)/'out.wav'
        source.write_bytes(data)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source),
            '-af', f'aresample=48000,asetrate={48000*pitch},aresample=48000,atempo={1/pitch},loudnorm=I=-16:TP=-1.5:LRA=7',
            '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le', str(output)], check=True)
        return announce.validate_wav(output.read_bytes())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', help='PulseAudio/PipeWire radio input; autodetects one Tytera')
    parser.add_argument('--sink', help='Radio playback sink; derived from --source by default')
    parser.add_argument('--seconds', type=float, default=30)
    parser.add_argument('--silence', type=float, default=0.8)
    parser.add_argument('--threshold', type=float, default=80, help='PCM RMS activity threshold')
    parser.add_argument('--input-wav', type=Path, help='Test with an existing recording, at most 60 s')
    parser.add_argument('--text', help='Test without recording or speech recognition')
    parser.add_argument('--direct-audio', action='store_true',
                        help='Send audio directly to Gemma E4B; caps live turns at 30 seconds')
    parser.add_argument('--audio-url', default=os.environ.get('GEMMA_AUDIO_URL',
                        'http://[fd10:0:0:fd00::85]:8080/v1'))
    parser.add_argument('--asr-model', default='Systran/faster-whisper-small')
    parser.add_argument('--asr-url', default=os.environ.get('ASR_URL', 'http://[fd10:0:0:fd00::84]:8000/v1/audio/transcriptions'))
    parser.add_argument('--gemma-url', default=os.environ.get('GEMMA_URL', 'http://10.253.0.81:8080/v1'))
    parser.add_argument('--gemma-key-file', type=Path, default=Path('~/.config/opencode/gemma-api-key'))
    parser.add_argument('--model')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--tts-url', default=os.environ.get('TTS_URL', 'http://10.253.0.83:8080/v1/audio/speech'))
    parser.add_argument('--language', choices=('auto', 'et', 'en'), default='auto',
                        help='Whisper input language; auto detects Estonian or English')
    parser.add_argument('--english-voice', default='en_US-lessac-medium')
    parser.add_argument('--web-port', type=int, default=8765, help='Local SSE dashboard port; 0 disables it')
    parser.add_argument('--voice', default='et_EE-news-medium')
    parser.add_argument('--kube-context', default='admin@ee-west-2.codemowers.io')
    parser.add_argument('--cache-dir', type=Path, default=Path('artifacts/tts-cache'))
    parser.add_argument('--output-dir', type=Path, default=Path('artifacts/radio-replies'))
    parser.add_argument('--pitch', type=float, default=1.5, help='Chipmunk pitch multiplier, 1..2')
    parser.add_argument('--gain', type=float, default=1.0,
                        help='Playback gain, 0 < gain <= 1 (default: 1)')
    parser.add_argument('--receiver-delay', type=float, default=2)
    parser.add_argument('--port', default='/dev/ttyACM0')
    parser.add_argument('--transmit', action='store_true', help='Transmit reply; otherwise generate WAV only')
    parser.add_argument('--loop', action='store_true', help='Listen again after each reply; Ctrl-C stops')
    args = parser.parse_args()
    if not (0.1 <= args.seconds <= 30 and 0.2 <= args.silence <= 10 and
            0 < args.threshold <= 32768 and 1 <= args.pitch <= 2 and
            0 < args.gain <= 1 and 0 <= args.receiver_delay <= 5 and 1 <= args.timeout <= 600):
        parser.error('Invalid recording, voice, or timeout settings')
    if not 0 <= args.web_port <= 65535:
        parser.error('Invalid web port')
    if args.direct_audio:
        args.seconds = min(args.seconds, 30)
        args.gemma_url = args.audio_url
    if args.loop and (args.text or args.input_wav):
        parser.error('--loop requires live recording')
    if (not args.text and not args.input_wav) or args.transmit:
        args.source = args.source or discover_source()
    if args.transmit:
        args.sink = args.sink or args.source.replace('alsa_input.', 'alsa_output.', 1)
        radio = announce.USB.Radio(args.port)
        try:
            status = radio.command('STATUS')
            if 'ptt_mode=Triggered' not in status or 'muted=0' not in status:
                raise ValueError('Set PTT Mode to Triggered and unmute Radio playback first')
        finally:
            radio.close()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    state = bot.BotState(args.output_dir)
    state.update(transmit=args.transmit, window_seconds=args.seconds, mode='Gemma audio' if args.direct_audio else 'Whisper + Gemma')
    server = bot.serve_ui(state, args.web_port) if args.web_port else None
    capture = None
    monitor = None
    if server:
        print(f'Siil Udus: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        if (not args.text and not args.input_wav) or args.transmit:
            radio = None
            try:
                radio = announce.USB.Radio(args.port)
                monitor = bot.ChannelMonitor(radio, state)
            except (OSError, ValueError, RuntimeError) as error:
                if radio:
                    radio.close()
                state.update(channel_error=str(error))
                print(f'Channel monitor: {error}', file=sys.stderr)
        if (not args.text and not args.input_wav) or args.transmit:
            capture = bot.ContinuousCapture(args.source, state, args.seconds, args.silence, args.threshold)
            print(f'One clip per transmission, maximum {args.seconds:g} seconds; {args.silence:g} s silence ends a clip.', flush=True)
        while True:
            state.update(phase='listening', error=None)
            snapshot = capture.next() if capture and not (args.text or args.input_wav) else None
            if snapshot:
                directory = Path(snapshot.pop('directory'))
            else:
                name = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S.%fZ')
                directory = args.output_dir/name
                directory.mkdir()
            recording = directory/'received.wav'
            if args.input_wav:
                shutil.copyfile(args.input_wav, recording)
            state.turn(directory, phase='processing', received=recording.exists(), capture=snapshot)
            started = time.monotonic()
            try:
                if args.text:
                    text = args.text
                elif args.direct_audio:
                    state.update(phase='thinking')
                    print('Sending completed transmission to Gemma E4B...', flush=True)
                    text = audio_content(recording)
                else:
                    state.update(phase='transcribing')
                    text = transcribe(recording, args)
                if not text:
                    state.turn(directory, phase='no speech')
                    if args.loop:
                        continue
                    return
                state.update(phase='thinking')
                turn = gemma_reply(text, args)
                print('Heard: '+turn['transcript'], flush=True)
                print('Siil: '+turn['reply'], flush=True)
                (directory/'transcript.txt').write_text(turn['transcript']+'\n')
                (directory/'reply.txt').write_text(turn['reply']+'\n')
                state.turn(directory, **turn, phase='synthesizing')
                state.update(phase='synthesizing')
                voice_args = copy.copy(args)
                voice_args.voice = args.english_voice if turn['language'] == 'en' else args.voice
                data = chipmunk(announce.synthesize(turn['reply'], voice_args), args.pitch)
                output = directory/'reply.wav'
                announce.prepare_audio(data, output, args.gain, args.receiver_delay)
                state.turn(directory, audio=True, processing_seconds=round(time.monotonic()-started, 2))
                print('Audio: '+str(output), flush=True)
                can_transmit = args.transmit and not state.snapshot()['status']['paused']
                if can_transmit and capture:
                    can_transmit = capture.wait_clear()
                if can_transmit:
                    state.update(phase='transmitting')
                    if capture:
                        capture.suppress(True)
                    try:
                        announce.transmit(output, None, args.port,
                            player_command=['paplay', '--device='+args.sink, str(output)],
                            radio=monitor.radio if monitor else None)
                    finally:
                        if capture:
                            capture.suppress(False)
                    state.turn(directory, transmitted=True)
                state.turn(directory, phase='done')
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
                state.turn(directory, phase='error', error=str(error))
                state.update(phase='error', error=str(error))
                if not args.loop:
                    raise
                print(f'Turn failed: {error}', file=sys.stderr, flush=True)
                time.sleep(1)
            if not args.loop:
                break
    finally:
        if capture:
            capture.close()
        if monitor:
            monitor.close()
        state.update(phase='stopped')
        state.closed = True
        with state.condition:
            state.condition.notify_all()
        if server:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, announce.interrupted)
    try:
        main()
    except KeyboardInterrupt:
        print('Stopped.', file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(f'Radio reply failed: {error}', file=sys.stderr)
        sys.exit(1)
