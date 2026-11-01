"""Read-path self-test: can a dump, and the checks of `write`, be trusted?

Passes that agree only rule out random errors. A data line that is open
or held for the whole read returns the same wrong bytes every time, and
`write` doesn't read one long stream: its blank checks and verifies read
single sectors, each after its own fast read command. So on top of the
passes this:
  - checks the address echo before and after (8 data bits, both ways)
  - reads once more through the other burst register (08 instead of 09)
  - reads every sector on its own, in shuffled order, and compares it with
    the long read (in order, a fast read that ignored its address and
    carried on would still match)
  - compares with a file, e.g. a dump from an earlier power cycle
Only the read-path opcodes of flash.py are sent.
"""

import random

from . import proto
from .dbgr import bus_report
from .flash import READ_REG_BLOCK, READ_REG_DATA
from .program import SECTOR, runs


def diff_offsets(a, b):
    """Offsets where a and b differ (same length)."""
    out = []
    for s in range(0, len(a), SECTOR):
        if a[s:s + SECTOR] != b[s:s + SECTOR]:
            out += [i for i in range(s, min(s + SECTOR, len(a))) if a[i] != b[i]]
    return out


def bit_report(want, got, offsets):
    """Which data lines the differences point at. One PDn always going the
    same way is a wiring fault; all bits both ways is real content."""
    up, down = [0] * 8, [0] * 8
    for i in offsets:
        for bit in range(8):
            if (want[i] ^ got[i]) >> bit & 1:
                if got[i] >> bit & 1:
                    up[bit] += 1
                else:
                    down[bit] += 1
    return ', '.join(f'PD{bit} (FFC pin {proto.LINE_FFC[bit]}) {up[bit]}x 0->1 {down[bit]}x 1->0'
                     for bit in range(8) if up[bit] or down[bit])


def describe(want, got, offsets):
    sectors = sorted({i & ~(SECTOR - 1) for i in offsets})
    return (f'{len(offsets)} bytes differ, in {runs(sectors)}; '
            f'bits: {bit_report(want, got, offsets)}')


class Report:
    """checks: (name, ok, detail) of the read path. ref: (ok, detail) of
    the comparison with a file, None without one. image: the first pass."""

    def __init__(self):
        self.checks = []
        self.ref = None
        self.image = None

    def add(self, name, ok, detail=''):
        self.checks.append((name, ok, detail))
        return ok

    @property
    def read_ok(self):
        return all(ok for _, ok, _ in self.checks)


def verify_read(dbgr, eflash, size, passes=3, ref=None, ignore=(), progress=None):
    """Run every check on [0, size). ignore: (base, size) ranges the EC
    firmware rewrites itself, left out of the comparison with ref."""
    rep = Report()
    total = passes + 2

    def cb(p):
        if progress:
            return lambda done, n: progress(p, total, done, n)
        return None

    def echo(when):
        high, low = dbgr.check_bus()
        return rep.add(f'address echo {when}', not (high or low), bus_report(high, low))

    if not echo('before reading'):
        return rep

    reads = [eflash.read(0, size, read_reg=READ_REG_BLOCK, progress=cb(p))
             for p in range(passes)]
    first = rep.image = reads[0]
    bad = []
    for k, img in enumerate(reads[1:], start=2):
        d = diff_offsets(first, img)
        if d:
            bad.append(f'pass {k}: {describe(first, img, d)}')
    rep.add(f'{passes} passes through reg 09 identical', not bad, '; '.join(bad))

    img = eflash.read(0, size, read_reg=READ_REG_DATA, progress=cb(passes))
    d = diff_offsets(first, img)
    rep.add('a pass through reg 08 reads the same', not d, describe(first, img, d) if d else '')

    order = list(range(0, size, SECTOR))
    random.Random(0).shuffle(order)
    img = bytearray(size)
    for n, a in enumerate(order):
        img[a:a + SECTOR] = eflash.read(a, SECTOR, read_reg=READ_REG_BLOCK, chunk=SECTOR)
        if progress:
            progress(passes + 1, total, (n + 1) * SECTOR, size)
    d = diff_offsets(first, img)
    rep.add(f'{len(order)} sectors read one by one, out of order, read the same', not d,
            describe(first, img, d) if d else '')

    echo('after reading')

    if ref is not None:
        def ignored(i):
            return any(b <= i < b + n for b, n in ignore)
        diffs = diff_offsets(ref, first)
        outside = [i for i in diffs if not ignored(i)]
        inside = [i for i in diffs if ignored(i)]
        if outside:
            rep.ref = (False, describe(ref, first, outside))
        elif inside:
            sectors = sorted({i & ~(SECTOR - 1) for i in inside})
            rep.ref = (True, f'identical outside the ignored ranges; {len(inside)} bytes '
                             f'differ inside them, in {runs(sectors)}')
        else:
            rep.ref = (True, 'identical')
    return rep
