"""USB wire protocol. Mirrors firmware/proto.h, keep both in sync."""

import struct

PROTO_VERSION = 1
REQ_MAGIC = 0xA5
RSP_MAGIC = 0x5A
MAX_BATCH = 4096
TRAILER_LEN = 4

# ─── Ops ────────────────────────────────────────────────────────────
OP_HELLO = 0x01
OP_CFG = 0x02
OP_SAFE = 0x03
OP_STRAP = 0x04
OP_PINS_SET = 0x05
OP_PINS_GET = 0x06
OP_PULLS = 0x07
OP_DELAY_US = 0x08
OP_WAIT_PIN = 0x09
OP_INIT_PULSE = 0x0A
OP_AW = 0x10
OP_AR = 0x11
OP_DW = 0x12
OP_DR = 0x13

# ─── Status ─────────────────────────────────────────────────────────
ST_OK = 0x00
ST_NWAIT_TIMEOUT = 0x01
ST_BAD_OP = 0x02
ST_NOT_ARMED = 0x03

STATUS_NAMES = {
    ST_OK: 'OK',
    ST_NWAIT_TIMEOUT: 'NWAIT_TIMEOUT',
    ST_BAD_OP: 'BAD_OP',
    ST_NOT_ARMED: 'NOT_ARMED',
}

# ─── OP_CFG flags ───────────────────────────────────────────────────
CFG_HANDSHAKE = 0x01
CFG_NWAIT_INV = 0x02
CFG_SWAP_STROBE = 0x04
CFG_NO_RELEASE = 0x08

# ─── OP_SAFE pull modes ─────────────────────────────────────────────
PULL_NONE = 0
PULL_UP = 1
PULL_DOWN = 2

# ─── OP_STRAP modes ─────────────────────────────────────────────────
STRAP_WEAK = 0
STRAP_DRIVE = 1

# ─── Lines (bit n of PINS_SET / PINS_GET) ───────────────────────────
LINE_PD0 = 0
LINE_STB = 8
LINE_AFD = 9
LINE_INIT = 10
LINE_SLIN = 11
LINE_KSI4 = 12
LINE_KSI5 = 13
LINE_BUSY = 14
LINE_COUNT = 15
LINES_MASK = 0x7FFF

LINE_NAMES = ['PD0', 'PD1', 'PD2', 'PD3', 'PD4', 'PD5', 'PD6', 'PD7',
              'STB#', 'AFD#', 'INIT#', 'SLIN#', 'KSI4', 'KSI5', 'BUSY']
LINE_EC = ['KSO0', 'KSO1', 'KSO2', 'KSO3', 'KSO4', 'KSO5', 'KSO6', 'KSO7',
           'KSI0', 'KSI1', 'KSI2', 'KSI3', 'KSI4', 'KSI5', 'KSO9']
# Keyboard FFC pin numbers for IT5261VG-256 / NM-C791
LINE_FFC = [7, 13, 11, 14, 10, 17, 15, 16, 9, 6, 8, 1, 5, 12, 20]
# Default Pico GPIOs (firmware/hwconfig.h)
LINE_GPIO = list(range(15))

OP_NAMES = {
    OP_HELLO: 'hello', OP_CFG: 'cfg', OP_SAFE: 'safe', OP_STRAP: 'strap',
    OP_PINS_SET: 'pins_set', OP_PINS_GET: 'pins_get', OP_PULLS: 'pulls',
    OP_DELAY_US: 'delay_us', OP_WAIT_PIN: 'wait_pin',
    OP_INIT_PULSE: 'init_pulse', OP_AW: 'aw', OP_AR: 'ar', OP_DW: 'dw',
    OP_DR: 'dr',
}

DR_MAX = 0xFFFF


class Batch:
    """One request. Tracks how many bytes the response will carry."""

    def __init__(self):
        self.ops = bytearray()
        self.expect = 0
        self.names = []      # one entry per op, for error messages
        self.outputs = []    # (op index, offset, length) of each output

    def _op(self, code, args=b'', out=0, name=None):
        if len(self.ops) + 1 + len(args) > MAX_BATCH:
            raise ValueError('batch too large')
        if out:
            self.outputs.append((len(self.names), self.expect, out))
        self.ops.append(code)
        self.ops += args
        self.expect += out
        self.names.append(name or OP_NAMES[code])
        return self

    def __len__(self):
        return len(self.ops)

    def encode(self):
        return bytes([REQ_MAGIC]) + struct.pack('<H', len(self.ops)) + bytes(self.ops)

    def describe(self, index):
        if 0 <= index < len(self.names):
            return f'op #{index} ({self.names[index]})'
        return f'op #{index}'

    # ─── Ops ────────────────────────────────────────────────────────
    def hello(self):
        return self._op(OP_HELLO, out=8)

    def cfg(self, setup_ns, strobe_ns, hold_ns, timeout_us, flags):
        args = struct.pack('<HHHHB', setup_ns, strobe_ns, hold_ns, timeout_us, flags)
        return self._op(OP_CFG, args)

    def safe(self, pull=PULL_NONE):
        return self._op(OP_SAFE, bytes([pull]))

    def strap(self, mode):
        return self._op(OP_STRAP, bytes([mode]))

    def pins_set(self, oe, val):
        return self._op(OP_PINS_SET, struct.pack('<HH', oe & LINES_MASK, val & LINES_MASK))

    def pins_get(self):
        return self._op(OP_PINS_GET, out=2)

    def pulls(self, up, down):
        return self._op(OP_PULLS, struct.pack('<HH', up & LINES_MASK, down & LINES_MASK))

    def delay_us(self, us):
        return self._op(OP_DELAY_US, struct.pack('<I', us))

    def wait_pin(self, line, level, timeout_ms):
        return self._op(OP_WAIT_PIN, struct.pack('<BBH', line, 1 if level else 0, timeout_ms), out=1)

    def init_pulse(self, us):
        return self._op(OP_INIT_PULSE, struct.pack('<H', us))

    def aw(self, val):
        return self._op(OP_AW, bytes([val & 0xFF]), name=f'aw {val:02x}')

    def ar(self):
        return self._op(OP_AR, out=1)

    def dw(self, val):
        return self._op(OP_DW, bytes([val & 0xFF]), name=f'dw {val:02x}')

    def dr(self, n):
        while n > 0:
            k = min(n, DR_MAX)
            self._op(OP_DR, struct.pack('<H', k), out=k, name=f'dr {k}')
            n -= k
        return self

    # ─── DBGR register helpers (EPP address = register, data = value) ─
    def reg_write(self, reg, val):
        return self.aw(reg).dw(val)

    def reg_writes(self, pairs):
        for reg, val in pairs:
            self.reg_write(reg, val)
        return self

    def reg_read(self, reg):
        return self.aw(reg).dr(1)


class Result:
    """Response of one batch."""

    def __init__(self, data, status, index):
        self.data = data
        self.status = status
        self.index = index

    @property
    def ok(self):
        return self.status == ST_OK

    @property
    def status_name(self):
        return STATUS_NAMES.get(self.status, f'0x{self.status:02x}')

    def u16(self, offset=0):
        return self.data[offset] | (self.data[offset + 1] << 8)
