"""One CPU halt per power cycle, and the write guards built around it.

Seen on an IT5261: after a verified write, the next command's reset+halt
(DBGR 0x27 <- 0x81) let the EC firmware run; it erased one of its data
sectors and left the e-flash ignoring erase and program.

Run from host/:  python -m unittest discover -s tests -v
"""

import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ite_pp_isp                                   # noqa: E402
from itepp import proto                             # noqa: E402
from itepp.dbgr import REG_CPU, Dbgr                # noqa: E402
from itepp.epp import EppConfig                     # noqa: E402
from itepp.flash import FlashError                  # noqa: E402
from itepp.program import (SECTOR, EflashWriter, FlashLocked,  # noqa: E402
                           FlashStateUnknown, plan, recheck, write_image)
from itepp.proto import Batch                       # noqa: E402
from itepp.session import Session                   # noqa: E402
from itepp.sim import SimEC, SimLink                # noqa: E402
from test_sim import clear_session, image           # noqa: E402

DATA = 0x3EC00          # an EC data sector (T14s layout)
CODE = 0x01000


def changed(data, *addrs):
    out = bytearray(data)
    for a in addrs:
        out[a + 0x302] ^= 0xFF
    return bytes(out)


def armed(ec, hotplug=True):
    link = SimLink(ec, hotplug=hotplug)
    EppConfig().apply(link)
    if hotplug:
        link.run(Batch().strap(proto.STRAP_DRIVE))
    return link


def run_quiet(fn, *a, **kw):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = fn(*a, **kw)
    return rc, out.getvalue() + err.getvalue()


def erase_data_sector_on_second_reset(ec):
    """EC firmware that runs after a reset: it 'repairs' its data area and
    locks the e-flash. Only a second reset reaches it here, as on the IT5261
    (the first halt comes before the firmware starts)."""
    if ec.cpu_resets >= 2:
        ec.flash[DATA:DATA + SECTOR] = b'\xFF' * SECTOR
        ec.locked = True


class CliBase(unittest.TestCase):
    def setUp(self):
        clear_session()
        self.tmp = tempfile.TemporaryDirectory()
        self.flash = image(256 * 1024)
        self.src = self.path('ec.bin', self.flash)

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name, data=None):
        p = os.path.join(self.tmp.name, name)
        if data is not None:
            with open(p, 'wb') as f:
                f.write(data)
        return p

    def ec(self, **kw):
        return SimEC(self.flash, size_code=0x02, **kw)

    def cmd(self, link, *argv):
        args = ite_pp_isp.build_parser().parse_args(['--sim', self.src] + list(argv))
        rc, out = run_quiet(args.func, link, args)
        return rc or 0, out

    def write_args(self, new, *extra):
        return ('write', '-i', self.path('new.bin', new), '--yes', '--settle-s', '0',
                '--backup', self.path('backup.bin')) + extra


