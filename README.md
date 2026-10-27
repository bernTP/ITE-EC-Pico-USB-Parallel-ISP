# ITE EC Pico USB Parallel ISP

This project reads the embedded flash of an **ITE IT5261VG-256** embedded controller (EC) through
the laptop's **keyboard FFC connector**. It uses the ITE "parallel ISP" path: the chip's debugger
(DBGR) talks IEEE 1284 EPP over the KSI/KSO keyboard-scan pins. A **Raspberry Pi Pico** acts as
the adapter, and any Linux machine with Python, such as a **Raspberry Pi Zero**, drives it over USB
and writes a `.bin`.

The tool is **read-only**. It never sends erase, write-enable or program commands to the flash.

It is inspired by [I2C-Pico-USB-ITE-EC-prog](https://github.com/JohnDoe150489/I2C-Pico-USB-ITE-EC-prog),
which drives the same DBGR block over I2C/SMBus. Here the transport is the keyboard connector
instead.

> **Status: not yet tested on real hardware.**
> - **Documented by ITE:** the pin roles, the EPP transport and the entry strap (IT5570 datasheet,
>   same EC family and ISP method).
> - **Ported:** the register-level protocol comes from Chromium's `iteflash`.
> - **Not confirmed:** that EPP address and data cycles map onto the DBGR registers exactly as the
>   I2C path does. That assumption has not been checked on a real chip.
>
> If the first run fails, see [Troubleshooting](#troubleshooting). The tool is built to try other
> settings from the command line without reflashing. [docs/PROTOCOL.md](docs/PROTOCOL.md) has all
> the evidence.

```
 laptop keyboard FFC ──15 wires + GND──> Pico (firmware/) ──USB──> Pi Zero (host/ite_pp_isp.py) ──> dump.bin
```

## Hardware

**Parts**
- Raspberry Pi Pico (RP2040) or Pico 2 (RP2350).
- Raspberry Pi Zero / Zero W (or any Linux PC) and a micro-USB OTG adapter.
- An FFC breakout board that matches the laptop's keyboard connector (pitch and pin count), plus
  jumper wires.
- Resistors: 2 × 1 kΩ (KSI4/KSI5) and 13 × 470 Ω (all other lines).

**Wiring** (IT5261VG-256 / NM-C791 keyboard connector pinout)

| FFC pin | EC pin | Parallel-port role  | Series R | Pico GPIO | Pico physical pin |
|--------:|--------|---------------------|---------:|-----------|------------------:|
| 7       | KSO0   | PD0                 | 470 Ω    | GP0       | 1  |
| 13      | KSO1   | PD1                 | 470 Ω    | GP1       | 2  |
| 11      | KSO2   | PD2                 | 470 Ω    | GP2       | 4  |
| 14      | KSO3   | PD3                 | 470 Ω    | GP3       | 5  |
| 10      | KSO4   | PD4                 | 470 Ω    | GP4       | 6  |
| 17      | KSO5   | PD5                 | 470 Ω    | GP5       | 7  |
| 15      | KSO6   | PD6                 | 470 Ω    | GP6       | 9  |
| 16      | KSO7   | PD7                 | 470 Ω    | GP7       | 10 |
| 9       | KSI0   | STB# (EPP nWrite)       | 470 Ω | GP8  | 11 |
| 6       | KSI1   | AFD# (EPP nDataStrobe)  | 470 Ω | GP9  | 12 |
| 8       | KSI2   | INIT# (EPP nInit)       | 470 Ω | GP10 | 14 |
| 1       | KSI3   | SLIN# (EPP nAddrStrobe) | 470 Ω | GP11 | 15 |
| 5       | KSI4   | entry strap (low)       | 1 kΩ  | GP12 | 16 |
| 12      | KSI5   | entry strap (low)       | 1 kΩ  | GP13 | 17 |
| 20      | KSO9   | BUSY (EPP nWait)        | 470 Ω | GP14 | 19 |
| —       | GND    | **board ground**        | —     | GND  | 3, 8, 13, 18 … |

All other FFC pins are not connected. The FFC carries **no ground**, so take GND from the board,
for example from a USB connector shell or a screw hole.

The pin numbers above are the board connector's. Depending on how the breakout is mounted, the
numbering can come out **mirrored**. Check one or two pins with a continuity tester, and use the
`lines` command.

The pin mapping is set in `firmware/hwconfig.h`. PD0–PD7 must stay on 8 consecutive GPIOs.

### Safety

- **3.3 V logic only.** Before connecting the Pico, power the board and measure an idle KSI pin
  against GND. It should read about 3.3 V. If it reads 5 V, stop.
- **Battery out.** Power the laptop board from the AC adapter only, as the original repo also
  advises.
- **Keep the series resistors.** The EC drives the KSO pins for keyboard scanning until it switches
  into parallel-port mode. The resistors limit the current when both sides drive a line at once.
- **The Pico drives nothing at boot.** All lines are high-impedance until you run `enter`. When
  you unplug USB, the firmware releases all lines.
- **In `--mode powerup`,** only KSI4/KSI5 are driven, and they are driven low. The other lines get
  weak pull-ups, so an unpowered EC is never driven high.

## Firmware (Pico)

Prebuilt images are in `firmware/prebuilt/`:
- `ite_pp_isp_pico.uf2` for the Pico (RP2040)
- `ite_pp_isp_pico2.uf2` for the Pico 2 (RP2350)

To flash, hold **BOOTSEL** while you plug in the Pico, then copy the `.uf2` to the `RPI-RP2` /
`RP2350` drive. The Pico shows up as USB device `1209:0001` ("ite-pp-isp"). Its LED is on while
the strap is driven.

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

1. **Check the adapter.**

   ```sh
   isp hello
   ```

2. **Check the wiring.** Run `lines` first with the board unpowered, then again with AC plugged in:

   ```sh
   isp lines
   ```

   Unpowered, connected lines usually read `low` with a pull-up, because the dead EC clamps them.
   Powered, they read `high` or `toggling` (keyboard scan). A line that reads `floating` while the
   board is powered is not connected, or the FFC is mirrored.

3. **Enter DBGR/EPP mode.**

   ```sh
   isp enter --mode hotplug
   ```

   This works with the board already powered (hot-plug). It prints DBGR registers 00..02; for an
   IT5261 expect something like `52 61 xx`.

   If that shows a timeout or `ff ff ff`, unplug AC and use the power-up entry instead:

   ```sh
   isp enter --mode powerup
   ```

   The tool tells you when to plug AC back in, and detects the power-up. This entry also works when
   the EC firmware blocks hot-plug.

4. **If `enter` didn't show a sensible ID**, find the EPP settings that work:

   ```sh
   isp probe
   ```

   Then add the suggested flags (for example `--swap-strobes`) to every later command.

5. **Identify the chip.**

   ```sh
   isp id
   ```

   Expected output: `CHIPID 5xxxx …, Flash size 256 kB` and `EFLASH TYPE = 8315` (or `KGD`).

6. **Dump the flash.**

   ```sh
   isp read -o it5261.bin
   ```

   This reads the flash twice and compares the two passes, then writes `it5261.bin`. It also
   prints a sha256, a 0xFF/0x00 sanity check and the ITE signature location if it finds one.
   If the passes differ, both are saved and the tool exits with code 2.

The Pico keeps the strap applied between commands. `isp safe` releases every line, and `lines`
does too, so run `enter` again after either one. You can also put `--enter hotplug` (or
`powerup`) in front of any command to do the entry first:

```sh
isp --enter hotplug read -o it5261.bin
```

### Commands

| Command | Purpose |
|---------|---------|
| `hello` | Check that the adapter and firmware answer. |
| `lines [--samples N]` | Wiring / power check of the 15 lines. Leaves them released. |
| `enter --mode hotplug\|powerup` | Apply the DBGR/EPP entry strap (KSI[5,4,2,1] = 0011b). |
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
| `Pico ISP adapter not found` | Check the cable and the OTG adapter. `lsusb` should list `1209:0001`. Flash the `.uf2` again. |
| `cannot open device … permissions` | Install the udev rule, then replug. |
| `libusb not found` | `sudo apt install libusb-1.0-0` |
| `NOT_ARMED` | Run `enter` first, or add `--enter hotplug`. |
| `NWAIT_TIMEOUT` on everything | Run `lines` to check wiring and GND. Try the power-up entry. Then run `probe` (it tries `--no-handshake` and `--nwait-invert`). |
| `regs 00..02` are `ff ff ff` or `00 00 00` | The EC is not in DBGR/EPP mode. Check the FFC orientation and the KSI4/KSI5 resistors, then try `enter --mode powerup`. |
| `Unknown EFLASH ID` | Chip ID works but follow mode doesn't. Try `--read-reg 08` with `--ignore-flash-id` and look at the dump. |
| The two passes differ | Use slower timing, for example `--setup-ns 2000 --hold-ns 1000`. |
| Dump is all `FF` / every page identical | The read address isn't advancing. Try `--read-reg 08`. |

**If it still doesn't work**, open an issue or send back the output of these commands. That is
enough to adapt the protocol:

```sh
isp -v hello
isp lines            # once unpowered, once powered
isp enter --mode hotplug ; isp probe --all ; isp regs
isp enter --mode powerup ; isp probe --all ; isp regs
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
