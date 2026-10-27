"""Tests of the real firmware batch executor (firmware/main.c), compiled for
the host with stubbed SDK/USB and a fake EPP (firmware/test/exec_harness.c).
Skipped when no C compiler is available.

Run from host/:  python -m unittest discover -s tests -v
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
FW = os.path.join(ROOT, 'firmware')
sys.path.insert(0, os.path.dirname(HERE))

from itepp import proto                 # noqa: E402
from itepp.proto import Batch           # noqa: E402

CC = shutil.which('cc') or shutil.which('gcc') or shutil.which('clang')


class ProtoHeaderTest(unittest.TestCase):
    """proto.h and proto.py must define the same numbers."""

    def test_constants_match(self):
        with open(os.path.join(FW, 'proto.h')) as f:
            text = f.read()
        defs = re.findall(r'#define\s+([A-Z_0-9]+)\s+(0x[0-9A-Fa-f]+|\d+)', text)
        self.assertGreater(len(defs), 30)
        checked = 0
        for name, value in defs:
            if name in ('FW_VERSION_MAJ', 'FW_VERSION_MIN'):
                continue
            self.assertTrue(hasattr(proto, name), f'{name} missing in proto.py')
            self.assertEqual(getattr(proto, name), int(value, 0), name)
            checked += 1
        self.assertGreater(checked, 30)


@unittest.skipIf(CC is None, 'no C compiler')
class FirmwareExecTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.exe = os.path.join(cls.tmp.name, 'exec_harness')
        subprocess.run([CC, '-std=c11', '-Wall', '-Wextra', '-Werror',
                        '-Wno-unused-function',
                        '-I', os.path.join(FW, 'test', 'stubs'), '-I', FW,
                        os.path.join(FW, 'test', 'exec_harness.c'), '-o', cls.exe],
                       check=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_fw(self, payload, timeout_at=None):
        env = dict(os.environ)
        if timeout_at is not None:
            env['FAKE_TIMEOUT_AT'] = str(timeout_at)
        return subprocess.run([self.exe], input=payload, stdout=subprocess.PIPE,
                              env=env, check=True).stdout

    def split(self, raw, batch):
        self.assertEqual(len(raw), batch.expect + 4)
        data, tr = raw[:-4], raw[-4:]
        self.assertEqual(tr[3], proto.RSP_MAGIC)
        return data, tr[0], tr[1] | (tr[2] << 8)

    def test_normal_batch(self):
        b = (Batch().hello().cfg(500, 1000, 250, 200, 1).strap(proto.STRAP_DRIVE)
             .aw(0x12).dr(100).ar().pins_get().wait_pin(14, 1, 10).delay_us(5))
        data, st, idx = self.split(self.run_fw(b.encode()), b)
        self.assertEqual(st, proto.ST_OK)
        self.assertEqual(idx, 0xFFFF)
        self.assertEqual(data[:4], b'ITPP')
        self.assertEqual(data[4], proto.PROTO_VERSION)
        self.assertEqual(list(data[8:108]), list(range(100)))
        self.assertEqual(data[108], 100)                  # ar
        self.assertEqual(data[109:111], b'\x34\x12')      # pins
        self.assertEqual(data[111], 1)                    # wait_pin

    def test_not_armed_pads_output(self):
        b = Batch().aw(0).dr(10).hello().pins_get()
        data, st, idx = self.split(self.run_fw(b.encode()), b)
        self.assertEqual(st, proto.ST_NOT_ARMED)
        self.assertEqual(idx, 0)
        self.assertEqual(data, bytes(len(data)))

    def test_timeout_mid_read(self):
        b = Batch().strap(proto.STRAP_DRIVE).aw(0).dr(10).ar()
        # cycle 1 = aw, cycles 2..5 = first 4 reads, cycle 6 times out
        data, st, idx = self.split(self.run_fw(b.encode(), timeout_at=6), b)
        self.assertEqual(st, proto.ST_NWAIT_TIMEOUT)
        self.assertEqual(idx, 2)
        self.assertEqual(list(data), [0, 1, 2, 3] + [0] * 7)

    def test_big_read(self):
        b = Batch().strap(proto.STRAP_DRIVE).dr(70000)
        data, st, _ = self.split(self.run_fw(b.encode()), b)
        self.assertEqual(st, proto.ST_OK)
        self.assertEqual(bytes(data[:512]), bytes(i & 0xFF for i in range(512)))
        self.assertEqual(data[70000 - 1], (70000 - 1) & 0xFF)

    def test_two_batches_and_junk(self):
        b1 = Batch().hello()
        b2 = Batch().strap(proto.STRAP_DRIVE).ar()
        raw = self.run_fw(b'\x00\x13' + b1.encode() + b2.encode())
        self.split(raw[:b1.expect + 4], b1)
        data, st, _ = self.split(raw[b1.expect + 4:], b2)
        self.assertEqual(st, proto.ST_OK)

    def test_truncated_op(self):
        raw = self.run_fw(bytes([proto.REQ_MAGIC, 2, 0, proto.OP_DR, 5]))
        self.assertEqual(raw, bytes([proto.ST_BAD_OP, 0, 0, proto.RSP_MAGIC]))

    def test_unknown_op(self):
        raw = self.run_fw(bytes([proto.REQ_MAGIC, 2, 0, proto.OP_PINS_GET, 0xEE]))
        self.assertEqual(raw[-4:], bytes([proto.ST_BAD_OP, 1, 0, proto.RSP_MAGIC]))

    def test_oversized_batch(self):
        n = proto.MAX_BATCH + 1
        raw = self.run_fw(bytes([proto.REQ_MAGIC, n & 0xFF, n >> 8]))
        self.assertEqual(raw, bytes([proto.ST_BAD_OP, 0, 0, proto.RSP_MAGIC]))


if __name__ == '__main__':
    unittest.main()
