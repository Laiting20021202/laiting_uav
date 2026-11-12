#!/usr/bin/env python3
"""Read-only verification after the explicitly authorized xHCI kernel reboot."""
from concurrent.futures import ThreadPoolExecutor
import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import time

EXPECTED_IMAGE = '898fb51a3ee68920f4b04d841663a9a56567c5d4f0941f4402ebbb5e00f24160'
EXPECTED_VERSION = '#1 SMP PREEMPT Tue Oct 6 13:38:43 CST 2026'
HALT_IMAGE = '4bf3a99e78cb4dc528a11813d2dad334dfe3dee0dd4be81cf992876352756e5e'
HALT_VERSION = '#2 SMP PREEMPT Wed Oct 7 14:34:16 CST 2026'
INTERFACE = 'enxac0033aa9633'
OUTPUT = Path('/var/lib/laiting-uav-xhci-validation')


def command(args, timeout=10):
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {'code': p.returncode, 'stdout': p.stdout[:12000], 'stderr': p.stderr[:1000]}
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {'error': type(exc).__name__}


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def connection(host, port):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(3)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, INTERFACE.encode()+b'\0')
            sock.connect((host, port))
        return True
    except OSError:
        return False


def usb_devices():
    devices = []
    for entry in Path('/sys/bus/usb/devices').iterdir():
        try:
            vendor = (entry/'idVendor').read_text().strip()
            product = (entry/'idProduct').read_text().strip()
            if (vendor, product) not in (('1286', '4e31'), ('2a58', 'a002')):
                continue
            devices.append({'path': entry.name, 'vendor': vendor, 'product': product,
                            'devnum': (entry/'devnum').read_text().strip()})
        except OSError:
            continue
    return devices


def sample():
    stats = {}
    for field in ('rx_bytes', 'tx_bytes', 'rx_errors', 'tx_errors'):
        try:
            stats[field] = int((Path('/sys/class/net')/INTERFACE/'statistics'/field).read_text())
        except OSError:
            stats[field] = None
    with ThreadPoolExecutor(max_workers=3) as pool:
        gateway = pool.submit(connection, '192.168.125.1', 80)
        external = pool.submit(connection, '1.1.1.1', 443)
        public = pool.submit(command, ['curl', '--fail-with-body', '--noproxy', '*', '--interface', INTERFACE,
                                      '--connect-timeout', '3', '--max-time', '5', '-sS',
                                      '-w', '\nHTTP=%{http_code} source=%{local_ip}\n',
                                      'https://uav.laiting1202.com/healthz'])
        results = {'gateway': gateway.result(), 'external_tcp': external.result(),
                   'public_4g': public.result()}
    return {'realtime': time.time(), 'monotonic': time.monotonic(), 'stats': stats,
            'usb': usb_devices(), **results}


def kernel_identity(halt_cleanup=False):
    markers = ['uav_xhci_short_retry=1'] + (['uav_xhci_halt_cleanup=1'] if halt_cleanup else [])
    cmdline = Path('/proc/cmdline').read_text().split()
    marker = all(cmdline.count(value) == 1 for value in markers)
    version = os.uname().version
    image_path = '/boot/Image.uav-xhci-halt-cleanup' if halt_cleanup else '/boot/Image.uav-xhci-retry'
    image = sha256(Path(image_path))
    return {'release': os.uname().release, 'version': version, 'boot_marker': marker,
            'required_markers': markers, 'image_path': image_path, 'image_sha256': image,
            'expected_kernel_loaded': (marker and os.uname().release == '5.15.185-tegra' and
                                      version == (HALT_VERSION if halt_cleanup else EXPECTED_VERSION) and
                                      image == (HALT_IMAGE if halt_cleanup else EXPECTED_IMAGE))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--halt-cleanup', action='store_true', help='Verify the separate reviewed f781 + e765 candidate')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run the installed copy as administrator')
    os.umask(0o077)
    output = Path('/var/lib/laiting-uav-xhci-halt-validation') if args.halt_cleanup else OUTPUT
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    result = {'boot_id': boot, 'realtime': time.time(), **kernel_identity(args.halt_cleanup),
              'root_services': command(['systemctl', 'show', 'uav-modem-recovery',
                                        'uav-usb-error-recorder', 'ssh', '-p', 'Id',
                                        '-p', 'MainPID', '-p', 'ActiveState', '-p', 'NRestarts']),
              'user_services': command(['runuser', '-u', 'hrc', '--', 'env',
                                        'XDG_RUNTIME_DIR=/run/user/1000', 'systemctl', '--user',
                                        'show', 'uav-v2', 'uav-cloudflared', 'uav-flight-network-monitor',
                                        '-p', 'Id', '-p', 'MainPID', '-p', 'ActiveState', '-p', 'NRestarts']),
              'routes': command(['ip', '-j', '-4', 'route']),
              'usbnet_version': Path('/sys/module/usbnet/version').read_text().strip()
              if Path('/sys/module/usbnet/version').exists() else None}
    (output/(boot+'-identity.json')).write_text(json.dumps(result, indent=2))
    with (output/(boot+'-samples.jsonl')).open('a') as stream:
        # Observe ten minutes; no service restart, device reset, or route change.
        for index in range(40):
            start = time.monotonic()
            stream.write(json.dumps(sample())+'\n')
            stream.flush()
            if index < 39:
                time.sleep(max(0, 15-(time.monotonic()-start)))


if __name__ == '__main__':
    main()
