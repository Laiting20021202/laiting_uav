#!/usr/bin/env python3
"""Read only the fixed onboard USB 2 hub descriptor and port 2/4 status.

No reset, driver claim, configuration, SET_FEATURE, CLEAR_FEATURE, or port
power request exists in this tool. Only the verified hub's usbfs file opens.
Linux USBDEVFS_CONTROL requires O_RDWR even for these three IN requests.
Run with root privileges; the report directory/file are private (700/600).
"""
import argparse
import ctypes
from dataclasses import dataclass
import errno
import json
import os
from pathlib import Path
import re
import stat
import struct
import tempfile
import time


HUB_NAME = '1-2'
VENDOR, PRODUCT = '0bda', '5489'
DEFAULT_GENERATION = (1, 2)
PORTS = (2, 4)
CONTROL_TIMEOUT_MS = 250
MAX_SAMPLES = 20
MAX_SECONDS = 10.0
MIN_INTERVAL = 0.2
MAX_REPORT_BYTES = 65536
GET_HUB_DESCRIPTOR = (0xa0, 6, 0x2900, 0, 9)
GET_PORT_STATUS = {port: (0xa3, 0, 0, port, 4) for port in PORTS}
APPROVED_READS = frozenset((GET_HUB_DESCRIPTOR, *GET_PORT_STATUS.values()))
USBDEVFS_CONTROL = 0xc0185500


class ControlTransfer(ctypes.Structure):
    # /usr/include/linux/usbdevice_fs.h struct usbdevfs_ctrltransfer.
    _fields_ = [
        ('bRequestType', ctypes.c_uint8), ('bRequest', ctypes.c_uint8),
        ('wValue', ctypes.c_uint16), ('wIndex', ctypes.c_uint16),
        ('wLength', ctypes.c_uint16), ('timeout', ctypes.c_uint32),
        ('data', ctypes.c_void_p),
    ]


LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.ioctl.argtypes = (ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p)
LIBC.ioctl.restype = ctypes.c_int


class HubIdentityError(RuntimeError):
    pass


@dataclass(frozen=True)
class HubIdentity:
    device: Path
    bus: int
    number: int
    sysfs_inode: int


def require_root():
    if os.geteuid() != 0:
        raise PermissionError('This read-only hub probe requires root privileges')


def check_abi():
    offsets = (ControlTransfer.bRequestType.offset, ControlTransfer.bRequest.offset,
               ControlTransfer.wValue.offset, ControlTransfer.wIndex.offset,
               ControlTransfer.wLength.offset, ControlTransfer.timeout.offset,
               ControlTransfer.data.offset)
    if (ctypes.sizeof(ctypes.c_void_p) != 8 or ctypes.sizeof(ControlTransfer) != 24
            or offsets != (0, 1, 2, 4, 6, 8, 16)):
        raise RuntimeError('Unsupported native USBDEVFS_CONTROL ABI')


def validate_request(request):
    if (type(request) is not tuple or len(request) != 5
            or any(type(value) is not int for value in request)
            or request not in APPROVED_READS):
        raise ValueError('Only the three fixed hub IN requests are allowed')
    return request


def parse_generation(value):
    if not isinstance(value, str) or not re.fullmatch(r'1:[1-9][0-9]*', value):
        raise ValueError('Expected the fixed hub bus 1 and positive device number')
    bus, number = map(int, value.split(':'))
    if number > 127:
        raise ValueError('USB device number exceeds the USB address range')
    return bus, number


def hub_identity(expected_generation=DEFAULT_GENERATION,
                 usb_root=Path('/sys/bus/usb/devices')):
    # Never scan for a similar hub when the fixed topology path is missing.
    fixed = usb_root/HUB_NAME
    try:
        device = fixed.resolve(strict=True)
        if device.name != HUB_NAME:
            raise HubIdentityError('Fixed USB hub topology is different')
        attributes = {name: (device/name).read_text().strip().lower()
                      for name in ('idVendor', 'idProduct', 'bDeviceClass',
                                   'busnum', 'devnum', 'speed')}
        if (attributes['idVendor'] != VENDOR or attributes['idProduct'] != PRODUCT
                or attributes['bDeviceClass'] != '09' or attributes['speed'] != '480'):
            raise HubIdentityError('Fixed USB hub identity or USB 2 speed differs')
        if not re.fullmatch(r'[1-9][0-9]*', attributes['busnum']):
            raise HubIdentityError('Invalid hub bus number')
        if not re.fullmatch(r'[1-9][0-9]*', attributes['devnum']):
            raise HubIdentityError('Invalid hub device number')
        generation = (int(attributes['busnum']), int(attributes['devnum']))
        if generation != expected_generation or generation[0] != 1 or generation[1] > 127:
            raise HubIdentityError('Fixed USB hub generation changed')
        return HubIdentity(device, *generation, device.stat().st_ino)
    except OSError as error:
        raise HubIdentityError('Fixed USB hub is unavailable') from error


