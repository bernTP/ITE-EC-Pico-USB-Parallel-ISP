"""ITE DBGR register access over EPP.

Ported from Chromium EC util/iteflash.cc and iteflash-i2cpicousb.py.
Over I2C, DBGR uses a command address (0x5A: select register) and a
data address (0x35: read/write it). Over EPP the same roles map to the
address cycle and the data cycle (confirmed on an IT5261).

An EPP address read returns the complement of the last address written,
which tests all 8 data lines in both directions without touching a register.
"""

import time

from . import proto
from .link import OpError
from .proto import Batch

# EC memory window (D2EC): 0x2F = address high, 0x2E = address low,
# 0x30 = data, 0x80 = address bits 23..16 on chips with 3-byte addressing
REG_MEM_BANK = 0x80
REG_MEM_HI = 0x2F
REG_MEM_LO = 0x2E
REG_MEM_DATA = 0x30
REG_CPU = 0x27

V2_SIZES_KB = [128, 192, 256, 384, 512, 0, 1024]
IT5XXXX_SIZES_KB = {0x01: 128, 0x02: 256, 0x0E: 512, 0x0F: 1024}

# Address echo patterns: every data bit is seen at 0 and at 1
ECHO_PATTERNS = (0x00, 0xFF, 0x55, 0xAA)


class DetectError(Exception):
    pass


class BusError(DetectError):
    """Some data bits don't answer: register numbers would be corrupted."""


def bus_report(stuck_high, stuck_low):
    """Text for the result of Dbgr.check_bus()."""
    if not (stuck_high or stuck_low):
        return 'Address echo OK: all 8 data bits answer.'
    parts = []
    for mask, level in ((stuck_high, 1), (stuck_low, 0)):
        for bit in range(8):
            if mask >> bit & 1:
                parts.append(f'PD{bit} (FFC pin {proto.LINE_FFC[bit]}) stuck at {level}')
    return 'Address echo FAILED: ' + ', '.join(parts)


class ChipInfo:
    def __init__(self):
        self.chip_id = 0
        self.ver = 0
        self.flash_size = 0
        self.flash_cmd_v2 = False
        self.addr_3bytes = False
        self.it5xxxx = False
        self.instruction_set_v2 = False
        self.deep_rescue = False
        self.size_code = None

    def __str__(self):
        s = (f'CHIPID {self.chip_id:05x}, CHIPVER {self.ver:02x}, '
             f'Flash size {self.flash_size // 1024} kB')
        if self.it5xxxx:
            s += f' (IT5xxxx, size code {self.size_code:#04x})'
        return s


