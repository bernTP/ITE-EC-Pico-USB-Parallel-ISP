#!/usr/bin/env python3
"""ite_pp_isp.py: read or write an ITE EC e-flash through the keyboard connector.

Parallel ISP (ITE DBGR over IEEE 1284 EPP on the KSI/KSO pins), using a
Raspberry Pi Pico running firmware/ in between. Only the `write` command
erases or programs the flash; it backs it up first and asks before writing.

Typical session (AC unplugged at the start):
    ./ite_pp_isp.py hello
    ./ite_pp_isp.py enter --mode powerup      (asks when to plug AC in, halts the
                                               EC CPU once: nothing after resets it)
    ./ite_pp_isp.py id
    ./ite_pp_isp.py read -o dump.bin --no-exit
    ./ite_pp_isp.py verify -i dump.bin        (can reads be trusted? read only)
    ./ite_pp_isp.py write -i new.bin          (backup, plan, confirm, write, verify)
"""

import argparse
import os
import signal
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from itepp import diag, proto                              # noqa: E402
from itepp.dbgr import (Dbgr, BusError, DetectError,      # noqa: E402
                        bus_report)
from itepp.dump import ITE_SIGNATURE, analyze, dump        # noqa: E402
from itepp.epp import EppConfig                            # noqa: E402
from itepp.flash import EFLASH_TYPE_8315, Eflash, FlashError  # noqa: E402
from itepp.program import (EC_DATA_AREA, SECTOR, EflashWriter,  # noqa: E402
                           FlashLocked, FlashStateUnknown, image_problems, plan,
                           recheck, runs, write_image)
from itepp.link import LinkError, OpError, UsbLink         # noqa: E402
from itepp.proto import Batch                              # noqa: E402
from itepp.session import Session                          # noqa: E402
from itepp.sim import SimEC, SimLink                       # noqa: E402
from itepp.verify import verify_read                       # noqa: E402

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
        enter(link, args.enter, interactive=not args.sim, sess=session(args))
    return link


def session(args):
    return Session(sim=bool(args.sim))


KSI0_3 = 0x0F << proto.LINE_STB
KSI4_5 = (1 << proto.LINE_KSI4) | (1 << proto.LINE_KSI5)


def ask(text):
    try:
        input(f'>>> {text} [Enter] ')
    except EOFError:
        raise SystemExit('no input: stopped (lines left as they are)')


def enter(link, mode, interactive=True, halt=None, sess=None):
    """Apply KSI[5,4,2,1] = 0011b so the EC enters DBGR/EPP. halt: reset and
    halt the EC CPU once it answers (default: power-up entry only)."""
    if halt is None:
        halt = mode == 'powerup'
    if mode == 'hotplug':
        link.run(Batch().strap(proto.STRAP_DRIVE).delay_us(100000))
        print('Strap driven (KSI4/KSI5 low, STB#/AFD#/INIT#/SLIN# high).')
    else:
        if sess:
            sess.clear()            # a new power cycle: the old halt is gone
        enter_powerup(link, interactive)
    if halt:
        halt_now(link, sess, interactive)


def halt_now(link, sess, interactive):
    """The one reset+halt of a power cycle: stop the EC CPU before its
    firmware takes KSO1/KSO2 as GPIO, and record it (itepp/session.py)."""
    state = sess.load() if sess else None
    if state:
        raise SystemExit(f'Not halting again: the EC CPU was already halted '
                         f'{Session.describe(state)}. The halt is a CPU reset, and a second '
                         'one can let the EC firmware run (on an IT5261 it erased one of its '
                         'data sectors). Drop --halt; after a power cycle, use `enter`.')
    n = Dbgr(link).halt_on_clean_bus(stable_s=3.0 if interactive else 0.05)
    if sess:
        sess.save(n)
    print(f'EC CPU reset and halted ({n} halt command(s)): its firmware is stopped, '
          'so the laptop is (or goes) off. Later commands do not reset it again.')


NOT_HALTED = """\
The EC CPU is not known to be halted in this power cycle{what}. Either:
  - add --enter powerup before the command: power-up entry and halt, then the
    command, in one go, or
  - run `enter` first: it halts the CPU once, and later commands leave it so, or
  - after `enter --mode hotplug`, add --halt (only if nothing halted the CPU
    since the board was powered)"""


