#!/usr/bin/env python3
"""Announce the nearest half-hour in Estonian using Piper, ALSA and TYT USB PTT."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
from datetime import datetime
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import urllib.request
import wave
from zoneinfo import ZoneInfo

SPEC = importlib.util.spec_from_file_location('tyt_usb', Path(__file__).with_name('tyt-usb.py'))
USB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(USB)
HOURS = ('kaksteist', 'üks', 'kaks', 'kolm', 'neli', 'viis', 'kuus',
         'seitse', 'kaheksa', 'üheksa', 'kümme', 'üksteist')
MONTHS = ('jaanuar', 'veebruar', 'märts', 'aprill', 'mai', 'juuni',
          'juuli', 'august', 'september', 'oktoober', 'november', 'detsember')
DAYS = ('esimene', 'teine', 'kolmas', 'neljas', 'viies', 'kuues', 'seitsmes',
        'kaheksas', 'üheksas', 'kümnes', 'üheteistkümnes', 'kaheteistkümnes',
        'kolmeteistkümnes', 'neljateistkümnes', 'viieteistkümnes',
        'kuueteistkümnes', 'seitsmeteistkümnes', 'kaheksateistkümnes',
        'üheksateistkümnes', 'kahekümnes', 'kahekümne esimene',
        'kahekümne teine', 'kahekümne kolmas', 'kahekümne neljas',
        'kahekümne viies', 'kahekümne kuues', 'kahekümne seitsmes',
        'kahekümne kaheksas', 'kahekümne üheksas', 'kolmekümnes',
        'kolmekümne esimene')
KEYUP_BUDGET = 1.5


def announcement(now):
    # Ties at :15 and :45 round forward. Half past three is "pool neli".
    slot = (now.hour * 60 + now.minute + 15) // 30
    hour, half = divmod(slot, 2)
    clock = 'Kell on ' + ('pool ' if half else '') + HOURS[(hour + half) % 12] + '.'
    return f'Täna on {DAYS[now.day - 1]} {MONTHS[now.month - 1]} {now.year}. {clock}'


def validate_wav(data):
    with wave.open(io.BytesIO(data), 'rb') as reader:
        duration = reader.getnframes() / reader.getframerate()
        if reader.getcomptype() != 'NONE' or not 0.1 <= duration <= 45:
            raise ValueError('TTS must return an uncompressed WAV lasting 0.1–45 seconds')
    return data


def synthesize(text, args):
    payload = {'input': text, 'model': 'piper', 'voice': args.voice,
               'response_format': 'wav'}
    body = json.dumps(payload, ensure_ascii=False).encode()
    key = hashlib.sha256(args.tts_url.encode() + body).hexdigest()
    cached = args.cache_dir / (key + '.wav')
    if cached.exists():
        return validate_wav(cached.read_bytes())
    token = os.environ.get('TTS_API_KEY')
    if not token:
        import base64
        encoded = subprocess.check_output([
            'kubectl', '--context', args.kube_context, '--request-timeout=10s',
            '-n', 'llm', 'get', 'secret', 'piper-tts-api-token',
            '-o', 'jsonpath={.data.token}'], text=True)
        token = base64.b64decode(encoded).decode()
    if not token:
        raise ValueError('Set TTS_API_KEY or provide access to the Piper Kubernetes Secret')
    request = urllib.request.Request(args.tts_url, data=body, headers={
        'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token})
    with urllib.request.urlopen(request, timeout=90) as response:
        data = response.read(10 * 1024 * 1024 + 1)
    if len(data) > 10 * 1024 * 1024:
        raise ValueError('TTS response is too large')
    validate_wav(data)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=args.cache_dir, delete=False) as tmp:
        tmp.write(data)
        temporary = Path(tmp.name)
    temporary.replace(cached)
    return data


def prepare_audio(data, output, gain, receiver_delay=2.0, speed=1.0):
    # Resample before opening the radio; no network or TTS work while keyed.
    with tempfile.TemporaryDirectory(prefix='tyt-tts-') as directory:
        source = Path(directory) / 'source.wav'
        converted = Path(directory) / 'converted.wav'
        source.write_bytes(data)
        filters = f'volume={gain}'
        if speed != 1.0:
            filters = f'atempo={speed},' + filters
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(source),
                        '-ac', '1', '-ar', '48000', '-c:a', 'pcm_s16le',
                        '-af', filters, str(converted)], check=True)
        with wave.open(str(converted), 'rb') as reader:
            frames = reader.readframes(reader.getnframes())
        with wave.open(str(output), 'wb') as writer:
            writer.setparams((1, 2, 48000, 0, 'NONE', 'not compressed'))
            # Key-up must complete within its budget, leaving the entire
            # receiver delay before speech even on the slowest accepted start.
            lead_frames = int((KEYUP_BUDGET + receiver_delay) * 48000)
            writer.writeframes(bytes(lead_frames * 2) + frames + bytes(48000))


def transmit(path, device, port, player_command=None, radio=None):
    own_radio = radio is None
    radio = radio or USB.Radio(port)
    player = None
    try:
        if not radio.command('PTT 0').startswith('OK'):
            raise RuntimeError('Radio refused PTT reset')
        player = subprocess.Popen(player_command or
                                  ['aplay', '-q', '-D', device, str(path)])
        started = time.monotonic()
        keyed = False
        while player.poll() is None:
            elapsed = time.monotonic() - started
            if elapsed >= 55:
                raise TimeoutError('Playback exceeded the announcement deadline')
            reply = radio.command('PTT 1', timeout=0.4)
            if not keyed and time.monotonic() - started >= KEYUP_BUDGET:
                raise TimeoutError('PTT startup was too slow to preserve the receiver buffer')
            if reply.startswith('OK'):
                keyed = True
            elif keyed or elapsed >= 1 or 'playback inactive' not in reply:
                raise RuntimeError('Radio rejected PTT: ' + reply)
            time.sleep(0.2 if keyed else 0.05)
        if player.returncode:
            raise RuntimeError(f'ALSA playback failed ({player.returncode})')
        if not keyed:
            raise RuntimeError('Playback ended before the radio accepted PTT')
    finally:
        try:
            radio.command('PTT 0', timeout=0.4)
        except (OSError, TimeoutError, ConnectionError):
            pass  # Disconnect and the firmware lease also release USB PTT.
        if own_radio:
            radio.close()
        if player is not None and player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=2)
            except subprocess.TimeoutExpired:
                player.kill()
                player.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tts-url', default=os.environ.get(
        'TTS_URL', 'http://10.253.0.83:8080/v1/audio/speech'))
    parser.add_argument('--kube-context', default='admin@ee-west-2.codemowers.io')
    parser.add_argument('--voice', default='et_EE-news-medium')
    parser.add_argument('--timezone', default='Europe/Tallinn')
    parser.add_argument('--at', type=lambda s: datetime.strptime(s, '%H:%M'),
                        help='Override the clock for a test, e.g. 15:30')
    parser.add_argument('--text', help='Speak supplied text instead of the time')
    parser.add_argument('--print-text', action='store_true', help='No TTS or radio access')
    parser.add_argument('--output', type=Path, default=Path('artifacts/time-announcement.wav'))
    parser.add_argument('--cache-dir', type=Path, default=Path('artifacts/tts-cache'))
    parser.add_argument('--gain', type=float, default=0.5, help='Audio gain, 0 < gain <= 1')
    parser.add_argument('--speed', type=float, default=1.0,
                        help='Speech tempo, 0.75–1.5 (default: 1)')
    parser.add_argument('--receiver-delay', type=float, default=2.0,
                        help='Minimum silence after PTT acknowledgement, 0–5 seconds (default: 2)')
    parser.add_argument('--transmit', action='store_true', help='Play and key the selected channel')
    parser.add_argument('--device', help='ALSA device for the radio, e.g. plughw:CARD=OpenRTX,DEV=0')
    parser.add_argument('--port', default='/dev/ttyACM0')
    args = parser.parse_args()
    if not 0 < args.gain <= 1:
        parser.error('--gain must be greater than 0 and at most 1')
    if not 0.75 <= args.speed <= 1.5:
        parser.error('--speed must be between 0.75 and 1.5')
    if not 0 <= args.receiver_delay <= 5:
        parser.error('--receiver-delay must be between 0 and 5 seconds')
    if args.transmit and not args.device:
        parser.error('--transmit requires --device')
    zone = ZoneInfo(args.timezone)
    def current_text():
        now = datetime.now(zone)
        if args.at:
            now = now.replace(hour=args.at.hour, minute=args.at.minute, second=0, microsecond=0)
        return args.text or announcement(now)
    if args.print_text:
        print(current_text())
        return
    for _ in range(3):
        text = current_text()
        data = synthesize(text, args)
        if current_text() == text:
            break
    else:
        raise RuntimeError('Clock announcement changed repeatedly during synthesis; retry')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    prepare_audio(data, args.output, args.gain, args.receiver_delay, args.speed)
    print(f'{text}  [AI voice: {args.voice}]\nAudio: {args.output}', flush=True)
    if args.transmit:
        transmit(args.output, args.device, args.port)


def interrupted(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    try:
        main()
    except KeyboardInterrupt:
        raise SystemExit(130)
