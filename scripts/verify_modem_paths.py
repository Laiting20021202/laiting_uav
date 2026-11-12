#!/usr/bin/env python3
"""Bounded live onboard failover check; optional CDC-only fault injection.

No hub/controller/flight-controller reset is permitted. Evidence is private.
The --inject-cdc action is a laboratory test, not a flight-time command.
"""
import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time

STATUS = Path('/run/laiting-uav-network/status.json')


def read_status():
    value = json.loads(STATUS.read_text())
    if value['boot_id'] != Path('/proc/sys/kernel/random/boot_id').read_text().strip():
        raise ValueError('Stale boot status')
    if not 0 <= time.monotonic()-value['monotonic'] < 8:
        raise ValueError('Stale path status')
    return value


def flight_generation():
    matches = []
    for device in Path('/sys/bus/usb/devices').iterdir():
        try:
            if ((device/'idVendor').read_text().strip() == '2a58' and
                    (device/'idProduct').read_text().strip() == 'a002' and
                    (device/'serial').read_text().strip() == '330032000D51333331333534'):
                matches.append((device.name, (device/'busnum').read_text().strip(),
                                (device/'devnum').read_text().strip()))
        except OSError:
            pass
    if len(matches) != 1:
        raise ValueError('Flight-controller identity missing or ambiguous')
    return matches[0]


def health(url):
    try:
        result = subprocess.run(['curl', '--noproxy', '*', '-fsS', '--connect-timeout', '1',
                                 '--max-time', '2', url], capture_output=True, text=True, timeout=3)
        return result.returncode == 0 and json.loads(result.stdout) == {'status': 'ok'}
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inject-cdc', action='store_true')
    parser.add_argument('--directory', type=Path, required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run the root-owned copy as administrator')
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    initial = read_status()
    if (initial['active_path'] != 'usb' or initial['preferred_path'] != 'auto' or
            not all(initial[path]['internet'] and initial[path]['connected'] for path in ('usb', 'wifi'))):
        raise ValueError('Both onboard paths must be healthy with automatic USB primary')
    if not health('http://127.0.0.1:8080/healthz') or not health('https://uav.laiting1202.com/healthz'):
        raise ValueError('Local and public web preflight failed')
    sys.path.insert(0, '/usr/local/lib/laiting-uav')
    import modem_recovery as recovery
    device = recovery.modem_device()
    control = recovery.cdc_control_interface(device, strict=True)
    flight_before = flight_generation()
    generation = ((device/'busnum').read_text().strip(), (device/'devnum').read_text().strip())
    started = time.monotonic()
    stop = threading.Event()
    lock = threading.Lock()
    rows = []
    fallback = []

    def record(value):
        with lock:
            rows.append({'elapsed': round(time.monotonic()-started, 4), **value})

    def probe():
        while not stop.is_set():
            record({'kind': 'public', 'ok': health('https://uav.laiting1202.com/healthz')})
            stop.wait(.2)

    def failsafe():
        if stop.wait(25):
            return
        try:
            observed = recovery.modem_device()
            same = ((observed/'busnum').read_text().strip(), (observed/'devnum').read_text().strip()) == generation
            if observed == device and same and not Path('/sys/class/net/enxac0033aa9633').exists():
                pair = recovery.cdc_control_interface(observed, strict=True)
                (recovery.CDC_DRIVER/'bind').write_text(pair.name)
                subprocess.run(['nmcli', '--wait', '10', 'connection', 'up', 'uuid', recovery.PROFILE],
                               capture_output=True, timeout=12)
                fallback.append('exact_cdc_bind')
        except (OSError, ValueError, subprocess.SubprocessError):
            fallback.append('failsafe_failed')

    probe_worker = threading.Thread(target=probe, daemon=True)
    fallback_worker = threading.Thread(target=failsafe, daemon=True)
    record({'kind': 'preflight', 'active_path': initial['active_path'], 'flight_generation': flight_before})
    if args.inject_cdc:
        # Revalidate the exact pair/generation immediately before the only fault.
        if (recovery.modem_device() != device or recovery.cdc_control_interface(device, strict=True) != control
                or flight_generation() != flight_before):
            raise ValueError('Device identity changed before CDC-only injection')
        (recovery.CDC_DRIVER/'unbind').write_text(control.name)
        record({'kind': 'cdc_unbound', 'device': device.name})
    probe_worker.start()
    fallback_worker.start()
    try:
        while time.monotonic()-started < 35:
            current = read_status()
            record({'kind': 'sample', 'active_path': current['active_path'], 'phase': current['phase'],
                    'usb_internet': current['usb']['internet'], 'wifi_internet': current['wifi']['internet'],
                    'usb_present': Path('/sys/class/net/enxac0033aa9633').exists(),
                    'local_ok': health('http://127.0.0.1:8080/healthz'),
                    'flight_unchanged': flight_generation() == flight_before})
            stop.wait(.2)
    finally:
        stop.set()
        probe_worker.join(4)
        fallback_worker.join(1)
    with lock:
        rows.sort(key=lambda value: value['elapsed'])
        (args.directory/'samples.jsonl').write_text(''.join(json.dumps(row)+'\n' for row in rows))
    samples = [row for row in rows if row['kind'] == 'sample']
    wifi_first = next((row['elapsed'] for row in samples if row['active_path'] == 'wifi'), None)
    public_after = next((row['elapsed'] for row in rows if row['kind'] == 'public' and row['ok']
                         and wifi_first is not None and row['elapsed'] >= wifi_first), None)
    driver_back = next((row['elapsed'] for row in samples if row['usb_present'] and row['usb_internet']), None)
    final = read_status()
    summary = {'injected_cdc': args.inject_cdc, 'first_wifi_seconds': wifi_first,
               'first_public_after_wifi_seconds': public_after, 'usb_recovered_seconds': driver_back,
               'fallback': fallback, 'local_always_healthy': all(row['local_ok'] for row in samples),
               'flight_generation_unchanged': all(row['flight_unchanged'] for row in samples),
               'final_active_path': final['active_path'], 'last_switch': final['last_switch'],
               'verified': (wifi_first is not None and public_after is not None and driver_back is not None
                            and final['last_switch']['website_seconds'] is not None
                            and not fallback and all(row['local_ok'] and row['flight_unchanged'] for row in samples))}
    (args.directory/'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
    print(json.dumps(summary, indent=2))


if __name__ == '__main__':
    main()
