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

**Halt early [hw].** On the ThinkPad the running EC firmware switches KSO1/KSO2 to GPIO
("GPIO mode overrides EPP mode"): PD1/PD2 then read 1 and are received as 1. It happened within a
minute of power-up, came and went every few milliseconds during the BIOS screen, and with the
laptop off PD2 stayed taken for 10 minutes straight. A halt sent while the bits change only
resets the EC (the laptop switches off) and the firmware starts again, with the pins clean for at
least 2 s. So after the power-up entry the tool sends `27←81` at the first clean address echo and
checks the echo stays clean for 3 s. If the bits are held at 1, `27` still arrives intact (its bits
1–2 are 1) and `81` arrives as `87`, which still resets the EC: the tool does that on purpose after
2 s of a held bus, then halts in the clean window that follows.

The Pico can't detect the power-up by itself: with AC out, its pull-ups charge the unpowered EC
within ~100 ms, after which the lines it would watch no longer read as clamped.

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

On the read path the only SPI opcodes the tool can emit are 0x05, 0x0B and 0x9F; `flash.py`
refuses anything else. Without a 0x06 (write enable) the flash ignores program and erase opcodes
anyway. Writing is a separate module (§5b).

Optional writes, all EC registers and never the flash:

| Flag | Write |
|------|-------|
| `--wdt-disable` | 0x1F05 = 0x30 |
| `--reset-gpio` | 0x2007 = 0x02 |
| DBGR exit at the end (default; `--no-exit` skips it; not checked on the IT5261) | 0x1C08 = 0x10 |

On 3-byte addressing parts these addresses are in bank 0xF0 (`0x80 ← F0`).

## 5b. Writing the e-flash [port] + [hypothesis]

The commands are iteflash's for IT5xxxx parts with the 8315 e-flash (`command_erase2`,
`write_flash2`); the way the words are sent differs (see below). A 1 KB sector has been erased,
programmed and verified on the IT5261; a full-image write is still to be done.

```
write enable:     SPI 06, then RDSR until (status & 03) == 02         (spi_check_write_enable)
sector erase @A:  SPI D7, 08←A[23:16] 08←A[15:8] 08←00, then RDSR until BUSY clears, SPI 04
                  1 KB sectors (4 pages) on the 8315 e-flash; 4 KB / 0x20 on KGD parts
AAI program @A:   SPI 06, check WE, SPI AD, 08←A[23:16] 08←A[15:8] 08←A[7:0], 08←D0 08←D1,
                  RDSR (its FSCE# rising edge starts the word), wait, read one status byte;
                  then for each next word: SPI AD, 08←Dn 08←Dn+1, RDSR, wait, read;
                  at the end SPI 04 (WRDI) ends AAI mode, then RDSR until BUSY clears
```

`SPI xx` is `05←FE 08←00 05←FD 08←xx`: FSCE# high (ends the previous command), then low with the
opcode, as in §5.

**Where it differs from iteflash.** After the first word, iteflash switches on a DBGR "quick
AAI" mode (`10←20`) and streams the rest of the data through its I2C block address (0x79); the
hardware then sends `AD Dn Dn+1` for each word by itself. EPP has no separate block address, and
which register a block write lands on in quick mode isn't documented, so this tool doesn't use it.
It sends each `AD Dn Dn+1` itself through ECINDDR (the SST-style AAI sequence quick mode
automates), then RDSR, waits `--word-delay-us` (default 20 µs) and reads one status byte.

**Seen on the IT5261 [hw]:**
- The status register reads `fc` at rest (BUSY and WEL clear, upper bits set) and `fe` after
  write enable.
- It reads `ff` while a sector erase runs, and for the whole AAI sequence: it can't tell when a
  word is done. So the per-word status value is ignored; the sector is read back instead, and a
  mismatch lengthens the delay (×4) and redoes the sector.
- The per-word RDSR + status read is still needed: with it, a 1 KB sector came back identical;
  without it (FSCE# high and a delay only, up to 1.3 ms per word), the data stayed `ff`.
- Without the protect-path writes the sector erase was silently ignored (content unchanged,
  reads all `ff` until the next CPU reset). With them, the erase worked.
- A 1 KB sector: erase, program (20 µs per word) and verify in 0.3 s.

Per sector the tool does erase → blank check → program → read back and compare, with up to 3
attempts. It only writes the sectors that differ from the image, and the one holding address 0
last. The writer refuses every opcode except 04, 05, 06, AD and D7: no chip erase (60/C7), no
status register write (50/01), no page program (02).

Like iteflash, `write` first does the "disable protect path" writes (32 zeros at EC 0xF020A0–BF;
`--keep-protect-path` skips them). Unlike iteflash it doesn't reset the GPIOs by default
(`--reset-gpio` on `read`).

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
