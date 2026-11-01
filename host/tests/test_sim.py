"""End-to-end tests against the simulated Pico + EC (no hardware).

Run from host/:  python -m unittest discover -s tests -v
"""

import atexit
import io
import os
import random
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import ite_pp_isp                                   # noqa: E402
from itepp import diag, proto                       # noqa: E402
from itepp.dbgr import BusError, Dbgr, DetectError  # noqa: E402
from itepp.dump import analyze, dump                # noqa: E402
from itepp.epp import EppConfig                     # noqa: E402
from itepp.flash import Eflash                      # noqa: E402
from itepp.link import OpError                      # noqa: E402
from itepp.proto import Batch                       # noqa: E402
from itepp.session import Session                   # noqa: E402
from itepp.sim import SimEC, SimLink                # noqa: E402

# the CLI records the halt in a session file: never the user's real one
SESSION_DIR = tempfile.mkdtemp(prefix='itepp-test-')
atexit.register(shutil.rmtree, SESSION_DIR, True)
os.environ['ITEPP_SESSION'] = os.path.join(SESSION_DIR, 'session.json')


def clear_session():
    Session(sim=True).clear()


def image(size, seed=1):
    rnd = random.Random(seed)
    data = bytearray(rnd.getrandbits(8) for _ in range(size))
    data[0x80:0x90] = bytes.fromhex('a5a5a5a5a5a5a5a5 85125a5a aa005555'.replace(' ', ''))
    return bytes(data)


def armed_link(flash, **kw):
    link = SimLink(SimEC(flash, **kw))
    EppConfig().apply(link)
    link.run(Batch().strap(proto.STRAP_DRIVE))
    return link


class ProtocolTest(unittest.TestCase):
    def test_hello(self):
        link = SimLink(SimEC(image(1024)))
        self.assertEqual(link.hello()['proto'], proto.PROTO_VERSION)

    def test_not_armed_keeps_length(self):
        link = SimLink(SimEC(image(1024)))
        b = Batch().aw(0).dr(10).pins_get()
        res = link.run(b, check=False)
        self.assertEqual(res.status, proto.ST_NOT_ARMED)
        self.assertEqual(res.index, 0)
        self.assertEqual(len(res.data), 12)
        with self.assertRaises(OpError):
            link.run(b)

    def test_dr_split(self):
        b = Batch().dr(70000)
        self.assertEqual(b.expect, 70000)
        self.assertEqual(len(b.names), 2)

    def test_batch_limit(self):
        b = Batch()
        with self.assertRaises(ValueError):
            for _ in range(proto.MAX_BATCH):
                b.aw(0)


class ChipTest(unittest.TestCase):
    def test_detect_it5261(self):
        link = armed_link(image(256 * 1024))
        info = Dbgr(link).detect()
        self.assertEqual(info.chip_id, 0x55261)
        self.assertTrue(info.it5xxxx)
        self.assertEqual(info.flash_size, 256 * 1024)
        # detect never resets the CPU: `enter` halts it once per power cycle
        self.assertEqual(link.ec.cpu_resets, 0)

    def test_detect_it5261_real_id3(self):
        # the real chip reads 52 61 02 with 0x00 as 3rd ID byte
        link = armed_link(image(256 * 1024), id3=0x00, ver=0x02)
        info = Dbgr(link).detect()
        self.assertEqual(info.chip_id, 0x05261)
        self.assertTrue(info.it5xxxx)
        self.assertEqual(info.flash_size, 256 * 1024)
        self.assertEqual(link.ec.cpu_resets, 0)

    def test_bus_check(self):
        link = armed_link(image(1024))
        self.assertEqual(Dbgr(link).check_bus(), (0, 0))

    def test_stuck_bits_stop_before_any_write(self):
        link = armed_link(image(256 * 1024))
        link.stuck = 0x06                   # KSO1/KSO2 taken as GPIO
        self.assertEqual(Dbgr(link).check_bus(), (0x06, 0))
        with self.assertRaises(BusError) as cm:
            Dbgr(link).detect()
        self.assertIn('PD1 (FFC pin 13) stuck at 1', str(cm.exception))
        self.assertFalse(link.ec.halted)
        self.assertEqual(link.ec.ec_writes, [])

    def test_detect_it83xx(self):
        link = armed_link(image(256 * 1024), chip=(0x83, 0x20), ver=0x43)
        info = Dbgr(link).detect()
        self.assertEqual(info.chip_id, 0x8320)
        self.assertFalse(info.addr_3bytes)
        self.assertEqual(info.flash_size, 256 * 1024)

    def test_detect_garbage(self):
        link = SimLink(SimEC(image(1024)))
        EppConfig(handshake=False).apply(link)
        link.run(Batch().strap(proto.STRAP_DRIVE))
        link.dead = True
        with self.assertRaises(DetectError):
            Dbgr(link).detect()

    def test_timeout(self):
        link = armed_link(image(1024))
        link.dead = True
        with self.assertRaises(OpError) as cm:
            Dbgr(link).detect()
        self.assertEqual(cm.exception.status, proto.ST_NWAIT_TIMEOUT)

    def test_flash_id(self):
        link = armed_link(image(1024))
        ident = Eflash(link).read_id()
        self.assertEqual(Eflash.flash_type(ident), '8315')

    def test_exit_and_wdt_use_bank(self):
        link = armed_link(image(256 * 1024))
        d = Dbgr(link)
        d.detect()
        d.disable_watchdog()
        d.exit()
        self.assertEqual(link.ec.ec_writes, [(0xF01F05, 0x30), (0xF01C08, 0x10)])