class OneHaltTest(CliBase):
    def test_enter_halts_once_and_nothing_resets_again(self):
        ec = self.ec()
        ec.on_reset = erase_data_sector_on_second_reset
        link = armed(ec, hotplug=False)
        run_quiet(ite_pp_isp.enter, link, 'powerup', interactive=False,
                  sess=Session(sim=True))
        self.assertEqual(ec.cpu_resets, 1)
        new = changed(self.flash, DATA)
        steps = [('id',),
                 ('read', '-o', self.path('r.bin'), '--no-exit'),
                 self.write_args(new, '-R', '3EC00:400'),
                 ('verify', '-i', self.path('new.bin'), '-o', self.path('v.bin'))]
        for argv in steps:
            rc, out = self.cmd(link, *argv)
            self.assertEqual(rc, 0, out)
            self.assertIn('not reset again', out)
        self.assertEqual(ec.cpu_resets, 1)
        # the data sector written in this session is still there
        self.assertEqual(bytes(ec.flash), new)

    def test_the_old_second_reset_erases_the_sector(self):
        # what `verify` did before: detect() sent 27 <- 81 again
        ec = self.ec()
        ec.on_reset = erase_data_sector_on_second_reset
        link = armed(ec)
        dbgr = Dbgr(link)
        dbgr.write_regs([(REG_CPU, 0x81)])
        ec.flash[DATA + 0x302] = 0x00
        dbgr.write_regs([(REG_CPU, 0x81)])
        self.assertEqual(bytes(ec.flash[DATA:DATA + SECTOR]), b'\xFF' * SECTOR)
        self.assertTrue(ec.locked)

    def test_write_refused_without_a_halted_cpu(self):
        ec = self.ec()
        link = armed(ec)
        with self.assertRaises(SystemExit) as cm:
            self.cmd(link, *self.write_args(changed(self.flash, CODE)))
        self.assertIn('not known to be halted', str(cm.exception))
        self.assertIn('--enter powerup', str(cm.exception))
        self.assertEqual((ec.cpu_resets, ec.dangerous), (0, []))
        # a dry run writes nothing, so it only warns
        rc, out = self.cmd(link, *self.write_args(changed(self.flash, CODE), '--dry-run'))
        self.assertEqual(rc, 0, out)
        self.assertIn('WARNING: the EC CPU is not known to be halted', out)

    def test_halt_option_once_then_refused(self):
        ec = self.ec()
        link = armed(ec)
        rc, out = self.cmd(link, *self.write_args(changed(self.flash, CODE), '--halt'))
        self.assertEqual(rc, 0, out)
        self.assertEqual(ec.cpu_resets, 1)
        self.assertIsNotNone(Session(sim=True).load())
        with self.assertRaises(SystemExit) as cm:
            self.cmd(link, 'id', '--halt')
        self.assertIn('Not halting again', str(cm.exception))
        self.assertEqual(ec.cpu_resets, 1)

    def test_hotplug_enter_does_not_halt_unless_asked(self):
        ec = self.ec()
        link = armed(ec)
        self.cmd(link, 'enter', '--mode', 'hotplug')
        self.assertEqual(ec.cpu_resets, 0)
        self.assertIsNone(Session(sim=True).load())
        self.cmd(link, 'enter', '--mode', 'hotplug', '--halt')
        self.assertEqual(ec.cpu_resets, 1)
        with self.assertRaises(SystemExit):
            self.cmd(link, 'enter', '--mode', 'hotplug', '--halt')
        self.assertEqual(ec.cpu_resets, 1)

    def test_powerup_entry_replaces_an_old_session(self):
        Session(sim=True).save(1)
        ec = self.ec()
        link = armed(ec, hotplug=False)
        run_quiet(ite_pp_isp.enter, link, 'powerup', interactive=False,
                  sess=Session(sim=True))
        self.assertEqual(ec.cpu_resets, 1)

    def test_safe_and_lines_end_the_session(self):
        for argv in (('safe',), ('lines', '--samples', '1')):
            Session(sim=True).save(1)
            self.cmd(armed(self.ec()), *argv)
            self.assertIsNone(Session(sim=True).load(), argv)

    def test_sim_and_real_sessions_are_separate(self):
        Session(sim=True).save(1)
        self.assertIsNone(Session(sim=False).load())
        clear_session()


class WriteGuardTest(CliBase):
    def setUp(self):
        super().setUp()
        Session(sim=True).save(1)            # as after `enter`

    def test_data_area_kept_by_default(self):
        new = changed(self.flash, CODE, DATA)
        ec = self.ec()
        rc, out = self.cmd(armed(ec), *self.write_args(new))
        self.assertEqual(rc, 0, out)
        self.assertIn('EC data area 0x03B800-0x03FFFF kept as it is (1 sector(s)', out)
        self.assertEqual(ec.erased, [CODE])

        ec = self.ec()
        rc, out = self.cmd(armed(ec), *self.write_args(new, '--with-data-area'))
        self.assertEqual(rc, 0, out)
        self.assertEqual(ec.erased, [CODE, DATA])

        ec = self.ec()
        rc, out = self.cmd(armed(ec), *self.write_args(new, '-R', '3EC00:400'))
        self.assertEqual(rc, 0, out)
        self.assertIn('WARNING: the range reaches into the EC data area', out)
        self.assertEqual(ec.erased, [DATA])

    def test_firmware_waking_up_stops_between_sectors(self):
        class WakesUp(SimEC):
            def _spi_end(self):
                super()._spi_end()
                if self.erased:              # after the first sector's erase
                    self.ec_mem[0xF020A0] = 0x01

        ec = WakesUp(self.flash, size_code=0x02)
        rc, out = self.cmd(armed(ec), *self.write_args(changed(self.flash, 0x400, 0x800)))
        self.assertEqual(rc, 3, out)
        self.assertIn('WRITE STOPPED', out)
        self.assertIn('protect path is set again', out)
        self.assertIn('--enter powerup', out)
        self.assertEqual(ec.erased, [0x400])

    def test_locked_flash_stops_with_the_right_diagnosis(self):
        ec = self.ec()
        ec.locked = True
        rc, out = self.cmd(armed(ec), *self.write_args(changed(self.flash, CODE)))
        self.assertEqual(rc, 3, out)
        self.assertIn('the erase was ignored', out)
        self.assertNotIn('retrying', out)
        self.assertEqual(ec.dangerous.count(0xD7), 1)

    def test_change_after_the_write_is_caught(self):
        ec = self.ec()
        new = changed(self.flash, CODE)

        def firmware_runs(_):
            ec.flash[CODE:CODE + SECTOR] = b'\xFF' * SECTOR

        with mock.patch('itepp.program.time.sleep', side_effect=firmware_runs):
            rc, out = self.cmd(armed(ec), *self.write_args(new)[:-4],
                               '--settle-s', '1', '--backup', self.path('b.bin'))
        self.assertEqual(rc, 3, out)
        self.assertIn('DATA CHANGED AFTER THE WRITE in 0x001000-0x0013FF', out)

    def test_recheck_passes_when_nothing_moves(self):
        rc, out = self.cmd(armed(self.ec()), *self.write_args(changed(self.flash, CODE))[:-4],
                           '--settle-s', '0.02', '--backup', self.path('b.bin'))
        self.assertEqual(rc, 0, out)
        self.assertIn('Still as written', out)


