# SPDX-License-Identifier: GPL-3.0-or-later
"""Continuous radio capture and the local Siil Udus SSE dashboard."""
import collections
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import select
import re
import struct
import subprocess
import threading
import time
from urllib.parse import unquote, urlsplit
import wave

RATE = 8000
BLOCK = 800  # 100 ms, mono signed 16 bit
ASSETS = Path(__file__).with_name('radio-ui')


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds')


def write_wav(path, pcm):
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
        wav.writeframes(pcm)


class TransmissionClips:
    """One clip per activity burst, ending at silence or the hard duration limit."""
    def __init__(self, seconds=30, silence=0.8, threshold=80, callback=None):
        self.capacity = int(seconds*RATE)
        self.silence_samples = round(silence*RATE)
        self.threshold = threshold
        self.callback = callback
        self.completed = collections.deque()
        self.position = 0
        self.last_loud = 0
        self.level = 0
        self.active = False
        self.waiting_for_end = False
        self.start = 0
        self.frames = 0
        self.data = bytearray()
        self.last_sample_time = None

    def finish(self, reason):
        if not self.active:
            return
        clip = {'start_sample': self.start, 'end_sample': self.start+self.frames,
                'speech_sample': self.last_loud, 'captured_at': timestamp(),
                'seconds': self.frames/RATE, 'end_reason': reason}
        if self.callback:
            clip.update(self.callback('end', clip) or {})
        else:
            clip['pcm'] = bytes(self.data)
        self.completed.append(clip)
        self.active = False
        self.data.clear()
        self.waiting_for_end = reason in ('limit', 'manual')

    def feed(self, pcm, muted=False):
        samples = struct.unpack('<'+'h'*(len(pcm)//2), pcm)
        self.level = math.sqrt(sum(x*x for x in samples)/len(samples)) if samples else 0
        start = self.position
        self.position += len(samples)
        self.last_sample_time = time.monotonic()
        if muted:
            self.level = 0
            self.finish('transmit')
            return
        loud = self.level >= self.threshold
        if loud:
            self.last_loud = self.position
        quiet = self.position-self.last_loud >= self.silence_samples
        if self.waiting_for_end:
            if quiet:
                self.waiting_for_end = False
            return
        if not self.active and loud:
            self.active = True
            self.start = start
            self.frames = 0
            if self.callback:
                self.callback('start', {'started_at': timestamp(), 'start_sample': start})
        if self.active:
            block = pcm[:max(0, self.capacity-self.frames)*2]
            self.frames += len(block)//2
            if self.callback:
                self.callback('data', block)
            else:
                self.data.extend(block)
            if self.frames >= self.capacity:
                self.finish('limit')
            elif quiet:
                self.finish('silence')

    def take(self, force=False):
        if force:
            self.finish('manual')
        return self.completed.popleft() if self.completed else None

    def channel_clear(self):
        return (self.last_sample_time is not None
                and time.monotonic()-self.last_sample_time < 0.5
                and not self.active and not self.waiting_for_end
                and self.position-self.last_loud >= self.silence_samples)


class BotState:
    def __init__(self, root):
        self.root = root.resolve()
        self.condition = threading.Condition()
        self.revision = 0
        self.closed = False
        self.status = {'phase': 'starting', 'paused': False, 'level': 0,
                       'buffer_seconds': 0, 'transmit': False}
        self.turns = []
        folders = sorted((p for p in self.root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime)[-100:]
        for folder in folders:
            try:
                if (folder/'turn.json').exists():
                    self.turns.append(json.loads((folder/'turn.json').read_text()))
                elif (folder/'reply.txt').exists():
                    self.turns.append({'id': folder.name,
                        'timestamp': datetime.fromtimestamp(folder.stat().st_mtime, timezone.utc).isoformat(),
                        'transcript': (folder/'transcript.txt').read_text() if (folder/'transcript.txt').exists() else '',
                        'reply': (folder/'reply.txt').read_text(), 'phase': 'archived',
                        'received': (folder/'received.wav').exists(), 'audio': (folder/'reply.wav').exists()})
            except (OSError, ValueError):
                pass
        self.force = False

    def update(self, **values):
        with self.condition:
            self.status.update(values)
            self.revision += 1
            self.condition.notify_all()

    def turn(self, directory, **values):
        with self.condition:
            entry = next((t for t in self.turns if t['id'] == directory.name), None)
            if entry is None:
                entry = {'id': directory.name, 'timestamp': timestamp()}
                self.turns.append(entry)
                self.turns = self.turns[-100:]
            entry.update(values)
            tmp = directory/'turn.json.tmp'
            tmp.write_text(json.dumps(entry, ensure_ascii=False, indent=2)+'\n')
            tmp.replace(directory/'turn.json')
            self.revision += 1
            self.condition.notify_all()

    def snapshot(self):
        with self.condition:
            return {'status': dict(self.status), 'turns': [dict(t) for t in self.turns]}

    def command(self, action):
        with self.condition:
            if action == 'pause':
                self.status['paused'] = True
            elif action == 'resume':
                self.status['paused'] = False
            elif action == 'reply':
                self.force = True
            else:
                raise ValueError('Unknown action')
            self.revision += 1
            self.condition.notify_all()


class ContinuousCapture:
    def __init__(self, source, state, seconds, silence, threshold):
        self.state = state
        self.writer = None
        self.directory = None
        self.window = TransmissionClips(seconds, silence, threshold, self.clip_event)
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.muted_until = 0
        self.error = None
        self.process = subprocess.Popen(['parec', '--raw', '--format=s16le', '--channels=1',
            '--rate=8000', '--latency-msec=100', '--device='+source], stdout=subprocess.PIPE)
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def clip_event(self, kind, payload):
        if kind == 'start':
            self.directory = self.state.root/datetime.now(timezone.utc).strftime('%Y-%m-%dT%H-%M-%S.%fZ')
            self.directory.mkdir()
            self.writer = wave.open(str(self.directory/'received.wav'), 'wb')
            self.writer.setparams((1, 2, RATE, 0, 'NONE', 'not compressed'))
            self.state.turn(self.directory, phase='recording', received=False, capture=payload)
        elif kind == 'data':
            self.writer.writeframes(payload)
        elif kind == 'end':
            self.writer.close()
            self.writer = None
            self.state.turn(self.directory, phase='queued', received=True, capture=payload)
            return {'directory': str(self.directory)}

    def wait_clear(self):
        self.state.update(phase='waiting for channel')
        while not self.stop.is_set():
            if self.error:
                raise RuntimeError(self.error)
            if self.state.snapshot()['status']['paused']:
                return False
            with self.lock:
                if self.window.channel_clear():
                    return True
            self.stop.wait(0.1)
        return False

    def suppress(self, active):
        with self.lock:
            # Keep draining USB during TX; allow a short receive-settling guard.
            self.muted_until = float('inf') if active else time.monotonic()+0.3

    def run(self):
        pending = bytearray()
        last_data = time.monotonic()
        last_publish = 0
        try:
            while not self.stop.is_set():
                if not select.select([self.process.stdout], [], [], 0.2)[0]:
                    if time.monotonic()-last_data > 3:
                        raise RuntimeError('Radio capture stalled for more than 3 seconds')
                    continue
                data = os.read(self.process.stdout.fileno(), BLOCK*2)
                if not data:
                    raise RuntimeError('Radio capture ended unexpectedly')
                last_data = time.monotonic()
                pending.extend(data)
                while len(pending) >= BLOCK*2:
                    block = bytes(pending[:BLOCK*2])
                    del pending[:BLOCK*2]
                    with self.lock:
                        self.window.feed(block, time.monotonic() < self.muted_until)
                        level = self.window.level
                        seconds = self.window.frames/RATE if self.window.active else 0
                        channel_clear = self.window.channel_clear()
                        queued = len(self.window.completed)
                    if time.monotonic()-last_publish > 0.25:
                        self.state.update(level=round(level), buffer_seconds=round(seconds, 1),
                                          channel_clear=channel_clear, queued=queued)
                        last_publish = time.monotonic()
        except Exception as exc:
            self.error = str(exc)
            self.state.update(phase='error', error=self.error)

    def next(self):
        while not self.stop.is_set():
            if self.error:
                raise RuntimeError(self.error)
            with self.state.condition:
                paused = self.state.status['paused']
                force = self.state.force
                self.state.force = False
            if not paused:
                with self.lock:
                    result = self.window.take(force)
                if result:
                    return result
            self.stop.wait(0.1)
        return None

    def close(self):
        self.stop.set()
        self.process.terminate()
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.thread.join(timeout=3)
        self.process.stdout.close()
        with self.lock:
            self.window.finish('stopped')


def parse_channel(message):
    match = re.fullmatch(r'CHANNEL index=(-?\d+) rx=(\d+) tx=(\d+) name=(.*)', message)
    if not match:
        raise ValueError('Invalid channel report')
    index, rx, tx, name = match.groups()
    return {'index': int(index), 'rx': int(rx), 'tx': int(tx), 'name': name}


class ChannelMonitor:
    """One serial owner shared with PTT; asynchronous events use frame channel 2."""
    def __init__(self, radio, state):
        self.radio, self.state = radio, state
        self.stop = threading.Event()
        self.thread = None
        if radio.command('EVENTS 1') != 'OK':
            raise ValueError('Channel reporting needs updated radio firmware')
        state.update(channel=parse_channel(radio.command('CHANNEL')))
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    def run(self):
        try:
            while not self.stop.is_set():
                for message in self.radio.poll_events():
                    self.state.update(channel=parse_channel(message))
                self.stop.wait(0.05)
        except (OSError, ValueError, RuntimeError) as exc:
            self.state.update(channel_error=str(exc))

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.radio.close()


def serve_ui(state, port):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send(self, body, content_type, code=200):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path == '/events':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('X-Accel-Buffering', 'no')
                self.end_headers()
                revision = -1
                try:
                    while not state.closed:
                        with state.condition:
                            state.condition.wait_for(lambda: state.closed or state.revision != revision, 15)
                            revision = state.revision
                            data = json.dumps(state.snapshot(), ensure_ascii=False)
                        self.wfile.write(('event: state\ndata: '+data+'\n\n').encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, TimeoutError):
                    pass
                return
            assets = {'/': ('index.html', 'text/html; charset=utf-8'),
                      '/app.js': ('app.js', 'text/javascript'),
                      '/style.css': ('style.css', 'text/css'),
                      '/icon.svg': ('icon.svg', 'image/svg+xml')}
            if path in assets:
                filename, mime = assets[path]
                self.send((ASSETS/filename).read_bytes(), mime)
                return
            parts = path.strip('/').split('/')
            if len(parts) == 3 and parts[0] == 'recordings' and parts[2] in (
                    'received.wav', 'reply.wav', 'transcript.txt', 'reply.txt', 'turn.json'):
                file = (state.root/parts[1]/parts[2]).resolve()
                if file.is_relative_to(state.root) and file.is_file():
                    self.send(file.read_bytes(), 'audio/wav' if file.suffix == '.wav' else 'text/plain; charset=utf-8')
                    return
            self.send(b'Not found', 'text/plain', 404)

        def do_POST(self):
            # Local controls only, with same-origin JSON requests (no cross-site forms).
            origin = self.headers.get('Origin')
            if origin and origin != 'http://'+self.headers.get('Host', ''):
                self.send(b'Forbidden', 'text/plain', 403)
                return
            if self.path != '/control' or self.headers.get('Content-Type') != 'application/json':
                self.send(b'Not found', 'text/plain', 404)
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 1024:
                    raise ValueError('Invalid body length')
                state.command(json.loads(self.rfile.read(length))['action'])
                self.send(b'{}', 'application/json')
            except (ValueError, KeyError, TypeError):
                self.send(b'Invalid command', 'text/plain', 400)

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
