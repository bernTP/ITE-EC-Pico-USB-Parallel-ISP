# ITE parallel ISP (DBGR over EPP): what is known

This file explains why the tool does what it does. Each claim is marked:
- **[doc]**: ITE documentation
- **[community]**: programmer pinout tables
- **[port]**: copied from the open-source I2C implementation
- **[hypothesis]**: not yet confirmed on hardware
- **[hw]**: observed on a real IT5261 rev 02 (ThinkPad T14s Gen 1, NM-C791 board, Pico 2 W),
  2026-10-31

## 1. Sources

| Source | What it gives |
|--------|---------------|
| ITE **IT5570** datasheet V0.3.1, §7.23 "Debugger (DBGR)", §7.24 "Parallel Port (PP)", Table 5-19 / 5-21, Fig. 7-43 | Pin roles, entry strap, DBGR has an EPP transport |
| Vertyanov programmer profile list, row `NM-C791 … IT5261VG-256 (evserv)` | The FFC pin of each parallel-port signal on this board |
| Chromium OS EC `util/iteflash.cc` and the original repo's port `iteflash-i2cpicousb.py` | DBGR register map and sequences over I2C |

The IT5570 is not the IT5261. It is a sibling 8051 ITE EC that the datasheet says has the same ISP
feature ("ISP via parallel port interface on existing KBS connector"), and the same feature line
appears in other ITE EC datasheets (IT8502E, IT8510E, IT8110E, IT81202/IT82202).

## 2. Physical layer [doc] [community]

IT5570 Fig. 7-43 maps the KBS pins onto a PC parallel port (LPT):

```
KSO0..7 = PD0..7     KSI0 = STB#    KSI1 = AFD#    KSI2 = INIT#    KSI3 = SLIN#
KSO8 = ACK#          KSO9 = BUSY    KSO10 = PE     KSO11 = ERR#    KSO12 = SLCT
KSI4, KSI5: tied to ground through 1 kΩ in ITE's cable
```

The NM-C791 (IT5261VG-256) programmer profile reads:

```
PD0=7 PD1=13 PD2=11 PD3=14 PD4=10 PD5=17 PD6=15 PD7=16
BUSY=20 STB=9 AFD=6 INIT=8 SLIN=1 KSI4=5 KSI5=12
```

This matches the board pinout image 1:1: KSO0..7, KSO9, KSI0..5. ACK#/PE/ERR#/SLCT are not on this
connector.

The set PD0–7 + STB#/AFD#/INIT#/SLIN# + BUSY is exactly the signal set of IEEE 1284 **EPP**:

| LPT name | EPP role | Direction |
|----------|----------|-----------|
| STB#  | nWrite | host → EC; low = write cycle |
| AFD#  | nDataStrobe | host → EC |
| SLIN# | nAddrStrobe | host → EC |
| INIT# | nInit | host → EC; must stay high, it is a strap bit |
| BUSY  | nWait | EC → host; low = ready, high = cycle acknowledged |

The datasheet says "Programming software supports EPP/SPP mode" and gives the PP pins as 3.3 V CMOS.

## 3. Entering DBGR/EPP [doc]