def ensure_halted(link, args, need=False):
    """Halt the EC CPU only when --halt asks for it, and never twice in a
    power cycle. need: refuse to go on unless it is known to be halted."""
    sess = session(args)
    if getattr(args, 'halt', False):
        halt_now(link, sess, interactive=not args.sim)
        return True
    state = sess.load()
    if state:
        print(f'EC CPU halted {Session.describe(state)}: not reset again.')
        return True
    if need:
        raise SystemExit(NOT_HALTED.format(what=', so nothing was written'))
    print('WARNING: the EC CPU is not known to be halted (no `enter` in this power cycle): '
          'if its firmware runs, its data area can change while it is read.', file=sys.stderr)
    return False


def enter_powerup(link, interactive):
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
    # (the Pico can't see the power-up: its pull-ups charge the unpowered EC
    #  within ~100 ms, so the clamp it would watch lets go before AC is in)
    if interactive:
        print('Pattern applied (KSI4/KSI5 low, pull-ups on KSI0-KSI3). PLUG IN AC NOW.')
        ask('Press Enter as soon as AC is in')
    link.run(Batch().strap(proto.STRAP_DRIVE))
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
    session(args).clear()
    print('All lines released (inputs). The strap is no longer applied.')
    print('Session ended: the next write needs `enter` (power-up entry) again.')


def cmd_lines(link, args):
    rows = diag.scan_lines(link, samples=args.samples)
    session(args).clear()
    print(diag.format_lines(rows))
    print('\nAll lines are released now: run `enter` again before talking to the EC.')


def cmd_enter(link, args):
    if args.enter == 'none':
        halt = False if args.no_halt else (True if args.halt else None)
        enter(link, args.mode, interactive=not args.sim, halt=halt, sess=session(args))
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
    ensure_halted(link, args)
    dbgr = Dbgr(link)
    try:
        info = dbgr.detect()
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
    # a line that opened during the read gives the same wrong bytes every pass
    stuck = dbgr.check_bus()

    with open(args.output, 'wb') as f:
        f.write(images[0])
    print(f'Wrote {args.output} ({size} bytes, {dt:.1f}s)')
    for note in analyze(images[0], base):
        print('  ' + note)

    if not args.no_exit:
        dbgr.exit()

    if any(stuck):
        print(f'VERIFY FAILED: after the read, {bus_report(*stuck)}. Identical passes '
              'can still be wrong: check that line and read again.')
        return 2
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


def cmd_verify(link, args):
    if args.passes < 2:
        raise SystemExit('verify needs --passes 2 or more')
    ref = None
    if args.input:
        with open(args.input, 'rb') as f:
            ref = f.read()
    dbgr, info = detect(link, args)
    check_flash_id(link, args)
    if ref is not None and len(ref) != info.flash_size:
        raise SystemExit(f'{args.input} is {len(ref)} bytes but the flash is '
                         f'{info.flash_size}: give a full dump')
    ignore = [parse_range(r, info.flash_size) for r in args.ignore]

    print(f'Checking the read path: {args.passes} passes through reg 09, 1 through reg 08, '
          f'then every 1 KB sector on its own...')
    rep = verify_read(dbgr, Eflash(link), info.flash_size, passes=args.passes, ref=ref,
                      ignore=ignore, progress=progress_printer())
    for name, ok, detail in rep.checks:
        print(f'  {"OK  " if ok else "FAIL"}  {name}' + ('' if ok else f'\n          {detail}'))
    if rep.ref:
        ok, detail = rep.ref
        print(f'  {"OK  " if ok else "FAIL"}  same as {args.input}: {detail}')

    if not rep.read_ok:
        print("READ PATH NOT RELIABLE: don't write. One PDn that always reads the same way "
              'is that wire or joint;\nrandom bits on every line point at timing '
              '(try --setup-ns 1000 --hold-ns 500).')
        return 2
    out = args.output or f'verify-{info.chip_id:05x}-{time.strftime("%Y%m%d-%H%M%S")}.bin'
    with open(out, 'wb') as f:
        f.write(rep.image)
    print(f'Wrote {out} (checked dump of the flash as it is now)')
    for note in analyze(rep.image):
        print('  ' + note)
    print('READ PATH OK: dumps, the backup of `write` and its per-sector checks can be '
          'trusted on this connection.')
    if rep.ref and not rep.ref[0]:
        print(f'The flash differs from {args.input} outside the ignored ranges.')
        return 2
    return 0


