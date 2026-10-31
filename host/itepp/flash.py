"""E-flash access in DBGR follow mode, read side (writes: program.py).

Follow mode maps the e-flash as a SPI device behind the EC-indirect
registers: ECINDAR3..0 = regs 0x07..0x04, ECINDDR = reg 0x08.
ECINDAR = 0x7FFFFExx: a write to ECINDDR raises FSCE# (ends a command)
ECINDAR = 0x7FFFFDxx: ECINDDR writes/reads shift bytes on the SPI bus
Ported from Chromium EC util/iteflash.cc.
"""

from .proto import Batch

SPI_CMD_READ_STATUS = 0x05
SPI_CMD_FAST_READ = 0x0B
SPI_CMD_RDID = 0x9F

# Only these opcodes may ever be sent: nothing here can modify the flash
READ_ONLY_OPCODES = {SPI_CMD_READ_STATUS, SPI_CMD_FAST_READ, SPI_CMD_RDID}

EFLASH_TYPE_8315 = '8315'
EFLASH_TYPE_KGD = 'KGD'

# Fast read must be re-sent at every 256 KB boundary
READ_BOUNDARY = 0x40000

# Register burst-read after fast read: 0x09 is what iteflash uses
# (I2C block address), 0x08 reads ECINDDR one SPI byte at a time
READ_REG_BLOCK = 0x09
READ_REG_DATA = 0x08


class FlashError(Exception):
    pass


def _follow_enter(b):
    return b.reg_writes([(0x07, 0x7F), (0x06, 0xFF), (0x05, 0xFE),
                         (0x04, 0x00), (0x08, 0x00)])


def _spi_command(b, cmd):
    if cmd not in READ_ONLY_OPCODES:
        raise ValueError(f'SPI opcode {cmd:#04x} refused on the read path')
    return b.reg_writes([(0x05, 0xFE), (0x08, 0x00), (0x05, 0xFD), (0x08, cmd)])


def _fast_read_cmd(b, addr, read_reg):
    _spi_command(b, SPI_CMD_FAST_READ)
    b.reg_writes([(0x08, (addr >> 16) & 0xFF), (0x08, (addr >> 8) & 0xFF),
                  (0x08, addr & 0xFF), (0x08, 0x00)])
    if read_reg != READ_REG_DATA:
        b.aw(read_reg)
    return b


class Eflash:
    def __init__(self, link):
        self.link = link

    def follow_exit(self):
        self.link.run(Batch().reg_writes([(0x07, 0x00), (0x06, 0x00)]))

    def read_id(self):
        """JEDEC ID through follow mode (check_flashid). Returns 16 bytes."""
        b = Batch().reg_writes([(0x07, 0x7F), (0x06, 0xFF), (0x04, 0x00),
                                (0x05, 0xFE), (0x08, 0x00), (0x05, 0xFD),
                                (0x08, SPI_CMD_RDID)])
        b.dr(16)
        return self.link.run(b).data

    @staticmethod
    def flash_type(ident):
        if ident[:3] == b'\xFF\xFF\xFE':
            return EFLASH_TYPE_8315
        if ident[0] in (0xC8, 0xEF):
            return EFLASH_TYPE_KGD
        return None

    def read(self, addr, size, read_reg=READ_REG_BLOCK, chunk=4096,
             progress=None):
        """Read size bytes at addr (page aligned) with SPI fast read."""
        if addr & 0xFF:
            raise FlashError(f'read must start on a 256-byte page: 0x{addr:X}')
        out = bytearray()
        self.link.run(_fast_read_cmd(_follow_enter(Batch()), addr, read_reg))
        remaining = size
        while remaining:
            n = min(chunk, remaining, READ_BOUNDARY - (addr % READ_BOUNDARY))
            out += self.link.run(Batch().dr(n)).data
            addr += n
            remaining -= n
            if progress:
                progress(size - remaining, size)
            if remaining and addr % READ_BOUNDARY == 0:
                self.link.run(_fast_read_cmd(Batch(), addr, read_reg))
        self.follow_exit()
        return bytes(out)
