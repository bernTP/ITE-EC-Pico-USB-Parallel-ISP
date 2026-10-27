"""Simulated Pico + ITE EC, for tests and dry runs without hardware.

SimLink parses batches exactly like firmware/main.c. SimEC models the
H1 hypothesis: EPP address cycle = DBGR register index, data cycle =
register value, with the follow-mode SPI path of iteflash.c behind it.
"""

import struct

from . import proto
from .link import BaseLink

# SPI opcodes that modify flash or its status register. The tool must
# never send these; the simulator records them so tests can check.
WRITE_OPCODES = {0x01, 0x02, 0x06, 0x20, 0x50, 0x60, 0xAD, 0xD7, 0xD8, 0xC7}


class SimEC:
    def __init__(self, flash, chip=(0x52, 0x61), id3=0x05, ver=0x00,
                 size_code=None):
        self.flash = bytes(flash)
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

    # ─── EPP cycles ─────────────────────────────────────────────────
    def addr_write(self, v):
        self.addr = v

    def addr_read(self):
        return self.addr

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

    def _spi_read(self):
        if not self.cs:
            return 0xFF
        i = self.spi_idx
        self.spi_idx += 1
        if self.spi_cmd == 0x9F:
            ident = b'\xFF\xFF\xFE'
            return ident[i] if i < len(ident) else 0x00
        if self.spi_cmd == 0x05:
            return 0x00
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
    dead:    EC never answers (nWait timeouts), like a wrong wiring.
    """

    def __init__(self, ec, hotplug=True, dead=False, nwait_inv=False):
        self.ec = ec
        self.hotplug = hotplug
        self.dead = dead
        self.sim_nwait_inv = nwait_inv
        self.armed = False
        self.entered = False
        self.flags = proto.CFG_HANDSHAKE
        self.pins = 0

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
        if write:
            (self.ec.addr_write if addr else self.ec.data_write)(val)
            return proto.ST_OK, 0
        return proto.ST_OK, (self.ec.addr_read() if addr else self.ec.data_read())

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
                i += 4
            elif op == proto.OP_PINS_GET:
                # powered EC: everything idles high, nWait low (ready)
                v = (proto.LINES_MASK & ~(1 << proto.LINE_BUSY)) if run else 0
                out += struct.pack('<H', v)
            elif op == proto.OP_DELAY_US:
                if not need(4):
                    status, fail = proto.ST_BAD_OP, idx
                    break
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