RECOVER = """\
The flash may now be partly written. Don't unplug AC or the Pico: the EC is
halted and stays in parallel-port mode, so you can simply:
  - run the same write command again (only sectors that still differ are
    written), or
  - put the old contents back: write -i {backup}"""

RECOVER_POWER_CYCLE = """\
The flash may now be partly written, and this power cycle can't be trusted
any more. The power-up entry is handled by the chip's hardware, not by the
firmware in the flash, so: `safe`, unplug AC until the EC is drained, then
  - run the same write with --enter powerup (only sectors that still differ
    are written), or
  - put the old contents back the same way: --enter powerup write -i {backup}"""


def sector_progress():
    t0 = time.time()

    def show(done, total):
        sys.stderr.write(f'\r  sector {done}/{total}  {done * 100 // total:3d}%  '
                         f'{time.time() - t0:5.1f}s')
        if done == total:
            sys.stderr.write('\n')
        sys.stderr.flush()
    return show


def write_range(args, flash_size):
    """(base, size, data_base): the range to write, and where the EC data
    area starts if it was left out of it (None otherwise)."""
    base, size = parse_range(args.range, flash_size)
    if size <= 0 or base + size > flash_size or base % SECTOR or size % SECTOR:
        raise SystemExit(f'range 0x{base:X}+0x{size:X}: must be inside the flash and '
                         f'1 KB aligned (0x{SECTOR:X})')
    data = EC_DATA_AREA.get(flash_size)
    if data is None or args.with_data_area:
        return base, size, None
    if args.range:
        if base + size > data:
            print(f'WARNING: the range reaches into the EC data area (0x{data:06X}-'
                  f'0x{flash_size - 1:06X}). The EC firmware rewrites it, and a dump of it '
                  'may hold an update it was in the middle of.')
        return base, size, None
    return 0, data, data