class ReadTest(unittest.TestCase):
    def check_read(self, size, base=0, length=None, read_reg=0x09, chunk=4096):
        flash = image(size)
        link = armed_link(flash)
        Dbgr(link).detect()
        length = size - base if length is None else length
        data = Eflash(link).read(base, length, read_reg=read_reg, chunk=chunk)
        self.assertEqual(data, flash[base:base + length])
        self.assertEqual(link.ec.dangerous, [])
        return link

    def test_full_256k_block_reg(self):
        self.check_read(256 * 1024)

    def test_full_256k_data_reg(self):
        self.check_read(256 * 1024, read_reg=0x08)

    def test_range(self):
        self.check_read(256 * 1024, base=0x1000, length=0x2300, chunk=1000)

    def test_512k_crosses_boundary(self):
        # only correct if fast read is re-sent at 0x40000
        self.check_read(512 * 1024, base=0x3F000, length=0x2000)

    def test_unaligned_rejected(self):
        link = armed_link(image(1024))
        with self.assertRaises(Exception):
            Eflash(link).read(0x10, 16)

    def test_dump_passes(self):
        flash = image(64 * 1024)
        link = armed_link(flash)
        images, diffs = dump(Eflash(link), 0, len(flash), passes=3)
        self.assertEqual(len(images), 3)
        self.assertEqual(diffs, [])
        self.assertEqual(images[2], flash)

    def test_refuses_write_opcodes(self):
        from itepp import flash as fl
        with self.assertRaises(ValueError):
            fl._spi_command(Batch(), 0x06)


class AnalyzeTest(unittest.TestCase):
    def test_blank(self):
        self.assertTrue(any('0xFF' in n for n in analyze(b'\xFF' * 4096)))

    def test_stuck_address(self):
        page = bytes(range(256))
        self.assertTrue(any('identical' in n for n in analyze(page * 8)))

    def test_signature(self):
        self.assertTrue(any('signature found at 0x000080' in n for n in analyze(image(4096))))

    def test_signature_it5261(self):
        data = bytearray(image(4096))
        data[0x80:0x90] = bytes.fromhex('a5a5a5a5a5a5a7148512763' '2aa005555')
        self.assertTrue(any('signature found at 0x000080' in n for n in analyze(bytes(data))))


