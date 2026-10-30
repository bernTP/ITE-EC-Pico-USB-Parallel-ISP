# ITE EC Pico USB Parallel ISP

This project reads the embedded flash of an **ITE IT5261VG-256** embedded controller (EC) through
the laptop's **keyboard FFC connector**. It uses the ITE "parallel ISP" path: the chip's debugger
(DBGR) talks IEEE 1284 EPP over the KSI/KSO keyboard-scan pins. A **Raspberry Pi Pico**
wired to that connector does the low-level signalling, and any Linux machine with Python, such as a
**Raspberry Pi Zero**, drives the Pico over USB and writes a `.bin`.

The tool is **read-only**. It never sends erase, write-enable or program commands to the flash.

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
   low and KSI1/KSI2 sit on pull-ups, which is the pattern the EC latches at power-up.

   Expected output on an IT5261:

   ```
   Strap now driven.
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

   `id` checks the address echo first and stops if a data bit doesn't answer. It then resets and
   halts the EC CPU, so its firmware stops running (`--no-halt` skips that).

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
removed, so `isp enter --mode hotplug` re-arms the Pico in the same power cycle. You can also put
`--enter powerup` in front of any command to do the entry first:

```sh
isp --enter powerup read -o it5261.bin --no-exit
```

### Commands

| Command | Purpose |
|---------|---------|
| `hello` | Check that the Pico and its firmware answer. |
| `lines [--samples N]` | Wiring / power check of the 15 lines. Leaves them released. |
| `enter [--mode powerup\|hotplug]` | Apply the DBGR/EPP entry strap (KSI[5,4,2,1] = 0011b), then check the address echo and print regs 00..02. `powerup` (default) asks when to plug AC in. |
| `probe [--all]` | Try handshake / strobe / timing variants and rank them by how plausible the chip ID looks. |
| `regs [--start 00 --end ff]` | Dump DBGR registers through EPP. |
| `raw "rr 00; aw 01; dr 1"` | Run hand-written EPP ops (`raw help` lists them). |
| `id [--no-halt]` | Chip ID, flash size, e-flash type. |
| `read -o FILE [-R BASE[:SIZE]] [-s KB] [--passes N]` | Dump the flash. |
| `safe` | Release all lines. |

Options for `read`:

| Option | Effect |
|--------|--------|
| `--read-reg 08` | Use the alternative burst-read register. |
| `--chunk N` | Bytes per USB transfer. |
| `--no-halt` | Don't reset and halt the EC CPU. |
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
| `Address echo FAILED: PD1 … stuck at 1` | The EC firmware has switched those KBS pins to GPIO, which overrides EPP (seen on KSO1/KSO2 of an IT5261 while its firmware ran). `id` and `read` refuse to continue, since register numbers would be corrupted. Run `enter` (or `id`) again a little later, or power-cycle and redo the power-up entry. Once `id` has halted the EC, it stays clean. || `Unknown EFLASH ID` | Chip ID works but follow mode doesn't. Try `--read-reg 08` with `--ignore-flash-id` and look at the dump. |
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
  itepp/          proto, link (USB), sim, epp, dbgr, flash, dump, diag
docs/PROTOCOL.md  how the ITE parallel ISP works, sources, open questions
```

## Credits

- [I2C-Pico-USB-ITE-EC-prog](https://github.com/JohnDoe150489/I2C-Pico-USB-ITE-EC-prog) (MIT) and
  Daniel Quadros' I2C-Pico-USB (MIT): structure and the USB/TinyUSB groundwork.
- Chromium OS EC `util/iteflash.cc` (BSD): the DBGR register sequences.
- ITE IT5570 datasheet §7.23–7.24 and the Vertyanov programmer profile list: pinout and entry
  strap.

MIT License. See [LICENSE](LICENSE).
