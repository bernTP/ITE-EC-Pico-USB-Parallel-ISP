"""E-flash erase and program in DBGR follow mode.

Only for the ITE "8315" e-flash (JEDEC ID FF FF FE): 1 KB sector erase
(0xD7) and AAI word program (0xAD), the commands Chromium's iteflash.cc
uses on IT5xxxx parts (command_erase2, write_flash2).

iteflash streams the words through the DBGR "quick AAI" mode on its I2C
block address, which has no known EPP equivalent. Here every word is a
plain AAI command on ECINDDR followed by RDSR, a fixed delay and a status
read. On an IT5261 the status reads ff for the whole AAI sequence, so it
can't say when a word is done, but the words were only programmed with
that read in place. Each sector is read back instead; a mismatch lengthens
the delay and the sector is erased and programmed again.

Chip erase (0x60) and status register writes (0x50/0x01) are never sent:
the writer refuses any opcode outside WRITE_PATH_OPCODES.
"""

import time

from .dump import ITE_SIGNATURE
from .flash import Eflash, FlashError, SPI_CMD_READ_STATUS
from .proto import Batch

SPI_CMD_WRITE_DISABLE = 0x04
SPI_CMD_WRITE_ENABLE = 0x06
SPI_CMD_SECTOR_ERASE_1K = 0xD7
SPI_CMD_WORD_PROGRAM = 0xAD

WRITE_PATH_OPCODES = {SPI_CMD_READ_STATUS, SPI_CMD_WRITE_ENABLE, SPI_CMD_WRITE_DISABLE,
                      SPI_CMD_SECTOR_ERASE_1K, SPI_CMD_WORD_PROGRAM}

SECTOR = 0x400
STATUS_BUSY = 0x01
STATUS_WEL = 0x02

WORDS_PER_BATCH = 64          # ~2.8 KB of ops, under MAX_BATCH
LAST_WORD_TIMEOUT_S = 0.1
ERASE_TIMEOUT_S = 10.0
ERASE_FIRST_POLL_US = 200     # give the e-flash time to raise BUSY
MAX_WORD_DELAY_US = 2000
ATTEMPTS = 3


class VerifyError(FlashError):
    """A sector didn't read back as written."""


class FlashStuck(FlashError):
    """The e-flash stopped answering (reads all FF): retrying won't help."""


STUCK_HINT = ('the e-flash is not answering (reads all ff): erase or program blocked, '
              'or still busy (was --keep-protect-path used?)')


class _Regs:
    """Batch builder: the address cycle is only re-sent when the selected
    DBGR register changes."""

    def __init__(self):
        self.b = Batch()
        self.sel = None

    def w(self, reg, val):
        if reg != self.sel:
            self.b.aw(reg)
            self.sel = reg
        self.b.dw(val)
        return self

    def read08(self, n=1):
        if self.sel != 0x08:
            self.b.aw(0x08)
            self.sel = 0x08
        self.b.dr(n)
        return self

    def spi(self, cmd):
        """FSCE# high (ends the previous command), then low with the opcode."""
        if cmd not in WRITE_PATH_OPCODES:
            raise ValueError(f'SPI opcode {cmd:#04x} refused by the writer')
        return self.w(0x05, 0xFE).w(0x08, 0x00).w(0x05, 0xFD).w(0x08, cmd)

    def status(self, delay_us=0):
        """RDSR and one status byte. The FSCE# rising edge at its start is
        what starts a pending erase or word program."""
        self.spi(SPI_CMD_READ_STATUS)
        if delay_us:
            self.b.delay_us(delay_us)
        return self.read08()

    def start(self, cmd):
        """FSCE# low with the opcode (FSCE# must already be high)."""
        if cmd not in WRITE_PATH_OPCODES:
            raise ValueError(f'SPI opcode {cmd:#04x} refused by the writer')
        return self.w(0x05, 0xFD).w(0x08, cmd)

    def cs_high(self):
        """FSCE# high: starts a pending word program or erase."""
        return self.w(0x05, 0xFE).w(0x08, 0x00)

    def delay(self, us):
        if us:
            self.b.delay_us(us)
        return self

    def follow_enter(self):
        return self.w(0x07, 0x7F).w(0x06, 0xFF).w(0x05, 0xFE).w(0x04, 0x00).w(0x08, 0x00)

    def follow_exit(self):
        return self.w(0x07, 0x00).w(0x06, 0x00)


def _addr3(r, addr):
    return r.w(0x08, (addr >> 16) & 0xFF).w(0x08, (addr >> 8) & 0xFF).w(0x08, addr & 0xFF)


