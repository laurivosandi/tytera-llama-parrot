#!/usr/bin/env python3
"""TYT framed CDC bring-up client (Python standard library only)."""
# SPDX-License-Identifier: GPL-3.0-or-later
import argparse
import binascii
import fcntl
import os
import select
import struct
import termios
import time
import tty
import threading
import collections

MAX_MESSAGE = 240


def encode(message):
    if not 1 <= len(message) <= MAX_MESSAGE:
        raise ValueError('message must be 1..240 bytes')
    data = message + struct.pack('<H', binascii.crc_hqx(message, 0))
    wire = bytearray([0])
    start, code = 0, 1
    for value in data:
        if value:
            wire.append(value)
            code += 1
        else:
            wire[start] = code
            start = len(wire)
            wire.append(0)
            code = 1
    wire[start] = code
    return bytes(wire) + b'\0'


def decode(wire):
    if not 4 <= len(wire) < 244:
        raise ValueError('invalid frame size')
    out = bytearray()
    pos = 0
    while pos < len(wire):
        code = wire[pos]
        pos += 1
        end = pos + code - 1
        if code == 0 or end > len(wire) or 0 in wire[pos:end]:
            raise ValueError('invalid COBS')
        out.extend(wire[pos:end])
        pos = end
        if code != 255 and pos < len(wire):
            out.append(0)
    if not 3 <= len(out) <= MAX_MESSAGE + 2:
        raise ValueError('invalid message size')
    if binascii.crc_hqx(out[:-2], 0) != int.from_bytes(out[-2:], 'little'):
        raise ValueError('CRC mismatch')
    return bytes(out[:-2])


class Radio:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        self.pending = bytearray()
        self.lock = threading.RLock()
        self.events = collections.deque(maxlen=64)
        try:
            tty.setraw(self.fd)
            attrs = termios.tcgetattr(self.fd)
            attrs[2] |= termios.CLOCAL | termios.CREAD
            attrs[2] &= ~termios.CRTSCTS
            attrs[4] = attrs[5] = termios.B115200
            termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
            fcntl.ioctl(self.fd, termios.TIOCMBIS, struct.pack('I', termios.TIOCM_DTR))
            termios.tcflush(self.fd, termios.TCIOFLUSH)
        except BaseException:
            os.close(self.fd)
            raise

    def command(self, command, timeout=0.5):
        with self.lock:
            return self._command(command, timeout)

    def _command(self, command, timeout=0.5):
        wire = encode(b'\x01' + command.encode('ascii'))
        deadline = time.monotonic() + timeout
        while wire:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [self.fd], [], remaining)[1]:
                raise TimeoutError('serial write timed out')
            try:
                written = os.write(self.fd, wire)
            except BlockingIOError:
                continue
            wire = wire[written:]
        while time.monotonic() < deadline:
            if not select.select([self.fd], [], [], max(0, deadline-time.monotonic()))[0]:
                break
            data = os.read(self.fd, 512)
            if not data:
                raise ConnectionError('radio disconnected')
            self.pending.extend(data)
            while 0 in self.pending:
                end = self.pending.index(0)
                frame = self.pending[:end]
                del self.pending[:end+1]
                try:
                    message = decode(frame)
                except ValueError:
                    continue
                if message[0] == 2:
                    self.events.append(message[1:].decode('utf-8', errors='replace'))
                if message[0] == 1:
                    return message[1:].decode('utf-8', errors='replace')
            if len(self.pending) > 4096:
                self.pending.clear()
        raise TimeoutError('radio did not reply')

    def poll_events(self, timeout=0.1):
        with self.lock:
            if select.select([self.fd], [], [], timeout)[0]:
                data = os.read(self.fd, 512)
                if not data:
                    raise ConnectionError('radio disconnected')
                self.pending.extend(data)
            while 0 in self.pending:
                end = self.pending.index(0)
                frame = self.pending[:end]
                del self.pending[:end+1]
                try:
                    message = decode(frame)
                except ValueError:
                    continue
                if message[0] == 2:
                    self.events.append(message[1:].decode('utf-8', errors='replace'))
            if len(self.pending) > 4096:
                self.pending.clear()
            events = list(self.events)
            self.events.clear()
            return events

    def close(self):
        os.close(self.fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('port', help='/dev/ttyACM0 or /dev/serial/by-id/...')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('status')
    commands.add_parser('channel', help='Current physical radio channel')
    commands.add_parser('watch', help='Watch physical channel changes; Ctrl-C stops')
    commands.add_parser('rxstat', help='FM receive stream counters')
    commands.add_parser('rxdrop', help='FM receive drop reasons')
    commands.add_parser('stop')
    ptt = commands.add_parser('ptt', help='hold USB PTT while playback is running')
    ptt.add_argument('--seconds', type=float, default=5)
    args = parser.parse_args()
    if args.command == 'ptt' and not 0 < args.seconds <= 60:
        parser.error('--seconds must be greater than 0 and at most 60')
    radio = Radio(args.port)
    try:
        if args.command == 'status':
            print(radio.command('STATUS'))
        elif args.command == 'channel':
            print(radio.command('CHANNEL'))
        elif args.command == 'watch':
            if radio.command('EVENTS 1') != 'OK':
                raise RuntimeError('Firmware does not support channel events')
            while True:
                for event in radio.poll_events():
                    print(event, flush=True)
        elif args.command in ('rxstat', 'rxdrop'):
            print(radio.command(args.command.upper()))
        elif args.command == 'stop':
            print(radio.command('PTT 0'))
        else:
            print(radio.command('PTT 0'))
            end = time.monotonic() + args.seconds
            while time.monotonic() < end:
                reply = radio.command('PTT 1')
                if not reply.startswith('OK'):
                    raise RuntimeError(reply)
                time.sleep(min(0.25, max(0, end - time.monotonic())))
    finally:
        if args.command == 'ptt':
            try:
                radio.command('PTT 0')
            except (OSError, TimeoutError, ConnectionError):
                pass  # Firmware also expires the lease when the client disappears.
        radio.close()


if __name__ == '__main__':
    main()