class WriterTest(unittest.TestCase):
    def setUp(self):
        self.old = image(16 * SECTOR)

    def writer(self, **kw):
        ec = SimEC(self.old, size_code=0x02, **kw)
        link = armed(ec)
        Dbgr(link).detect()
        return ec, EflashWriter(link)

    def test_wel_checked_before_the_erase_is_sent(self):
        ec, w = self.writer(write_protect=True)
        with self.assertRaises(FlashError) as cm:
            w.erase_sector(SECTOR)
        self.assertIn('write enable not accepted', str(cm.exception))
        self.assertIn(0x06, ec.dangerous)
        self.assertNotIn(0xD7, ec.dangerous)

    def test_wel_checked_before_the_first_word(self):
        ec, w = self.writer(write_protect=True)
        with self.assertRaises(FlashError):
            w.program(SECTOR, b'\x00' * 4)
        self.assertNotIn(0xAD, ec.dangerous)

    def test_locked_blank_sector_is_diagnosed(self):
        self.old = b'\xFF' * len(self.old)
        ec, w = self.writer()
        ec.locked = True
        with self.assertRaises(FlashLocked) as cm:
            w.write_sector(SECTOR, image(SECTOR), log=lambda *_: None)
        self.assertIn('not a single byte programmed in 3 attempts', str(cm.exception))

    def test_failed_cleanup_is_not_retried(self):
        ec, w = self.writer()
        real, calls = w._close, []

        def close(settle_us=0):
            calls.append(settle_us)
            real(settle_us)
            return len(calls) != 2          # the cleanup after programming fails

        w._close = close
        with self.assertRaises(FlashStateUnknown):
            w.write_sector(SECTOR, image(SECTOR, seed=7))
        self.assertEqual(ec.dangerous.count(0xD7), 1)

    def test_begin_enters_follow_mode_first(self):
        # outside follow mode ECINDDR never answers (IT5261): a bare WRDI timed out
        ec, w = self.writer()
        w.begin()
        self.assertFalse(ec._follow())
        self.assertFalse(ec.wel)

    def test_begin_fails_cleanly(self):
        _, w = self.writer()
        w.link.dead = True
        with self.assertRaises(FlashStateUnknown):
            w.begin()

    def test_check_runs_before_each_sector(self):
        ec, w = self.writer()
        new = changed(self.old, SECTOR, 2 * SECTOR, 3 * SECTOR)
        seen = []

        def check(addr):
            seen.append(addr)
            if len(seen) == 2:
                raise FlashLocked('firmware running')

        with self.assertRaises(FlashLocked):
            write_image(w, new, plan(new, self.old, 0, len(new)), check=check,
                        log=lambda *_: None)
        self.assertEqual(seen, [SECTOR, 2 * SECTOR])
        self.assertEqual(ec.erased, [SECTOR])

    def test_recheck(self):
        ec, w = self.writer()
        img = bytes(ec.flash)
        self.assertEqual(recheck(w, img, [0, SECTOR], 0.02, lambda: True,
                                 sleep=lambda _: None), [])

        def erase(_):
            ec.flash[SECTOR:2 * SECTOR] = b'\xFF' * SECTOR

        self.assertEqual(recheck(w, img, [0, SECTOR], 1, lambda: True, sleep=erase),
                         [SECTOR])
        with self.assertRaises(FlashError):
            recheck(w, img, [0], 1, lambda: False, sleep=lambda _: None)


if __name__ == '__main__':
    unittest.main()
