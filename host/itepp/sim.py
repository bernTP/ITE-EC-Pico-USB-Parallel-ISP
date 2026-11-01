"""Simulated Pico + ITE EC, for tests and dry runs without hardware.

SimLink parses batches exactly like firmware/main.c. SimEC models what an
IT5261 does: EPP address cycle = DBGR register index, data cycle =
register value, address read = complement of the address, with the
follow-mode SPI path of iteflash.c behind it.
"""

import struct

from . import proto
from .link import BaseLink

# SPI opcodes that modify flash or its status register. The tool must
# never send these; the simulator records them so tests can check.
WRITE_OPCODES = {0x01, 0x02, 0x06, 0x20, 0x50, 0x60, 0xAD, 0xD7, 0xD8, 0xC7}


class SimEC:
    """prog_us / erase_us: how long a word program / sector erase keeps the
    status BUSY bit set, on the clock that SimLink advances (1 us per EPP
    cycle plus delay ops). write_protect: WREN is ignored."""

    def __init__(self, flash, chip=(0x52, 0x61), id3=0x05, ver=0x00,
                 size_code=None, prog_us=0, erase_us=0, write_protect=False,
                 erase_blocked=False, busy_reads_ff=False, aai_status_ff=False):
        self.flash = bytearray(flash)
        self.regs = bytearray(256)
        self.regs[0], self.regs[1], self.regs[2] = chip[0], chip[1], ver
        if size_code is None:
            size_code = {128: 0x01, 256: 0x02, 512: 0x0E, 1024: 0x0F}.get(
                len(self.flash) // 1024, 0x00)
        self.ec_mem = {0xF02085: id3, 0xF01080: size_code}
        self.addr = 0
        self.bank = 0
        self.mem_hi = 0
        self.mem_lo = 0
        self.ecindar = [0, 0, 0, 0]
        self.cs = False
        self.spi_cmd = None
        self.spi_in = bytearray()
        self.spi_idx = 0
        self.halted = False
        self.dangerous = []     # write/erase SPI opcodes seen
        self.ec_writes = []     # (address, value) EC memory writes
        self.prog_us = prog_us
        self.erase_us = erase_us
        self.write_protect = write_protect
        self.erase_blocked = erase_blocked  # erase ignored, then SPI reads all FF
        self.dead_spi = False
        self.busy_reads_ff = busy_reads_ff  # status reads ff while busy
        self.aai_status_ff = aai_status_ff  # status reads ff during AAI (IT5261)
        self.now_us = 0
        self.busy_until = 0
        self.wel = False
        self.aai_addr = None    # next AAI word address while in AAI mode
        self.erased = []        # sector addresses, in erase order
        self.cpu_resets = 0     # 0x27 writes with the reset bit
        self.on_reset = None    # on_reset(ec): EC firmware that runs after a reset
        self.locked = False     # WREN accepted, erase and program ignored

    # ─── EPP cycles ─────────────────────────────────────────────────
    def addr_write(self, v):
        self.addr = v

    def addr_read(self):
        return ~self.addr & 0xFF

    def data_write(self, v):
        r = self.addr
        if 4 <= r <= 7:
            self.ecindar[r - 4] = v
        elif r == 0x08:
            self._ecinddr_write(v)
        elif r == 0x80:
            self.bank = v
        elif r == 0x2F:
            self.mem_hi = v
        elif r == 0x2E:
            self.mem_lo = v
        elif r == 0x30:
            self.ec_writes.append((self._mem_addr(), v))
            self.ec_mem[self._mem_addr()] = v
        elif r == 0x27:
            self.halted = bool(v & 0x01)
            if v & 0x80:
                self.cpu_resets += 1
                if self.on_reset:
                    self.on_reset(self)
        elif r > 0x02:
            self.regs[r] = v

    def data_read(self):
        r = self.addr
        if r == 0x08:
            if self._follow() and self.ecindar[1] == 0xFD:
                return self._spi_read()
            return 0xFF
        if r == 0x09:
            return self._spi_read()
        if r == 0x30:
            return self.ec_mem.get(self._mem_addr(), 0x00)
        return self.regs[r]

    # ─── Internals ──────────────────────────────────────────────────
    def _mem_addr(self):
        return (self.bank << 16) | (self.mem_hi << 8) | self.mem_lo

    def _follow(self):
        return (self.ecindar[3] & 0x7F) == 0x7F and self.ecindar[2] == 0xFF

    def _ecinddr_write(self, v):
        if not self._follow():
            return
        if self.ecindar[1] == 0xFE:
            if self.cs:
                self._spi_end()
            self.cs = False
        elif self.ecindar[1] == 0xFD:
            if not self.cs:
                self.cs = True
                self.spi_cmd = v
                self.spi_in = bytearray()
                self.spi_idx = 0
                if v in WRITE_OPCODES:
                    self.dangerous.append(v)
            else:
                self.spi_in.append(v)

    def _busy(self):
        return self.now_us < self.busy_until

    def _program(self, a, data):
        for k, v in enumerate(data):
            self.flash[(a + k) % len(self.flash)] &= v    # bits only go 1 -> 0
        self.busy_until = self.now_us + self.prog_us

    def _spi_end(self):
        """FSCE# rising edge: the command just shifted in takes effect."""
        cmd, d = self.spi_cmd, self.spi_in
        if self._busy() and cmd != 0x05:
            return                       # ignored while busy, like real flash
        if cmd == 0x06 and not self.write_protect:
            self.wel = True
        elif cmd == 0x04:
            self.wel = False
            self.aai_addr = None
        elif cmd in (0xD7, 0xAD) and self.locked:
            pass
        elif cmd == 0xD7 and self.wel and self.erase_blocked:
            self.dead_spi = True
        elif cmd == 0xD7 and self.wel and len(d) >= 3:
            a = ((d[0] << 16) | (d[1] << 8) | d[2]) & ~0x3FF
            self.flash[a:a + 0x400] = b'\xFF' * 0x400
            self.erased.append(a)
            self.wel = False
            self.busy_until = self.now_us + self.erase_us
        elif cmd == 0xAD and self.aai_addr is not None and len(d) == 2:
            self._program(self.aai_addr, d)
            self.aai_addr += 2
        elif cmd == 0xAD and self.wel and self.aai_addr is None and len(d) == 5:
            a = (d[0] << 16) | (d[1] << 8) | d[2]
            self._program(a, d[3:5])
            self.aai_addr = a + 2

    def _spi_read(self):
        if not self.cs or self.dead_spi:
            return 0xFF
        i = self.spi_idx
        self.spi_idx += 1
        if self.spi_cmd == 0x9F:
            ident = b'\xFF\xFF\xFE'
            return ident[i] if i < len(ident) else 0x00
        if self.spi_cmd == 0x05:
            if (self.busy_reads_ff and self._busy()) or \
                    (self.aai_status_ff and self.aai_addr is not None):
                return 0xFF
            return ((0x01 if self._busy() else 0) | (0x02 if self.wel else 0) |
                    (0x40 if self.aai_addr is not None else 0))
        if self.spi_cmd == 0x0B and len(self.spi_in) >= 4:
            a = (self.spi_in[0] << 16) | (self.spi_in[1] << 8) | self.spi_in[2]
            # the read address only advances inside a 256 KB window,
            # which is why iteflash re-sends fast read at 0x40000
            a = (a & ~0x3FFFF) | ((a + i) & 0x3FFFF)
            return self.flash[a % len(self.flash)]
        return 0xFF


class SimLink(BaseLink):
    """Executes batches like the firmware, against a SimEC.

    hotplug: EC enters DBGR/EPP when the strap is driven while powered.
             Otherwise only the power-up pattern enters it: KSI1/KSI2 on
             pull-ups and KSI4/KSI5 driven low (AC plugged in between).
    dead:    EC never answers (nWait timeouts), like a wrong wiring.
    stuck:   data bits the EC neither drives nor receives (KBS pins taken
             as GPIO by its firmware): they read 1 and arrive as 1.
    """

    def __init__(self, ec, hotplug=True, dead=False, nwait_inv=False, stuck=0,
                 release_on_reset=False):
        self.ec = ec
        self.hotplug = hotplug
        self.dead = dead
        self.sim_nwait_inv = nwait_inv
        self.stuck = stuck
        self.release_on_reset = release_on_reset
        self.armed = False
        self.entered = False
        self.flags = proto.CFG_HANDSHAKE
        self.pins = 0
        self.oe = 0
        self.val = 0
        self.up = 0

    def _check_powerup_pattern(self):
        ksi12 = (1 << proto.LINE_AFD) | (1 << proto.LINE_INIT)
        ksi45 = (1 << proto.LINE_KSI4) | (1 << proto.LINE_KSI5)
        if (self.up & ksi12 == ksi12 and not self.oe & ksi12
                and self.oe & ksi45 == ksi45 and not self.val & ksi45):
            self.entered = True

    def _epp_ok(self):
        if self.dead or not self.entered:
            return False
        if self.flags & proto.CFG_HANDSHAKE:
            # handshake only completes when the host expects our polarity
            return bool(self.flags & proto.CFG_NWAIT_INV) == self.sim_nwait_inv
        return True

    def _cycle(self, addr, write, val=0):
        """One EPP cycle. Returns (status, value)."""
        if not self.armed:
            return proto.ST_NOT_ARMED, 0
        if not self._epp_ok():
            if self.flags & proto.CFG_HANDSHAKE:
                return proto.ST_NWAIT_TIMEOUT, 0
            return proto.ST_OK, 0xFF if not write else 0
        if self.flags & proto.CFG_SWAP_STROBE:
            addr = not addr
        if not addr and self.ec.addr == 0x08 and not self.ec._follow():
            # IT5261: ECINDDR outside follow mode never acknowledges
            return proto.ST_NWAIT_TIMEOUT, 0
        self.ec.now_us += 1
        if write:
            (self.ec.addr_write if addr else self.ec.data_write)(val | self.stuck)
            if (not addr and self.ec.addr == 0x27 and (val | self.stuck) & 0x80
                    and self.release_on_reset):
                self.stuck = 0          # EC reset: firmware restarts, pins default
            return proto.ST_OK, 0
        v = self.ec.addr_read() if addr else self.ec.data_read()
        return proto.ST_OK, v | self.stuck

    def transact(self, payload, expect):
        if payload[0] != proto.REQ_MAGIC:
            raise ValueError('bad request magic')
        n = struct.unpack_from('<H', payload, 1)[0]
        p = payload[3:3 + n]
        out = bytearray()
        status, fail = proto.ST_OK, 0xFFFF
        i = idx = 0

        def need(k):
            return i + k <= len(p)

        while i < len(p):
            op = p[i]
            i += 1
            run = status == proto.ST_OK
            st = proto.ST_OK
            if op == proto.OP_HELLO:
                hello = b'ITPP' + bytes([proto.PROTO_VERSION, 1, 0, proto.LINE_COUNT])
                out += hello if run else bytes(8)
            elif op == proto.OP_CFG:
                if not need(9):
                    status, fail = proto.ST_BAD_OP, idx
                    break
                if run:
                    self.flags = p[i + 8]
                i += 9
            elif op == proto.OP_SAFE:
                if not need(1):
                    status, fail = proto.ST_BAD_OP, idx
                    break
                if run:
                    self.armed = False
                    self.oe = 0
                    self.up = proto.LINES_MASK if p[i] == proto.PULL_UP else 0
                i += 1
            elif op == proto.OP_STRAP:
                if not need(1):
                    status, fail = proto.ST_BAD_OP, idx
                    break
                if run:
                    self.armed = p[i] == proto.STRAP_DRIVE
                    if self.hotplug or p[i] == proto.STRAP_WEAK:
                        self.entered = True
                i += 1
            elif op in (proto.OP_PINS_SET, proto.OP_PULLS):
                if not need(4):
                    status, fail = proto.ST_BAD_OP, idx
                    break
                if run:
                    a, b = struct.unpack_from('<HH', p, i)
                    if op == proto.OP_PINS_SET:
                        self.oe, self.val = a & proto.LINES_MASK, b
                    else:
                        self.up = a
                    self._check_powerup_pattern()
                i += 4
            elif op == proto.OP_PINS_GET:
                # powered EC: everything idles high, nWait low (ready)
                v = (proto.LINES_MASK & ~(1 << proto.LINE_BUSY)) if run else 0
                out += struct.pack('<H', v)
            elif op == proto.OP_DELAY_US:
                if not need(4):
                    status, fail = proto.ST_BAD_OP, idx
                    break
                if run:
                    self.ec.now_us += struct.unpack_from('<I', p, i)[0]
                i += 4
            elif op == proto.OP_WAIT_PIN:
                if not need(4) or p[i] >= proto.LINE_COUNT:
                    status, fail = proto.ST_BAD_OP, idx
                    break
                i += 4
                out.append(1 if run else 0)
            elif op == proto.OP_INIT_PULSE:
                if not need(2):
                    status, fail = proto.ST_BAD_OP, idx
                    break
                if run and not self.armed:
                    st = proto.ST_NOT_ARMED
                i += 2
            elif op in (proto.OP_AW, proto.OP_DW):
                if not need(1):
                    status, fail = proto.ST_BAD_OP, idx
                    break
                if run:
                    st, _ = self._cycle(op == proto.OP_AW, True, p[i])
                i += 1
            elif op == proto.OP_AR:
                v = 0
                if run:
                    st, v = self._cycle(True, False)
                out.append(v)
            elif op == proto.OP_DR:
                if not need(2):
                    status, fail = proto.ST_BAD_OP, idx
                    break
                k = struct.unpack_from('<H', p, i)[0]
                i += 2
                for _ in range(k):
                    v = 0
                    if run:
                        st, v = self._cycle(False, False)
                        run = st == proto.ST_OK
                    out.append(v)
            else:
                status, fail = proto.ST_BAD_OP, idx
                break
            if st != proto.ST_OK and status == proto.ST_OK:
                status, fail = st, idx
            idx += 1

        return bytes(out) + bytes([status, fail & 0xFF, fail >> 8, proto.RSP_MAGIC])
