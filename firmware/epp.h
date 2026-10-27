/**
 * @file epp.h
 *
 * IEEE 1284 EPP host cycles over the KBS lines
 */

#ifndef EPP_H
#define EPP_H

#include "pico/types.h"

void epp_config(uint16_t setup_ns, uint16_t strobe_ns, uint16_t hold_ns,
                uint16_t timeout_us, uint8_t flags);
int epp_write(bool addr, uint8_t val);
int epp_read(bool addr, uint8_t *val);
void epp_init_pulse(uint16_t us);

#endif
