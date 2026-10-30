"""Multi-pass flash dump and sanity checks on the result."""

import hashlib

# ITE EC images carry a run of 0xA5 bytes in their signature block
# (IT5261 at 0x80: A5 A5 A5 A5 A5 A5 A7 14 85 12 ..., then the image name)
ITE_SIGNATURE = b'\xA5' * 6


def dump(eflash, base, size, passes=2, read_reg=0x09, chunk=4096,
         progress=None):
    """Read the range `passes` times. Returns (list of images, mismatch offsets)."""
    images = []
    for p in range(passes):
        cb = None
        if progress:
            def cb(done, total, p=p):
                progress(p, passes, done, total)
        images.append(eflash.read(base, size, read_reg=read_reg, chunk=chunk,
                                  progress=cb))
    diffs = []
    for img in images[1:]:
        diffs += [i for i in range(size) if img[i] != images[0][i]]
    return images, sorted(set(diffs))


def analyze(data, base=0):
    """Human-readable remarks about an image."""
    notes = []
    n = len(data)
    if n == 0:
        return ['empty image']
    notes.append(f'sha256 {hashlib.sha256(data).hexdigest()}')
    distinct = len(set(data))
    if data.count(0xFF) == n:
        notes.append('WARNING: all bytes are 0xFF (blank chip, or nothing is answering)')
    elif data.count(0x00) == n:
        notes.append('WARNING: all bytes are 0x00 (bus stuck low or reads not executed)')
    elif distinct <= 2:
        notes.append(f'WARNING: only {distinct} distinct byte values: suspicious')
    if n >= 512 and distinct > 2:
        page = data[:256]
        if all(data[i:i + 256] == page for i in range(0, n - 255, 256)):
            notes.append('WARNING: every 256-byte page is identical: '
                         'the read address is not advancing')
    pos = data.find(ITE_SIGNATURE)
    if pos >= 0:
        sig = data[pos:pos + 16]
        notes.append(f'ITE signature found at 0x{base + pos:06X}: {sig.hex(" ")}')
    elif distinct > 2:
        notes.append('no ITE 0xA5 signature run found (fine for a partial range)')
    ff = data.count(0xFF) * 100 // n
    notes.append(f'{ff}% of bytes are 0xFF, {distinct} distinct byte values')
    return notes
