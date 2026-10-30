#!/usr/bin/env python3
"""ite_pp_isp.py: read an ITE EC e-flash through the keyboard connector.

Parallel ISP (ITE DBGR over IEEE 1284 EPP on the KSI/KSO pins), using a
Raspberry Pi Pico running firmware/ in between. Read-only: no erase
or program command is ever sent to the flash.

Typical session (AC unplugged at the start):
    ./ite_pp_isp.py hello
    ./ite_pp_isp.py enter --mode powerup      (asks when to plug AC in)
    ./ite_pp_isp.py id
    ./ite_pp_isp.py read -o dump.bin --no-exit
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from itepp import diag, proto                              # noqa: E402
from itepp.dbgr import (Dbgr, BusError, DetectError,      # noqa: E402
                        bus_report)
from itepp.dump import analyze, dump                       # noqa: E402
from itepp.epp import EppConfig                            # noqa: E402
from itepp.flash import Eflash, FlashError                 # noqa: E402
from itepp.link import LinkError, OpError, UsbLink         # noqa: E402
from itepp.proto import Batch                              # noqa: E402
from itepp.sim import SimEC, SimLink                       # noqa: E402

PAGE = 256

TROUBLE = """\
No answer from the EC. Things to check:
  - wiring: with the board unpowered, `lines` must show connected lines low
    with the pull-up (the EC clamps them); a line reading high is not connected
  - common GND between the Pico and the laptop board
  - FFC pin order mirrored? (connector orientation)
  - entry: unplug AC and use `enter --mode powerup`. KSI1/KSI2 must be on
    pull-ups when AC goes in; hot-plug entry is blocked on some boards
  - `probe` tries other handshake / strobe settings"""


# ─── Setup ──────────────────────────────────────────────────────────

def epp_config(args):
    return EppConfig(setup_ns=args.setup_ns, strobe_ns=args.strobe_ns,
                     hold_ns=args.hold_ns, timeout_us=args.timeout_us,
                     handshake=not args.no_handshake,
                     nwait_invert=args.nwait_invert,
                     swap_strobes=args.swap_strobes,
                     no_release=args.no_release)


def connect(args):
    if args.sim:
        with open(args.sim, 'rb') as f:
            ec = SimEC(f.read())
        link = SimLink(ec, hotplug=not args.sim_powerup, dead=args.sim_dead,
                       stuck=args.sim_stuck)
    else:
        link = UsbLink(timeout_ms=args.usb_timeout)
    info = link.hello()
    if args.verbose:
        print(f'Pico: firmware {info["fw"]}, protocol v{info["proto"]}')
    epp_config(args).apply(link)
    if args.enter != 'none':
        enter(link, args.enter, interactive=not args.sim)
    return link


KSI0_3 = 0x0F << proto.LINE_STB
KSI4_5 = (1 << proto.LINE_KSI4) | (1 << proto.LINE_KSI5)


def ask(text):
    try:
        input(f'>>> {text} [Enter] ')
    except EOFError:
        raise SystemExit('no input: stopped (lines left as they are)')


def enter(link, mode, interactive=True):
    """Apply KSI[5,4,2,1] = 0011b so the EC enters DBGR/EPP."""
    if mode == 'hotplug':
        link.run(Batch().strap(proto.STRAP_DRIVE).delay_us(100000))
        print('Strap driven (KSI4/KSI5 low, STB#/AFD#/INIT#/SLIN# high).')
        return

    # powerup: the EC latches the strap once, at VSTBY power-up, and only from
    # pulls (pulled up = 1, pulled down or floating = 0, driven = unknown).
    # 1. drain: released RP2350 pins leak enough to keep an unpowered EC half
    #    alive, so every line is driven low while it discharges
    link.run(Batch().pulls(0, 0).pins_set(proto.LINES_MASK, 0))
    if interactive:
        print('AC must be UNPLUGGED (battery out). All lines are driven low so the EC')
        print('discharges. Wait until it is fully off: an unused keyboard pin such as')
        print('FFC pin 21 near 0 V on a meter, or a few minutes.')
        ask('Press Enter, then plug in AC straight away')
    # 2. pattern: KSI4/KSI5 low, pull-ups on KSI0-3 only, the rest released
    link.run(Batch().safe(proto.PULL_NONE).pulls(KSI0_3, 0).pins_set(KSI4_5, 0))
    if interactive:
        print('Pattern applied (KSI4/KSI5 low, pull-ups on KSI0-KSI3). PLUG IN AC NOW.')
        ask('Press Enter once AC is in')
    link.run(Batch().strap(proto.STRAP_DRIVE).delay_us(100000))
    print('Strap now driven.')


def progress_printer():
    t0 = time.time()

    def show(p, passes, done, total):
        dt = max(time.time() - t0, 1e-6)
        rate = (p * total + done) / dt / 1024
        sys.stderr.write(f'\r  pass {p + 1}/{passes}  {done * 100 // total:3d}%  '
                         f'{rate:6.1f} KB/s')
        if done == total and p + 1 == passes:
            sys.stderr.write('\n')
        sys.stderr.flush()
    return show


# ─── Commands ───────────────────────────────────────────────────────

def cmd_hello(link, args):
    info = link.hello()
    print(f'Pico OK: firmware {info["fw"]}, protocol v{info["proto"]}, '
          f'{info["lines"]} lines')


def cmd_safe(link, args):
    pull = {'none': proto.PULL_NONE, 'up': proto.PULL_UP, 'down': proto.PULL_DOWN}[args.pull]
    link.run(Batch().safe(pull))
    print('All lines released (inputs). The strap is no longer applied.')


def cmd_lines(link, args):
    rows = diag.scan_lines(link, samples=args.samples)
    print(diag.format_lines(rows))
    print('\nAll lines are released now: run `enter` again before talking to the EC.')


def cmd_enter(link, args):
    if args.enter == 'none':
        enter(link, args.mode, interactive=not args.sim)
    try:
        print(bus_report(*Dbgr(link).check_bus()))
    except OpError as e:
        print(f'First EPP access: {e.result.status_name}. Try `probe`.')
        return 1
    res = link.run(Batch().reg_read(0).reg_read(1).reg_read(2), check=False)
    if res.ok:
        print(f'DBGR regs 00..02: {res.data.hex(" ")}  (IT5261: 52 61 02)')
    else:
        print(f'DBGR regs 00..02: {res.status_name}. Try `probe`.')


def cmd_probe(link, args):
    if link.run(Batch().aw(0x00), check=False).status == proto.ST_NOT_ARMED:
        raise SystemExit('Lines are not armed: run `enter` first (or add --enter).')
    results = diag.probe(link, epp_config(args), try_all=args.all)
    print(diag.format_probe(results))
    best = results[0]
    if best['score'] >= 6:
        flags = best['cfg'].cli_args() or '(defaults)'
        print(f'\nBest candidate: regs {best["regs"].hex(" ")}  flags: {flags}')
    else:
        print('\nNo setting gave a plausible chip ID.')
        print(TROUBLE)


def cmd_regs(link, args):
    res, data = diag.dump_regs(link, args.start, args.end)
    print(diag.format_regs(data, args.start))
    if not res.ok:
        print(f'status {res.status_name} from register {args.start + res.index // 2:02x}')


def cmd_raw(link, args):
    if args.ops.strip().lower() == 'help':
        print(diag.RAW_HELP)
        return
    b = diag.parse_raw(args.ops)
    print(diag.format_raw(b, link.run(b, check=False)))


def detect(link, args):
    dbgr = Dbgr(link)
    try:
        info = dbgr.detect(halt=not args.no_halt)
    except BusError as e:
        # never continue: register numbers would be corrupted
        raise SystemExit(f'error: {e}')
    except DetectError as e:
        if not getattr(args, 'size', 0):
            print(f'Chip detection failed: {e}', file=sys.stderr)
            raise SystemExit(TROUBLE)
        print(f'WARNING: {e}; continuing with -s {args.size}', file=sys.stderr)
        info = dbgr.info
    if getattr(args, 'size', 0):
        info.flash_size = args.size * 1024
        print(f'Flash size overridden to {args.size} kB')
    print(info)
    return dbgr, info


def check_flash_id(link, args):
    ident = Eflash(link).read_id()
    ftype = Eflash.flash_type(ident)
    if ftype:
        print(f'EFLASH TYPE = {ftype} (id {ident[:3].hex(" ")})')
    else:
        msg = f'Unknown EFLASH ID: {ident[:3].hex(" ")}'
        if not getattr(args, 'ignore_flash_id', False):
            raise SystemExit(msg + ' (follow mode not working?) '
                             'Use --ignore-flash-id to read anyway.')
        print('WARNING: ' + msg)
    return ident


def cmd_id(link, args):
    detect(link, args)
    check_flash_id(link, args)


def parse_range(text, flash_size):
    if not text:
        return 0, flash_size
    parts = text.split(':')
    base = int(parts[0], 16)
    size = int(parts[1], 16) if len(parts) > 1 and parts[1] else flash_size - base
    return base, size


def cmd_read(link, args):
    dbgr, info = detect(link, args)
    if args.reset_gpio:
        dbgr.reset_gpio()
    check_flash_id(link, args)
    if args.wdt_disable:
        dbgr.disable_watchdog()

    base, size = parse_range(args.range, info.flash_size)
    if size <= 0 or base + size > info.flash_size:
        raise SystemExit(f'range 0x{base:X}+0x{size:X} is outside the '
                         f'{info.flash_size // 1024} kB flash')
    if base % PAGE:
        raise SystemExit('range base must be a multiple of 0x100')

    print(f'Reading {size} bytes at 0x{base:06X}, {args.passes} pass(es)...')
    t0 = time.time()
    images, diffs = dump(Eflash(link), base, size, passes=args.passes,
                         read_reg=args.read_reg, chunk=args.chunk,
                         progress=progress_printer())
    dt = time.time() - t0

    with open(args.output, 'wb') as f:
        f.write(images[0])
    print(f'Wrote {args.output} ({size} bytes, {dt:.1f}s)')
    for note in analyze(images[0], base):
        print('  ' + note)

    if not args.no_exit:
        dbgr.exit()

    if diffs:
        stem, ext = os.path.splitext(args.output)
        for i, img in enumerate(images[1:], start=2):
            path = f'{stem}.pass{i}{ext or ".bin"}'
            with open(path, 'wb') as f:
                f.write(img)
            print(f'Wrote {path}')
        first = ', '.join(f'0x{base + d:06X}' for d in diffs[:8])
        print(f'VERIFY FAILED: {len(diffs)} bytes differ between passes '
              f'(first at {first}). Try slower timing (--setup-ns/--hold-ns).')
        return 2
    if args.passes > 1:
        print(f'Verify OK: {args.passes} passes identical.')
    return 0


# ─── Main ───────────────────────────────────────────────────────────

def build_parser():
    p = argparse.ArgumentParser(
        description='Read an ITE EC e-flash over the keyboard connector '
                    '(parallel ISP / DBGR over EPP) with a Raspberry Pi Pico. Read-only.',
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=TROUBLE)

    g = p.add_argument_group('EPP tuning (any command)')
    g.add_argument('--no-handshake', action='store_true',
                   help='ignore BUSY/nWait, use fixed delays')
    g.add_argument('--nwait-invert', action='store_true', help='invert BUSY/nWait polarity')
    g.add_argument('--swap-strobes', action='store_true',
                   help='AFD# is the address strobe and SLIN# the data strobe')
    g.add_argument('--no-release', action='store_true',
                   help="don't wait for nWait to return after a strobe")
    g.add_argument('--setup-ns', type=int, default=500, metavar='NS')
    g.add_argument('--strobe-ns', type=int, default=1000, metavar='NS',
                   help='strobe width without handshake')
    g.add_argument('--hold-ns', type=int, default=250, metavar='NS')
    g.add_argument('--timeout-us', type=int, default=200, metavar='US',
                   help='nWait timeout per handshake step')

    g = p.add_argument_group('general')
    g.add_argument('--enter', choices=['none', 'hotplug', 'powerup'], default='none',
                   help='apply the entry strap before the command')
    g.add_argument('--usb-timeout', type=int, default=2000, metavar='MS')
    g.add_argument('-v', '--verbose', action='store_true')
    g.add_argument('--sim', metavar='BIN', help='simulate an EC holding BIN (no hardware)')
    g.add_argument('--sim-powerup', action='store_true', help=argparse.SUPPRESS)
    g.add_argument('--sim-dead', action='store_true', help=argparse.SUPPRESS)
    g.add_argument('--sim-stuck', type=lambda x: int(x, 16), default=0, help=argparse.SUPPRESS)

    sub = p.add_subparsers(dest='cmd', metavar='command')
    sub.required = True

    s = sub.add_parser('hello', help='check that the Pico answers')
    s.set_defaults(func=cmd_hello)

    s = sub.add_parser('safe', help='release all lines (Hi-Z)')
    s.add_argument('--pull', choices=['none', 'up', 'down'], default='none')
    s.set_defaults(func=cmd_safe)

    s = sub.add_parser('lines', help='check wiring / EC power on the 15 lines')
    s.add_argument('--samples', type=int, default=100)
    s.set_defaults(func=cmd_lines)

    s = sub.add_parser('enter', help='apply the DBGR/EPP entry strap')
    s.add_argument('--mode', choices=['hotplug', 'powerup'], default='powerup',
                   help='powerup (default): AC unplugged first, the tool says when to '
                        'plug it in. hotplug: board already powered')
    s.set_defaults(func=cmd_enter)

    s = sub.add_parser('probe', help='try EPP settings, report which one answers')
    s.add_argument('--all', action='store_true',
                   help="try every variant, don't stop at the first full answer")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser('regs', help='dump DBGR registers through EPP')
    s.add_argument('--start', type=lambda x: int(x, 16), default=0x00)
    s.add_argument('--end', type=lambda x: int(x, 16), default=0xFF)
    s.set_defaults(func=cmd_regs)

    s = sub.add_parser('raw', help='run raw ops, e.g. "aw 00; dr 4" ("help" lists ops)')
    s.add_argument('ops')
    s.set_defaults(func=cmd_raw)

    s = sub.add_parser('id', help='chip ID, flash size and e-flash type')
    s.add_argument('--no-halt', action='store_true',
                   help="don't reset+halt the EC CPU on IT5xxxx")
    s.set_defaults(func=cmd_id)

    s = sub.add_parser('read', help='dump the e-flash to a file')
    s.add_argument('-o', '--output', required=True, metavar='FILE')
    s.add_argument('-R', '--range', metavar='BASE[:SIZE]', help='hex range, e.g. 4000:10000')
    s.add_argument('-s', '--size', type=int, default=0, metavar='KB',
                   help='override the detected flash size')
    s.add_argument('--passes', type=int, default=2, help='read N times and compare (default 2)')
    s.add_argument('--read-reg', type=lambda x: int(x, 16), default=0x09, metavar='RR',
                   help='DBGR register for burst reads: 09 (default, like iteflash) or 08')
    s.add_argument('--chunk', type=int, default=4096, help='bytes per USB batch')
    s.add_argument('--no-halt', action='store_true',
                   help="don't reset+halt the EC CPU on IT5xxxx")
    s.add_argument('--wdt-disable', action='store_true',
                   help='also write the EC watchdog off (PP mode does it in hardware)')
    s.add_argument('--reset-gpio', action='store_true', help='reset EC GPIOs to default first')
    s.add_argument('--ignore-flash-id', action='store_true',
                   help='read even if the e-flash ID is not recognised')
    s.add_argument('--no-exit', action='store_true', help="don't send DBGR exit at the end")
    s.set_defaults(func=cmd_read)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if getattr(args, 'passes', 1) < 1:
        raise SystemExit('--passes must be >= 1')
    try:
        link = connect(args)
    except LinkError as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    try:
        return args.func(link, args) or 0
    except OpError as e:
        print(f'error: EPP {e}', file=sys.stderr)
        if e.status == proto.ST_NOT_ARMED:
            print('Lines are not armed: run `enter` first (or add --enter).', file=sys.stderr)
        elif e.status == proto.ST_NWAIT_TIMEOUT:
            print(TROUBLE, file=sys.stderr)
        return 1
    except (LinkError, FlashError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\ninterrupted', file=sys.stderr)
        return 130
    finally:
        link.close()


if __name__ == '__main__':
    sys.exit(main())
