"""Read-path self-test against the simulated EC, with injected faults.

Run from host/:  python -m unittest discover -s tests -v
"""

import io
import os
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import ite_pp_isp                                   # noqa: E402
from itepp import proto                             # noqa: E402
from itepp.dbgr import Dbgr                         # noqa: E402
from itepp.epp import EppConfig                     # noqa: E402
from itepp.flash import Eflash                      # noqa: E402
from itepp.proto import Batch                       # noqa: E402
from itepp.sim import SimEC, SimLink                # noqa: E402
from itepp.verify import bit_report, diff_offsets, verify_read  # noqa: E402
from test_sim import clear_session, image           # noqa: E402


def arm(link):
    EppConfig().apply(link)
    link.run(Batch().strap(proto.STRAP_DRIVE))
    return link


class OpensOnRead(SimLink):
    """A data line whose contact opens once the dump starts (first fast
    read): from then on it reads 1 and arrives as 1, on every pass."""

    def __init__(self, ec, bit):
        super().__init__(ec)
        self.bit = bit

    def _cycle(self, addr, write, val=0):
        if self.ec.spi_cmd == 0x0B:
            self.stuck = self.bit
        return super()._cycle(addr, write, val)


class Flaky(SimLink):
    """Bit 2 now and then reads 1 on data reads: a joint that makes and
    breaks. Off until `rate` is set."""

    def __init__(self, ec):
        super().__init__(ec)
        self.rnd = random.Random(1)
        self.rate = 0

    def _cycle(self, addr, write, val=0):
        st, v = super()._cycle(addr, write, val)
        if not addr and not write and self.rnd.random() < self.rate:
            v |= 0x04
        return st, v


class CarryOnEC(SimEC):
    """Fast read ignores its address and carries on where the last stopped."""
    pos = 0

    def _spi_read(self):
        if self.cs and self.spi_cmd == 0x0B and len(self.spi_in) >= 4:
            v = self.flash[self.pos % len(self.flash)]
            self.pos += 1
            return v
        return super()._spi_read()


def quiet_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = ite_pp_isp.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class HelpersTest(unittest.TestCase):
    def test_diff_and_bits(self):
        a = bytes(4096)
        b = bytearray(a)
        b[5] = 0x04
        b[3000] = 0x06
        self.assertEqual(diff_offsets(a, bytes(b)), [5, 3000])
        rep = bit_report(a, b, [5, 3000])
        self.assertIn('PD2 (FFC pin 11) 2x 0->1 0x 1->0', rep)
        self.assertIn('PD1 (FFC pin 13) 1x 0->1', rep)
        self.assertNotIn('PD0', rep)


