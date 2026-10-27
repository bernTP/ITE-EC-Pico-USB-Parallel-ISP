/**
 * @file lines.c
 *
 * Raw control of the 15 KBS lines.
 *
 * Every line boots as an input with no pull, so an unpowered EC is never
 * driven high. Lines are only driven when the host asks for it.
 */

#include "pico/stdlib.h"
#include "hardware/gpio.h"

#include "hwconfig.h"
#include "proto.h"
#include "lines.h"

static const uint8_t line_gpio[LINE_COUNT] = {
    PD_BASE_PIN + 0, PD_BASE_PIN + 1, PD_BASE_PIN + 2, PD_BASE_PIN + 3,
    PD_BASE_PIN + 4, PD_BASE_PIN + 5, PD_BASE_PIN + 6, PD_BASE_PIN + 7,
    STB_PIN, AFD_PIN, INIT_PIN, SLIN_PIN, KSI4_PIN, KSI5_PIN, BUSY_PIN
};

// True when the control lines are driven and EPP cycles are allowed
static bool armed = false;

uint lines_gpio(uint8_t line) {
    return line_gpio[line];
}

void lines_init(void) {
    for (int i = 0; i < LINE_COUNT; i++) {
        gpio_init(line_gpio[i]);
    }
    lines_safe(PULL_NONE);
}

void lines_safe(uint8_t pull) {
    for (int i = 0; i < LINE_COUNT; i++) {
        uint gp = line_gpio[i];
        gpio_set_dir(gp, GPIO_IN);
        gpio_set_pulls(gp, pull == PULL_UP, pull == PULL_DOWN);
    }
    armed = false;
}

void lines_pulls(uint16_t up, uint16_t down) {
    for (int i = 0; i < LINE_COUNT; i++) {
        gpio_set_pulls(line_gpio[i], (up >> i) & 1, (down >> i) & 1);
    }
}

void lines_set(uint16_t oe, uint16_t val) {
    for (int i = 0; i < LINE_COUNT; i++) {
        uint gp = line_gpio[i];
        if ((oe >> i) & 1) {
            gpio_put(gp, (val >> i) & 1);
            gpio_set_dir(gp, GPIO_OUT);
        } else {
            gpio_set_dir(gp, GPIO_IN);
        }
    }
}

uint16_t lines_get(void) {
    uint32_t all = gpio_get_all();
    uint16_t v = 0;
    for (int i = 0; i < LINE_COUNT; i++) {
        if ((all >> line_gpio[i]) & 1) {
            v |= 1u << i;
        }
    }
    return v;
}

// Apply KSI[5,4,2,1] = 0011b (DBGR/EPP entry).
// KSI4/KSI5 are driven low (safe even if the EC is unpowered).
// STB#/AFD#/INIT#/SLIN# are the idle-high EPP control lines; in weak
// mode they only get the internal pull-up, so nothing drives an
// unpowered EC high while waiting for VSTBY.
void lines_strap(uint8_t mode) {
    static const uint8_t ctrl[] = { LINE_STB, LINE_AFD, LINE_INIT, LINE_SLIN };

    gpio_put(KSI4_PIN, 0);
    gpio_put(KSI5_PIN, 0);
    gpio_set_dir(KSI4_PIN, GPIO_OUT);
    gpio_set_dir(KSI5_PIN, GPIO_OUT);

    for (unsigned i = 0; i < sizeof(ctrl); i++) {
        uint gp = line_gpio[ctrl[i]];
        gpio_pull_up(gp);
        if (mode == STRAP_DRIVE) {
            gpio_put(gp, 1);
            gpio_set_dir(gp, GPIO_OUT);
        } else {
            gpio_set_dir(gp, GPIO_IN);
        }
    }

    // Data bus and nWait are inputs until an EPP cycle needs them
    for (int i = 0; i < 8; i++) {
        gpio_set_dir(PD_BASE_PIN + i, GPIO_IN);
        gpio_pull_up(PD_BASE_PIN + i);
    }
    // Pull-up: an unconnected nWait reads "not ready" and times out cleanly
    gpio_set_dir(BUSY_PIN, GPIO_IN);
    gpio_pull_up(BUSY_PIN);

    armed = (mode == STRAP_DRIVE);
}

bool lines_armed(void) {
    return armed;
}
