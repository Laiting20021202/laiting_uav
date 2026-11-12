#!/usr/bin/env python3
"""Record flight network evidence every ten seconds; never change any device."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import subprocess
import threading
import time

from network_health import snapshot
from tunnel_supervisor import ssh_transport_ready
from modem_radio import RadioProbe


def http_check(url):
    started = time.monotonic()
    result = subprocess.run(
        ['curl', '--noproxy', '*', '-fsS', '--connect-timeout', '3', '--max-time', '5', url],
        capture_output=True, text=True, timeout=6)
    try:
        body = json.loads(result.stdout)
    except ValueError:
        body = None
    healthy = (isinstance(body, dict) and
               (body.get('readyConnections', 0) > 0 if url.endswith('/ready')
                else body == {'status': 'ok'}))
    return {'ok': result.returncode == 0 and healthy,
            'curl_code': result.returncode, 'body': body,
            'seconds': round(time.monotonic()-started, 3), 'error': result.stderr[:300]}


def gateway_check():
    started = time.monotonic()
    try:
        with socket.create_connection(('192.168.125.1', 80), timeout=2):
            pass
        return {'ok': True, 'seconds': round(time.monotonic()-started, 3)}
    except OSError as exc:
        return {'ok': False, 'error': str(exc)}


def local_evidence():
    net = {}
    for name in ('enxac0033aa9633', 'eno1', 'wlP1p1s0'):
        base = Path('/sys/class/net')/name
        values = {}
        for key in ('operstate', 'carrier', 'statistics/rx_bytes', 'statistics/tx_bytes',
                    'statistics/rx_errors', 'statistics/tx_errors', 'statistics/rx_dropped',
                    'statistics/tx_dropped'):
            try:
                values[key] = (base/key).read_text().strip()
            except OSError:
                values[key] = None
        net[name] = values
    routes = subprocess.run(['ip', '-j', 'route'], capture_output=True, text=True, timeout=2)
    journal = subprocess.run(
        ['journalctl', '--boot=0', '_SYSTEMD_USER_UNIT=uav-v2.service',
         '--since=-20s', '--output=json', '--no-pager'],
        capture_output=True, text=True, timeout=3)
    app = []
    for line in journal.stdout.splitlines():
        entry = json.loads(line)
        message = entry.get('MESSAGE', '')
        if isinstance(message, str) and any(s in message for s in (
                'uav.camera:', 'uav.telemetry:', 'watchdog', 'uav.g3p:')):
            app.append({k: entry.get(k) for k in (
                '__REALTIME_TIMESTAMP', '__MONOTONIC_TIMESTAMP', '_BOOT_ID', 'MESSAGE')})
    return {'interfaces': net, 'routes': routes.stdout[:3000], 'application_events': app[-20:],
            'serial_devices': [p.name for p in Path('/dev/serial/by-id').glob('*')]}


def main():
    os.umask(0o077)
    directory = Path.home()/'.local/state/laiting-uav/network'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(directory/'flight-samples.jsonl', maxBytes=4*1024*1024,
                                  backupCount=3, encoding='utf-8')
    logger = logging.getLogger('flight-network-evidence')
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    checks = {
        'upstream': snapshot,
        'gateway': gateway_check,
        'local': lambda: http_check('http://127.0.0.1:8080/healthz'),
        'public': lambda: http_check('https://uav.laiting1202.com/healthz'),
        'tunnel': lambda: http_check('http://127.0.0.1:20241/ready'),
        'ssh': lambda: ssh_transport_ready(str(Path.home()/'.local/bin/cloudflared'),
                                           'ssh-uav.laiting1202.com'),
        'devices': local_evidence,
        'radio': RadioProbe(),
    }
    previous = None
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        while not stopped.is_set():
            started = time.monotonic()
            futures = {key: pool.submit(check) for key, check in checks.items()}
            record = {'monotonic': started, 'realtime': time.time(),
                      'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
            for key, future in futures.items():
                try:
                    record[key] = future.result()
                except Exception as exc:
                    record[key] = {'ok': False, 'error': str(exc)[:300]}
            logger.info(json.dumps(record, ensure_ascii=False))
            os.fsync(handler.stream.fileno())
            status = {key: record[key].get('ok') for key in ('gateway', 'local', 'public', 'tunnel')}
            status['ssh'] = record['ssh']
            status['upstream_failed'] = [key for key, value in record['upstream'].get('checks', {}).items()
                                         if not value['ok']]
            if status != previous:
                print(json.dumps({'realtime': time.time(), 'status': status}), flush=True)
                previous = status
            stopped.wait(max(0, 10-(time.monotonic()-started)))
    handler.close()


if __name__ == '__main__':
    main()
