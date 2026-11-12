#!/usr/bin/env python3
"""Install/load/rollback official usbnet fixes for this Jetson and verified D-Link."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

PACKAGE = 'laiting-uav-usbnet'
VERSION = '1.0'
KERNEL = '5.15.185-tegra'
MODULE_VERSION = 'laiting-usbnet-1.0'
SOURCE = Path(__file__).resolve().parent.parent/'kernel/usbnet-backport'
INSTALLED_SOURCE = Path('/usr/src')/f'{PACKAGE}-{VERSION}'
BACKUP = Path('/var/backups/laiting-uav/usbnet-20261006')
MODEM_SCRIPT = Path('/usr/local/lib/laiting-uav/modem_recovery.py')
RECOVERY_UNIT = 'uav-modem-recovery.service'
INTERFACE = 'enxac0033aa9633'
PROFILE = '8391cb7e-8a3a-317c-bf18-612aa9c5300e'


def run(*args, timeout=180):
    return subprocess.run(list(args), check=True, text=True, capture_output=True, timeout=timeout)


def device_check():
    if os.uname().release != KERNEL:
        raise RuntimeError('This backport is restricted to the verified Jetson kernel')
    net = Path('/sys/class/net')/INTERFACE
    expected = {'idVendor': '1286', 'idProduct': '4e31', 'serial': '200806006809080000'}
    if (net/'address').exists():
        if (net/'address').read_text().strip().lower() != 'ac:00:33:aa:96:33':
            raise RuntimeError('Expected D-Link MAC is unavailable')
        device = (net/'device').resolve(strict=True).parent
    else:
        # A failed network driver may remove the interface. Rollback and
        # recovery must still identify the same USB device by its exact serial.
        matches = []
        for candidate in Path('/sys/bus/usb/devices').iterdir():
            try:
                if all((candidate/name).read_text().strip() == value
                       for name, value in expected.items()):
                    matches.append(candidate.resolve())
            except OSError:
                continue
        if len(matches) != 1:
            raise RuntimeError('Expected exactly one matching D-Link USB identity')
        device = matches[0]
    if any((device/name).read_text().strip() != value for name, value in expected.items()):
        raise RuntimeError('USB identity differs from the verified D-Link')
    return device


def validate_abi(module):
    known = {parts[1]: parts[0] for line in
             Path(f'/lib/modules/{KERNEL}/build/Module.symvers').read_text().splitlines()
             if len(parts := line.split()) >= 4}
    # DKMS cleans Module.symvers after building; validate the actual ELF module.
    exports = {parts[1]: parts[0] for line in
               run('modprobe', '--show-exports', str(module)).stdout.splitlines()
               if len(parts := line.split()) == 2}
    if not exports:
        raise RuntimeError('Backport exported-symbol list is empty')
    for name, crc in exports.items():
        if known.get(name) != crc:
            raise RuntimeError(f'Exported ABI mismatch: {name}')
    imports = run('modprobe', '--dump-modversions', str(module)).stdout
    for line in imports.splitlines():
        crc, name = line.split()
        if known.get(name) != crc:
            raise RuntimeError(f'Imported ABI mismatch: {name}')
    if run('modinfo', '-F', 'vermagic', str(module)).stdout.split()[0] != KERNEL:
        raise RuntimeError('Backport kernel version mismatch')
    if run('modinfo', '-F', 'version', str(module)).stdout.strip() != MODULE_VERSION:
        raise RuntimeError('Backport version marker is missing')


def install():
    device_check()
    metadata = json.loads((SOURCE/'provenance.json').read_text())
    digest = hashlib.sha256((SOURCE/'usbnet.c').read_bytes()).hexdigest()
    if digest != metadata['backport_sha256']:
        raise RuntimeError('Backport source differs from reviewed provenance')
    BACKUP.mkdir(parents=True, exist_ok=True, mode=0o700)
    stock = Path(f'/lib/modules/{KERNEL}/kernel/drivers/net/usb/usbnet.ko')
    backup = BACKUP/'usbnet.stock.ko'
    if not backup.exists():
        shutil.copyfile(stock, backup)
        backup.chmod(0o600)
    INSTALLED_SOURCE.mkdir(parents=True, exist_ok=True, mode=0o755)
    for name in ('usbnet.c', 'Makefile', 'dkms.conf', 'provenance.json', 'COPYING'):
        path = SOURCE/name
        if path.is_file():
            shutil.copyfile(path, INSTALLED_SOURCE/name)
            (INSTALLED_SOURCE/name).chmod(0o644)
    status = run('dkms', 'status', '-m', PACKAGE, '-v', VERSION).stdout
    if not status.strip():
        run('dkms', 'add', '-m', PACKAGE, '-v', VERSION)
    print('Building official usbnet fixes against installed NVIDIA headers', flush=True)
    result = run('dkms', 'build', '--force', '-m', PACKAGE, '-v', VERSION, '-k', KERNEL, timeout=300)
    print(result.stdout[-1200:], flush=True)
    module = Path('/var/lib/dkms')/PACKAGE/VERSION/KERNEL/'aarch64/module/usbnet.ko'
    validate_abi(module)
    result = run('dkms', 'install', '-m', PACKAGE, '-v', VERSION, '-k', KERNEL)
    print(result.stdout[-1200:], flush=True)
    selected = run('modinfo', '-F', 'filename', 'usbnet').stdout.strip()
    if run('modinfo', '-F', 'version', 'usbnet').stdout.strip() != MODULE_VERSION:
        raise RuntimeError('depmod did not select the installed backport')
    print(f'Installed and ABI-verified: {selected}; load step pending', flush=True)


def reload_driver(rollback=False):
    device = device_check()
    expected_interfaces = {f'{device.name}:1.0', f'{device.name}:1.1'}
    driver = Path('/sys/bus/usb/drivers/cdc_ether')
    bound = {entry.name for entry in driver.iterdir() if entry.is_symlink() and ':' in entry.name}
    if (not bound and not rollback) or not bound <= expected_interfaces:
        raise RuntimeError('Other CDC Ethernet devices are present; refusing driver reload')
    holders_root = Path('/sys/module/usbnet/holders')
    holders = {path.name for path in holders_root.iterdir()} if holders_root.is_dir() else set()
    if (holders != {'cdc_ether'} and not rollback) or not holders <= {'cdc_ether'}:
        raise RuntimeError('Other USB network drivers are active; refusing driver reload')
    run('systemctl', 'stop', RECOVERY_UNIT, timeout=15)
    try:
        run('modprobe', '-r', 'cdc_ether', timeout=20)
        run('modprobe', '-r', 'usbnet', timeout=20)
        if rollback:
            run('dkms', 'remove', '-m', PACKAGE, '-v', VERSION, '--all')
        run('modprobe', 'usbnet', timeout=20)
        loaded_version = Path('/sys/module/usbnet/version')
        if not rollback and (not loaded_version.exists() or
                             loaded_version.read_text().strip() != MODULE_VERSION):
            raise RuntimeError('The active module is not the verified backport')
        run('modprobe', 'cdc_ether', timeout=20)
        run('nmcli', '--wait', '20', 'connection', 'up', 'uuid', PROFILE,
            'ifname', INTERFACE, timeout=25)
    except Exception:
        # Recover the stock module path if activation fails. Existing application
        # and flight-controller serial services are not stopped or reset.
        for name in ('cdc_ether', 'usbnet'):
            subprocess.run(['modprobe', '-r', name], capture_output=True, timeout=20)
        subprocess.run(['dkms', 'remove', '-m', PACKAGE, '-v', VERSION, '--all'],
                       capture_output=True, timeout=180)
        run('modprobe', 'usbnet', timeout=20)
        run('modprobe', 'cdc_ether', timeout=20)
        run('nmcli', '--wait', '20', 'connection', 'up', 'uuid', PROFILE,
            'ifname', INTERFACE, timeout=25)
        raise
    finally:
        run('systemctl', 'start', RECOVERY_UNIT, timeout=20)
    deadline = time.monotonic()+60
    while time.monotonic() < deadline:
        sample = json.loads(run('/usr/bin/python3', '-I', str(MODEM_SCRIPT), 'check', timeout=10).stdout)
        if sample['gateway'] and sample['internet'] and sample['public']:
            print(json.dumps(sample, indent=2), flush=True)
            print('4G gateway, bound Internet and public website verified', flush=True)
            return
        time.sleep(2)
    raise RuntimeError('Driver is active, but 4G public health did not recover within validation window')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'load', 'rollback'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Local administrator authentication is required')
    if args.action == 'install':
        install()
    else:
        reload_driver(rollback=args.action == 'rollback')


if __name__ == '__main__':
    main()