- Table 5-21 (hardware strap): "KSI[5,4,2,1] … The value of 0011b is the entry of DBGR/EPP".
  So KSI5 = 0, KSI4 = 0, KSI2 (INIT#) = 1 and KSI1 (AFD#) = 1. An idle LPT port plus ITE's 1 kΩ
  pull-downs on KSI4/KSI5 produces exactly that.
- §7.24.3.1: "The DBGR/EPP debug mode takes place when VSTBY is supplied".
- §7.23.3.1: "Hot-plug is available."
- "If Parallel Port cable is detected by internal hardware strap" then the ROM address match
  interrupt and the **internal/external watchdog are disabled**.
- Firmware can block it: `KSICTRLR.OVRPPK` "overrides PP function which is enabled by hardware
  strap in KBS interface and disables it", and "GPIO mode overrides EPP mode".

- Table 5-21 note: "The strapped pin will be strapped 1b if it is pulled up. The strapped pin will
  be strapped 0b if it is pulled down or not pulled. The strapped pin will be strapped an unknown
  value if it is driven." Straps are latched when VSTBY powers up (Fig. 10-2, Table 10-2).
- §7.4.2: "Supports GPIO mode for all KBS pins (GPIO mode overrides EPP mode)", set pin by pin
  (KSOLGCTRLR, KSIGCTRLR). KSO1/KSO2 have no other alternate function.

What the IT5261 did **[hw]**:

| Pattern when AC went in | Result |
|-------------------------|--------|
| Any pattern applied after power-up (hot-plug), board on standby or booted | No entry. With KSI4/KSI5 low the EC scans the keyboard (one KSO low at a time). |
| KSI4/KSI5 driven low, KSI0–KSI3 floating | No entry, keyboard scan. KSI1/KSI2 floating latch as 0. |
| KSI4/KSI5 driven low, Pico pull-ups (~50 kΩ) on KSI0–KSI3, rest released | **Entry.** Keyboard scan stops, BUSY idles low, EPP cycles are acknowledged. |

On this board FFC pin 12 (KSI5) never reaches the EC: an unpowered EC doesn't clamp it. Floating
latches as 0, which is what the pattern needs, so it didn't matter.

The EC must be fully discharged before AC goes in. A released RP2350 pin leaks (erratum E9):
with the Pico's lines released and AC out, an unused KSO pin sat at 0.408 V; with every line
driven low it fell to 24 mV. Pull-ups on all 15 lines charged the EC's rail within seconds.

The EC firmware keeps running in this mode (the laptop even boots). At times it held KSO1/KSO2 so
that the EC neither drove nor received PD1/PD2: they read 1 and register numbers arrived with
bits 1 and 2 set. A few seconds after another power-up all 8 bits worked. Halting the CPU
(reg 0x27) keeps it that way.

The tool's entry modes:
- **powerup** (default): AC out, every line driven low while the EC discharges; then KSI4/KSI5
  driven low and pull-ups on KSI0–KSI3 only; AC goes in; then the control lines are driven high.
- **hotplug:** drive the strap with the board already on. Didn't work on the IT5261 above.

## 4. DBGR register access [port] + [hypothesis]

The datasheet says DBGR "can be performed by one of the two interfaces: DBGR/EPP, DBGR/SMB. Both
ISP and ISD can be performed by either" **[doc]**. So the EPP side reaches the same register file
that iteflash drives over I2C.

Over I2C, DBGR uses three slave addresses **[port]**:

| I2C address | Use | EPP equivalent used here |
|-------------|-----|--------------------------|
| 0x5A | write = select register | **address write cycle** |
| 0x35 | read/write one byte of the selected register | **data cycle** |
| 0x79 | burst read/write of the selected register | **repeated data cycles** |

The mapping in the last column is **confirmed [hw]**: address write `r` then data read gives
DBGR register `r` (regs 00..02 = `52 61 02`, stable over 12 reads; the selected register persists
between cycles). Standard EPP signalling, no strobe swap, nWait low = ready.

An EPP **address read** returns the complement of the last address written **[hw]** (00→ff,
a5→5a, 55→aa). The tool uses that as a bus check before any write: it exercises all 8 data lines
in both directions without touching a register.

Reading reg 0x08 (ECINDDR) outside follow mode ends in an nWait timeout **[hw]**.

Registers used (all from iteflash) **[port]**:

| Reg | Meaning |
|-----|---------|
| 0x00, 0x01 | chip ID high/low (`0x83xx` = IT83xx; otherwise a 3rd byte is read from EC 0xF02085) |
| 0x02 | chip version; flash size in bits 7..4 on non-IT5xxxx parts |
| 0x04..0x07 | ECINDAR0..3 (EC-indirect address) |
| 0x08 | ECINDDR (EC-indirect data) |
| 0x09 | selected before burst reads after a fast read (paired with I2C 0x79) |
| 0x27 | CPU control: 0x81 = reset + halt (done for IT5xxxx) |
| 0x2E / 0x2F / 0x30 | EC memory address low / high / data (D2EC) |
| 0x80 | EC memory address bits 23..16 (3-byte addressing parts) |

Detection follows `check_chipid()`:

1. Read regs 0x00, 0x01, 0x02.
2. If the high byte is not 0x83, read the 3rd ID byte at EC 0xF02085. A chip ID of the form
   `0x5xxxx` is an IT5xxxx part. The IT5261 reads `00` there **[hw]**, so the tool also takes a
   16-bit ID of the form `0x5xxx` as IT5xxxx.
3. For an IT5xxxx part:
   - halt the CPU (reg 0x27 = 0x81) **[hw]: works, the bus stays clean afterwards**;
   - read the flash size code at EC 0xF01080 (bits 3..0: 1 = 128 KB, 2 = 256 KB, E = 512 KB,
     F = 1 MB). The IT5261VG-256 reads `02` **[hw]**.

## 5. Reading the e-flash: follow mode [port]

Follow mode exposes the e-flash as an SPI device behind ECINDAR/ECINDDR:

```
enter follow mode:   07←7F 06←FF 05←FE 04←00 08←00      (ECINDAR = 7FFFFE00, FSCE# high)
SPI command <op>:    05←FE 08←00 05←FD 08←<op>         (end previous, start new, send op)
JEDEC ID:            ...08←9F, then read 3 bytes      FF FF FE = "8315" e-flash, C8/EF = KGD
fast read @A:        SPI 0B, 08←A[23:16] 08←A[15:8] 08←A[7:0] 08←00 (dummy),
                     select reg 09, then burst-read N bytes
                     re-send at every 0x40000 boundary
exit follow mode:    07←00 06←00
```

`rr←vv` means: EPP address write `rr`, then EPP data write `vv`.

Confirmed on the IT5261 **[hw]**: RDID returns `ff ff fe ff ff ff ff ff ff ff 5a f0 83 15 02 62`
(type 8315). Burst reads through reg 0x09 and through reg 0x08 return identical data, about
170 KB/s; the 256 KB image reads twice in about 2 s. The image starts with a table of jumps, has
the ITE signature at 0x80 (`a5 a5 a5 a5 a5 a5 a7 14 85 12 76 32 aa 00 55 55`, then
`ITE8380-EC-V0.00`), and the Lenovo EC version string (`R1BHT82W`).

The only SPI opcodes the tool can emit are 0x05, 0x0B and 0x9F; `flash.py` refuses anything else.
Without a 0x06 (write enable) the flash ignores program and erase opcodes anyway.

Optional writes, all EC registers and never the flash:

| Flag | Write |
|------|-------|
| `--wdt-disable` | 0x1F05 = 0x30 |
| `--reset-gpio` | 0x2007 = 0x02 |
| DBGR exit at the end (default; `--no-exit` skips it; not checked on the IT5261) | 0x1C08 = 0x10 |

On 3-byte addressing parts these addresses are in bank 0xF0 (`0x80 ← F0`).

## 6. Open questions and the matching knobs

Settled on the IT5261 **[hw]**: nWait handshake with normal polarity, strobes in LPT order, the
default timing, both burst read registers, and the EPP ↔ DBGR mapping. The knobs stay for other
chips and boards:

| Unknown | Knob |
|---------|------|
| nWait handshake used? polarity? | `--no-handshake`, `--nwait-invert`, `--no-release`, `probe` |
| Address and data strobes swapped vs. the LPT naming | `--swap-strobes`, `probe` (tried last, only if the normal order fails) |
| Timing | `--setup-ns --strobe-ns --hold-ns --timeout-us` |
| Burst read register after fast read (0x09 like I2C 0x79, or ECINDDR 0x08) | `read --read-reg 09/08` |
| Hot-plug blocked by EC firmware (it was on the IT5261; OVRPPK?) | `enter --mode powerup` |
| EPP address/data ↔ DBGR mapping itself | `raw`, `regs`: inspect by hand |

The EC stays in parallel-port mode after the Pico releases the lines **[hw]**: `safe`, then
`enter --mode hotplug` in the same power cycle gave a clean address echo and `52 61 02`.

Still open: whether the DBGR exit write (0x1C08) is right for the IT5261.

`probe` reads regs 00..02 twice with each variant and ranks them. A plausible answer is a status
of OK, a high ID byte that is a known ITE prefix, and the same value on both reads.

## 7. USB wire protocol (Pico ↔ host)

See `firmware/proto.h`; `host/itepp/proto.py` mirrors it, and a test checks that they match.

The host sends one batch of ops per bulk OUT transfer. The Pico runs the ops and streams the read
data back on bulk IN, ending with a 4-byte trailer:

```
status, failed_op_index (u16), 0x5A
```

After an error, the Pico still sends zeros for every byte the remaining ops would have returned,
so the length never changes. A 4 KB read costs one USB round trip.