class Dbgr:
    def __init__(self, link):
        self.link = link
        self.info = ChipInfo()

    # ─── Primitives ─────────────────────────────────────────────────
    def write_regs(self, pairs):
        self.link.run(Batch().reg_writes(pairs))

    def read_regs(self, regs):
        b = Batch()
        for r in regs:
            b.reg_read(r)
        return list(self.link.run(b).data)

    def check_bus(self):
        """Address-write/read echo. Returns (stuck_high, stuck_low) bit masks.

        The EC firmware can switch KBS pins to GPIO, which overrides EPP
        (seen on KSO1/KSO2 of an IT5261): those bits then read 1 and are
        received as 1, so register numbers get corrupted."""
        b = Batch()
        for v in ECHO_PATTERNS:
            b.aw(v).ar()
        got = self.link.run(b).data
        stuck_high = stuck_low = 0
        for v, r in zip(ECHO_PATTERNS, got):
            want = ~v & 0xFF
            stuck_high |= r & ~want
            stuck_low |= want & ~r
        return stuck_high & 0xFF, stuck_low & 0xFF

    def bus_clean(self):
        """check_bus() without exceptions: False while the EC doesn't answer."""
        try:
            return self.check_bus() == (0, 0)
        except OpError:
            return False

    def halt_on_clean_bus(self, timeout_s=60, stable_s=3.0, settle_s=0.5,
                          kick_after_s=2.0, max_kicks=5, log=print):
        """Reset and halt the CPU (27<-81, iteflash's IT5xxxx halt) at the first
        clean address echo, then check the bus stays clean.

        The running EC firmware can take KSO1/KSO2 as GPIO (IT5261 on a
        ThinkPad), and then nothing can be sent reliably. If the bus stays held
        with bits stuck at 1 only, register 0x27 still arrives intact (its bits
        1 and 2 are already 1) and 0x81 arrives as 0x87: that still resets the
        EC, the firmware starts again with the pins at their defaults, and the
        next clean echo gets the real halt. A halt that doesn't hold is retried.
        Returns the number of halts sent."""
        t0 = time.monotonic()
        halts = kicks = 0
        dirty_since = None
        while time.monotonic() - t0 < timeout_s:
            try:
                high, low = self.check_bus()
            except OpError:
                high = low = None                    # not answering (in reset)
            if (high, low) != (0, 0):
                now = time.monotonic()
                if high and not low and kicks < max_kicks:
                    dirty_since = dirty_since or now
                    if now - dirty_since > kick_after_s:
                        kicks += 1
                        log(f'{bus_report(high, low)}: resetting the EC for a clean window')
                        self.link.run(Batch().reg_write(REG_CPU, 0x81), check=False)
                        dirty_since = None
                else:
                    dirty_since = None
                time.sleep(0.001)
                continue
            dirty_since = None
            b = Batch()
            for v in ECHO_PATTERNS:
                b.aw(v).ar()
            if not self.link.run(b.reg_write(REG_CPU, 0x81), check=False).ok:
                continue
            halts += 1
            t1 = time.monotonic()
            held = True
            while time.monotonic() - t1 < stable_s:
                if not self.bus_clean() and time.monotonic() - t1 > settle_s:
                    held = False
                    break
                time.sleep(0.02)
            if held:
                return halts
            log(f'halt #{halts} did not hold (EC firmware running again), retrying')
        raise BusError(f'no lasting clean bus within {timeout_s:.0f}s '
                       f'({halts} halt(s), {kicks} reset(s) sent)')

    def _mem_select(self, b, addr, bank):
        if bank:
            b.reg_write(REG_MEM_BANK, (addr >> 16) & 0xFF)
        b.reg_write(REG_MEM_HI, (addr >> 8) & 0xFF)
        b.reg_write(REG_MEM_LO, addr & 0xFF)

    def ec_read(self, addr, bank=None):
        """Read one byte of EC memory / registers through DBGR."""
        if bank is None:
            bank = self.info.addr_3bytes or addr > 0xFFFF
        b = Batch()
        self._mem_select(b, addr, bank)
        b.reg_read(REG_MEM_DATA)
        return self.link.run(b).data[0]

    def ec_write(self, addr, val, bank=None):
        """Write one byte of EC memory / registers (never flash)."""
        if bank is None:
            bank = self.info.addr_3bytes or addr > 0xFFFF
        b = Batch()
        self._mem_select(b, addr, bank)
        b.reg_write(REG_MEM_DATA, val)
        self.link.run(b)

    def _ec_addr(self, addr16):
        """EC register address as iteflash.cc builds it (bank 0xF0 if 3-byte)."""
        return (0xF00000 | addr16) if self.info.addr_3bytes else addr16

    # ─── Chip identification (check_chipid) ─────────────────────────
    def detect(self):
        """Chip ID and flash size. Never resets or halts the CPU: that is done
        once per power cycle (halt_on_clean_bus), as a second reset can let
        the EC firmware run."""
        info = ChipInfo()
        self.info = info
        # before any register write: a stuck bit would send it elsewhere
        stuck = self.check_bus()
        if any(stuck):
            raise BusError(bus_report(*stuck) + '. The EC firmware may hold these pins as '
                           'GPIO: power-cycle and run `enter --mode powerup` again.')
        hi, lo, ver = self.read_regs([0x00, 0x01, 0x02])
        cid = (hi << 8) | lo
        info.ver = ver

        if (cid & 0xFF00) != 0x8300:
            cid |= self.ec_read(0xF02085, bank=True) << 16
            # Boot ROM deep-rescue mode: core clock domain gated
            if (cid & 0xFF00F) in (0x01002, 0x02002):
                cid |= 0x80000
                info.deep_rescue = True
            if (cid & 0xF000F) in (0x80001, 0x80002):
                info.flash_cmd_v2 = True
                info.addr_3bytes = True
                info.instruction_set_v2 = (cid & 0xF00F) == 0x2002
            # iteflash expects 0x5 in the 3rd byte; an IT5261 reads 52 61 02
            # with 0x00 there, but its size code is at the IT5xxxx address
            elif (cid & 0xF0000) == 0x50000 or (cid & 0xF000) == 0x5000:
                info.it5xxxx = True
                info.flash_cmd_v2 = True
                info.addr_3bytes = True
                info.size_code = self.ec_read(0xF01080, bank=True)
            else:
                info.chip_id = cid
                raise DetectError(f'invalid chip id {cid:05x} '
                                  f'(raw regs 00..02 = {hi:02x} {lo:02x} {ver:02x})')
        else:
            info.flash_cmd_v2 = (ver & 0x0F) >= 0x03

        info.chip_id = cid
        if info.it5xxxx:
            info.flash_size = IT5XXXX_SIZES_KB.get(info.size_code & 0x0F, 0) * 1024
        elif info.flash_cmd_v2:
            i = (ver & 0xF0) >> 5
            info.flash_size = (V2_SIZES_KB[i] if i < len(V2_SIZES_KB) else 0) * 1024
        else:
            info.flash_size = (128 + (ver & 0xF0)) * 1024
        if info.flash_size == 0:
            raise DetectError(f'invalid flash size ({info})')
        return info

    # ─── Optional EC register writes (not flash) ────────────────────
    def disable_watchdog(self):
        """ETWCFG: stop the EC watchdog (PP mode already does it in hardware)."""
        reg = 0x1F85 if self.info.instruction_set_v2 else 0x1F05
        self.ec_write(self._ec_addr(reg), 0x30, bank=self.info.addr_3bytes)

    def disable_protect_path(self):
        """iteflash dbgr_disable_protect_path: zero EC 0x20A0..0x20BF, which
        EC firmware can use to keep DBGR from erasing or programming."""
        for i in range(32):
            self.ec_write(self._ec_addr(0x20A0 + i), 0x00, bank=self.info.addr_3bytes)

    def protect_path(self):
        """EC 0x20A0..0x20BF, read in one batch: all 0 once disabled. A value
        that comes back means EC firmware is running and set it again."""
        b = Batch()
        for i in range(32):
            self._mem_select(b, self._ec_addr(0x20A0 + i), self.info.addr_3bytes)
            b.reg_read(REG_MEM_DATA)
        return self.link.run(b).data

    def reset_gpio(self):
        self.ec_write(self._ec_addr(0x2007), 0x02, bank=self.info.addr_3bytes)

    def exit(self):
        self.ec_write(self._ec_addr(0x1C08), 0x10, bank=self.info.addr_3bytes)