class HubReadOnlyProbe:
    def __init__(self, expected_generation=DEFAULT_GENERATION):
        self.expected_generation = expected_generation
        self.identity = None
        self.fd = None
        self.node_inode = None

    def _verify(self):
        current = hub_identity(self.expected_generation)
        if self.identity is not None and current != self.identity:
            raise HubIdentityError('Fixed USB hub identity changed during probe')
        if self.fd is not None:
            info = os.stat(self.node, follow_symlinks=False)
            if (not stat.S_ISCHR(info.st_mode) or info.st_ino != self.node_inode
                    or info.st_rdev != self.node_rdev):
                raise HubIdentityError('Fixed USB hub usbfs node changed during probe')
        return current

    def __enter__(self):
        require_root()
        check_abi()
        self.identity = self._verify()
        self.node = f'/dev/bus/usb/{self.identity.bus:03d}/{self.identity.number:03d}'
        self.node_rdev = os.makedev(189, (self.identity.bus-1)*128+self.identity.number-1)
        self.fd = os.open(self.node, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            info = os.fstat(self.fd)
            if not stat.S_ISCHR(info.st_mode) or info.st_rdev != self.node_rdev:
                raise HubIdentityError('Opened file is not the fixed hub usbfs node')
            self.node_inode = info.st_ino
            self._verify()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def read(self, request, deadline):
        request = validate_request(request)
        if self.fd is None:
            raise RuntimeError('Verified hub usbfs file is not open')
        self._verify()
        remaining_ms = int((deadline-time.monotonic())*1000)
        if remaining_ms < CONTROL_TIMEOUT_MS:
            raise TimeoutError('Read-only hub probe time limit reached')
        buffer = ctypes.create_string_buffer(request[4])
        arguments = ControlTransfer(*request, CONTROL_TIMEOUT_MS, ctypes.addressof(buffer))
        count = LIBC.ioctl(self.fd, USBDEVFS_CONTROL, ctypes.byref(arguments))
        if count < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        self._verify()
        if count != request[4]:
            raise ValueError('Hub IN request returned an unexpected byte count')
        return buffer.raw


def decode_descriptor(raw):
    if len(raw) != 9 or raw[0:3] != bytes((9, 0x29, 4)):
        raise ValueError('Expected this fixed USB 2 four-port hub descriptor')
    characteristics = int.from_bytes(raw[3:5], 'little')
    return {
        'descriptor_length': raw[0], 'descriptor_type': raw[1], 'ports': raw[2],
        'characteristics': characteristics,
        'power_switching': ('ganged', 'individual', 'none', 'reserved')[characteristics & 3],
        'overcurrent_protection': ('ganged', 'individual', 'none', 'reserved')[
            (characteristics >> 3) & 3],
        'compound_device': bool(characteristics & 4),
        'power_good_delay_ms': raw[5]*2, 'hub_controller_current_ma': raw[6],
    }


def decode_port_status(raw):
    if len(raw) != 4:
        raise ValueError('USB 2 port status must contain exactly four bytes')
    status_word, change_word = struct.unpack('<HH', raw)
    fields = {name: bool(status_word & mask) for name, mask in (
        ('connected', 1), ('enabled', 2), ('suspended', 4), ('overcurrent', 8),
        ('reset', 16), ('l1', 32), ('power', 256), ('low_speed', 512), ('high_speed', 1024),
    )}
    speed = ('disconnected' if not fields['connected'] else
             'not_enabled' if not fields['enabled'] else
             'invalid' if fields['low_speed'] and fields['high_speed'] else
             'low' if fields['low_speed'] else 'high' if fields['high_speed'] else 'full')
    return {
        'status_word': status_word, 'change_word': change_word, **fields, 'speed': speed,
        'change': {name: bool(change_word & mask) for name, mask in (
            ('connection', 1), ('enable', 2), ('suspend', 4), ('overcurrent', 8),
            ('reset', 16), ('l1', 32),
        )},
    }


def validate_limits(samples, interval, max_seconds):
    if type(samples) is not int or not 1 <= samples <= MAX_SAMPLES:
        raise ValueError('Samples must be between 1 and 20')
    if not isinstance(interval, (int, float)) or not MIN_INTERVAL <= interval <= 1:
        raise ValueError('Sample interval must be between 0.2 and 1 second')
    if not isinstance(max_seconds, (int, float)) or not 1 <= max_seconds <= MAX_SECONDS:
        raise ValueError('Probe duration must be between 1 and 10 seconds')


def collect(samples=MAX_SAMPLES, interval=MIN_INTERVAL, max_seconds=MAX_SECONDS,
            expected_generation=DEFAULT_GENERATION):
    require_root()
    validate_limits(samples, interval, max_seconds)
    started = time.monotonic()
    deadline = started+max_seconds
    report = {
        'schema_version': 1, 'read_only': True, 'started_monotonic': started,
        'hub': {'device': HUB_NAME, 'vendor': VENDOR, 'product': PRODUCT,
                'usb_generation': ':'.join(map(str, expected_generation))},
        'control_timeout_ms': CONTROL_TIMEOUT_MS, 'ports': list(PORTS),
        'samples': [], 'stop_reason': 'sample_limit',
        'change_bits_are_latched': True,
    }
    try:
        with HubReadOnlyProbe(expected_generation) as probe:
            try:
                report['hub']['descriptor'] = decode_descriptor(probe.read(GET_HUB_DESCRIPTOR, deadline))
            except TimeoutError as error:
                if error.errno is None:
                    raise
                report['hub']['descriptor_error'] = {'type': type(error).__name__, 'errno': error.errno}
            except (OSError, ValueError) as error:
                report['hub']['descriptor_error'] = {'type': type(error).__name__,
                                                     'errno': getattr(error, 'errno', None)}
            for _ in range(samples):
                point = {'monotonic': time.monotonic(), 'ports': {}}
                report['samples'].append(point)
                for port in PORTS:
                    try:
                        point['ports'][str(port)] = decode_port_status(probe.read(GET_PORT_STATUS[port], deadline))
                    except TimeoutError as error:
                        if error.errno is None:
                            raise
                        point['ports'][str(port)] = {'error_type': type(error).__name__, 'errno': error.errno}
                    except OSError as error:
                        point['ports'][str(port)] = {'error_type': type(error).__name__, 'errno': error.errno}
                        if error.errno in (errno.ENODEV, errno.ENOENT):
                            raise HubIdentityError('Hub disappeared during an IN request') from error
                    except ValueError as error:
                        point['ports'][str(port)] = {'error_type': type(error).__name__}
                if len(report['samples']) < samples:
                    remaining = deadline-time.monotonic()
                    if remaining < interval+CONTROL_TIMEOUT_MS/1000:
                        raise TimeoutError('Read-only hub probe time limit reached')
                    time.sleep(interval)
    except TimeoutError:
        report['stop_reason'] = 'time_limit'
    except HubIdentityError:
        report['stop_reason'] = 'hub_identity_unavailable_or_changed'
    except OSError as error:
        report['stop_reason'] = 'hub_open_or_io_error'
        report['error'] = {'type': type(error).__name__, 'errno': error.errno}
    report['elapsed_seconds'] = round(time.monotonic()-started, 6)
    return report


def save_report(report, directory):
    directory = Path(directory)
    if not directory.is_absolute():
        raise ValueError('Use an absolute private report directory')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise PermissionError('Report directory must be owned by the caller with mode 700')
    payload = json.dumps(report, sort_keys=True, indent=2).encode()+b'\n'
    if len(payload) > MAX_REPORT_BYTES:
        raise ValueError('Read-only hub report exceeds its bounded size')
    fd, temporary = tempfile.mkstemp(prefix='.hub-status-', dir=directory)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'wb') as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        destination = directory/'hub-status.json'
        os.replace(temporary, destination)
        return destination
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--expected-generation', default='1:2')
    parser.add_argument('--samples', type=int, default=MAX_SAMPLES)
    parser.add_argument('--interval', type=float, default=MIN_INTERVAL)
    parser.add_argument('--max-seconds', type=float, default=MAX_SECONDS)
    parser.add_argument('--directory', type=Path,
                        default=Path('/var/lib/laiting-uav-recovery')/
                        ('hub-readonly-'+time.strftime('%Y%m%d-%H%M%S')))
    args = parser.parse_args()
    try:
        require_root()
        generation = parse_generation(args.expected_generation)
        validate_limits(args.samples, args.interval, args.max_seconds)
        report = collect(args.samples, args.interval, args.max_seconds, generation)
        location = save_report(report, args.directory)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(1, f'Read-only hub probe refused: {type(error).__name__}\n')
    print(json.dumps({'report': str(location), 'samples': len(report['samples']),
                      'stop_reason': report['stop_reason'],
                      'elapsed_seconds': report['elapsed_seconds']}))


if __name__ == '__main__':
    main()
