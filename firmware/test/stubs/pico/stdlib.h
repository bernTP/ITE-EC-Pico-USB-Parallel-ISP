// Host-side stub of the Pico SDK, just enough to compile main.c
#ifndef STUB_PICO_STDLIB_H
#define STUB_PICO_STDLIB_H

#include "pico/types.h"

#define GPIO_OUT 1
#define GPIO_IN 0

static inline void gpio_init(uint gpio) { (void)gpio; }
static inline void gpio_set_dir(uint gpio, bool out) { (void)gpio; (void)out; }
static inline void gpio_put(uint gpio, bool v) { (void)gpio; (void)v; }
bool gpio_get(uint gpio);

static inline void sleep_us(uint64_t us) { (void)us; }
typedef uint64_t absolute_time_t;
absolute_time_t get_absolute_time(void);
static inline uint32_t to_ms_since_boot(absolute_time_t t) { return (uint32_t)(t / 1000); }

#endif
