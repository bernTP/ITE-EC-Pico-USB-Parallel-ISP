# Checking the keyboard connector pins with a multimeter

Do these checks **before** you connect the Pico. They answer four questions:

1. Which end of the connector is pin 1? (Is the numbering mirrored?)
2. Do the 15 pins we use really go to the EC?
3. What voltages are on them? (They must be 3.3 V logic, never 5 V.)
4. Is the finished wiring right? (Resistors in place, no shorts, ground connected.)

It takes about 20 minutes. Write your readings into the [results table](#results-table) at the
end. If something looks wrong, send that table back with the `isp lines` output.

## What you need

- A multimeter with **DC volts**, **diode test** and **continuity / ohms** modes.
- Fine probe tips. A keyboard FFC connector has a 0.5–1 mm pitch, so standard probes will short
  two pins together. Push a sewing needle into each probe tip, or, better, measure on the pads of
  your FFC breakout board.
- The pin table from the [README](../README.md#hardware) (the image of the connector).

## Before you start

- **Battery disconnected.** The board is powered only by the AC adapter, and only when a step
  says so.
- **Never measure ohms or diode mode on a powered board.** Unplug AC for steps 1–3.
- When measuring volts, put the black probe on ground and touch only one pin at a time with the
  red probe. Slipping between two pins on a powered board can damage the EC.

## Step 0: find a ground point

The keyboard connector has no ground pin, so you need one on the board:

- the metal shell of a USB or HDMI connector,
- a screw hole with a copper ring,
- the negative (GND) pin of the battery connector.

With the board **unpowered**, set the meter to continuity and check that two of these points beep
together. Use that point as **GND** for everything below, and later for the Pico's GND wire.

## Step 1: is the numbering mirrored? (unpowered, diode mode)

Every EC pin has a small protection diode to ground. In diode mode you can "see" it, which tells
you whether a connector pin really goes to the EC.

1. Set the meter to **diode test** (the ⏵| symbol).
2. Put the **red** probe on **GND** and keep it there.
3. Touch each connector pin, 1 to 32, with the **black** probe, and note the reading.

What the readings mean:

| Reading | Meaning |
|---------|---------|
| about 0.3–0.8 V | The pin is connected to a chip (probably the EC). |
| OL / 1 (open) | Nothing connected (NC). |
| 0.00 V / beeps | The pin is ground. |

Compare which pins gave a 0.3–0.8 V reading with the expected set:

```
expected (normal):   1  5  6  7  8  9 10 11 12 13 14 15 16 17 20
expected (mirrored): 13 16 17 18 19 20 21 22 23 24 25 26 27 28 32
```

- If your readings match the first line, the numbering on your breakout is correct.
- If they match the second line, the connector is **mirrored**: real pin `n` is your pin `33 − n`
  (for a 32-pin connector). Either flip the cable or swap the wiring.
- If neither matches, count the pins of the connector. The community pinout row for this board
  says `KB_pins=40`, while the image shows 32 pins. With 40 pins the mirror rule is `41 − n`.

Some NC pins may also read 0.3–0.8 V if they go to something else, such as a keyboard backlight.
That's fine, as long as all 15 expected pins read in that range and look alike.

> **This test didn't work on the NM-C791 (IT5261) board:** most pins read OL, including pins
> known to reach the EC. If you see the same, skip it. The voltage check in step 3 settled the
> orientation there, and with the Pico connected, `isp lines` on the unpowered board shows every
> connected line low (the EC clamps it).

## Step 2: check for shorts between neighbours (unpowered, continuity)

With the meter on **continuity**, touch each pair of neighbouring connector pins (1–2, 2–3, …).
None of them should beep. A beep means a solder bridge on the breakout, or two pins tied together
on the board.

## Step 3: check the voltages (powered, DC volts)

1. Plug in the **AC adapter only** (battery still out). Don't press the power button. The EC runs
   on standby power as soon as AC is in, which is the state the tool uses.
2. Set the meter to **DC volts**, 20 V range (or auto). Black probe on GND.
3. Measure each of the 15 pins.

Expected readings:

| Pins | Signal | Expected |
|------|--------|----------|
| 9, 6, 8, 1, 5, 12 | KSI0–KSI5 (inputs, pulled up by the EC) | **about 3.3 V** |
| 7, 13, 11, 14, 10, 17, 15, 16, 20 | KSO0–KSO7, KSO9 (outputs) | **0 V or 3.3 V**, or anything in between |

KSO pins vary because in standby many ECs drive all KSO pins low and wait for a key press. While
the EC scans the keyboard, the meter shows an average somewhere between 0 and 3.3 V.

**Stop if any pin reads above 3.6 V.** The Pico's GPIOs are 3.3 V only, so a 5 V pin would
damage it.

If every pin reads 0 V, the EC is probably not powered. Check that AC is plugged in and that your
ground point is really ground.

## Step 4: check the finished wiring (everything unpowered)

Do this after soldering the resistors and wires, with the AC adapter **unplugged** from the
laptop board and the Pico **not connected to USB**.

1. **Ground:** continuity between the board GND point and a Pico GND pin (physical pin 3, 8, 13
   or 18) should beep, about 0 Ω.
2. **Each line:** in ohms mode, measure from each connector pin to its Pico GPIO pin. You should
   read roughly the resistor value, about **1 kΩ** (a bit less is normal: the EC and the Pico
   are in parallel with it). A reading of 0 Ω means the resistor is bypassed. OL means a broken
   wire.
3. **No crossed lines:** between two neighbouring Pico GPIO pins you should read much more than
   2 kΩ (usually tens of kΩ or OL). About 2 kΩ means the two lines are shorted on the board side.

| FFC pin | Signal | Pico GPIO | Pico physical pin | Expected R |
|--------:|--------|-----------|------------------:|-----------:|
| 7  | PD0 / KSO0 | GP0  | 1  | 1 kΩ |
| 13 | PD1 / KSO1 | GP1  | 2  | 1 kΩ |
| 11 | PD2 / KSO2 | GP2  | 4  | 1 kΩ |
| 14 | PD3 / KSO3 | GP3  | 5  | 1 kΩ |
| 10 | PD4 / KSO4 | GP4  | 6  | 1 kΩ |
| 17 | PD5 / KSO5 | GP5  | 7  | 1 kΩ |
| 15 | PD6 / KSO6 | GP6  | 9  | 1 kΩ |
| 16 | PD7 / KSO7 | GP7  | 10 | 1 kΩ |
| 9  | STB# / KSI0 | GP8  | 11 | 1 kΩ |
| 6  | AFD# / KSI1 | GP9  | 12 | 1 kΩ |
| 8  | INIT# / KSI2 | GP10 | 14 | 1 kΩ |
| 1  | SLIN# / KSI3 | GP11 | 15 | 1 kΩ |
| 5  | KSI4 | GP12 | 16 | 1 kΩ |
| 12 | KSI5 | GP13 | 17 | 1 kΩ |
| 20 | BUSY / KSO9 | GP14 | 19 | 1 kΩ |

## Step 5: check the entry strap (powered, DC volts)

This confirms the Pico really puts the chip-entry pattern on the pins.

1. AC in, Pico on USB to the Pi.
2. Run `isp enter --mode hotplug`.
3. Measure at the connector, black probe on GND:

| Pins | Expected after `enter` |
|------|------------------------|
| 5, 12 (KSI4, KSI5) | **low**, under about 0.5 V |
| 9, 6, 8, 1 (KSI0–KSI3) | **high**, about 3.3 V |

4. Run `isp safe`. The pins should go back to the values you measured in step 3.

If KSI4/KSI5 don't go low, check their resistors and the GP12/GP13 wires. If KSI0–KSI3 stay
low, the EC may be driving them, or the wire goes to the wrong pin.

You can compare the meter with what the Pico reads: `isp raw "pins"` prints the level of all 15
lines.

## Results table

Copy this and fill it in:

```
Connector pin count: ___     Mirrored? yes / no     Ground point used: ________

FFC  signal  | diode (step 1) | volts AC-in (step 3) | ohms to Pico (step 4) | volts after enter (step 5)
  1  KSI3    |                |                      |                       |
  5  KSI4    |                |                      |                       |
  6  KSI1    |                |                      |                       |
  7  KSO0    |                |                      |                       |
  8  KSI2    |                |                      |                       |
  9  KSI0    |                |                      |                       |
 10  KSO4    |                |                      |                       |
 11  KSO2    |                |                      |                       |
 12  KSI5    |                |                      |                       |
 13  KSO1    |                |                      |                       |
 14  KSO3    |                |                      |                       |
 15  KSO6    |                |                      |                       |
 16  KSO7    |                |                      |                       |
 17  KSO5    |                |                      |                       |
 20  KSO9    |                |                      |                       |
```
