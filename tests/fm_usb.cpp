/* SPDX-License-Identifier: GPL-3.0-or-later */
#include <cassert>
#include "interfaces/platform.h"
#include "interfaces/radio.h"
#include "interfaces/usb_audio.h"
#include "core/usb_audio.h"
#include "rtx/OpMode_FM.hpp"

static bool key, remote, playback, rfTx, lineInput, failStart, failUpdate;
static unsigned txCount;
static bool fmCapture;
static long long clockMs;
static AudioSource lastSource;

bool platform_getPttStatus() { return key; }
void platform_ledOn(led_t) {}
void platform_ledOff(led_t) {}
void sleepFor(unsigned int, unsigned int) {}
long long getTick() { return clockMs; }
rssi_t rtx_getRssi() { return -120; }
bool radio_checkRxDigitalSquelch() { return false; }
void radio_enableRx() { rfTx = false; }
void radio_disableRtx() { rfTx = false; }
void radio_enableTx() { rfTx = true; ++txCount; }
void radio_setTxAudioSource(bool mcu) { lineInput = mcu; }
pathId audioPath_request(AudioSource source, AudioSink, AudioPriority)
{
    lastSource = source;
    return 1;
}
void audioPath_release(pathId) {}
void usbAudio_setRxRssi(int32_t) {}
void usbAudio_setFmRx(bool active) { fmCapture = active; }
bool usbAudio_ptt() { return remote; }
bool usbAudio_playing() { return playback; }
void usbAudio_releasePtt() { remote = false; }
void usbAudioBridge_stop() {}
int usbAudioBridge_startTx(pathId) { return failStart ? -1 : 0; }
bool usbAudioBridge_update(bool, bool tx) { return tx && !failUpdate; }

static rtxStatus_t status;
static void reset()
{
    key = remote = playback = rfTx = lineInput = failStart = failUpdate = false;
    clockMs = 10;
    txCount = 0;
    fmCapture = false;
    status = {};
    status.opStatus = RX;
}

int main()
{
    reset();
    {
        OpMode_FM mode;
        mode.enable();
        remote = playback = true;
        status.txDisable = 1;
        mode.update(&status, false);
        assert(!rfTx && txCount == 0);
        assert(fmCapture);
        status.txDisable = 0;
        mode.update(&status, false);
        assert(rfTx && lineInput && lastSource == SOURCE_MCU);
        assert(!fmCapture);
        remote = false; // Host lease expired.
        mode.update(&status, false);
        assert(!rfTx && !lineInput);
        mode.disable();
        assert(!fmCapture);
    }
    reset();
    {
        OpMode_FM mode;
        mode.enable();
        remote = true; // Playback disappeared between PTT sampling and routing.
        mode.update(&status, false);
        assert(!rfTx && txCount == 0); // Must never fall back to the microphone.
        playback = true;
        failStart = true;
        mode.update(&status, false);
        assert(!rfTx && txCount == 0);
        failStart = false;
        mode.update(&status, false);
        assert(rfTx);
        failUpdate = true;
        mode.update(&status, false);
        assert(!rfTx && !remote);
        mode.disable();
        assert(!fmCapture);
    }
    reset();
    {
        OpMode_FM mode;
        mode.enable();
        key = true;
        mode.update(&status, false);
        assert(rfTx && !lineInput && lastSource == SOURCE_MIC);
        clockMs += 60000;
        mode.update(&status, false);
        assert(!rfTx);
        mode.update(&status, false);
        assert(!rfTx); // A held physical PTT cannot defeat the timeout.
        key = false;
        mode.update(&status, false);
        key = true;
        mode.update(&status, false);
        assert(rfTx);
        status.txDisable = 1;
        mode.update(&status, false);
        assert(!rfTx);
        mode.disable();
        assert(!fmCapture);
    }
    reset();
    {
        OpMode_FM mode;
        mode.enable();
        key = playback = true;
        mode.update(&status, false);
        assert(rfTx && lineInput);
        playback = false;
        mode.update(&status, false);
        assert(!rfTx); // Disconnect drops RF even with physical PTT held.
        mode.update(&status, false);
        assert(!rfTx); // Releasing PTT is required before microphone fallback.
        mode.disable();
        assert(!fmCapture);
    }
}
