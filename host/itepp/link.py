"""Transport to the Pico: USB (pyusb) or the simulator, same interface."""

from . import proto
from .proto import Batch, Result

USB_VID = 0x1209
USB_PID = 0x0001


class LinkError(Exception):
    """USB or framing problem."""


class OpError(Exception):
    """An op failed on the Pico (e.g. nWait timeout)."""

    def __init__(self, result, batch):
        self.result = result
        self.status = result.status
        self.index = result.index
        super().__init__(f'{result.status_name} at {batch.describe(result.index)}')


class BaseLink:
    def transact(self, payload, expect):
        """Send one encoded batch, return expect + trailer bytes."""
        raise NotImplementedError

    def close(self):
        pass

    def run(self, batch, check=True):
        raw = self.transact(batch.encode(), batch.expect)
        if len(raw) != batch.expect + proto.TRAILER_LEN:
            raise LinkError(f'response length {len(raw)}, '
                            f'expected {batch.expect + proto.TRAILER_LEN}')
        trailer = raw[-proto.TRAILER_LEN:]
        if trailer[3] != proto.RSP_MAGIC:
            raise LinkError('bad response trailer (out of sync?)')
        res = Result(bytes(raw[:-proto.TRAILER_LEN]), trailer[0],
                     trailer[1] | (trailer[2] << 8))
        if check and not res.ok:
            raise OpError(res, batch)
        return res

    def hello(self):
        res = self.run(Batch().hello())
        d = res.data
        if d[:4] != b'ITPP':
            raise LinkError(f'unexpected HELLO reply {d.hex()} (wrong firmware?)')
        if d[4] != proto.PROTO_VERSION:
            raise LinkError(f'firmware protocol v{d[4]}, host expects '
                            f'v{proto.PROTO_VERSION}: update firmware or host')
        return {'proto': d[4], 'fw': f'{d[5]}.{d[6]}', 'lines': d[7]}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class UsbLink(BaseLink):
    """Pico over USB bulk endpoints (vendor class)."""

    def __init__(self, vid=USB_VID, pid=USB_PID, timeout_ms=2000):
        try:
            import usb.core
            import usb.util
        except ImportError as e:
            raise LinkError('pyusb is not installed: run host/setup_venv.sh '
                            'and use host/.venv/bin/python') from e
        self._usb = usb
        self.timeout_ms = timeout_ms
        self.dev = None

        try:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
        except usb.core.NoBackendError as e:
            raise LinkError('libusb not found: sudo apt install libusb-1.0-0') from e
        if dev is None:
            raise LinkError(
                f'Pico ISP adapter not found (VID={vid:#06x} PID={pid:#06x}). '
                'Check the USB cable, `lsusb`, and that the ite_pp_isp '
                'firmware is flashed.')
        self.dev = dev
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (NotImplementedError, usb.core.USBError):
            pass
        try:
            # don't re-send SET_CONFIGURATION to an already configured device
            try:
                dev.get_active_configuration()
            except usb.core.USBError:
                dev.set_configuration()
            usb.util.claim_interface(dev, 0)
        except usb.core.USBError as e:
            raise LinkError(f'cannot open device: {e} '
                            '(permissions? install the udev rule)') from e

        intf = dev.get_active_configuration()[(0, 0)]
        out = usb.util.ENDPOINT_OUT
        self.ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) == out)
        self.ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress) != out)
        if self.ep_out is None or self.ep_in is None:
            raise LinkError('bulk endpoints not found (wrong firmware?)')
        self._drain()

    def _drain(self):
        """Discard leftovers from an interrupted previous session."""
        while True:
            try:
                if not self.ep_in.read(4096, 50):
                    continue
            except self._usb.core.USBError:
                return

    def transact(self, payload, expect):
        usb = self._usb
        want = expect + proto.TRAILER_LEN
        # allow ~20 us per expected byte on top of the base timeout
        timeout = self.timeout_ms + want // 50
        try:
            self.ep_out.write(payload, timeout)
        except usb.core.USBError as e:
            raise LinkError(f'USB write failed: {e}') from e

        buf = bytearray()
        while len(buf) < want:
            size = (want - len(buf) + 63) // 64 * 64
            try:
                buf += self.ep_in.read(size, timeout)
            except usb.core.USBError as e:
                tail = ''
                if len(buf) >= 4 and buf[-1] == proto.RSP_MAGIC:
                    st = proto.STATUS_NAMES.get(buf[-4], hex(buf[-4]))
                    tail = f', firmware ended early with {st}'
                raise LinkError(f'USB read failed after {len(buf)}/{want} bytes: {e}{tail}') from e
        return bytes(buf)

    def close(self):
        if self.dev is not None:
            usb = self._usb
            try:
                usb.util.release_interface(self.dev, 0)
            except usb.core.USBError:
                pass
            usb.util.dispose_resources(self.dev)
            self.dev = None