def cmd_write(link, args):
    with open(args.input, 'rb') as f:
        image = f.read()

    ensure_halted(link, args, need=not args.dry_run)
    dbgr = Dbgr(link)
    try:
        info = dbgr.detect()
    except DetectError as e:            # BusError included
        raise SystemExit(f'error: {e}\nNothing was written.')
    print(info)
    ident = Eflash(link).read_id()
    ftype = Eflash.flash_type(ident)
    print(f'EFLASH TYPE = {ftype or "unknown"} (id {ident[:3].hex(" ")})')
    if ftype != EFLASH_TYPE_8315:
        raise SystemExit('Only the 8315 e-flash (id ff ff fe) can be written. '
                         'Nothing was written.')
    if len(image) != info.flash_size:
        raise SystemExit(f'{args.input} is {len(image)} bytes but the flash is '
                         f'{info.flash_size}: give a full image. Nothing was written.')
    problems = image_problems(image)
    if problems and not args.force:
        raise SystemExit('REFUSED: ' + '; '.join(problems) +
                         '\nNothing was written (--force writes it anyway).')
    for p in problems:
        print(f'WARNING: {p}')
    base, size, data_base = write_range(args, info.flash_size)

    print('Reading the current flash twice (backup)...')
    images, diffs = dump(Eflash(link), 0, info.flash_size, passes=2,
                         read_reg=args.read_reg, progress=progress_printer())
    if diffs:
        raise SystemExit(f'the two reads differ in {len(diffs)} bytes: the link is not '
                         'reliable enough to write. Nothing was written.')
    stuck_high, stuck_low = dbgr.check_bus()
    if stuck_high or stuck_low:
        raise SystemExit(f'error: after the backup read, {bus_report(stuck_high, stuck_low)}'
                         '\nNothing was written.')
    current = images[0]
    backup = args.backup or f'backup-{info.chip_id:05x}-{time.strftime("%Y%m%d-%H%M%S")}.bin'
    with open(backup, 'wb') as f:
        f.write(current)
    print(f'Backup: {backup}')
    for note in analyze(current):
        print('  ' + note)

    sectors = plan(image, current, base, size, rewrite_all=args.all)
    if data_base is not None:
        kept = plan(image, current, data_base, info.flash_size - data_base)
        print(f'EC data area 0x{data_base:06X}-0x{info.flash_size - 1:06X} kept as it is'
              + (f' ({len(kept)} sector(s) of the file differ there; --with-data-area '
                 'writes them).' if kept else '.'))
    if not sectors:
        print('The flash already holds this image in that range: nothing to write.')
        return 0
    print(f'{len(sectors)} sector(s) of 1 KB to erase and program: {runs(sectors)}')
    if args.dry_run:
        print('Dry run: nothing was written.')
        return 0

    if not args.yes:
        print('The EC firmware will be replaced in these sectors. If the result does '
              'not boot,\nthis same tool can write the backup back.')
        try:
            answer = input('>>> Type WRITE to go ahead: ')
        except EOFError:
            answer = ''
        if answer.strip() != 'WRITE':
            raise SystemExit('Not confirmed: nothing was written.')

    if not args.keep_protect_path:
        dbgr.disable_protect_path()
        if any(dbgr.protect_path()):
            raise SystemExit('error: the protect path (EC 0x20A0-0x20BF) does not stay at 0: '
                             'is the EC firmware running? Nothing was written.')
        print('Protect path disabled (EC 0x20A0-0x20BF = 0).')

    def check(addr):
        # the EC CPU must still be halted: its firmware takes KSO1/KSO2 and
        # sets the protect path again when it runs
        stuck = dbgr.check_bus()
        if any(stuck):
            raise FlashStateUnknown(f'before sector 0x{addr:06X}: {bus_report(*stuck)}')
        if not args.keep_protect_path and any(dbgr.protect_path()):
            raise FlashLocked(f'before sector 0x{addr:06X}: the protect path is set again, '
                              'so the EC firmware is running')

    stop = []
    old_handler = signal.signal(signal.SIGINT, lambda *_: stop.append(1))
    sig = current.find(ITE_SIGNATURE)
    ref = (sig, current[sig:sig + 16]) if sig >= 0 else None
    writer = EflashWriter(link, word_delay_us=args.word_delay_us, read_reg=args.read_reg,
                          ref=ref)
    print('Writing (Ctrl-C stops cleanly after the current sector)...')
    try:
        writer.begin()
        write_image(writer, image, sectors, progress=sector_progress(),
                    should_stop=lambda: bool(stop), check=check)
    except (FlashLocked, FlashStateUnknown) as e:
        print(f'\nWRITE STOPPED: {e}', file=sys.stderr)
        print(RECOVER_POWER_CYCLE.format(backup=backup), file=sys.stderr)
        return 3
    except (FlashError, OpError, LinkError) as e:
        print(f'\nWRITE FAILED: {e}', file=sys.stderr)
        print(RECOVER.format(backup=backup), file=sys.stderr)
        return 3
    finally:
        signal.signal(signal.SIGINT, old_handler)

    print(f'Verifying 0x{base:06X}-0x{base + size - 1:06X}...')
    final = Eflash(link).read(base, size, read_reg=args.read_reg)
    want = image[base:base + size]
    if final != want:
        bad = [i for i in range(size) if final[i] != want[i]]
        print(f'VERIFY FAILED: {len(bad)} bytes differ (first at 0x{base + bad[0]:06X}).',
              file=sys.stderr)
        print(RECOVER.format(backup=backup), file=sys.stderr)
        return 3
    print(f'Verify OK. Word delay used: {writer.word_delay_us} us.')

    if args.settle_s > 0:
        print(f'Checking that the written sectors stay as written ({args.settle_s:g}s, '
              'no reset)...')
        try:
            changed = recheck(writer, image, sectors, args.settle_s, dbgr.bus_clean)
        except (FlashError, OpError, LinkError) as e:
            print(f'RECHECK FAILED: {e}', file=sys.stderr)
            print(RECOVER_POWER_CYCLE.format(backup=backup), file=sys.stderr)
            return 3
        if changed:
            print(f'DATA CHANGED AFTER THE WRITE in {runs(changed)}: something erased or '
                  'rewrote them after they were verified, and only EC code does that: the '
                  'EC CPU ran.', file=sys.stderr)
            print(RECOVER_POWER_CYCLE.format(backup=backup), file=sys.stderr)
            return 3
        print('Still as written.')
    print('The EC is still halted. To boot the new firmware: `safe`, unplug AC until the EC '
          'is drained (pin 21 below about 0.5 V), plug it back in.')
    return 0


# ─── Main ───────────────────────────────────────────────────────────

