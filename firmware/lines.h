/**
 * @file lines.h
 *
 * Raw control of the 15 KBS lines (logical line numbers from proto.h)
 */

#ifndef LINES_H
#define LINES_H

#include "pico/types.h"

void lines_init(void);
void lines_safe(uint8_t pull);
void lines_pulls(uint16_t up, uint16_t down);
void lines_set(uint16_t oe, uint16_t val);
uint16_t lines_get(void);
void lines_strap(uint8_t mode);
bool lines_armed(void);
uint lines_gpio(uint8_t line);

#endif
