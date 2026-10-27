/**
 * @file proto.h
 *
 * USB wire protocol between the host tool (host/itepp/proto.py) and the
 * Pico firmware. Keep both files in sync.
 *
 * Transport: one vendor-class interface, bulk OUT 0x01 / bulk IN 0x81.
 *
 * Request (host -> Pico):
 *   0xA5, len_lo, len_hi, <len bytes of ops>
 *
 * Response (Pico -> host):
 *   <bytes produced by the ops, in order>, status, idx_lo, idx_hi, 0x5A
 *
 * The host knows how many bytes its ops produce, so it reads exactly
 * that many plus the 4-byte trailer. If an op fails, the remaining ops
 * are still parsed but not executed and every byte they would have
 * produced is sent as 0x00, so the response length never changes.
 * idx is the index of the first failing op (0xFFFF if none).
 */

#ifndef PROTO_H
#define PROTO_H

#define PROTO_VERSION   1
#define FW_VERSION_MAJ  1
#define FW_VERSION_MIN  0

#define REQ_MAGIC       0xA5
#define RSP_MAGIC       0x5A
#define MAX_BATCH       4096

// ─── Ops (args are little-endian) ───────────────────────────────────
#define OP_HELLO        0x01  // -> 'I','T','P','P', proto, fw_maj, fw_min, n_lines
#define OP_CFG          0x02  // setup_ns u16, strobe_ns u16, hold_ns u16, timeout_us u16, flags u8
#define OP_SAFE         0x03  // pull u8: all lines input
#define OP_STRAP        0x04  // mode u8: drive the DBGR/EPP entry strap
#define OP_PINS_SET     0x05  // oe u16, val u16 (logical line bits)
#define OP_PINS_GET     0x06  // -> levels u16
#define OP_PULLS        0x07  // up u16, down u16
#define OP_DELAY_US     0x08  // us u32
#define OP_WAIT_PIN     0x09  // line u8, level u8, timeout_ms u16 -> 1 if seen, 0 if not
#define OP_INIT_PULSE   0x0A  // us u16 (breaks the strap: experiments only)
#define OP_AW           0x10  // b u8: EPP address write
#define OP_AR           0x11  // -> u8: EPP address read
#define OP_DW           0x12  // b u8: EPP data write
#define OP_DR           0x13  // n u16 -> n bytes: EPP data reads

// ─── Status ─────────────────────────────────────────────────────────
#define ST_OK           0x00
#define ST_NWAIT_TIMEOUT 0x01
#define ST_BAD_OP       0x02
#define ST_NOT_ARMED    0x03

// ─── OP_CFG flags ───────────────────────────────────────────────────
#define CFG_HANDSHAKE   0x01  // wait on nWait (BUSY); else fixed delays only
#define CFG_NWAIT_INV   0x02  // nWait polarity inverted
#define CFG_SWAP_STROBE 0x04  // AFD# is address strobe, SLIN# is data strobe
#define CFG_NO_RELEASE  0x08  // don't wait for nWait to return after strobe

// ─── OP_SAFE pull modes ─────────────────────────────────────────────
#define PULL_NONE       0
#define PULL_UP         1
#define PULL_DOWN       2

// ─── OP_STRAP modes ─────────────────────────────────────────────────
// KSI[5,4,2,1] = 0011b is the DBGR/EPP entry (IT5570 datasheet, table 5-21)
#define STRAP_WEAK      0  // KSI4/5 low, control lines high via pull-ups only
#define STRAP_DRIVE     1  // KSI4/5 low, control lines driven high: EPP ops allowed

// ─── Logical line numbers (bit n in PINS_SET / PINS_GET masks) ──────
#define LINE_PD0        0     // PD0..PD7 = lines 0..7 (KSO0..KSO7)
#define LINE_STB        8     // KSI0 / STB#  = EPP nWrite
#define LINE_AFD        9     // KSI1 / AFD#  = EPP nDataStrobe
#define LINE_INIT       10    // KSI2 / INIT# = EPP nInit
#define LINE_SLIN       11    // KSI3 / SLIN# = EPP nAddrStrobe
#define LINE_KSI4       12
#define LINE_KSI5       13
#define LINE_BUSY       14    // KSO9 / BUSY  = EPP nWait
#define LINE_COUNT      15
#define LINES_MASK      0x7FFF

#endif
