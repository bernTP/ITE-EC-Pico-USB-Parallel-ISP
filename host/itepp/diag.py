"""Bring-up helpers: line scan, EPP variant probe, register dump, raw ops."""

import shlex

from . import proto
from .proto import Batch

# First byte of DBGR reg 0x00 seen on known ITE ECs (0x83: IT83xx,
# 0x52/0x55/0x85/0x89: IT5xxx / IT85xx / IT89xx naming)
KNOWN_ID_HI = {0x52, 0x55, 0x56, 0x57, 0x81, 0x82, 0x83, 0x85, 0x86, 0x87, 0x89}


# ─── Line scan ──────────────────────────────────────────────────────

def scan_lines(link, samples=100, interval_us=200):
    """Sample all lines with pull-downs, then pull-ups. Leaves lines Hi-Z."""
    runs = {}
    for name, pull in (('down', proto.PULL_DOWN), ('up', proto.PULL_UP)):
        b = Batch().safe(pull).delay_us(5000)
        for _ in range(samples):
            b.pins_get().delay_us(interval_us)
        res = link.run(b)
        runs[name] = [res.u16(2 * i) for i in range(samples)]
    link.run(Batch().safe(proto.PULL_NONE))

    rows = []
    for line in range(proto.LINE_COUNT):
        bit = 1 << line
        d = {v & bit for v in runs['down']}
        u = {v & bit for v in runs['up']}
        if len(d) > 1 or len(u) > 1:
            verdict = 'toggling: driven by the EC (keyboard scan?)'
        elif d == {bit} and u == {bit}:
            verdict = 'high: pulled up / driven by a powered EC'
        elif d == {0} and u == {0}:
            verdict = 'low: driven low, or clamped by an unpowered EC'
        elif d == {0} and u == {bit}:
            verdict = 'floating: not connected, or EC pin Hi-Z'
        else:
            verdict = 'follows the opposite pull: check wiring'
        rows.append((line, _level(d, bit), _level(u, bit), verdict))
    return rows


def _level(values, bit):
    if len(values) > 1:
        return '~'
    return '1' if values == {bit} else '0'


def format_lines(rows):
    out = ['line   EC    FFC  GP    pull-dn pull-up  verdict']
    for line, d, u, verdict in rows:
        out.append(f'{proto.LINE_NAMES[line]:<6} {proto.LINE_EC[line]:<5} '
                   f'{proto.LINE_FFC[line]:>3}  GP{proto.LINE_GPIO[line]:<3} '
                   f'{d:^7} {u:^7}  {verdict}')
    return '\n'.join(out)


# ─── EPP variant probe ──────────────────────────────────────────────

def probe_variants(base):
    """EPP settings worth trying, most likely first."""
    slow = dict(setup_ns=2000, strobe_ns=5000, hold_ns=1000, timeout_us=2000)
    v = []
    for swap in (False, True):
        v.append(base.copy(handshake=True, nwait_invert=False, swap_strobes=swap))
        v.append(base.copy(handshake=True, nwait_invert=False, swap_strobes=swap,
                           no_release=True))
        v.append(base.copy(handshake=True, nwait_invert=True, swap_strobes=swap))
        v.append(base.copy(handshake=False, swap_strobes=swap, **slow))
        v.append(base.copy(handshake=True, nwait_invert=False, swap_strobes=swap,
                           **slow))
    return v


def probe_one(link, cfg):
    """Read DBGR regs 00..02 twice with one EPP config. Returns a dict."""
    b = cfg.add_to(Batch())
    for _ in range(2):
        for r in (0x00, 0x01, 0x02):
            b.reg_read(r)
    res = link.run(b, check=False)
    first, second = res.data[:3], res.data[3:6]
    score = 0
    if res.ok:
        score += 2
    if first[0] not in (0x00, 0xFF):
        score += 2
    if first == second and res.ok:
        score += 1
    if first[0] in KNOWN_ID_HI:
        score += 3
    return {'cfg': cfg, 'status': res.status_name,
            'fail': (b.describe(res.index) if not res.ok else ''),
            'regs': first, 'again': second, 'score': score}


PROBE_FULL_SCORE = 8


