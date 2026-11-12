#!/usr/bin/env python3
"""Restore RF only when this modem explicitly reports CFUN=4 and SIM READY.

USBFS claims only the verified, otherwise unbound Mobile AT Interface 4.
It never resets USB, detaches a driver, changes configuration/APN, scans
operators, or sends a SIM PIN. Working internet bypasses all AT operations.
"""
import argparse
import array
import ctypes
import errno
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time

if __package__:
    from .modem_recovery import modem_device, inspect_modem, notify, onboard_hotspot_in_use, connect
else:
    # The installed root-owned sibling is trusted; -I excludes the cwd.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from modem_recovery import modem_device, inspect_modem, notify, onboard_hotspot_in_use, connect

LOG = logging.getLogger('uav.modem.rf')
STATE = Path('/var/lib/laiting-uav-rf-recovery')
INTERFACE_NUMBER = 4
OUT_ENDPOINT, IN_ENDPOINT = 0x0a, 0x81
CLAIM, RELEASE, BULK_IOCTL = 0x8004550f, 0x80045510, 0xc0185502
QUERY_TIMEOUT, MAX_REPLY = 2.0, 4096
READ_COMMANDS = frozenset(('AT', 'AT+CFUN?', 'AT+CPIN?'))


class Bulk(ctypes.Structure):
    _fields_ = [('ep', ctypes.c_uint), ('length', ctypes.c_uint),
                ('timeout', ctypes.c_uint), ('data', ctypes.c_void_p)]


LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.ioctl.restype = ctypes.c_int


class AtClient:
    """A short-lived claim of the exact modem's proven AT bulk interface."""
    def __init__(self):
        if ctypes.sizeof(Bulk) != 24 or Bulk.data.offset != 16:
            raise RuntimeError('Unsupported USBFS bulk ABI')
        self.fd = None
        self.claimed = False
        self.device = None
        self.generation = None

    def _verify(self):
        device = modem_device()
        generation = tuple((device/name).read_text().strip() for name in ('busnum', 'devnum'))
        if self.device is not None and (device != self.device or generation != self.generation):
            raise RuntimeError('USB modem generation changed')
        if any(not value.isdecimal() or not 1 <= int(value) <= 999 for value in generation):
            raise ValueError('Invalid USB modem generation')
        interface = device/(device.name+':1.4')
        if ((interface/'bInterfaceNumber').read_text().strip() != '04' or
                (interface/'interface').read_text().strip() != 'Mobile AT Interface' or
                (interface/'bInterfaceClass').read_text().strip().lower() != 'ff'):
            raise ValueError('Expected Mobile AT Interface 4 is unavailable')
        driver = interface/'driver'
        if driver.exists() or driver.is_symlink():
            # CLAIMINTERFACE binds usbfs_driver; accept only our own claim.
            if not self.claimed or driver.resolve(strict=True).name != 'usbfs':
                raise ValueError('AT interface already has a driver')
        endpoints = {}
        for endpoint in interface.glob('ep_*'):
            address = int((endpoint/'bEndpointAddress').read_text().strip(), 16)
            attributes = int((endpoint/'bmAttributes').read_text().strip(), 16)
            endpoints[address] = attributes & 3
        if endpoints != {OUT_ENDPOINT: 2, IN_ENDPOINT: 2, 0x88: 3}:
            raise ValueError('AT bulk endpoint descriptors do not match')
        return device, generation

    def __enter__(self):
        self.device, self.generation = self._verify()
        bus, number = map(int, self.generation)
        self.fd = os.open(f'/dev/bus/usb/{bus:03d}/{number:03d}', os.O_RDWR | os.O_CLOEXEC)
        try:
            self._verify()
            fcntl.ioctl(self.fd, CLAIM, array.array('I', [INTERFACE_NUMBER]), True)
            self.claimed = True
            self._verify()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.fd is not None:
            try:
                if self.claimed:
                    self._verify()
                    fcntl.ioctl(self.fd, RELEASE, array.array('I', [INTERFACE_NUMBER]), True)
            except (OSError, ValueError, RuntimeError):
                # Closing the original descriptor also releases its claims;
                # no operation is issued against a changed USB generation.
                LOG.warning('AT interface release deferred to descriptor close')
            finally:
                os.close(self.fd)
                self.fd, self.claimed = None, False

    def _transfer(self, endpoint, deadline, data=None, limit=MAX_REPLY):
        if self.fd is None or not self.claimed:
            raise RuntimeError('AT interface is not claimed')
        self._verify()
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise TimeoutError('AT query deadline reached')
        size = len(data) if data is not None else limit
        buffer = ctypes.create_string_buffer(data if data is not None else size)
        arguments = Bulk(endpoint, size, min(800, max(1, math.ceil(remaining*1000))),
                         ctypes.addressof(buffer))
        count = LIBC.ioctl(ctypes.c_int(self.fd), ctypes.c_ulong(BULK_IOCTL), ctypes.byref(arguments))
        if count < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if count > size or (data is not None and count != size):
            raise ValueError('Invalid AT bulk transfer size')
        return buffer.raw[:count]

    def _exchange(self, command):
        if command not in READ_COMMANDS and command != 'AT+CFUN=1':
            raise ValueError('Unsupported AT command')
        deadline = time.monotonic()+QUERY_TIMEOUT
        self._transfer(OUT_ENDPOINT, deadline, (command+'\r').encode('ascii'))
        raw = b''
        while time.monotonic() < deadline:
            try:
                raw += self._transfer(IN_ENDPOINT, deadline, limit=MAX_REPLY-len(raw))
            except OSError as error:
                if error.errno in (errno.ETIMEDOUT, errno.EAGAIN):
                    continue
                raise
            lines = [line.strip() for line in raw.decode('ascii', 'replace').splitlines()]
            if any(line in ('OK', 'ERROR') or line.startswith('+CME ERROR:') for line in lines):
                # Exclude unsolicited SMS, identifiers and diagnostic data.
                return [line for line in lines if line in ('OK', 'ERROR') or
                        re.fullmatch(r'\+CFUN:\s*\d+(?:\s*,\s*\d+)?', line) or
                        re.fullmatch(r'\+CPIN:\s*(?:READY|SIM PIN|SIM PUK)', line) or
                        re.fullmatch(r'\+CME ERROR:\s*\d+', line)]
            if len(raw) >= MAX_REPLY:
                raise ValueError('AT response exceeds limit')
        raise TimeoutError('AT response did not complete')

    def query(self, command):
        if command not in READ_COMMANDS:
            raise ValueError('Only approved read-only AT queries are allowed')
        return self._exchange(command)

    def snapshot(self):
        function = self.query('AT+CFUN?')
        pin = self.query('AT+CPIN?')
        modes = [int(re.fullmatch(r'\+CFUN:\s*(\d+)(?:\s*,\s*\d+)?', line)[1])
                 for line in function if line.startswith('+CFUN:')]
        return {'device': self.device.name, 'usb_generation': ':'.join(self.generation),
                'cfun': modes[0] if len(modes) == 1 and 'OK' in function else None,
                'sim_ready': '+CPIN: READY' in pin and 'OK' in pin}

    def enable_if_disabled(self):
        # Never trust the monitor's older sample for the mutating command.
        before = self.snapshot()
        if before['cfun'] != 4 or not before['sim_ready']:
            return {'command_acknowledged': False, 'before': before}
        reply = self._exchange('AT+CFUN=1')
        return {'command_acknowledged': 'OK' in reply and 'ERROR' not in reply,
                'before': before}


