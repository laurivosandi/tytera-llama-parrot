#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read live C6000 FM RAM snapshots over CDC; does not transmit or alter registers."""
import argparse
import importlib.util
import json
from pathlib import Path
import struct
import sys
import time

spec = importlib.util.spec_from_file_location(
    'tyt_usb', Path(__file__).with_name('tyt-usb.py'))
usb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usb)


def parse_reply(reply, chunk):
    fields = reply.split()
    if len(fields) != 8 or fields[0] != 'RXPROBE':
        raise ValueError(f'Unexpected probe reply: {reply}')
    received, start, end = map(int, fields[1:4])
    registers = [int(value, 16) for value in fields[4:7]]
    data = bytes.fromhex(fields[7])
    if received != chunk or len(data) != 64:
        raise ValueError('Probe chunk mismatch or incomplete RAM data')
    if not all(0 <= tick <= 0xffffffff for tick in (start, end)):
        raise ValueError('Invalid device timestamp')
    if not all(0 <= register <= 255 for register in registers):
        raise ValueError('Invalid register value')
    if not registers[0] & 0x80:
        raise ValueError('Radio left FM mode during the probe')
    return dict(chunk=chunk, address=0x30 + 64 * chunk,
                tick_start=start, tick_end=end,
                mode=registers[0], irq_mask=registers[1], irq_status=registers[2],
                data=data.hex())


def summarize(data):
    """Compare endian interpretations without assuming a sample format."""
    result = {}
    for label, endian in [('big_endian', '>'), ('little_endian', '<')]:
        samples = struct.unpack(endian + 'h' * (len(data) // 2), data)
        result[label] = dict(min=min(samples), max=max(samples),
                             mean=round(sum(samples) / len(samples), 2))
    result['distinct_bytes'] = len(set(data))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', default='/dev/ttyACM0')
    parser.add_argument('--snapshots', type=int, default=8)
    parser.add_argument('--registers-only', action='store_true',
                        help='compare fast/slow SPI register reads, without RAM capture')
    parser.add_argument('--interval', type=float, default=0.1,
                        help='seconds between full snapshots (default 0.1)')
    parser.add_argument('--output', type=Path,
                        default=Path('artifacts/fm-rx-probe.jsonl'))
    args = parser.parse_args()
    if not 1 <= args.snapshots <= 1000 or not 0 <= args.interval <= 10:
        parser.error('use 1..1000 snapshots and interval 0..10 seconds')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    radio = usb.Radio(args.port)
    try:
        print(radio.command('STATUS'))
        print(radio.command('RXREGS', timeout=2))
        if args.registers_only:
            return
        # Check the device before creating an otherwise empty output file.
        first = parse_reply(radio.command('RXPROBE 00', timeout=2), 0)
        # Exclusive create keeps earlier evidence intact.
        with args.output.open('x') as output:
            previous = None
            for number in range(args.snapshots):
                chunks = []
                for chunk in range(16):
                    record = (first if number == 0 and chunk == 0 else
                              parse_reply(radio.command(f'RXPROBE {chunk:02d}',
                                                        timeout=2), chunk))
                    record.update(snapshot=number, host_time_ns=time.time_ns())
                    output.write(json.dumps(record) + '\n')
                    output.flush()
                    chunks.append(bytes.fromhex(record['data']))
                data = b''.join(chunks)
                changed = None if previous is None else sum(
                    a != b for a, b in zip(data, previous))
                print(json.dumps(dict(snapshot=number, changed_bytes=changed,
                                      half_1=summarize(data[:512]),
                                      half_2=summarize(data[512:]))))
                previous = data
                if number + 1 < args.snapshots:
                    time.sleep(args.interval)
    finally:
        radio.close()
    print(f'Saved {args.output}. Live snapshots can tear; this is not a PCM recording.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, TimeoutError) as error:
        print(f'Probe failed: {error}', file=sys.stderr)
        sys.exit(1)