class VerifyTest(unittest.TestCase):
    def setUp(self):
        self.flash = image(64 * 1024)

    def run_verify(self, link, **kw):
        dbgr = Dbgr(arm(link))
        dbgr.detect()
        return verify_read(dbgr, Eflash(link), len(self.flash), **kw)

    def failed(self, rep):
        return [name for name, ok, _ in rep.checks if not ok]

    def test_clean_link(self):
        link = SimLink(SimEC(self.flash, size_code=0x02))
        rep = self.run_verify(link)
        self.assertTrue(rep.read_ok, rep.checks)
        self.assertEqual(rep.image, self.flash)
        self.assertEqual(len(rep.checks), 5)
        self.assertEqual(link.ec.dangerous, [])

    def test_line_opening_during_the_read(self):
        link = OpensOnRead(SimEC(self.flash, size_code=0x02), 0x04)
        rep = self.run_verify(link)
        # every pass reads the same wrong bytes: only the echo after sees it
        self.assertEqual(self.failed(rep), ['address echo after reading'])
        self.assertIn('PD2 (FFC pin 11) stuck at 1', rep.checks[-1][2])

    def test_flaky_joint_points_at_the_line(self):
        link = Flaky(SimEC(self.flash, size_code=0x02))
        dbgr = Dbgr(arm(link))
        dbgr.detect()
        link.rate = 0.0005
        rep = verify_read(dbgr, Eflash(link), len(self.flash))
        self.assertFalse(rep.read_ok)
        detail = ' '.join(d for _, ok, d in rep.checks if not ok)
        self.assertIn('PD2 (FFC pin 11)', detail)
        self.assertNotIn('PD1', detail)

    def test_fast_read_ignoring_its_address(self):
        link = SimLink(CarryOnEC(self.flash, size_code=0x02))
        rep = self.run_verify(link)
        # long reads all agree; only the shuffled sector reads see it
        self.assertEqual(len(self.failed(rep)), 1)
        self.assertIn('one by one', self.failed(rep)[0])

    def test_reference_and_ignored_range(self):
        link = SimLink(SimEC(self.flash, size_code=0x02))
        ref = bytearray(self.flash)
        ref[0xF010] ^= 0xFF
        rep = self.run_verify(link, ref=bytes(ref), ignore=[(0xF000, 0x1000)])
        self.assertTrue(rep.read_ok)
        self.assertTrue(rep.ref[0])
        self.assertIn('inside them, in 0x00F000-0x00F3FF', rep.ref[1])

        ref[0x100] ^= 0x01
        rep = self.run_verify(SimLink(SimEC(self.flash, size_code=0x02)), ref=bytes(ref),
                              ignore=[(0xF000, 0x1000)])
        self.assertTrue(rep.read_ok)
        self.assertFalse(rep.ref[0])
        self.assertIn('1 bytes differ, in 0x000000-0x0003FF', rep.ref[1])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.flash = image(256 * 1024)
        self.src = self.path('ec.bin', self.flash)
        clear_session()

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name, data=None):
        p = os.path.join(self.tmp.name, name)
        if data is not None:
            with open(p, 'wb') as f:
                f.write(data)
        return p

    def test_verify_ok(self):
        dst = self.path('checked.bin')
        rc, out, err = quiet_cli('--sim', self.src, '--enter', 'hotplug', 'verify',
                                 '-i', self.src, '-o', dst)
        self.assertEqual(rc, 0, out + err)
        self.assertIn('READ PATH OK', out)
        self.assertIn(f'same as {self.src}: identical', out)
        with open(dst, 'rb') as f:
            self.assertEqual(f.read(), self.flash)

    def test_verify_against_a_different_dump(self):
        old = bytearray(self.flash)
        old[0x3C000] ^= 0x55               # EC data area: ignored
        ref = self.path('old.bin', bytes(old))
        rc, out, err = quiet_cli('--sim', self.src, '--enter', 'hotplug', 'verify',
                                 '-i', ref, '--ignore', '3B800', '-o', self.path('a.bin'))
        self.assertEqual(rc, 0, out + err)
        self.assertIn('1 bytes differ inside them, in 0x03C000-0x03C3FF', out)
        old[0x2000] ^= 0x01
        ref = self.path('old2.bin', bytes(old))
        rc, out, err = quiet_cli('--sim', self.src, '--enter', 'hotplug', 'verify',
                                 '-i', ref, '--ignore', '3B800', '-o', self.path('b.bin'))
        self.assertEqual(rc, 2, out + err)
        self.assertIn('READ PATH OK', out)
        self.assertIn('differs from', out)

    def test_wrong_size_reference(self):
        with self.assertRaises(SystemExit) as cm:
            quiet_cli('--sim', self.src, '--enter', 'hotplug', 'verify',
                      '-i', self.path('short.bin', self.flash[:1024]))
        self.assertIn('full dump', str(cm.exception))

    def test_read_checks_the_bus_afterwards(self):
        dst = self.path('out.bin')
        link = arm(OpensOnRead(SimEC(self.flash), 0x04))
        args = ite_pp_isp.build_parser().parse_args(['read', '-o', dst, '--no-exit'])
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            rc = ite_pp_isp.cmd_read(link, args)
        self.assertEqual(rc, 2)
        self.assertIn('VERIFY FAILED: after the read', out.getvalue())
        self.assertNotIn('Verify OK', out.getvalue())

    def test_write_backup_checks_the_bus_even_in_dry_run(self):
        backup = self.path('backup.bin')
        link = arm(OpensOnRead(SimEC(self.flash), 0x04))
        args = ite_pp_isp.build_parser().parse_args(
            ['write', '-i', self.src, '--dry-run', '--backup', backup])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            ite_pp_isp.cmd_write(link, args)
        self.assertIn('after the backup read', str(cm.exception))
        self.assertFalse(os.path.exists(backup))


if __name__ == '__main__':
    unittest.main()
