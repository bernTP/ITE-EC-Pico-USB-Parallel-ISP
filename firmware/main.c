/**
 * @file main.c
 *
 * ITE EC parallel ISP (DBGR/EPP) adapter for RP2040 / RP2350.
 *
 * The host sends batches of small ops (see proto.h). Each batch is
 * executed in order and its output is streamed back over the bulk IN
 * endpoint, so a long run of EPP reads costs one USB round trip.
 */

#include <string.h>

#include "pico/stdlib.h"
#include "tusb.h"

#include "hwconfig.h"
#include "proto.h"
#include "lines.h"
#include "epp.h"

// ─── USB output buffering ───────────────────────────────────────────

static uint8_t tx_buf[64];
static uint32_t tx_n;

static void tx_flush(void) {
    uint32_t off = 0;
    while (off < tx_n) {
        if (!tud_vendor_mounted()) {
            break;  // host is gone, drop the data
        }
        off += tud_vendor_write(tx_buf + off, tx_n - off);
        if (off < tx_n) {
            tud_task();
        }
    }
    tx_n = 0;
}

static void tx_put(uint8_t b) {
    tx_buf[tx_n++] = b;
    if (tx_n == sizeof(tx_buf)) {
        tx_flush();
    }
}

static void tx_trailer(uint8_t status, uint16_t idx) {
    tx_put(status);
    tx_put(idx & 0xFF);
    tx_put(idx >> 8);
    tx_put(RSP_MAGIC);
    tx_flush();
    tud_vendor_write_flush();
}

// Wait while keeping USB alive
static void idle_us(uint32_t us) {
    while (us > 1000) {
        sleep_us(1000);
        tud_task();
        us -= 1000;
    }
    sleep_us(us);
}

static void led(bool on) {
#ifdef LED_PIN
    gpio_put(LED_PIN, on ? LED_ON : LED_OFF);
#else
    (void)on;
#endif
}

// ─── Batch executor ─────────────────────────────────────────────────

static inline uint16_t rd16(const uint8_t *p) {
    return (uint16_t)(p[0] | (p[1] << 8));
}

static inline uint32_t rd32(const uint8_t *p) {
    return p[0] | (p[1] << 8) | (p[2] << 16) | ((uint32_t)p[3] << 24);
}

