"""Erase/program path against the simulated EC (no hardware).

Run from host/:  python -m unittest discover -s tests -v
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ite_pp_isp                                   # noqa: E402
from itepp.dbgr import BusError, Dbgr               # noqa: E402
from itepp.flash import FlashError                  # noqa: E402
from itepp.program import (SECTOR, EflashWriter, FlashStuck, _Regs,  # noqa: E402
                           image_problems, plan, runs, write_image)
from itepp.session import Session                   # noqa: E402
from test_sim import armed_link, clear_session, image  # noqa: E402

READ_PATH_FORBIDDEN = {0x01, 0x02, 0x20, 0x50, 0x60, 0xC7, 0xD8}


def changed(data, *sectors):
    out = bytearray(data)
    for s in sectors:
        out[s * SECTOR + 10] ^= 0x5A
    return bytes(out)


def quiet(*_):
    pass


class PlanTest(unittest.TestCase):
    def test_skips_identical_and_puts_sector_0_last(self):
        old = image(16 * SECTOR)
        new = changed(old, 0, 3, 15)
        self.assertEqual(plan(new, old, 0, len(old)), [3 * SECTOR, 15 * SECTOR, 0])

    def test_range_and_all(self):
        old = image(16 * SECTOR)
        self.assertEqual(plan(old, old, 0, len(old)), [])
        self.assertEqual(plan(old, old, 4 * SECTOR, 2 * SECTOR, rewrite_all=True),
                         [4 * SECTOR, 5 * SECTOR])

    def test_runs(self):
        self.assertEqual(runs([0x400, 0x800, 0x1000]), '0x000400-0x000BFF, 0x001000-0x0013FF')

    def test_image_problems(self):
        self.assertEqual(image_problems(image(4096)), [])
        self.assertIn('blank', image_problems(b'\xFF' * 4096)[0])
        self.assertIn('0x00', image_problems(bytes(4096))[0])
        self.assertIn('signature', image_problems(bytes(range(256)) * 16)[0])


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.old = image(64 * SECTOR)

    def link(self, **kw):
        link = armed_link(self.old, size_code=0x02, **kw)
        Dbgr(link).detect()          # like the CLI: no reset
        return link

    def write(self, link, new, **kw):
        writer = EflashWriter(link, **kw)
        write_image(writer, new, plan(new, self.old, 0, len(new)), log=quiet)
        return writer

    def test_writes_only_changed_sectors(self):
        link = self.link()
        new = changed(self.old, 0, 7, 63)
        self.write(link, new)
        ec = link.ec
        self.assertEqual(bytes(ec.flash), new)
        self.assertEqual(ec.erased, [7 * SECTOR, 63 * SECTOR, 0])
        self.assertFalse(READ_PATH_FORBIDDEN & set(ec.dangerous))   # no chip erase, no WRSR

    def test_blank_sector_is_only_erased(self):
        link = self.link()
        new = bytearray(self.old)
        new[5 * SECTOR:6 * SECTOR] = b'\xFF' * SECTOR
        self.write(link, bytes(new))
        self.assertEqual(bytes(link.ec.flash), bytes(new))

    def test_slow_words_raise_the_delay(self):
        link = self.link(prog_us=30)
        new = changed(self.old, 2)
        writer = self.write(link, new, word_delay_us=10)
        self.assertEqual(bytes(link.ec.flash), new)
        self.assertGreaterEqual(writer.word_delay_us, 30)
        self.assertEqual(link.ec.erased, [2 * SECTOR, 2 * SECTOR])   # first try redone

    def test_slow_erase_is_polled(self):
        link = self.link(erase_us=400)
        new = changed(self.old, 4)
        self.write(link, new)
        self.assertEqual(bytes(link.ec.flash), new)

    def test_write_protected_flash_fails_cleanly(self):
        link = self.link(write_protect=True)
        with self.assertRaises(FlashError) as cm:
            self.write(link, changed(self.old, 1))
        self.assertIn('write enable not accepted', str(cm.exception))
        self.assertEqual(bytes(link.ec.flash), self.old)
        self.assertIsNone(link.ec.aai_addr)

    def test_blocked_erase_times_out(self):
        link = self.link(erase_blocked=True)
        writer = EflashWriter(link, ref=(0x80, self.old[0x80:0x90]))
        writer.erase_timeout_s = 0.2
        with self.assertRaises(FlashError) as cm:
            write_image(writer, changed(self.old, 3), [3 * SECTOR], log=quiet)
        self.assertIn('still busy', str(cm.exception))
        self.assertIn('ff@0ms', str(cm.exception))
        self.assertEqual(link.ec.erased, [])

    def test_status_ff_while_busy_is_waited_for(self):
        link = self.link(erase_us=3000, prog_us=5, busy_reads_ff=True)
        writer = EflashWriter(link, ref=(0x80, self.old[0x80:0x90]))
        new = changed(self.old, 3)
        write_image(writer, new, [3 * SECTOR], log=quiet)
        self.assertEqual(bytes(link.ec.flash), new)
        sector, secs, trail = writer.erase_log[0]
        self.assertEqual(trail[0][1], 0xFF)

    def test_like_the_it5261(self):
        # status ff while erasing and for the whole AAI sequence
        link = self.link(erase_us=3000, prog_us=15, busy_reads_ff=True, aai_status_ff=True)
        new = changed(self.old, 3, 9)
        writer = self.write(link, new, word_delay_us=20)
        self.assertEqual(bytes(link.ec.flash), new)
        self.assertEqual(link.ec.erased, [3 * SECTOR, 9 * SECTOR])   # no retries

    def test_unreadable_flash_after_erase_is_caught(self):
        link = self.link()
        writer = EflashWriter(link, ref=(0x80, b'\x12' * 16))   # never matches
        with self.assertRaises(FlashStuck):
            write_image(writer, changed(self.old, 3), [3 * SECTOR], log=quiet)

    def test_stop_between_sectors(self):
        link = self.link()
        new = changed(self.old, 1, 2, 3)
        calls = []
        with self.assertRaises(FlashError) as cm:
            write_image(EflashWriter(link), new, plan(new, self.old, 0, len(new)),
                        should_stop=lambda: calls.append(1) or len(calls) > 1, log=quiet)
        self.assertIn('1 of 3 sectors written', str(cm.exception))
        self.assertEqual(link.ec.erased, [1 * SECTOR])

    def test_protect_path_writes(self):
        link = self.link()
        dbgr = Dbgr(link)
        dbgr.detect()
        dbgr.disable_protect_path()
        self.assertEqual(link.ec.ec_writes, [(0xF020A0 + i, 0) for i in range(32)])

    def test_refuses_dangerous_opcodes(self):
        for op in (0x60, 0xC7, 0x01, 0x50, 0x02, 0x20):
            with self.assertRaises(ValueError):
                _Regs().spi(op)


class EarlyHaltTest(unittest.TestCase):
    def test_halts_on_clean_bus(self):
        link = armed_link(image(4096))
        self.assertEqual(Dbgr(link).halt_on_clean_bus(stable_s=0.05, log=quiet), 1)
        self.assertTrue(link.ec.halted)

    def test_never_halts_on_a_stuck_bus(self):
        link = armed_link(image(4096))
        link.stuck = 0x06
        with self.assertRaises(BusError):
            Dbgr(link).halt_on_clean_bus(timeout_s=0.2, stable_s=0.05, max_kicks=0, log=quiet)
        self.assertFalse(link.ec.halted)

    def test_reset_kick_then_halt(self):
        link = armed_link(image(4096))
        link.stuck, link.release_on_reset = 0x06, True
        logs = []
        n = Dbgr(link).halt_on_clean_bus(stable_s=0.05, kick_after_s=0.05, log=logs.append)
        self.assertEqual(n, 1)
        self.assertEqual(len(logs), 1)
        self.assertIn('resetting the EC', logs[0])
        self.assertEqual(link.stuck, 0)


class WriteCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old = image(256 * 1024)
        self.src = self.path('ec.bin', self.old)
        self.backup = os.path.join(self.tmp.name, 'backup.bin')
        clear_session()
        Session(sim=True).save(1)        # as after `enter` in this power cycle

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name, data):
        p = os.path.join(self.tmp.name, name)
        with open(p, 'wb') as f:
            f.write(data)
        return p

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ite_pp_isp.main(['--sim', self.src, '--enter', 'hotplug', 'write',
                                  '--backup', self.backup, '--settle-s', '0'] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_write(self):
        new = self.path('new.bin', changed(self.old, 0, 100, 255))
        rc, out, err = self.cli('-i', new, '--yes', '--with-data-area')   # 255: data area
        self.assertEqual(rc, 0, out + err)
        self.assertIn('3 sector(s)', out)
        self.assertIn('Verify OK', out)
        with open(self.backup, 'rb') as f:
            self.assertEqual(f.read(), self.old)

    def test_protect_path(self):
        new = self.path('new.bin', changed(self.old, 3))
        rc, out, err = self.cli('-i', new, '--yes')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('Protect path disabled', out)
        new = self.path('new2.bin', changed(self.old, 4))
        rc, out, err = self.cli('-i', new, '--yes', '--keep-protect-path')
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn('Protect path disabled', out)

    def test_dry_run(self):
        new = self.path('new.bin', changed(self.old, 9))
        rc, out, err = self.cli('-i', new, '--dry-run')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('0x002400-0x0027FF', out)
        self.assertIn('Dry run', out)

    def test_nothing_to_do(self):
        rc, out, err = self.cli('-i', self.src, '--yes')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('nothing to write', out)

    def test_range_limits_the_write(self):
        new = self.path('new.bin', changed(self.old, 1, 50))
        rc, out, err = self.cli('-i', new, '--yes', '-R', '400:400')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('1 sector(s)', out)

    def test_refusals(self):
        cases = [
            (self.path('blank.bin', b'\xFF' * len(self.old)), (), 'blank'),
            (self.path('short.bin', self.old[:1024]), (), 'full image'),
            (self.path('new.bin', changed(self.old, 3)), ('-R', '100:400'), '1 KB aligned'),
        ]
        for img, extra, msg in cases:
            with self.assertRaises(SystemExit) as cm:
                self.cli('-i', img, '--yes', *extra)
            self.assertIn(msg, str(cm.exception))

    def test_needs_confirmation(self):
        new = self.path('new.bin', changed(self.old, 3))
        sys.stdin, saved = io.StringIO('no\n'), sys.stdin
        try:
            with self.assertRaises(SystemExit) as cm:
                self.cli('-i', new)
        finally:
            sys.stdin = saved
        self.assertIn('nothing was written', str(cm.exception))

    def test_stuck_bus_refuses(self):
        new = self.path('new.bin', changed(self.old, 3))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            ite_pp_isp.main(['--sim', self.src, '--sim-stuck', '02', '--enter', 'hotplug',
                             'write', '-i', new, '--yes', '--backup', self.backup])
        self.assertIn('Nothing was written', str(cm.exception))


if __name__ == '__main__':
    unittest.main()
