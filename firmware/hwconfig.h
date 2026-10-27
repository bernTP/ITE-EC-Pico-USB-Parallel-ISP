/**
 * @file hwconfig.h
 *
 * Hardware definitions for the ITE parallel ISP (DBGR/EPP) interface.
 *
 * Wiring to the keyboard FFC (IT5261VG-256 / NM-C791 pinout):
 *
 *   EPP signal     EC pin   FFC pin   Pico
 *   PD0..PD7       KSO0..7  7,13,11,14,10,17,15,16   GP0..GP7
 *   STB#  nWrite   KSI0     9         GP8
 *   AFD#  nDStrb   KSI1     6         GP9
 *   INIT# nInit    KSI2     8         GP10
 *   SLIN# nAStrb   KSI3     1         GP11
 *   (strap)        KSI4     5         GP12
 *   (strap)        KSI5     12        GP13
 *   BUSY  nWait    KSO9     20        GP14
 *   GND            board GND (not on the FFC)
 */

#ifndef HWCONFIG_H
#define HWCONFIG_H

// Pin that has a LED connected
#ifdef PICO_DEFAULT_LED_PIN
#define LED_PIN PICO_DEFAULT_LED_PIN
#endif

// Logic levels to turn LED on and off
#define LED_ON  1
#define LED_OFF 0

// Data bus: PD0..PD7 must be 8 consecutive GPIOs starting here
#define PD_BASE_PIN 0

// Control lines (any GPIO)
#define STB_PIN     8
#define AFD_PIN     9
#define INIT_PIN    10
#define SLIN_PIN    11
#define KSI4_PIN    12
#define KSI5_PIN    13
#define BUSY_PIN    14

// USB IDs: pid.codes test VID/PID, change if it clashes with something
#define USBD_VID    0x1209
#define USBD_PID    0x0001
#define USBD_DEVICE 0x0100

// EPP timing defaults (host can override with OP_CFG)
#define DEF_SETUP_NS    500
#define DEF_STROBE_NS   1000
#define DEF_HOLD_NS     250
#define DEF_TIMEOUT_US  200

#endif
