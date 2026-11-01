# ITE EC Pico USB Parallel ISP

This project reads and writes the embedded flash of an **ITE IT5261VG-256** embedded controller (EC)
through the laptop's **keyboard FFC connector**. It uses the ITE "parallel ISP" path: the chip's debugger
(DBGR) talks IEEE 1284 EPP over the KSI/KSO keyboard-scan pins. A **Raspberry Pi Pico**
wired to that connector does the low-level signalling, and any Linux machine with Python, such as a
**Raspberry Pi Zero**, drives the Pico over USB and writes a `.bin`.

Only the `write` command erases or programs the flash. It backs the flash up first, only touches the
1 KB sectors that differ, verifies each one, and asks before it starts. Every other command is
read-only: the read path refuses any SPI opcode other than read, fast read and read status.

It is inspired by [I2C-Pico-USB-ITE-EC-prog](https://github.com/JohnDoe150489/I2C-Pico-USB-ITE-EC-prog),
which drives the same DBGR block over I2C/SMBus. Here the transport is the keyboard connector
instead.

> **Status: works on real hardware.** An IT5261 rev 02 (ThinkPad T14s Gen 1, NM-C791 board) was
> read in full with a Pico 2 W: chip ID `52 61 02`, 256 KB, e-flash type 8315, two identical passes
> in about 2 seconds.
> - **Documented by ITE:** the pin roles, the EPP transport and the entry strap (IT5570 datasheet,
>   same EC family and ISP method).
> - **Ported and confirmed:** the register-level protocol comes from Chromium's `iteflash`. EPP
>   address and data cycles map onto the DBGR registers exactly as the I2C path does.
> - **Learned on the hardware:** the entry only works at power-up, with KSI1/KSI2 on pull-ups
>   (see [Reading the chip](#reading-the-chip)).
> - **`write` works on this chip** for a single sector so far. Its erase and AAI word-program
>   commands are the ones `iteflash` uses, sent one word at a time instead of through iteflash's
>   "quick AAI" shortcut. See [Writing the chip](#writing-the-chip).
>
> [docs/PROTOCOL.md](docs/PROTOCOL.md) has all the evidence.

```
 laptop keyboard FFC ──15 wires + GND──> Pico (firmware/) ──USB──> Pi Zero (host/ite_pp_isp.py) ──> dump.bin
```

## Hardware

**Parts**
- Raspberry Pi Pico (RP2040) or Pico 2 (RP2350).
- Raspberry Pi Zero / Zero W (or any Linux PC) and a micro-USB OTG adapter.
- An FFC breakout board that matches the laptop's keyboard connector (pitch and pin count), plus
  jumper wires.
- Resistors: 15 × 1 kΩ, one in series on every line. Anything from 470 Ω to 1 kΩ works; 1 kΩ limits
  the current best if the EC and the Pico ever drive the same line at once (3.3 mA at most).

**Wiring** (IT5261VG-256 / NM-C791 keyboard connector pinout)

| FFC pin | EC pin | Parallel-port role  | Series R | Pico GPIO | Pico physical pin |
|--------:|--------|---------------------|---------:|-----------|------------------:|
| 7       | KSO0   | PD0                 | 1 kΩ     | GP0       | 1  |
| 13      | KSO1   | PD1                 | 1 kΩ     | GP1       | 2  |
| 11      | KSO2   | PD2                 | 1 kΩ     | GP2       | 4  |
| 14      | KSO3   | PD3                 | 1 kΩ     | GP3       | 5  |
| 10      | KSO4   | PD4                 | 1 kΩ     | GP4       | 6  |
| 17      | KSO5   | PD5                 | 1 kΩ     | GP5       | 7  |
| 15      | KSO6   | PD6                 | 1 kΩ     | GP6       | 9  |
| 16      | KSO7   | PD7                 | 1 kΩ     | GP7       | 10 |
| 9       | KSI0   | STB# (EPP nWrite)       | 1 kΩ  | GP8  | 11 |
| 6       | KSI1   | AFD# (EPP nDataStrobe)  | 1 kΩ  | GP9  | 12 |
| 8       | KSI2   | INIT# (EPP nInit)       | 1 kΩ  | GP10 | 14 |
| 1       | KSI3   | SLIN# (EPP nAddrStrobe) | 1 kΩ  | GP11 | 15 |
| 5       | KSI4   | entry strap (low)       | 1 kΩ  | GP12 | 16 |
| 12      | KSI5   | entry strap (low)       | 1 kΩ  | GP13 | 17 |
| 20      | KSO9   | BUSY (EPP nWait)        | 1 kΩ  | GP14 | 19 |
| —       | GND    | **board ground**        | —     | GND  | 3, 8, 13, 18 … |

All other FFC pins are not connected. The FFC carries **no ground**, so take GND from the board,
for example from a USB connector shell or a screw hole.

The pin numbers above are the board connector's. Depending on how the breakout is mounted, the
numbering can come out **mirrored**. Follow [docs/MULTIMETER.md](docs/MULTIMETER.md) to check
orientation, voltages and wiring with a multimeter before connecting the Pico, then use the
`lines` command.

The pin mapping is set in `firmware/hwconfig.h`. PD0–PD7 must stay on 8 consecutive GPIOs.

### Power

There are two separate power sources, and both are needed:

| What | Powered by | Why |
|------|------------|-----|
| Pico | USB from the Pi Zero | runs the `ite_pp_isp` firmware |
| Laptop board (EC) | **its AC adapter**, main battery removed | the EC must be powered to answer |

The EC only answers when its standby rail (VSTBY, often called 3VALW on schematics) is on. The
ITE datasheet says "The DBGR/EPP debug mode takes place when VSTBY is supplied". That rail is on as
soon as the AC adapter is plugged in. **Don't press the power button.** Some boards (the ThinkPad
used here) boot by themselves when AC goes in; that does no harm. The RTC/CMOS coin cell plays no
part and can stay connected.

The EC chooses parallel-port mode **once, at the moment AC goes in**, so `enter --mode powerup`
asks you when to plug it in. Before that, the EC must be fully discharged. A released Pico 2
(RP2350) pin leaks enough current to keep an unpowered EC half alive (about 0.4 V measured on
its rail), so the tool drives every line low while you wait.

The Pico cannot power the EC through the 15 signal wires, and must not try. With the board
unpowered, `isp lines` shows every connected line as low (the EC clamps it), and the EC will
never answer.

Only GND is shared between the two sides; never connect the Pico's 3V3 or VBUS to the laptop board.

### Safety

- **3.3 V logic only.** Before connecting the Pico, power the board and measure an idle KSI pin
  against GND. It should read about 3.3 V. If it reads 5 V, stop.
- **Battery out.** See [Power](#power). The original repo gives the same advice.
- **Keep the series resistors, on all 15 lines.** The EC drives the KSO pins for keyboard scanning
  until it switches into parallel-port mode. The resistors limit the current when both sides drive
  a line at once, and when the Pico drives a line of the unpowered EC.
- **Solder every joint.** Header pins pushed into breakout holes without solder made contact only
  while a meter probe pressed on them; none of the Pico's own checks can see that.
- **The Pico drives nothing at boot.** All lines are high-impedance until you run `enter`. When
  you unplug USB, the firmware releases all lines.
- **In `--mode powerup`,** the lines are only ever driven low while the board is unpowered. When
  you plug AC in, KSI4/KSI5 are driven low and KSI0–KSI3 only have the Pico's weak pull-ups.
- **Run `safe` before unplugging AC** if the strap is still driven (after `enter`, `id`, `read`).

## Firmware (Pico)

Prebuilt images are in `firmware/prebuilt/`:
- `ite_pp_isp_pico.uf2` for the Pico (RP2040)
- `ite_pp_isp_pico2.uf2` for the Pico 2 (RP2350)

To flash, hold **BOOTSEL** while you plug in the Pico, then copy the `.uf2` to the `RPI-RP2` /
`RP2350` drive. The Pico shows up as USB device `1209:0001` ("ite-pp-isp"). Its LED is on while
the strap is driven.

`lsusb` tells you the state of the Pico:

| `lsusb` shows | Meaning | Image |
|---------------|---------|-------|
| `2e8a:0003 … RP2 Boot` | Pico (RP2040) in BOOTSEL mode, waiting for firmware | `ite_pp_isp_pico.uf2` |
| `2e8a:000f … RP2350 Boot` | Pico 2 (RP2350) in BOOTSEL mode, waiting for firmware | `ite_pp_isp_pico2.uf2` |
| `1209:0001` | firmware running, ready | — |

On a headless Pi Zero (no desktop, so the drive is not mounted automatically), with the Pico in
BOOTSEL mode:

```sh
lsblk                                   # the Pico is a small (~128 MB) disk, e.g. sda1
sudo mount /dev/sda1 /mnt
sudo cp ite-pp-isp/firmware/prebuilt/ite_pp_isp_pico2.uf2 /mnt/   # or _pico.uf2 for RP2040
sync; sudo umount /mnt                  # the Pico reboots into the firmware by itself
lsusb | grep 1209:0001
```

To build it yourself you need the Pico SDK 2.x and an `arm-none-eabi` toolchain:

```sh
export PICO_SDK_PATH=~/pico/pico-sdk                    # defaults used by build.sh
export PICO_TOOLCHAIN_PATH=~/pico/arm-gnu-toolchain
firmware/build.sh            # builds pico and pico2 into firmware/prebuilt/
firmware/build.sh pico2      # one board only
```

## Host (Raspberry Pi Zero)

```sh
sudo apt install python3-venv libusb-1.0-0
host/setup_venv.sh                       # creates host/.venv with pyusb
sudo cp host/99-ite-pp-isp.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules && sudo udevadm trigger
```

Create the `.venv` on the Pi itself. A venv built on another machine will not run there.

Run the tool with `host/.venv/bin/python host/ite_pp_isp.py …`. The examples below shorten this to
`isp`:

```sh
alias isp="$PWD/host/.venv/bin/python $PWD/host/ite_pp_isp.py"
```

On Windows, bind the device to WinUSB with Zadig. On macOS, nothing extra is needed.

## Reading the chip

1. **Check the Pico.**

   ```sh
   isp hello
   ```

2. **Check the wiring.** Run `lines` first with the board unpowered, then again with AC plugged in:

   ```sh
   isp lines
   ```

   Unpowered, connected lines read `low` with the pull-up, because the dead EC clamps them. A line
   that reads high there is **not connected**. On a Pico 2 (RP2350) the pull-downs don't work
   (erratum E9), so an unconnected line also reads high and can look "powered": trust the
   unpowered run. Powered, lines read `high` or `toggling` (keyboard scan).

3. **Enter DBGR/EPP mode**, starting with **AC unplugged**:

   ```sh
   isp enter --mode powerup
   ```

   The tool first drives every line low so the EC discharges, and asks you to wait until it is
   fully off (an unused keyboard pin such as FFC pin 21 near 0 V on a meter, or a few minutes).
   Press Enter, **plug AC in straight away**, then press Enter again. At that moment KSI4/KSI5 are
   low and KSI1/KSI2 sit on pull-ups, which is the pattern the EC latches at power-up. The tool then
   resets and halts the EC CPU at the first clean address echo. The EC firmware has to be stopped:
   on the ThinkPad it switches KSO1/KSO2 (PD1/PD2) to GPIO while it runs, and then nothing can be
   sent reliably. If those bits are already held, the tool resets the EC once on purpose (the reset
   gets through even with them stuck) and halts it in the clean seconds that follow. With the EC
   stopped, the laptop is or goes off. `--no-halt` skips all this.

   Expected output on an IT5261:

   ```
   Strap now driven.
   EC CPU reset and halted (1 halt command(s)): its firmware is stopped, so the laptop is (or goes) off.
   Address echo OK: all 8 data bits answer.
   DBGR regs 00..02: 52 61 02  (IT5261: 52 61 02)
   ```

   The address echo writes a few address bytes and reads them back (the EC returns their
   complement). It tests all 8 data lines without touching a register.

   `isp enter --mode hotplug` applies the pattern to a board that is already powered. On the
   ThinkPad used here it never worked as a first entry: the EC kept scanning the keyboard. It
   does re-arm the Pico after a power-up entry, as long as AC stayed in.

4. **If `enter` didn't show a sensible ID**, find the EPP settings that work:

   ```sh
   isp probe
   ```

   Then add the suggested flags (for example `--swap-strobes`) to every later command.

5. **Identify the chip.**

   ```sh
   isp id
   ```

   Expected output on an IT5261:

   ```
   CHIPID 05261, CHIPVER 02, Flash size 256 kB (IT5xxxx, size code 0x02)
   EFLASH TYPE = 8315 (id ff ff fe)
   ```

   `id` checks the address echo first and stops if a data bit doesn't answer. It doesn't reset the
   EC CPU: `enter` halted it once, and a second reset can let its firmware run (see
   [One CPU halt per power cycle](#one-cpu-halt-per-power-cycle)).

6. **Dump the flash.**

   ```sh
   isp read -o it5261.bin --no-exit
   ```

   This reads the flash twice and compares the two passes, then writes `it5261.bin`. It also
   prints a sha256, a 0xFF/0x00 sanity check and the ITE signature location if it finds one.
   If the passes differ, both are saved and the tool exits with code 2. 256 KB take about
   2 seconds.

   `--no-exit` skips the final DBGR exit write (EC register 0x1C08), which hasn't been checked on
   an IT5261 yet. The verified dump was made that way. Afterwards, run `isp safe`, unplug AC: the
   EC starts normally at the next power-up.

The Pico keeps the strap applied between commands. `isp safe` releases every line, and `lines`
does too. The EC latches the pattern at power-up and stays in parallel-port mode until AC is
removed, so `isp enter --mode hotplug` re-arms the Pico in the same power cycle (without halting
the CPU again). You can also put `--enter powerup` in front of any command to do the entry first:

```sh
isp --enter powerup read -o it5261.bin --no-exit
```

### Commands

| Command | Purpose |
|---------|---------|
| `hello` | Check that the Pico and its firmware answer. |
| `lines [--samples N]` | Wiring / power check of the 15 lines. Leaves them released. |
| `enter [--mode powerup\|hotplug]` | Apply the DBGR/EPP entry strap (KSI[5,4,2,1] = 0011b), then check the address echo and print regs 00..02. `powerup` (default) asks when to plug AC in, then halts the EC CPU (`--no-halt` skips it). `hotplug` doesn't halt unless `--halt`. |
| `probe [--all]` | Try handshake / strobe / timing variants and rank them by how plausible the chip ID looks. |
| `regs [--start 00 --end ff]` | Dump DBGR registers through EPP. |
| `raw "rr 00; aw 01; dr 1"` | Run hand-written EPP ops (`raw help` lists them). |
| `id` | Chip ID, flash size, e-flash type. |
| `read -o FILE [-R BASE[:SIZE]] [-s KB] [--passes N]` | Dump the flash. Checks the address echo afterwards. |
| `verify [-i FILE] [--ignore BASE[:SIZE]] [-o FILE]` | Read only: check that reads can be trusted before writing. See [Writing the chip](#writing-the-chip). |
| `write -i FILE [-R BASE[:SIZE]] [--dry-run]` | Back up, then erase and program the sectors that differ from `FILE`. See [Writing the chip](#writing-the-chip). |
| `safe` | Release all lines. |

Options for `read`:

| Option | Effect |
|--------|--------|
| `--read-reg 08` | Use the alternative burst-read register. |
| `--chunk N` | Bytes per USB transfer. |
| `--halt` | Reset and halt the EC CPU first. Only after `enter --mode hotplug`, when nothing halted it since the board was powered; refused if `enter` already did (also on `id`, `verify`, `write`). |
| `--wdt-disable` | Write the EC watchdog off. |
| `--reset-gpio` | Reset the EC GPIOs first. |
| `--ignore-flash-id` | Read even if the e-flash ID isn't recognised. |
| `--no-exit` | Skip the DBGR exit at the end. |

EPP tuning flags work on every command:

| Flag | Effect |
|------|--------|
| `--no-handshake` | Use fixed delays instead of waiting on BUSY. |
| `--nwait-invert` | Invert the BUSY polarity. |
| `--swap-strobes` | Swap the address and data strobes (AFD#/SLIN#). |
| `--no-release` | Don't wait for nWait to return after a strobe. |
| `--setup-ns`, `--strobe-ns`, `--hold-ns`, `--timeout-us` | Timing values. |

## Writing the chip

> **Tested on the IT5261 (ThinkPad T14s Gen 1):** 8 data-area sectors erased, programmed and
> verified in 1.8 s. A full-image write hasn't been done yet. Make a verified dump first (`verify`)
> and keep it somewhere safe.

**Check the read path first.** `write` trusts its reads three times: the backup, the blank check
after each erase and the read-back after each sector. Two `read` passes that agree only rule out
random errors: a data line that is open for the whole read returns the same wrong bytes every
time. `verify` sends only read commands and checks more:

```sh
isp verify -i it5261.bin --ignore 3B800
```

1. the address echo before and after the reads (all 8 data lines, both directions),
2. 3 passes through register 09, identical,
3. 1 pass through register 08 (the other burst-read path), identical,
4. every 1 KB sector read on its own, in shuffled order, the way `write` reads them. In order, a
   fast read that ignored its address and carried on would still match,
5. with `-i`, the comparison with a dump from an earlier power cycle. `--ignore 3B800` leaves out
   the area the EC firmware rewrites itself (0x3B800 to the end on the T14s).

A failed check names the lines involved: `PD2 (FFC pin 11) 37x 0->1 0x 1->0` (one line, always
the same way) is that wire or joint. All lines flipping both ways is real content. On success it
saves the dump as `verify-<chip>-<date>.bin`. That is the flash as it is right now, so use it as
the source of the test below. Exit code 0 means every check passed, 2 means one didn't.

`write` replaces the EC firmware with the contents of a file. Do the power-up entry and the write
in one command:

```sh
isp --enter powerup write -i new.bin
```

### One CPU halt per power cycle

The halt (DBGR 0x27 ← 0x81) is a CPU reset. On the T14s, a second one sent by a later command let
the EC firmware run for a moment: it erased a data sector that had just been written and
verified, and left the e-flash ignoring erase and program until the next power cycle. So only
`enter` (or `--halt`) halts the CPU, once, and records it in `~/.cache/ite-pp-isp/session.json`.
`id`, `read`, `verify` and `write` never reset it. `write` refuses to run without that record
(`--dry-run` only warns). `safe` and `lines` end the session, since they come before unplugging AC.

What `write` does, in order:

1. Checks that the EC CPU was halted in this power cycle, detects the chip and checks the address
   echo. It stops if a data bit doesn't answer, or if the e-flash isn't the 8315 type
   (id `ff ff fe`).
2. Checks the file: it must be a full image (256 KB here), not blank, with the ITE signature
   (a run of `0xA5`) in its first 4 KB. `--force` overrides the content checks, not the size.
3. Reads the whole flash twice, stops if the reads differ or the address echo fails afterwards,
   and saves it as `backup-<chip>-<date>.bin` (or `--backup FILE`).
4. Lists the 1 KB sectors that differ from the file. Without `-R`, the EC data area (0x3B800 to
   the end on the T14s) is kept as it is: the EC firmware rewrites it while it runs, so a dump
   only holds a snapshot of it, maybe taken in the middle of an update. `--with-data-area` writes
   it too. `--dry-run` stops here.
5. Asks you to type `WRITE` (`--yes` skips the question).
6. Zeroes the protect path (EC 0x20A0–0x20BF) and reads it back.
7. For each sector: checks the address echo and the protect path (a running EC firmware takes
   KSO1/KSO2 and sets the protect path again), erases once write enable reads set, checks it reads
   blank, programs it word by word, reads it back and compares. A failed sector is redone up to
   3 times, except when the e-flash ignores erase or program (locked) or the cleanup after a
   sector fails: then it stops at once. The sector holding address 0 (reset vector and signature)
   is written last.
8. Reads the whole range back and compares it with the file.
9. Waits 10 s (`--settle-s`) and reads the written sectors again. A change there means EC code ran.

Afterwards the EC is still halted. Run `isp safe`, unplug AC until the EC is drained (pin 21 below
about 0.5 V) and plug it back in: the EC then boots the new firmware.

**If it fails halfway:**

- `WRITE FAILED`: don't unplug anything. The EC stays halted in parallel-port mode, so you can run
  the same command again (only the sectors that still differ are written), or put the backup back
  with `isp write -i backup-….bin`.
- `WRITE STOPPED`, `RECHECK FAILED` or `DATA CHANGED AFTER THE WRITE`: this power cycle can't be
  trusted any more. Run `isp safe`, unplug AC until the EC is drained, then run the same write with
  `--enter powerup`. The power-up entry is handled by the chip's hardware, not by the firmware in
  the flash, so it works even after a partial write.

**A first test that changes nothing:** rewrite a sector with its own contents, from a dump made in
the same power cycle (the `verify-….bin` file). The flash ends up exactly as it was.

```sh
isp write -i verify-05261-….bin -R 3FC00:400 --all    # erase, program, verify, recheck
```

That sector is in the EC's data area, away from the code. Never write the data area from a dump
of an earlier power cycle: on Oct 31 a dump caught the EC firmware in the middle of an update,
and writing it back left a half-written record that the firmware erased as soon as it ran.

| Option | Effect |
|--------|--------|
| `-R BASE[:SIZE]` | Only write this hex range of the image (1 KB aligned). |
| `--all` | Rewrite every sector in the range, even unchanged ones. |
| `--dry-run` | Back up and list the sectors that would change; write nothing. |
| `--backup FILE` | Where to save the current flash. |
| `--yes` | Don't ask for confirmation. |
| `--force` | Write an image that fails the content checks. |
| `--keep-protect-path` | Don't zero EC registers 0x20A0–0x20BF first. By default `write` zeroes them, as iteflash does; on the ThinkPad IT5261 the erase is silently ignored without it. |
| `--word-delay-us N` | Wait after each 2-byte word (default 20). Raised automatically when a sector doesn't read back as written. |
| `--with-data-area` | Also write the EC data area (T14s: 0x3B800–0x3FFFF). |
| `--settle-s S` | Seconds to wait before reading the written sectors again (default 10, 0 skips it). |
| `--halt` | Halt the EC CPU first: only after `enter --mode hotplug`, see above. |
| `--read-reg 08` | Burst-read register for the backup and the checks. |

`write` never sends chip erase (`0x60`) or status register writes (`0x50`/`0x01`). It uses only
write enable/disable, 1 KB sector erase (`0xD7`), AAI word program (`0xAD`) and read status.

## Troubleshooting

| Symptom | What to try |
|---------|-------------|
| `Pico not found` | Check the cable and the OTG adapter. `lsusb` should list `1209:0001`. Flash the `.uf2` again. |
| `cannot open device … permissions` | Install the udev rule, then replug. |
| `libusb not found` | `sudo apt install libusb-1.0-0` |
| `NOT_ARMED` | Run `enter` first, or add `--enter powerup`. |
| `NWAIT_TIMEOUT` on everything | Run `lines` (unpowered) to check wiring and GND. Use the power-up entry. Then run `probe` (it tries `--no-handshake` and `--nwait-invert`). |
| `regs 00..02` are `ff ff ff` or `00 00 00` | The EC is not in DBGR/EPP mode. Check the FFC orientation, then redo `enter --mode powerup`: the EC must be fully discharged first, and AC must go in while the tool shows "PLUG IN AC NOW". |
| A `probe` with handshake and the slow timing says `OK`, but the data is `ff` | The EC is still scanning the keyboard: its pulses on BUSY (KSO9) fake the handshake. Same fix as the row above. |
| `Address echo FAILED: PD1 … stuck at 1` | The EC firmware has switched those KBS pins to GPIO, which overrides EPP (seen on KSO1/KSO2 of an IT5261 while its firmware ran). `id` and `read` refuse to continue, since register numbers would be corrupted. Unplug AC and redo `enter`: it halts the EC before the firmware runs. Waiting for the pins to come back is unreliable (with the laptop off they stayed taken for 10 minutes). |
| `Unknown EFLASH ID` | Chip ID works but follow mode doesn't. Try `--read-reg 08` with `--ignore-flash-id` and look at the dump. |
| The two passes differ | Use slower timing, for example `--setup-ns 2000 --hold-ns 1000`. |
| Dump is all `FF` / every page identical | The read address isn't advancing. Try `--read-reg 08`. |

**If it still doesn't work**, open an issue or send back the output of these commands. That is
enough to adapt the protocol:

```sh
isp -v hello
isp lines            # once unpowered, once powered
isp enter --mode powerup ; isp probe --all ; isp regs --end 0f
```

## Development

- **Tests:** run `cd host && python3 -m unittest discover -s tests -v`. They need no hardware.
  - A simulated Pico + EC runs the full read path (`--sim file.bin` works on any command).
  - The real firmware batch executor is compiled for the host with a stubbed SDK and checked for
    framing and error handling.
- **Wire protocol:** defined in `firmware/proto.h`, mirrored in `host/itepp/proto.py`. A test
  checks that the two agree.
- **Background and evidence:** see [docs/PROTOCOL.md](docs/PROTOCOL.md).

```
firmware/   Pico firmware (C, Pico SDK, TinyUSB vendor class)
  main.c      USB loop + batch executor     epp.c    EPP cycles
  lines.c     pin control + entry strap     proto.h  wire protocol
host/       Python tool (pyusb)
  ite_pp_isp.py   CLI
  itepp/          proto, link (USB), sim, epp, dbgr, flash, dump, diag, program, verify
docs/PROTOCOL.md  how the ITE parallel ISP works, sources, open questions
```

## Credits

- [I2C-Pico-USB-ITE-EC-prog](https://github.com/JohnDoe150489/I2C-Pico-USB-ITE-EC-prog) (MIT) and
  Daniel Quadros' I2C-Pico-USB (MIT): structure and the USB/TinyUSB groundwork.
- Chromium OS EC `util/iteflash.cc` (BSD): the DBGR register sequences.
- ITE IT5570 datasheet §7.23–7.24 and the Vertyanov programmer profile list: pinout and entry
  strap.

MIT License. See [LICENSE](LICENSE).
