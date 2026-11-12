#!/usr/bin/env python3
"""Read-only per-boot acceptance of the onboard 4G route and public website.

An unsuccessful window is retried by systemd while recovery services continue.
Wi-Fi rescue is reported explicitly and never counted as a repaired USB path.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import re
import signal
import stat
import subprocess
import threading
import time

if __package__:
    from .modem_path_manager import INTERFACES, PROFILES, actual_route, atomic_json, path_ready
else:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from modem_path_manager import INTERFACES, PROFILES, actual_route, atomic_json, path_ready

STATE = Path('/var/lib/laiting-uav-boot-validation/network-stability')
STATUS = Path('/run/laiting-uav-network/status.json')
STABLE_SECONDS = 20


def health(url):
    try:
        result = subprocess.run(['curl', '--noproxy', '*', '-fsS', '--connect-timeout', '1',
                                 '--max-time', '3', url], capture_output=True, text=True, timeout=4)
        return result.returncode == 0 and json.loads(result.stdout) == {'status': 'ok'}
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def qualified(status, boot, now, route, local, public):
    """Reject stale samples, unrelated routes, and unverified path identities."""
    if not isinstance(status, dict) or route not in INTERFACES:
        return False
    stamp = status.get('monotonic')
    path = status.get(route, {})
    return bool(isinstance(stamp, (int, float)) and not isinstance(stamp, bool)
                and 0 <= now-stamp <= 8 and status.get('boot_id') == boot
                and status.get('active_path') == route and isinstance(path, dict)
                and path_ready(path) and path.get('interface') == INTERFACES[route]
                and path.get('profile_uuid') == PROFILES[route]
                and path.get('gateway') == '192.168.125.1'
                and (route != 'usb' or path.get('device') and path.get('usb_generation'))
                and status.get('tunnel_ready') is True and local is True and public is True)


def inspect(boot):
    try:
        info = STATUS.lstat()
        parent = STATUS.parent.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022
                or not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022):
            raise ValueError('Untrusted path status')
        status = json.loads(STATUS.read_text())
        if not isinstance(status, dict):
            status = {}
    except (OSError, ValueError):
        status = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        probes = [pool.submit(health, url) for url in
                  ('http://127.0.0.1:8080/healthz', 'https://uav.laiting1202.com/healthz')]
        local, public = [probe.result() for probe in probes]
    now, route = time.monotonic(), actual_route()
    return {'boot_id': boot, 'boot_seconds': now, 'realtime': time.time(),
            'active_path': route, 'local_ok': local, 'public_ok': public,
            'ready': qualified(status, boot, now, route, local, public),
            'paths': {name: status.get(name, {}) for name in INTERFACES},
            'preferred_path': status.get('preferred_path'),
            'tunnel_ready': status.get('tunnel_ready') is True}


class Acceptance:
    def __init__(self):
        self.key = self.since = None

    def observe(self, sample):
        path = sample.get('active_path')
        identity = sample.get('paths', {}).get(path, {})
        key = (path, identity.get('device'), identity.get('usb_generation'))
        now = sample['boot_seconds']
        if sample.get('ready') is not True:
            self.key = self.since = None
            return False
        if self.key != key:
            self.key, self.since = key, now
        return now-self.since >= STABLE_SECONDS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Print one read-only sample without saving')
    args = parser.parse_args()
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if args.check:
        print(json.dumps(inspect(boot), indent=2))
        return 0
    os.umask(0o077)
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    start = time.monotonic()
    acceptance = Acceptance()
    samples = []
    while not stopped.is_set() and time.monotonic()-start < 180:
        sample = inspect(boot)
        samples.append(sample)
        accepted = acceptance.observe(sample)
        report = {'boot_id': boot, 'started_boot_seconds': start,
                  'observed_boot_seconds': sample['boot_seconds'], 'accepted': accepted,
                  'stable_seconds': STABLE_SECONDS, 'active_path': sample['active_path'],
                  'usb_ready': path_ready(sample['paths']['usb']),
                  'wifi_ready': path_ready(sample['paths']['wifi']),
                  'samples': samples[-90:]}
        atomic_json(STATE/(boot+'.json'), report, 0o600)
        atomic_json(STATE/'latest.json', report, 0o600)
        if accepted:
            print(json.dumps({key: value for key, value in report.items() if key != 'samples'}), flush=True)
            # Bound per-boot records without deleting the current acceptance.
            previous = sorted((file for file in STATE.glob('*.json')
                               if re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\.json', file.name)),
                              key=lambda file: file.stat().st_mtime, reverse=True)
            for file in previous[16:]:
                file.unlink()
            return 0
        stopped.wait(2)
    print('Onboard 4G boot acceptance pending; recovery continues and systemd will retry.', flush=True)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