class DiagTest(unittest.TestCase):
    def test_probe_prefers_working(self):
        link = SimLink(SimEC(image(1024)))
        link.run(Batch().strap(proto.STRAP_DRIVE))
        results = diag.probe(link, EppConfig())
        self.assertEqual(len(results), 1)   # stopped at the first full answer
        best = results[0]
        self.assertEqual(best['regs'], bytes([0x52, 0x61, 0x00]))
        self.assertFalse(best['cfg'].swap_strobes)
        self.assertFalse(best['cfg'].nwait_invert)

    def test_probe_finds_inverted_nwait(self):
        link = SimLink(SimEC(image(1024)))
        link.run(Batch().strap(proto.STRAP_DRIVE))
        link.sim_nwait_inv = True
        results = diag.probe(link, EppConfig(), try_all=True)
        self.assertEqual(len(results), len(diag.probe_variants(EppConfig())))
        good = [r for r in results if r['score'] >= diag.PROBE_FULL_SCORE]
        self.assertTrue(good)
        self.assertTrue(all(r['cfg'].nwait_invert or not r['cfg'].handshake for r in good))

    def test_raw(self):
        link = armed_link(image(1024))
        b = diag.parse_raw('rr 00; aw 01; dr 1; pins; ar')
        out = diag.format_raw(b, link.run(b, check=False))
        self.assertIn('dr 1: 52', out)
        self.assertIn('dr 1: 61', out)
        self.assertIn('status OK', out)

    def test_address_read_is_complement(self):
        link = armed_link(image(1024))
        self.assertEqual(link.run(Batch().aw(0x5A).ar().aw(0x00).ar()).data, b'\xa5\xff')

    def test_lines(self):
        link = SimLink(SimEC(image(1024)))
        rows = diag.scan_lines(link, samples=3)
        self.assertEqual(len(rows), proto.LINE_COUNT)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, 'ec.bin')
        self.flash = image(256 * 1024)
        with open(self.src, 'wb') as f:
            f.write(self.flash)
        clear_session()

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ite_pp_isp.main(['--sim', self.src] + list(argv))
        return rc, out.getvalue(), err.getvalue()

    def test_read(self):
        dst = os.path.join(self.tmp.name, 'out.bin')
        rc, out, err = self.cli('--enter', 'hotplug', 'read', '-o', dst)
        self.assertEqual(rc, 0, out + err)
        with open(dst, 'rb') as f:
            self.assertEqual(f.read(), self.flash)
        self.assertIn('Verify OK', out)

    def test_read_range(self):
        dst = os.path.join(self.tmp.name, 'part.bin')
        rc, out, err = self.cli('--enter', 'hotplug', 'read', '-o', dst, '-R', '4000:1000')
        self.assertEqual(rc, 0, out + err)
        with open(dst, 'rb') as f:
            self.assertEqual(f.read(), self.flash[0x4000:0x5000])

    def test_powerup_entry(self):
        rc, out, err = self.cli('--sim-powerup', '--enter', 'powerup', 'id')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('CHIPID 55261', out)

    def test_enter_reports_bus(self):
        rc, out, err = self.cli('--enter', 'hotplug', 'enter')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('Address echo OK', out)
        self.assertIn('DBGR regs 00..02: 52 61 00', out)

    def test_stuck_bits_abort_even_with_size(self):
        dst = os.path.join(self.tmp.name, 'out.bin')
        with self.assertRaises(SystemExit) as cm:
            self.cli('--sim-stuck', '06', '--enter', 'hotplug', 'read', '-o', dst, '-s', '256')
        self.assertIn('stuck at 1', str(cm.exception))
        self.assertFalse(os.path.exists(dst))

    def test_hotplug_blocked(self):
        rc, out, err = self.cli('--sim-powerup', '--enter', 'hotplug', 'id')
        self.assertEqual(rc, 1)
        self.assertIn('NWAIT_TIMEOUT', err)

    def test_not_entered(self):
        rc, out, err = self.cli('id')
        self.assertEqual(rc, 1)
        self.assertIn('not armed', err)

    def test_probe_and_regs(self):
        rc, out, err = self.cli('--enter', 'hotplug', 'probe')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('Best candidate: regs 52 61 00', out)
        rc, out, err = self.cli('--enter', 'hotplug', 'regs', '--end', '0f')
        self.assertEqual(rc, 0, out + err)
        self.assertIn('00:  52 61 00', out)


if __name__ == '__main__':
    unittest.main()
