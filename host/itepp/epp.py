"""EPP timing / handshake settings sent to the Pico with OP_CFG."""

from . import proto
from .proto import Batch


class EppConfig:
    def __init__(self, setup_ns=500, strobe_ns=1000, hold_ns=250,
                 timeout_us=200, handshake=True, nwait_invert=False,
                 swap_strobes=False, no_release=False):
        self.setup_ns = setup_ns
        self.strobe_ns = strobe_ns
        self.hold_ns = hold_ns
        self.timeout_us = timeout_us
        self.handshake = handshake
        self.nwait_invert = nwait_invert
        self.swap_strobes = swap_strobes
        self.no_release = no_release

    @property
    def flags(self):
        f = 0
        if self.handshake:
            f |= proto.CFG_HANDSHAKE
        if self.nwait_invert:
            f |= proto.CFG_NWAIT_INV
        if self.swap_strobes:
            f |= proto.CFG_SWAP_STROBE
        if self.no_release:
            f |= proto.CFG_NO_RELEASE
        return f

    def copy(self, **changes):
        c = EppConfig(**vars(self))
        for k, v in changes.items():
            setattr(c, k, v)
        return c

    def add_to(self, batch):
        return batch.cfg(self.setup_ns, self.strobe_ns, self.hold_ns,
                         self.timeout_us, self.flags)

    def apply(self, link):
        link.run(self.add_to(Batch()))

    def cli_args(self):
        """The command-line flags that reproduce this config."""
        a = []
        if not self.handshake:
            a.append('--no-handshake')
        if self.nwait_invert:
            a.append('--nwait-invert')
        if self.swap_strobes:
            a.append('--swap-strobes')
        if self.no_release:
            a.append('--no-release')
        d = EppConfig()
        for k in ('setup_ns', 'strobe_ns', 'hold_ns', 'timeout_us'):
            if getattr(self, k) != getattr(d, k):
                a.append(f'--{k.replace("_", "-")} {getattr(self, k)}')
        return ' '.join(a)

    def __str__(self):
        hs = 'off' if not self.handshake else ('inv' if self.nwait_invert else 'on')
        return (f'handshake={hs} swap={"y" if self.swap_strobes else "n"} '
                f'release={"n" if self.no_release else "y"} '
                f'setup={self.setup_ns}ns strobe={self.strobe_ns}ns '
                f'hold={self.hold_ns}ns timeout={self.timeout_us}us')
