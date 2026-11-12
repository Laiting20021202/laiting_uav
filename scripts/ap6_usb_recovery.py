#!/usr/bin/env python3
"""Inspect AP6 USB; manually recover this exact device once (requires root)."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import stat
import time

IDENTITY = ('2a58', 'a002', '330032000D51333331333534')
SYS_USB = Path('/sys/bus/usb')
USBFS = Path('/dev/bus/usb')
USBDEVFS_RESET = 0x5514  # _IO('U', 20), linux/usbdevice_fs.h on this host.


def read(path):
    return path.read_text().strip()


def find_ap6(root=SYS_USB):
    matches = []
    for device in (root / 'devices').iterdir():
        try:
            if tuple(read(device / name) for name in ('idVendor', 'idProduct', 'serial')) == IDENTITY:
                matches.append(device)
        except OSError:
            continue
    if len(matches) != 1:
        raise RuntimeError(f'Expected exactly one AP6 USB device; found {len(matches)}')
    device = matches[0]
    return {'device': device.name,
            'generation': read(device / 'busnum') + ':' + read(device / 'devnum'),
            'interface': device.name + ':1.0'}


def verify(target, root=SYS_USB, bound=True):
    current = find_ap6(root)
    if current != target:
        raise RuntimeError('AP6 USB generation changed; refusing stale recovery')
    interface = root / 'devices' / target['interface']
    driver = interface / 'driver'
    if bound and driver.resolve() != (root / 'drivers/cdc_acm').resolve():
        raise RuntimeError('AP6 control interface is not bound to cdc_acm')
    if not bound and driver.exists():
        raise RuntimeError('AP6 interface was rebound by another worker')


def inspect(root=SYS_USB):
    target = find_ap6(root)
    verify(target, root)
    device = root / 'devices' / target['device']
    return {**target, 'identity': IDENTITY,
            'power_control': read(device / 'power/control'),
            'runtime_status': read(device / 'power/runtime_status')}


def rebind(expected_generation, root=SYS_USB):
    target = find_ap6(root)
    if target['generation'] != expected_generation:
        raise RuntimeError('Expected AP6 generation no longer exists; inspect again')
    verify(target, root)
    interface = target['interface']
    # Recheck immediately before the single mutation; never write USB reset,
    # authorization, hub, controller, or a device selected by tty numbering.
    verify(target, root)
    (root / 'drivers/cdc_acm/unbind').write_text(interface)
    try:
        time.sleep(.5)
    finally:
        # Don't bind a replacement device that reused the same physical port.
        verify(target, root, bound=False)
        (root / 'drivers/cdc_acm/bind').write_text(interface)
    return {'rebound': True, **target,
            'telemetry_verified': False}


def reset(expected_generation, root=SYS_USB, usbfs=USBFS):
    """One USB-level reset of the verified AP6; use for ground maintenance.

    This can interrupt telemetry. It is never an automatic in-flight recovery
    and sends no MAVLink flight commands. A successful ioctl is not proof of
    restored telemetry.
    """
    target = find_ap6(root)
    if target['generation'] != expected_generation:
        raise RuntimeError('Expected AP6 generation no longer exists; inspect again')
    verify(target, root)
    bus, number = map(int, target['generation'].split(':'))
    if not 1 <= bus or not 1 <= number <= 127:
        raise RuntimeError('Invalid AP6 USB address')
    expected_rdev = os.makedev(189, (bus - 1) * 128 + number - 1)
    device = root / 'devices' / target['device']
    major, minor = map(int, read(device / 'dev').split(':'))
    if os.makedev(major, minor) != expected_rdev:
        raise RuntimeError('AP6 sysfs USB node identity mismatch')
    node = usbfs / f'{bus:03d}' / f'{number:03d}'
    fd = os.open(node, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISCHR(info.st_mode) or info.st_rdev != expected_rdev:
            raise RuntimeError('Opened file is not the verified AP6 USB node')
        verify(target, root)
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)
    return {'usb_reset_returned_success': True, **target,
            'telemetry_verified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('inspect', help='Read identity and USB generation without mutations')
    recover = commands.add_parser('rebind', help='Rebind this AP6 CDC interface once; no flight commands')
    recover.add_argument('--expected-generation', required=True)
    reset_command = commands.add_parser('reset', help='Ground maintenance: reset only this AP6 USB once; interrupts telemetry')
    reset_command.add_argument('--expected-generation', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'inspect':
            result = inspect()
        else:
            if os.geteuid() != 0:
                raise PermissionError('AP6 USB recovery needs root; no changes made')
            # One root operation at a time, including calls from another terminal.
            flags = os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC
            fd = os.open('/run/laiting-ap6-rebind.lock', flags, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                action = rebind if args.action == 'rebind' else reset
                result = action(args.expected_generation)
            finally:
                os.close(fd)
        print(json.dumps(result, indent=2))
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f'{type(exc).__name__}: {exc}\n')


if __name__ == '__main__':
    main()