static void exec_batch(const uint8_t *p, uint32_t len) {
    uint32_t i = 0;
    uint16_t idx = 0;
    uint8_t status = ST_OK;
    uint16_t fail_idx = 0xFFFF;

    led(!lines_armed());

// Malformed batch: output length is unknown, stop here
#define NEED(n) \
    if (i + (n) > len) { \
        status = ST_BAD_OP; \
        fail_idx = idx; \
        goto done; \
    }

    while (i < len) {
        uint8_t op = p[i++];
        bool run = (status == ST_OK);
        int st = ST_OK;

        switch (op) {
        case OP_HELLO: {
            static const uint8_t hello[] = {
                'I', 'T', 'P', 'P', PROTO_VERSION, FW_VERSION_MAJ,
                FW_VERSION_MIN, LINE_COUNT
            };
            for (unsigned k = 0; k < sizeof(hello); k++) {
                tx_put(run ? hello[k] : 0);
            }
            break;
        }
        case OP_CFG:
            NEED(9);
            if (run) {
                epp_config(rd16(p + i), rd16(p + i + 2), rd16(p + i + 4),
                           rd16(p + i + 6), p[i + 8]);
            }
            i += 9;
            break;
        case OP_SAFE:
            NEED(1);
            if (run) {
                lines_safe(p[i]);
            }
            i += 1;
            break;
        case OP_STRAP:
            NEED(1);
            if (run) {
                lines_strap(p[i]);
            }
            i += 1;
            break;
        case OP_PINS_SET:
            NEED(4);
            if (run) {
                lines_set(rd16(p + i) & LINES_MASK, rd16(p + i + 2));
            }
            i += 4;
            break;
        case OP_PINS_GET: {
            uint16_t v = run ? lines_get() : 0;
            tx_put(v & 0xFF);
            tx_put(v >> 8);
            break;
        }
        case OP_PULLS:
            NEED(4);
            if (run) {
                lines_pulls(rd16(p + i), rd16(p + i + 2));
            }
            i += 4;
            break;
        case OP_DELAY_US:
            NEED(4);
            if (run) {
                idle_us(rd32(p + i));
            }
            i += 4;
            break;
        case OP_WAIT_PIN: {
            NEED(4);
            uint8_t line = p[i];
            bool level = p[i + 1] != 0;
            uint16_t ms = rd16(p + i + 2);
            i += 4;
            if (line >= LINE_COUNT) {
                status = ST_BAD_OP;
                fail_idx = idx;
                goto done;
            }
            uint8_t seen = 0;
            if (run) {
                uint32_t t0 = to_ms_since_boot(get_absolute_time());
                do {
                    if (gpio_get(lines_gpio(line)) == level) {
                        seen = 1;
                        break;
                    }
                    tud_task();
                } while (to_ms_since_boot(get_absolute_time()) - t0 < ms);
            }
            tx_put(seen);
            break;
        }
        case OP_INIT_PULSE:
            NEED(2);
            if (run) {
                if (lines_armed()) {
                    epp_init_pulse(rd16(p + i));
                } else {
                    st = ST_NOT_ARMED;
                }
            }
            i += 2;
            break;
        case OP_AW:
        case OP_DW:
            NEED(1);
            if (run) {
                if (lines_armed()) {
                    st = epp_write(op == OP_AW, p[i]);
                } else {
                    st = ST_NOT_ARMED;
                }
            }
            i += 1;
            break;
        case OP_AR: {
            uint8_t v = 0;
            if (run) {
                if (lines_armed()) {
                    st = epp_read(true, &v);
                } else {
                    st = ST_NOT_ARMED;
                }
            }
            tx_put(v);
            break;
        }
        case OP_DR: {
            NEED(2);
            uint16_t n = rd16(p + i);
            i += 2;
            if (run && !lines_armed()) {
                st = ST_NOT_ARMED;
                run = false;
            }
            for (uint32_t k = 0; k < n; k++) {
                uint8_t v = 0;
                if (run) {
                    st = epp_read(false, &v);
                    run = (st == ST_OK);
                }
                tx_put(v);
            }
            break;
        }
        default:
            status = ST_BAD_OP;
            fail_idx = idx;
            goto done;
        }

        if (st != ST_OK && status == ST_OK) {
            status = (uint8_t)st;
            fail_idx = idx;
        }
        idx++;
    }
#undef NEED

done:
    tx_trailer(status, fail_idx);
    led(lines_armed());
}

// ─── USB input framing ──────────────────────────────────────────────

static uint8_t rx_buf[MAX_BATCH];
static uint32_t rx_len, rx_need;
static enum { RX_MAGIC, RX_LEN0, RX_LEN1, RX_BODY } rx_state = RX_MAGIC;

static void usb_rx(void) {
    uint8_t tmp[64];

    while (tud_vendor_available()) {
        uint32_t n = tud_vendor_read(tmp, sizeof(tmp));
        for (uint32_t k = 0; k < n; k++) {
            uint8_t b = tmp[k];
            switch (rx_state) {
            case RX_MAGIC:
                if (b == REQ_MAGIC) {   // anything else is junk: resync
                    rx_state = RX_LEN0;
                }
                break;
            case RX_LEN0:
                rx_need = b;
                rx_state = RX_LEN1;
                break;
            case RX_LEN1:
                rx_need |= (uint32_t)b << 8;
                rx_len = 0;
                rx_state = RX_MAGIC;
                if (rx_need > MAX_BATCH) {
                    tx_trailer(ST_BAD_OP, 0);
                } else if (rx_need == 0) {
                    exec_batch(rx_buf, 0);
                } else {
                    rx_state = RX_BODY;
                }
                break;
            case RX_BODY:
                rx_buf[rx_len++] = b;
                if (rx_len == rx_need) {
                    rx_state = RX_MAGIC;
                    exec_batch(rx_buf, rx_len);
                }
                break;
            }
        }
    }
}

// Host went away: release every line
void tud_umount_cb(void) {
    lines_safe(PULL_NONE);
    rx_state = RX_MAGIC;
    led(false);
}

int main(void) {
#ifdef LED_PIN
    gpio_init(LED_PIN);
    gpio_set_dir(LED_PIN, GPIO_OUT);
#endif
    led(false);

    lines_init();
    epp_config(DEF_SETUP_NS, DEF_STROBE_NS, DEF_HOLD_NS, DEF_TIMEOUT_US,
               CFG_HANDSHAKE);

    tusb_init();

    while (true) {
        tud_task();
        usb_rx();
    }
}