def _trail(trail):
    return ' '.join(f'{st:02x}@{t * 1000:.0f}ms' for t, st in trail)


def _check_answering(status, what):
    # every protect bit, AAI, WEL and BUSY at once: nothing drives the bus
    if status == 0xFF:
        raise FlashStuck(f'{what}: status reads ff: {STUCK_HINT}')


def _check_wel(status, what):
    _check_answering(status, what)
    if status & (STATUS_BUSY | STATUS_WEL) != STATUS_WEL:
        raise FlashError(f'{what}: write enable not accepted (status {status:02x}); '
                         'flash protected or follow mode not working')


class EflashWriter:
    def __init__(self, link, word_delay_us=10, read_reg=0x09, ref=None):
        """ref: (address, bytes) known to be in the flash outside the sectors
        being written, e.g. the ITE signature. Read back after each erase: an
        all-FF read path would otherwise pass the blank check."""
        self.link = link
        self.word_delay_us = word_delay_us
        self.read_reg = read_reg
        self.reader = Eflash(link)
        self.ref = ref
        self.erase_timeout_s = ERASE_TIMEOUT_S
        self.erase_log = []     # (sector, seconds, [(t, status), ...])

    def _close(self, settle_us=0):
        """WRDI and leave follow mode. After an error, settle_us first lets a
        word or erase still in progress finish: the flash ignores WRDI while
        busy and would stay in AAI mode."""
        r = _Regs()
        if settle_us:
            r.status(settle_us)
        r.spi(SPI_CMD_WRITE_DISABLE).status().follow_exit()
        self.link.run(r.b, check=False)

    def read(self, addr, size):
        return self.reader.read(addr, size, read_reg=self.read_reg, chunk=size)

    # ─── Primitives ─────────────────────────────────────────────────
    def erase_sector(self, addr):
        """Sector erase, then only status reads (no other command) until BUSY
        clears, like iteflash's spi_poll_busy. The status can read ff while
        the e-flash is busy: that counts as busy. Returns the erase time."""
        if addr % SECTOR:
            raise FlashError(f'sector erase at 0x{addr:06X}: not 1 KB aligned')
        r = _Regs().follow_enter().spi(SPI_CMD_WRITE_ENABLE).status()
        _addr3(r.spi(SPI_CMD_SECTOR_ERASE_1K), addr).status(ERASE_FIRST_POLL_US)
        ok = False
        try:
            we, st = self.link.run(r.b).data
            _check_wel(we, f'erase 0x{addr:06X}')
            t0 = time.time()
            trail = [(0.0, st)]
            while st & STATUS_BUSY:
                if time.time() - t0 > self.erase_timeout_s:
                    raise FlashError(f'erase 0x{addr:06X}: still busy after '
                                     f'{self.erase_timeout_s:.0f}s; status trail '
                                     f'{_trail(trail)}. If the sector is unchanged, the '
                                     'erase is blocked (was --keep-protect-path used?)')
                time.sleep(0.001)
                st = self.link.run(Batch().aw(0x08).dr(1)).data[0]
                if st != trail[-1][1]:
                    trail.append((time.time() - t0, st))
            self.erase_log.append((addr, time.time() - t0, trail))
            ok = True
            return time.time() - t0
        finally:
            self._close(0 if ok else MAX_WORD_DELAY_US)

    def program(self, addr, data):
        """AAI word program of data at addr (both even). The range must be
        erased. Every word is followed by RDSR and a status read, as in
        iteflash: on an IT5261 the words were only programmed with that read
        in place, though its value is useless (ff during AAI). So the status
        is not checked; word_delay_us is waited and the caller verifies."""
        if addr % 2 or len(data) % 2 or not data:
            raise FlashError('AAI program needs an even address and length')
        d = self.word_delay_us
        what = f'program 0x{addr:06X}'
        r = _Regs().follow_enter().spi(SPI_CMD_WRITE_ENABLE).status()
        _addr3(r.spi(SPI_CMD_WORD_PROGRAM), addr).w(0x08, data[0]).w(0x08, data[1]).status(d)
        first = True
        ok = False
        try:
            for i in range(2, len(data), 2):
                if (i // 2) % WORDS_PER_BATCH == 0:
                    out = self.link.run(r.b).data
                    if first:
                        _check_wel(out[0], what)
                        first = False
                    r = _Regs()
                r.spi(SPI_CMD_WORD_PROGRAM).w(0x08, data[i]).w(0x08, data[i + 1]).status(d)
            out = self.link.run(r.spi(SPI_CMD_WRITE_DISABLE).status().b).data
            if first:
                _check_wel(out[0], what)
            st, t0 = out[-1], time.time()
            while st & STATUS_BUSY:          # ff counts as busy
                if time.time() - t0 > LAST_WORD_TIMEOUT_S:
                    raise FlashError(f'{what}: still busy {LAST_WORD_TIMEOUT_S * 1000:.0f} ms '
                                     f'after the last word (status {st:02x})')
                st = self.link.run(Batch().aw(0x08).dr(1)).data[0]
            ok = True
        finally:
            self._close(0 if ok else MAX_WORD_DELAY_US)

    def _check_ref(self, sector):
        if not self.ref:
            return
        a, want = self.ref
        if sector <= a < sector + SECTOR:
            return
        base = a & ~0xFF
        got = self.read(base, 0x100)[a - base:a - base + len(want)]
        if got != want:
            raise FlashStuck(f'after erasing 0x{sector:06X}, 0x{a:06X} reads {got[:8].hex(" ")} '
                             f'instead of {want[:8].hex(" ")}: {STUCK_HINT}')

    # ─── One sector, checked ────────────────────────────────────────
    def write_sector(self, addr, data, log=print):
        """Erase, blank check, program, verify. Retries the whole sector."""
        last = None
        for attempt in range(1, ATTEMPTS + 1):
            try:
                self.erase_sector(addr)
                got = self.read(addr, SECTOR)
                if got != b'\xFF' * SECTOR:
                    raise FlashError(f'sector 0x{addr:06X} not blank after erase '
                                     f'({SECTOR - got.count(0xFF)} bytes left)')
                self._check_ref(addr)
                if data != b'\xFF' * SECTOR:
                    self.program(addr, data)
                got = self.read(addr, SECTOR)
                if got != data:
                    bad = [i for i in range(SECTOR) if got[i] != data[i]]
                    raise VerifyError(f'sector 0x{addr:06X} verify failed: {len(bad)} bytes '
                                      f'differ, first at 0x{addr + bad[0]:06X} (read '
                                      f'{got[bad[0]]:02x}, expected {data[bad[0]]:02x})')
                return attempt
            except FlashStuck as e:
                if last:
                    raise FlashStuck(f'{e}\n  (previous attempt: {last})') from e
                raise
            except VerifyError as e:
                last = e
                self.word_delay_us = min(max(self.word_delay_us * 4, 50), MAX_WORD_DELAY_US)
                log(f'  {e}; retrying with {self.word_delay_us} us per word')
            except FlashError as e:
                last = e
                log(f'  attempt {attempt}: {e}')
        raise FlashError(f'sector 0x{addr:06X} failed after {ATTEMPTS} attempts: {last}')


def image_problems(image):
    """Reasons not to write this file as an EC image (empty list if none)."""
    out = []
    if image.count(0xFF) == len(image):
        out.append('the image is blank (all 0xFF)')
    elif image.count(0x00) == len(image):
        out.append('the image is all 0x00')
    elif image.find(ITE_SIGNATURE, 0, 0x1000) < 0:
        out.append('no ITE signature (a run of 0xA5) in its first 4 KB, where the boot '
                   'ROM looks for it: is this an EC image for this chip?')
    return out


def runs(sectors):
    """'0x000400-0x0007FF, ...' for a list of sector addresses."""
    spans = []
    for a in sorted(sectors):
        if spans and spans[-1][1] == a:
            spans[-1][1] = a + SECTOR
        else:
            spans.append([a, a + SECTOR])
    return ', '.join(f'0x{a:06X}-0x{b - 1:06X}' for a, b in spans)


def plan(image, current, base, size, rewrite_all=False):
    """1 KB sectors of [base, base+size) to write. The sector holding
    address 0 (reset vector, ITE signature) goes last."""
    out = [a for a in range(base, base + size, SECTOR)
           if rewrite_all or image[a:a + SECTOR] != current[a:a + SECTOR]]
    if out and out[0] == 0:
        out = out[1:] + out[:1]
    return out


def write_image(writer, image, sectors, progress=None, should_stop=None, log=print):
    """Write the planned sectors. Stops cleanly between sectors on request."""
    for n, addr in enumerate(sectors):
        if should_stop and should_stop():
            raise FlashError(f'stopped by user: {n} of {len(sectors)} sectors written, '
                             f'next was 0x{addr:06X}')
        attempts = writer.write_sector(addr, image[addr:addr + SECTOR], log=log)
        if attempts > 1:
            log(f'  sector 0x{addr:06X} OK on attempt {attempts}')
        if progress:
            progress(n + 1, len(sectors))
