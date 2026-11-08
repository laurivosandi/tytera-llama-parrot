/*
 * SPDX-FileCopyrightText: Copyright 2026 OpenRTX Contributors
 * SPDX-License-Identifier: GPL-3.0-or-later
 */
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include "interfaces/cps_io.h"
#include "interfaces/platform.h"
#include "interfaces/nvmem.h"
#include "core/state.h"

#ifdef PLATFORM_MD3x0
enum { CH_BASE = 0x1ee00, CT_BASE = 0x5f80, MAX_CH = 1000, MEMBERS = 16 };
#else
enum { CH_BASE = 0x110000, CT_BASE = 0x140000, MAX_CH = 3000, MEMBERS = 64 };
#endif
static uint8_t flash[0x200000];
static bool read_failure;
static unsigned writes;
static hwInfo_t hardware;

const hwInfo_t *platform_getHwInfo(void) { return &hardware; }
static int read_flash(const struct nvmDevice *dev, uint32_t addr, void *out, size_t len)
{
    (void)dev;
    assert(addr <= sizeof(flash) && len <= sizeof(flash) - addr);
    if (read_failure) return -1;
    memcpy(out, flash + addr, len);
    return 0;
}
static const struct nvmOps ops = {.read = read_flash};
const struct nvmDevice eflash = {.ops = &ops};
int nvm_writeSettingsAndVfo(const settings_t *settings, const channel_t *vfo)
{
    (void)settings; (void)vfo;
    ++writes;
    return 0;
}
static void shutdown_state(void)
{
    pthread_mutex_init(&state_mutex, NULL);
    state.settings.brightness = 50;
    state_terminate();
}
static uint32_t checksum(void)
{
    uint32_t sum = 0;
    for (size_t i = 0; i < sizeof(flash); ++i) sum = sum * 33 + flash[i];
    return sum;
}
static void reset(uint8_t fill)
{
    cps_close();
    memset(flash, fill, sizeof(flash));
    memset(&hardware, 0, sizeof(hardware));
    hardware.uhf_band = 1;
    hardware.uhf_minFreq = 400;
    hardware.uhf_maxFreq = 480;
    read_failure = false;
    writes = 0;
}
static void put16(uint8_t *out, uint16_t value)
{
    out[0] = value;
    out[1] = value >> 8;
}
static void bcd(uint8_t *out, uint32_t hz)
{
    uint32_t value = hz / 10;
    for (size_t i = 0; i < 4; ++i) {
        unsigned lo = value % 10; value /= 10;
        unsigned hi = value % 10; value /= 10;
        out[i] = lo | (hi << 4);
    }
}
static void name(uint8_t *out, const char *text)
{
    memset(out, 0, 32);
    for (size_t i = 0; (i < 16) && text[i]; ++i) put16(out + 2*i, text[i]);
}
static uint8_t *channel(unsigned slot)
{
    uint8_t *raw = flash + CH_BASE + slot * 64;
    memset(raw, 0, 64);
    raw[0] = 1; // Analog, narrow bandwidth.
    raw[30] = 1; // UV low power.
    bcd(raw + 16, 433500000);
    bcd(raw + 20, 433500000);
    put16(raw + 24, 0xffff);
    put16(raw + 26, 0xffff);
    name(raw + 32, "TEST");
    return raw;
}
static void assert_fallback(void)
{
    uint32_t before = checksum();
    assert(cps_open(NULL) == 0);
    assert(cps_fallbackActive());
    for (unsigned i = 0; i < 16; ++i) {
        channel_t ch;
        assert(cps_readChannel(&ch, i) == 0);
        assert(ch.mode == OPMODE_FM && ch.bandwidth == BW_12_5);
        assert(ch.rx_frequency == 446006250u + 12500u * i);
        assert(ch.tx_frequency == ch.rx_frequency && !ch.rx_only && ch.power == 1000);
        assert(!ch.fm.rxToneEn && !ch.fm.txToneEn);
        assert(strncmp(ch.name, "PMR ", 4) == 0);
        assert(cps_readBankData(0, i) == (int)i);
    }
    channel_t ch;
    bankHdr_t bank;
    contact_t ct;
    assert(cps_readChannel(&ch, 16) == -1);
    assert(cps_readChannel(&ch, 65535) == -1);
    assert(cps_readBankHeader(&bank, 0) == 0 && bank.ch_count == 16);
    assert(strcmp(bank.name, "PMR TX/RX") == 0);
    assert(cps_readBankHeader(&bank, 1) == -1);
    assert(cps_readBankData(0, 16) == -1);
    assert(cps_readBankData(1, 0) == -1);
    assert(cps_readContact(&ct, 0) == -1);
    assert(cps_create(NULL) == -1);
    shutdown_state();
    assert(writes == 0 && checksum() == before);
    cps_close();
}
int main(void)
{
    reset(0xff); assert_fallback();
    reset(0); assert_fallback();
    reset(0xff); read_failure = true; assert_fallback();
    for (unsigned seed = 1; seed <= 8; ++seed) {
        reset(0);
        uint32_t random = seed;
        for (size_t i = 0; i < sizeof(flash); ++i) {
            random = random * 1664525u + 1013904223u;
            flash[i] = random >> 24;
        }
        assert_fallback();
    }
    // Each malformed record alone must trigger recovery, never a partial parse.
    for (unsigned kind = 0; kind < 12; ++kind) {
        reset(0xff);
        uint8_t *raw = channel(5);
        switch (kind) {
            case 0: memset(raw + 32, 0xff, 32); break;
            case 1: memset(raw + 32, 0, 32); break;
            case 2: put16(raw + 32, 1); break;
            case 3: raw[0] = 3; break;
            case 4: raw[0] |= 12; break;
            case 5: raw[16] = 0xfa; break;
            case 6: bcd(raw + 16, 999000000); break;
            case 7: raw[20] = 0xff; break;
            case 8: raw[11] = 255; break;
            case 9: raw[12] = 255; break;
            case 10: raw[0] = 2; raw[1] = 12; break; // Invalid DMR slot.
            case 11: put16(raw + 32, 0xd800); break; // Broken surrogate pair.
        }
        assert_fallback();
    }
    reset(0xff);
    channel(8);
    uint8_t *raw = channel(999);
    name(raw + 32, "1234567890123456");
    uint8_t *bad = channel(10); bad[0] = 0;
    uint8_t *zone = flash + 0x149e0 + 3 * 64;
    memset(zone, 0, 64);
    name(zone, "ZONE");
    put16(zone + 32, 9);    // First valid slot, vendor one-based.
    put16(zone + 34, 11);   // Invalid channel: omit from bank.
    put16(zone + 36, 65535);
    put16(zone + 38, 1000); // Last valid slot.
#ifndef PLATFORM_MD3x0
    memset(flash + 0x31000 + 3 * 224, 0, 224);
    put16(flash + 0x31000 + 3 * 224 + 47 * 2, 9); // Member 64.
#endif
    uint32_t before = checksum();
    assert(cps_open(NULL) == 0 && !cps_fallbackActive());
    channel_t ch;
    bankHdr_t bank;
    assert(cps_readChannel(&ch, 0) == 0 && !ch.rx_only);
    assert(cps_readChannel(&ch, 1) == 0 && ch.name[16] == 0);
    assert(strcmp(ch.name, "1234567890123456") == 0);
    assert(cps_readChannel(&ch, 2) == -1);
    assert(cps_readBankHeader(&bank, 0) == 0);
    assert(bank.ch_count == (MEMBERS == 16 ? 2 : 3));
    assert(cps_readBankData(0, 0) == 0);
    assert(cps_readBankData(0, 1) == 1);
    if (MEMBERS == 64) assert(cps_readBankData(0, 2) == 0);
    assert(cps_readBankData(0, MEMBERS) == -1);
    assert(cps_readBankData(0, 65535) == -1);
    assert(cps_readBankHeader(&bank, 1) == -1);
    shutdown_state();
    assert(writes == 1 && checksum() == before);
    // I/O failure after the initial scan must still fail safely.
    read_failure = true;
    assert(cps_readChannel(&ch, 0) == -1);
    assert(cps_readBankHeader(&bank, 0) == -1);
    assert(cps_readBankData(0, 0) == -1);

    // Exhaustive bank bounds include every malformed 16-bit position.
    read_failure = false;
    for (unsigned i = MEMBERS; i <= 65535; ++i)
        assert(cps_readBankData(0, i) == -1);

    // RX-only records tolerate unset TX frequency; CTCSS is still decoded.
    reset(0xff); raw = channel(0); raw[1] = 2;
    memset(raw + 20, 0xff, 4); put16(raw + 24, 0x0670);
    assert(cps_open(NULL) == 0 && !cps_fallbackActive());
    assert(cps_readChannel(&ch, 0) == 0 && ch.rx_only);
    assert(ch.tx_frequency == ch.rx_frequency && ch.fm.rxToneEn && ch.fm.rxTone == 0);

    // RX-only is not permission to accept arbitrary garbage TX bytes.
    reset(0xff); raw = channel(0); raw[1] = 2; raw[20] = 0xfe;
    assert_fallback();

    // A valid DMR contact is one-based in flash and zero-based at the API.
    reset(0xff); raw = channel(0); raw[0] = 2; raw[1] = 4 | (7 << 4);
    put16(raw + 6, 1);
    uint8_t *contact = flash + CT_BASE;
    memset(contact, 0, 36); contact[0] = 123; contact[3] = 1;
    name(contact + 4, "GROUP");
    assert(cps_open(NULL) == 0 && !cps_fallbackActive());
    assert(cps_readChannel(&ch, 0) == 0 && ch.dmr.contact_index == 0);
    assert(ch.dmr.dmr_timeslot == 1 && ch.dmr.rxColorCode == 7);
    contact_t ct;
    assert(cps_readContact(&ct, 0) == 0 && ct.info.dmr.contactType == GROUP);
    assert(cps_readContact(&ct, 65535) == -1);
    cps_close(); contact[3] = 0; assert_fallback();

    // A VHF-only unit must never be tuned to PMR446.
    reset(0xff);
    hardware.uhf_band = 0; hardware.vhf_band = 1;
    hardware.vhf_minFreq = 136; hardware.vhf_maxFreq = 174;
    assert(cps_open(NULL) == 0 && cps_fallbackActive());
    assert(cps_readChannel(&ch, 0) == 0 && !ch.rx_only);
    assert(ch.rx_frequency == 145500000);
    assert(ch.tx_frequency == 145500000 && ch.power == 1000);
    assert(ch.mode == OPMODE_FM && ch.bandwidth == BW_12_5);
    assert(strcmp(ch.name, "FM CALL") == 0);
    assert(cps_readChannel(&ch, 1) == -1);
    cps_close();
    puts("TYT codeplug validation and fallback passed");
}
