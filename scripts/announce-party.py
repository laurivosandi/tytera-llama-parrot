#!/usr/bin/env python3
"""Speak the party wake-up announcement using the shared TYT TTS client."""
# SPDX-License-Identifier: GPL-3.0-or-later
from pathlib import Path
import os
import sys

PHRASE = 'Pane kark alla ja ajame peo käima! Äratus!'


if __name__ == '__main__':
    command = [sys.executable, str(Path(__file__).with_name('announce-time.py')),
               '--speed', '1.12',
               *sys.argv[1:], '--text', PHRASE,
               '--output', 'artifacts/party-announcement.wav']
    # Replace this process so signals reach the shared client's PTT cleanup.
    os.execv(sys.executable, command)
