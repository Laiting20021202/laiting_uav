#!/usr/bin/env python3
"""Read-only one-second USB, network, host voltage and temperature evidence."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import threading
import time

try:
    from .modem_recovery import INTERFACE, modem_device
except ImportError:
    from modem_recovery import INTERFACE, modem_device


def read(path, numeric=False):
    try:
        value = path.read_text().strip()
        return int(value) if numeric else value
    except (OSError, ValueError):
        return None


def hardware_snapshot(sys_root=Path('/sys')):
    sensors = {}
    for base in (sys_root/'class/hwmon').glob('hwmon*'):
        name = read(base/'name')
        if name == 'ina3221':
            for channel in range(1, 4):
                label = read(base/f'in{channel}_label')
                if label:
                    sensors[label] = {
                        'voltage_mv': read(base/f'in{channel}_input', True),
                        'current_ma': read(base/f'curr{channel}_input', True),
                        'current_max_alarm': read(base/f'curr{channel}_max_alarm', True),
                        'current_crit_alarm': read(base/f'curr{channel}_crit_alarm', True),
                    }
        elif name == 'soctherm_oc':
            sensors['overcurrent_events'] = {
                f'oc{channel}': read(base/f'oc{channel}_event_cnt', True)
                for channel in range(1, 4)}
    temperatures = {read(base/'type') or base.name: read(base/'temp', True)
                    for base in (sys_root/'class/thermal').glob('thermal_zone*')}
    usb = {}
    try:
        device = modem_device(sys_root/'class/net', sys_root/'bus/usb/devices')
        # Record both the verified modem and its physical USB ancestors.
        # Reading ancestor state never changes or resets those devices.
        for base in (device, *device.parents):
            if not (base/'idVendor').exists():
                continue
            usb[base.name] = {key: read(base/key) for key in (
                'idVendor', 'idProduct', 'devnum', 'power/control', 'power/runtime_status',
                'power/runtime_active_time', 'power/runtime_suspended_time')}
    except (OSError, ValueError) as exc:
        usb['inspection_error'] = str(exc)
    net_base = sys_root/'class/net'/INTERFACE
    network = {key: read(net_base/key, True) for key in (
        'carrier', 'statistics/rx_bytes', 'statistics/tx_bytes',
        'statistics/rx_errors', 'statistics/tx_errors')}
    return {'sensors': sensors, 'temperatures_mc': temperatures,
            'usb': usb, 'network': network}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if args.check:
        print(json.dumps(hardware_snapshot(), indent=2))
        return
    os.umask(0o077)
    directory = Path.home()/'.local/state/laiting-uav/network'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(directory/'hardware-samples.jsonl',
                                  maxBytes=8*1024*1024, backupCount=3, encoding='utf-8')
    logger = logging.getLogger('uav.device.evidence')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    try:
        while not stopped.is_set():
            started = time.monotonic()
            record = {'boot_id': boot, 'monotonic': started, 'realtime': time.time(),
                      **hardware_snapshot()}
            logger.info(json.dumps(record))
            os.fsync(handler.stream.fileno())
            stopped.wait(max(0, 1-(time.monotonic()-started)))
    finally:
        handler.close()


if __name__ == '__main__':
    main()
