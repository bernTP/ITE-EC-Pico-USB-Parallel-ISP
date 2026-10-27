// Host-side stub of the TinyUSB vendor API used by main.c
#ifndef STUB_TUSB_H
#define STUB_TUSB_H

#include "pico/types.h"

void tud_task(void);
bool tud_vendor_mounted(void);
uint32_t tud_vendor_available(void);
uint32_t tud_vendor_read(void *buf, uint32_t size);
uint32_t tud_vendor_write(const void *buf, uint32_t size);
uint32_t tud_vendor_write_flush(void);
static inline bool tusb_init(void) { return true; }

#endif