def eligible(health):
    return bool(health and not health['internet'] and health['gateway'] and
                health['device'] and health['interface_present'] and health['carrier'])


def onboard_wifi_healthy():
    # Both routes share the same SIM/radio. Working verified Wi-Fi proves RF is
    # on even if CDC is failing; avoid opening the troubled USB AT interface.
    return onboard_hotspot_in_use() and any(
        connect(host, 443, tls=True, interface='wlP1p1s0') for host in ('1.1.1.1', '8.8.8.8'))


class RFPolicy:
    def __init__(self, state=None):
        state = state or {}
        self.count = state.get('count', 0)
        self.next_action = state.get('next_action', 0)
        self.key, self.since = None, None

    def observe(self, health, radio, now):
        key = ((radio.get('device'), radio.get('usb_generation')) if radio else None)
        expected = ((health.get('device'), health.get('usb_generation')) if health else None)
        if (not eligible(health) or not radio or key != expected or
                radio.get('cfun') != 4 or not radio.get('sim_ready')):
            self.key, self.since = None, None
            return False
        if self.key != key:
            self.key, self.since = key, now
        return now-self.since >= 5 and now >= self.next_action

    def attempted(self, now):
        self.count += 1
        self.next_action = now+15

    def state(self):
        return {'count': self.count, 'next_action': self.next_action}


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_suffix('.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def append_action(value):
    path = STATE/'actions.jsonl'
    if path.exists() and path.stat().st_size >= 1024*1024:
        previous = path.with_name('actions.jsonl.1')
        if previous.exists():
            previous.replace(path.with_name('actions.jsonl.2'))
        path.replace(previous)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(value)+'\n')


def monitor():
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    try:
        previous = json.loads((STATE/'state.json').read_text())
        state = previous.get('policy', {}) if previous.get('boot_id') == boot else {}
    except (OSError, ValueError, TypeError):
        state = {}
    policy = RFPolicy(state)
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    notify('READY=1')
    while not stopped.is_set():
        health = radio = None
        status = {'boot_id': boot, 'realtime': time.time(), 'monotonic': time.monotonic()}
        try:
            health = inspect_modem()
            status['health'] = {key: health.get(key) for key in
                                ('device', 'usb_generation', 'gateway', 'internet',
                                 'interface_present', 'carrier')}
            if eligible(health) and not onboard_wifi_healthy():
                with AtClient() as client:
                    radio = client.snapshot()
                status['radio'] = radio
            now = time.monotonic()
            if policy.observe(health, radio, now):
                policy.attempted(now)
                save_json(STATE/'state.json', {'boot_id': boot, 'policy': policy.state()})
                with AtClient() as client:
                    result = client.enable_if_disabled()
                status['action'] = result
                append_action({**status, 'action': 'enable-if-disabled', 'result': result})
                LOG.warning('RF restore attempted for verified modem; acknowledged=%s',
                            result['command_acknowledged'])
        except (OSError, ValueError, RuntimeError) as error:
            policy.observe(None, None, time.monotonic())
            status['error'] = type(error).__name__
            LOG.info('RF inspection unavailable: %s', type(error).__name__)
        save_json(STATE/'status.json', status)
        notify('WATCHDOG=1')
        stopped.wait(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'monitor'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Local administrator authentication is required.')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.action == 'check':
        with AtClient() as client:
            print(json.dumps(client.snapshot(), indent=2))
    else:
        monitor()


if __name__ == '__main__':
    main()
