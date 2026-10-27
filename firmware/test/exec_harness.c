/*
 * Host build of the firmware batch executor (main.c) with fake USB, lines
 * and EPP, so the framing and error padding can be tested without a Pico.
 *
 *   stdin  -> bytes the host would send on bulk OUT
 *   stdout -> bytes the firmware sends on bulk IN
 *
 * Fake EPP: data/address reads return a running counter. If FAKE_TIMEOUT_AT=n
 * is set, the n-th EPP cycle (1-based) fails with ST_NWAIT_TIMEOUT.
 * Usage (from host/tests/test_firmware.py):
 *   cc -Ifirmware/test/stubs -Ifirmware firmware/test/exec_harness.c
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define main fw_main
#include "../main.c"
#undef main

// ─── Fake USB ───────────────────────────────────────────────────────
static uint8_t in_data[1 << 20];
static size_t in_len, in_pos;

void tud_task(void) {}
bool tud_vendor_mounted(void) { return true; }
uint32_t tud_vendor_available(void) { return (uint32_t)(in_len - in_pos); }

uint32_t tud_vendor_read(void *buf, uint32_t size) {
    uint32_t n = tud_vendor_available();
    if (n > size) {
        n = size;
    }
    memcpy(buf, in_data + in_pos, n);
    in_pos += n;
    return n;
}

uint32_t tud_vendor_write(const void *buf, uint32_t size) {
    // accept at most 17 bytes per call to exercise the retry loop
    uint32_t n = size > 17 ? 17 : size;
    fwrite(buf, 1, n, stdout);
    return n;
}

uint32_t tud_vendor_write_flush(void) { return 0; }

bool gpio_get(uint gpio) { (void)gpio; return true; }
absolute_time_t get_absolute_time(void) { return 0; }

// ─── Fake lines / EPP ───────────────────────────────────────────────
static bool fake_armed;
static long cycles, timeout_at;
static uint8_t counter;

uint lines_gpio(uint8_t line) { return line; }
void lines_init(void) {}
void lines_safe(uint8_t pull) { (void)pull; fake_armed = false; }
void lines_pulls(uint16_t up, uint16_t down) { (void)up; (void)down; }
void lines_set(uint16_t oe, uint16_t val) { (void)oe; (void)val; }
uint16_t lines_get(void) { return 0x1234; }
void lines_strap(uint8_t mode) { fake_armed = (mode == STRAP_DRIVE); }
bool lines_armed(void) { return fake_armed; }

void epp_config(uint16_t s, uint16_t st, uint16_t h, uint16_t t, uint8_t f) {
    (void)s; (void)st; (void)h; (void)t; (void)f;
}

static int cycle(void) {
    return (++cycles == timeout_at) ? ST_NWAIT_TIMEOUT : ST_OK;
}

int epp_write(bool addr, uint8_t val) { (void)addr; (void)val; return cycle(); }

int epp_read(bool addr, uint8_t *val) {
    (void)addr;
    int st = cycle();
    *val = (st == ST_OK) ? counter++ : 0;
    return st;
}

void epp_init_pulse(uint16_t us) { (void)us; }

int main(void) {
    const char *t = getenv("FAKE_TIMEOUT_AT");
    timeout_at = t ? atol(t) : -1;
    in_len = fread(in_data, 1, sizeof(in_data), stdin);
    usb_rx();
    fflush(stdout);
    return 0;
}
