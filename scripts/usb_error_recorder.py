#!/usr/bin/env python3
"""Record xHCI completion errors and device identity; never reset a USB device."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import select
import signal
import subprocess
import threading
import time

INSTANCE = 'laiting-uav-usb-errors'
UNIT = 'uav-usb-error-recorder.service'
INSTALLED = Path('/usr/local/lib/laiting-uav/usb_error_recorder.py')
DIRECTORY = Path('/var/lib/laiting-uav-usb-evidence')
# Completion codes 2..12 include USB transaction, babble and endpoint STALL.
# Exclude success (1), normal short packets (13), and normal stop/cancel events.
# xhci_urb_giveback's status is still EINPROGRESS on this kernel; do not use it.
ERROR_FILTER = 'field2 >= 33554432 && field2 < 218103808'


def configure_trace(trace_root):
    event = trace_root/'events/xhci-hcd/xhci_handle_event'
    if not (event/'format').is_file():
        raise RuntimeError('This kernel does not provide the required xHCI tracepoint')
    instance = trace_root/'instances'/INSTANCE
    instance.mkdir(exist_ok=True)
    target = instance/'events/xhci-hcd/xhci_handle_event'
    (target/'enable').write_text('0')
    (instance/'buffer_size_kb').write_text('256')
    (target/'filter').write_text(ERROR_FILTER)
    accepted = (target/'filter').read_text().strip()
    if accepted != ERROR_FILTER:
        raise RuntimeError('Kernel rejected USB error filter; tracing remains disabled')
    (instance/'trace_clock').write_text('mono')
    (instance/'trace').write_text('')
    (target/'enable').write_text('1')
    return instance


def identity_snapshot(debug_root=Path('/sys/kernel/debug/usb/xhci'),
                      usb_root=Path('/sys/bus/usb/devices')):
    result = {}
    for name_path in debug_root.glob('*/devices/*/name'):
        try:
            name = name_path.read_text().strip()
            device = usb_root/name
            result[name_path.parent.name] = {
                'controller': name_path.parents[2].name,
                'device': name,
                **{key: (device/key).read_text().strip()
                   for key in ('idVendor', 'idProduct')},
            }
        except OSError:
            continue
    return result


def monitor():
    os.umask(0o077)
    DIRECTORY.mkdir(exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(DIRECTORY/'errors.jsonl', maxBytes=4*1024*1024,
                                  backupCount=3, encoding='utf-8')
    logger = logging.getLogger('uav.usb.evidence')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    trace_root = Path('/sys/kernel/tracing')
    if not (trace_root/'events/xhci-hcd/xhci_handle_event/format').exists():
        trace_root = Path('/sys/kernel/debug/tracing')
    instance = configure_trace(trace_root)
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    fd = os.open(instance/'trace_pipe', os.O_RDONLY | os.O_NONBLOCK)
    pending = b''
    print('Filtered xHCI error recorder active; USB devices retained', flush=True)
    try:
        while not stop.is_set():
            if not select.select([fd], [], [], 1)[0]:
                continue
            try:
                pending += os.read(fd, 65536)
            except BlockingIOError:
                continue
            lines = pending.split(b'\n')
            pending = lines.pop()[-4096:]
            for line in lines:
                logger.info(json.dumps({'realtime': time.time(), 'monotonic': time.monotonic(),
                                        'boot_id': boot, 'trace_clock': 'mono',
                                        'event': line.decode('utf-8', 'replace')[:1500],
                                        'slots': identity_snapshot()}))
            if lines:
                os.fsync(handler.stream.fileno())
    finally:
        os.close(fd)
        (instance/'events/xhci-hcd/xhci_handle_event/enable').write_text('0')
        handler.close()


def install():
    if not Path('/sys/kernel/tracing/events/xhci-hcd/xhci_handle_event/format').is_file():
        raise RuntimeError('Required kernel tracepoint is unavailable')
    INSTALLED.parent.mkdir(exist_ok=True, parents=True)
    INSTALLED.write_bytes(Path(__file__).read_bytes())
    INSTALLED.chmod(0o644)
    unit = f'''[Unit]
Description=UAV filtered USB controller error evidence (no device reset)
After=systemd-udevd.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/usr/bin/python3 -I {INSTALLED} monitor
Restart=always
RestartSec=10
TimeoutStopSec=5
UMask=0077
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/sys/kernel/tracing/instances
StateDirectory=laiting-uav-usb-evidence
StateDirectoryMode=0700
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''
    path = Path('/etc/systemd/system')/UNIT
    path.write_text(unit)
    path.chmod(0o644)
    subprocess.run(['systemd-analyze', 'verify', str(path)], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', '--now', UNIT], check=True)
    subprocess.run(['systemctl', 'is-active', '--quiet', UNIT], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('install', 'monitor'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Local administrator authentication is required')
    (install if args.action == 'install' else monitor)()


if __name__ == '__main__':
    main()
