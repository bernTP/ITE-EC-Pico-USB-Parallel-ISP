/**
 * @file epp.c
 *
 * IEEE 1284 EPP host cycles over the KBS lines.
 *
 * Signals (host side):
 *   STB#  = nWrite       low for a write cycle
 *   AFD#  = nDataStrobe  data cycle strobe
 *   SLIN# = nAddrStrobe  address cycle strobe
 *   BUSY  = nWait        peripheral handshake: low = ready, high = ack
 *
 * Write cycle: nWrite low, drive PD, wait ready, strobe low, wait ack,
 *              strobe high, wait ready, release PD, nWrite high.
 * Read cycle:  wait ready, strobe low, wait ack, sample PD, strobe high,
 *              wait ready.
 * Without handshake the waits on nWait are replaced by fixed delays.
 */

#include "pico/stdlib.h"
#include "hardware/gpio.h"
#include "hardware/clocks.h"

#include "hwconfig.h"
#include "proto.h"
#include "epp.h"

#define PD_MASK (0xFFu << PD_BASE_PIN)

static struct {
    uint32_t setup_cyc;
    uint32_t strobe_cyc;
    uint32_t hold_cyc;
    uint32_t timeout_us;
    uint8_t flags;
} cfg;

static uint32_t ns_to_cycles(uint16_t ns) {
    uint32_t mhz = clock_get_hz(clk_sys) / 1000000;
    return (ns * mhz) / 1000 + 1;
}

void epp_config(uint16_t setup_ns, uint16_t strobe_ns, uint16_t hold_ns,
                uint16_t timeout_us, uint8_t flags) {
    cfg.setup_cyc = ns_to_cycles(setup_ns);
    cfg.strobe_cyc = ns_to_cycles(strobe_ns);
    cfg.hold_cyc = ns_to_cycles(hold_ns);
    cfg.timeout_us = timeout_us;
    cfg.flags = flags;
}

// Level of nWait that means "peripheral ready"
static inline bool ready_level(void) {
    return (cfg.flags & CFG_NWAIT_INV) ? true : false;
}

static bool wait_busy(bool level) {
    uint32_t t0 = time_us_32();
    while (gpio_get(BUSY_PIN) != level) {
        if ((time_us_32() - t0) > cfg.timeout_us) {
            return false;
        }
    }
    return true;
}

static inline uint strobe_pin(bool addr) {
    bool swap = (cfg.flags & CFG_SWAP_STROBE) != 0;
    return (addr != swap) ? SLIN_PIN : AFD_PIN;
}

// Second half of a cycle: wait ack (or delay), strobe high, wait ready
static int finish_strobe(uint strobe, bool sample, uint8_t *val) {
    bool hs = (cfg.flags & CFG_HANDSHAKE) != 0;
    int st = ST_OK;

    if (hs) {
        if (!wait_busy(!ready_level())) {
            st = ST_NWAIT_TIMEOUT;
        }
    } else {
        busy_wait_at_least_cycles(cfg.strobe_cyc);
    }
    if (sample) {
        // let PD settle through the input synchronizers
        busy_wait_at_least_cycles(cfg.hold_cyc);
        *val = (uint8_t)((gpio_get_all() & PD_MASK) >> PD_BASE_PIN);
    }
    gpio_put(strobe, 1);
    if (st == ST_OK && hs && !(cfg.flags & CFG_NO_RELEASE)) {
        if (!wait_busy(ready_level())) {
            st = ST_NWAIT_TIMEOUT;
        }
    }
    return st;
}

int epp_write(bool addr, uint8_t val) {
    uint strobe = strobe_pin(addr);

    if ((cfg.flags & CFG_HANDSHAKE) && !wait_busy(ready_level())) {
        return ST_NWAIT_TIMEOUT;
    }
    gpio_put(STB_PIN, 0);
    gpio_put_masked(PD_MASK, (uint32_t)val << PD_BASE_PIN);
    gpio_set_dir_out_masked(PD_MASK);
    busy_wait_at_least_cycles(cfg.setup_cyc);

    gpio_put(strobe, 0);
    int st = finish_strobe(strobe, false, NULL);

    busy_wait_at_least_cycles(cfg.hold_cyc);
    gpio_set_dir_in_masked(PD_MASK);
    gpio_put(STB_PIN, 1);
    return st;
}

int epp_read(bool addr, uint8_t *val) {
    uint strobe = strobe_pin(addr);

    *val = 0;
    if ((cfg.flags & CFG_HANDSHAKE) && !wait_busy(ready_level())) {
        return ST_NWAIT_TIMEOUT;
    }
    gpio_set_dir_in_masked(PD_MASK);
    busy_wait_at_least_cycles(cfg.setup_cyc);

    gpio_put(strobe, 0);
    return finish_strobe(strobe, true, val);
}

void epp_init_pulse(uint16_t us) {
    gpio_put(INIT_PIN, 0);
    sleep_us(us);
    gpio_put(INIT_PIN, 1);
}