def probe(link, base, try_all=False):
    """Try EPP variants. Stops at the first full score unless try_all:
    swapped-strobe variants turn address writes into data writes, so
    they only run when the standard wiring does not answer."""
    results = []
    for cfg in probe_variants(base):
        results.append(probe_one(link, cfg))
        if results[-1]['score'] >= PROBE_FULL_SCORE and not try_all:
            break
    base.apply(link)  # leave the user's settings in place
    return sorted(results, key=lambda r: -r['score'])


def format_probe(results):
    out = ['score  status         regs 00..02  repeat     settings']
    for r in results:
        out.append(f'{r["score"]:>5}  {r["status"]:<13}  {r["regs"].hex(" "):<11}  '
                   f'{r["again"].hex(" "):<9}  {r["cfg"]}')
    return '\n'.join(out)


# ─── Register dump ──────────────────────────────────────────────────

def dump_regs(link, start=0x00, end=0xFF):
    b = Batch()
    for r in range(start, end + 1):
        b.reg_read(r)
    res = link.run(b, check=False)
    return res, bytes(res.data)


def format_regs(data, start=0):
    out = ['     ' + ' '.join(f'{i:02x}' for i in range(16))]
    row0 = start - start % 16
    for row in range(row0, start + len(data), 16):
        cells = []
        for i in range(16):
            a = row + i
            k = a - start
            cells.append(f'{data[k]:02x}' if 0 <= k < len(data) else '  ')
        out.append(f'{row:02x}:  ' + ' '.join(cells))
    return '\n'.join(out)


# ─── Raw op mini-language ───────────────────────────────────────────

RAW_HELP = """\
ops separated by ';' (numbers are hex unless noted):
  aw XX        EPP address write      ar          EPP address read
  dw XX        EPP data write         dr N        N data reads (decimal)
  rw RR VV     register write (aw RR; dw VV)
  rr RR        register read  (aw RR; dr 1)
  strap weak|drive   apply the entry strap
  safe [none|up|down]  release all lines
  pins         read all lines         delay US    wait (decimal us)
  init US      INIT# pulse (breaks the strap!)"""


def parse_raw(text):
    """Parse the mini-language into a Batch."""
    b = Batch()
    for part in text.split(';'):
        tok = shlex.split(part)
        if not tok:
            continue
        cmd, args = tok[0].lower(), tok[1:]

        def hx(i):
            return int(args[i], 16)

        if cmd == 'aw':
            b.aw(hx(0))
        elif cmd == 'ar':
            b.ar()
        elif cmd == 'dw':
            for i in range(len(args)):
                b.dw(hx(i))
        elif cmd == 'dr':
            b.dr(int(args[0]) if args else 1)
        elif cmd == 'rw':
            b.reg_write(hx(0), hx(1))
        elif cmd == 'rr':
            b.reg_read(hx(0))
        elif cmd == 'strap':
            mode = (args[0] if args else 'drive').lower()
            b.strap(proto.STRAP_WEAK if mode == 'weak' else proto.STRAP_DRIVE)
        elif cmd == 'safe':
            pull = {'none': proto.PULL_NONE, 'up': proto.PULL_UP,
                    'down': proto.PULL_DOWN}[(args[0] if args else 'none').lower()]
            b.safe(pull)
        elif cmd == 'pins':
            b.pins_get()
        elif cmd == 'delay':
            b.delay_us(int(args[0]))
        elif cmd == 'init':
            b.init_pulse(int(args[0]))
        else:
            raise ValueError(f'unknown raw op "{cmd}"')
    return b


def format_raw(batch, res):
    out = []
    for index, offset, length in batch.outputs:
        data = res.data[offset:offset + length]
        name = batch.names[index]
        if name == 'pins_get':
            v = data[0] | (data[1] << 8)
            bits = ' '.join(f'{proto.LINE_NAMES[i]}={(v >> i) & 1}'
                            for i in range(proto.LINE_COUNT))
            out.append(f'#{index} pins: {v:04x}  {bits}')
        else:
            out.append(f'#{index} {name}: {data.hex(" ")}')
    out.append(f'status {res.status_name}' +
               ('' if res.ok else f' at {batch.describe(res.index)}'))
    return '\n'.join(out)