HALT_HELP = ('reset+halt the EC CPU first: only after `enter --mode hotplug`, when nothing '
             'halted it since the board was powered (a second reset can let its firmware '
             'run; refused if `enter` already halted it)')

def build_parser():
    p = argparse.ArgumentParser(
        description='Read or write an ITE EC e-flash over the keyboard connector '
                    '(parallel ISP / DBGR over EPP) with a Raspberry Pi Pico. Only '
                    '`write` changes the flash.',
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
    h = s.add_mutually_exclusive_group()
    h.add_argument('--no-halt', action='store_true',
                   help="powerup: don't reset+halt the EC CPU once it answers")
    h.add_argument('--halt', action='store_true',
                   help='hotplug: reset+halt the EC CPU too (refused if it was already '
                        'halted in this power cycle)')
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
    s.add_argument('--halt', action='store_true', help=HALT_HELP)
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
    s.add_argument('--halt', action='store_true', help=HALT_HELP)
    s.add_argument('--wdt-disable', action='store_true',
                   help='also write the EC watchdog off (PP mode does it in hardware)')
    s.add_argument('--reset-gpio', action='store_true', help='reset EC GPIOs to default first')
    s.add_argument('--ignore-flash-id', action='store_true',
                   help='read even if the e-flash ID is not recognised')
    s.add_argument('--no-exit', action='store_true', help="don't send DBGR exit at the end")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser('verify', help='check that reads can be trusted before writing '
                                      '(read only)')
    s.add_argument('-i', '--input', metavar='FILE',
                   help='also compare with this full dump, e.g. one from an earlier power cycle')
    s.add_argument('--ignore', action='append', default=[], metavar='BASE[:SIZE]',
                   help='hex range left out of the comparison with FILE: data the EC '
                        'firmware rewrites itself (T14s: 3B800). Repeatable')
    s.add_argument('-o', '--output', metavar='FILE',
                   help='where to save the checked dump (default verify-<chip>-<time>.bin)')
    s.add_argument('--passes', type=int, default=3,
                   help='sequential passes through reg 09 (default 3)')
    s.add_argument('--halt', action='store_true', help=HALT_HELP)
    s.add_argument('--ignore-flash-id', action='store_true',
                   help='check even if the e-flash ID is not recognised')
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser('write', help='erase and program the e-flash from a file '
                                     '(backs up and asks first)')
    s.add_argument('-i', '--input', required=True, metavar='FILE',
                   help='full flash image (same size as the flash)')
    s.add_argument('-R', '--range', metavar='BASE[:SIZE]',
                   help='only write this hex range of the image, 1 KB aligned')
    s.add_argument('--all', action='store_true',
                   help='rewrite every sector in the range, even unchanged ones')
    s.add_argument('--dry-run', action='store_true',
                   help='back up and list the sectors that would change, write nothing')
    s.add_argument('--backup', metavar='FILE',
                   help='where to save the current flash (default backup-<chip>-<time>.bin)')
    s.add_argument('--yes', action='store_true', help="don't ask for confirmation")
    s.add_argument('--force', action='store_true',
                   help='write an image that fails the sanity checks')
    s.add_argument('--word-delay-us', type=int, default=20, metavar='US',
                   help='wait after each 2-byte word (default 20; raised automatically '
                        'when a sector does not verify)')
    s.add_argument('--keep-protect-path', action='store_true',
                   help="don't zero EC 0x20A0-0x20BF first (iteflash and this tool do by "
                        'default; without it the IT5261 ignores erases)')
    s.add_argument('--with-data-area', action='store_true',
                   help='also write the EC data area (T14s: 0x3B800-0x3FFFF), which the '
                        'EC firmware rewrites itself; without -R it is kept as it is')
    s.add_argument('--settle-s', type=float, default=10, metavar='S',
                   help='after the final verify, wait S seconds and read the written '
                        'sectors again (default 10; 0 skips it)')
    s.add_argument('--halt', action='store_true', help=HALT_HELP)
    s.add_argument('--read-reg', type=lambda x: int(x, 16), default=0x09, metavar='RR',
                   help='DBGR register for burst reads (09 or 08)')
    s.set_defaults(func=cmd_write)
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
    except (LinkError, FlashError, DetectError) as e:
        print(f'error: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\ninterrupted', file=sys.stderr)
        return 130
    finally:
        link.close()


if __name__ == '__main__':
    sys.exit(main())
