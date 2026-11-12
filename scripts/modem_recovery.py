#!/usr/bin/env python3
"""Recover only this UAV's stalled USB modem; never reset a hub or flight controller.

Install locally with sudo python3 scripts/modem_recovery.py install.
The root service uses a root-owned copy and only Python's standard library.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import errno
import fcntl
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import signal
import socket
import ssl
import stat
import struct
import subprocess
import sys
import threading
import time

INTERFACE = 'enxac0033aa9633'
MAC = 'ac:00:33:aa:96:33'
VENDOR = '1286'
PRODUCT = '4e31'
SERIAL = '200806006809080000'
PROFILE = '8391cb7e-8a3a-317c-bf18-612aa9c5300e'
INSTALLED = Path('/usr/local/lib/laiting-uav/modem_recovery.py')
STATE = Path('/var/lib/laiting-uav-recovery')
UNIT = 'uav-modem-recovery.service'
RESOLVER_UNIT = 'uav-tunnel-resolver.service'
RESOLVED_DOH = Path('/etc/systemd/resolved.conf.d/91-uav-https-dns.conf')
LOG = logging.getLogger('uav.modem.recovery')
STALL_CONFIRM_SECONDS = 5
LOCAL_CONFIRM_SECONDS = 5
LOCAL_RETRY_SECONDS = 15
MAX_RETRY_SECONDS = 60
USB_WAIT_LOG_SECONDS = 60
CDC_DRIVER = Path('/sys/bus/usb/drivers/cdc_ether')


def onboard_hotspot_in_use(status_path=Path('/run/laiting-uav-network/status.json')):
    """A USB reset must not interrupt the verified onboard Wi-Fi rescue path."""
    # The route and NM profile remain authoritative if the path service is
    # restarting or its status has aged during an exceptionally slow probe.
    try:
        route = subprocess.run(['ip', '-j', '-4', 'route', 'get', '1.1.1.1'],
                               capture_output=True, text=True, timeout=2, check=True)
        if json.loads(route.stdout)[0].get('dev') == 'wlP1p1s0':
            profile = subprocess.run(['nmcli', '-g', 'GENERAL.CON-UUID', 'device', 'show', 'wlP1p1s0'],
                                     capture_output=True, text=True, timeout=2, check=True)
            if profile.stdout.strip() == '7b8a7e5f-0e63-46ed-9c9c-9666959890b2':
                return True
    except (OSError, ValueError, TypeError, AttributeError, IndexError, subprocess.SubprocessError):
        pass
    try:
        parent = status_path.parent.lstat()
        info = status_path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022
                or not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022):
            return False
        report = json.loads(status_path.read_text())
        now = time.monotonic()
        updated = report.get('monotonic')
        if (not isinstance(updated, (int, float)) or not 0 <= now-updated <= 8 or
                report.get('boot_id') != Path('/proc/sys/kernel/random/boot_id').read_text().strip()):
            return False
        wifi = report.get('wifi', {})
        if (report.get('active_path') != 'wifi' or wifi.get('internet') is not True or
                wifi.get('connected') is not True or wifi.get('interface') != 'wlP1p1s0' or
                wifi.get('profile_uuid') != '7b8a7e5f-0e63-46ed-9c9c-9666959890b2'):
            return False
        route = subprocess.run(['ip', '-j', '-4', 'route', 'get', '1.1.1.1'],
                               capture_output=True, text=True, timeout=2, check=True)
        return json.loads(route.stdout)[0].get('dev') == 'wlP1p1s0'
    except (OSError, ValueError, TypeError, AttributeError, KeyError, IndexError, subprocess.SubprocessError):
        return False


def modem_device(net_root=Path('/sys/class/net'), usb_root=Path('/sys/bus/usb/devices')):
    interface = net_root/INTERFACE
    if (interface/'address').exists():
        if (interface/'address').read_text().strip().lower() != MAC:
            raise ValueError('Modem MAC does not match; refusing device operation')
        device = (interface/'device').resolve(strict=True).parent
    else:
        # Interface can vanish while the same modem is still enumerated.
        # Never fall back to an arbitrary USB port or a different modem.
        candidates = []
        for candidate in usb_root.iterdir():
            try:
                if ((candidate/'idVendor').read_text().strip() == VENDOR and
                        (candidate/'idProduct').read_text().strip() == PRODUCT and
                        (candidate/'serial').read_text().strip() == SERIAL):
                    candidates.append(candidate.resolve())
            except OSError:
                continue
        if len(candidates) != 1:
            raise FileNotFoundError('Expected exactly one matching modem USB identity')
        device = candidates[0]
    if ((device/'idVendor').read_text().strip() != VENDOR or
            (device/'idProduct').read_text().strip() != PRODUCT or
            (device/'serial').read_text().strip() != SERIAL or
            (device/'bDeviceClass').read_text().strip() == '09'):
        raise ValueError('USB device identity does not match the modem')
    return device


def connect(host, port, tls=False, interface=None):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(2)
            if interface:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                interface.encode('ascii') + b'\0')
            sock.connect((host, port))
            if tls:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                    pass
        return True
    except (OSError, ValueError):
        return False


def dns_ready(server, timeout=2, interface=None):
    """Bounded SRV query using a literal DNS address; no new dependency."""
    name = '_v2-origintunneld._tcp.argotunnel.com'
    question = b''.join(bytes([len(label)])+label.encode('ascii') for label in name.split('.'))
    question += b'\x00' + struct.pack('!HH', 33, 1)
    query_id = os.urandom(2)
    query = query_id+struct.pack('!HHHHH', 0x0100, 1, 0, 0, 0)+question
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            if interface:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                                interface.encode('ascii') + b'\0')
            sock.connect((server, 53))
            sock.send(query)
            reply = sock.recv(4096)
        if len(reply) < 12:
            return False
        flags, _, answers = struct.unpack('!HHH', reply[2:8])
        return (reply[:2] == query_id and flags & 0x8000 != 0 and
                flags & 0x000f == 0 and answers > 0)
    except OSError:
        return False


def public_ready(interface=None):
    try:
        binding = ['--interface', interface] if interface else []
        result = subprocess.run(
            ['curl', '--noproxy', '*', *binding, '-fsS', '--connect-timeout', '2', '--max-time', '3',
             'https://uav.laiting1202.com/healthz'], capture_output=True, text=True, timeout=4)
        return result.returncode == 0 and json.loads(result.stdout) == {'status': 'ok'}
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def network_manager_state():
    """Read only this modem's numeric NM state/profile and IPv4 addresses."""
    unknown = {'nm_state': None, 'nm_profile': None, 'ipv4': []}
    try:
        result = subprocess.run(
            ['nmcli', '-t', '-g', 'GENERAL.STATE,GENERAL.CON-UUID,IP4.ADDRESS',
             'device', 'show', INTERFACE], capture_output=True, text=True, timeout=3)
        lines = result.stdout.splitlines()
        if result.returncode or len(lines) < 2:
            return unknown
        match = re.match(r'^(\d+)(?:\s|$)', lines[0])
        if not match:
            return unknown
        profile = lines[1].strip()
        profile = None if profile in ('', '--') else profile
        addresses = []
        for line in lines[2:]:
            if line.strip() in ('', '--'):
                continue
            ip = ipaddress.ip_interface(line.strip())
            if ip.version == 4 and not ip.ip.is_unspecified:
                addresses.append(str(ip))
        return {'nm_state': int(match[1]), 'nm_profile': profile, 'ipv4': addresses}
    except (OSError, ValueError, subprocess.SubprocessError):
        return unknown


def cdc_control_interface(device, driver=CDC_DRIVER, strict=False):
    """Validate the modem's CDC pair without touching its AT/diagnostic ports.

    An already-bound interface is authoritative for older sysfs layouts, but
    authorizing a missing-interface repair requires both complete descriptors.
    """
    interfaces = list(device.glob(device.name+':*'))
    by_number = {}
    for interface in interfaces:
        number = (interface/'bInterfaceNumber').read_text().strip().lower()
        by_number.setdefault(number, []).append(interface)
    if len(by_number.get('00', [])) != 1:
        raise ValueError('Expected exactly one modem CDC control interface')
    control = by_number['00'][0]
    for name, expected in (('bInterfaceClass', '02'), ('bInterfaceSubClass', '06')):
        attribute = control/name
        if attribute.exists() or strict:
            if attribute.read_text().strip().lower() != expected:
                raise ValueError('Modem control interface is not CDC Ethernet')
    data_interfaces = by_number.get('01', [])
    if len(data_interfaces) > 1 or (strict and len(data_interfaces) != 1):
        raise ValueError('Expected exactly one modem CDC data interface')
    if data_interfaces:
        data = data_interfaces[0]
        attribute = data/'bInterfaceClass'
        if attribute.exists() or strict:
            if attribute.read_text().strip().lower() != '0a':
                raise ValueError('Modem data interface is not CDC data')
        data_driver = data/'driver'
        if data_driver.exists() or data_driver.is_symlink():
            if data_driver.resolve(strict=True) != driver.resolve(strict=True):
                raise ValueError('Modem data interface has an unexpected driver')
            if not (control/'driver').exists():
                raise ValueError('Modem CDC interfaces are only partially bound')
        elif (control/'driver').exists():
            raise ValueError('Modem CDC interfaces are only partially bound')
    control_driver = control/'driver'
    if control_driver.exists() or control_driver.is_symlink():
        if control_driver.resolve(strict=True) != driver.resolve(strict=True):
            raise ValueError('Modem control interface has an unexpected driver')
    return control


def cdc_driver_state(device, driver=CDC_DRIVER):
    try:
        control = cdc_control_interface(device, driver, strict=True)
        # A control driver with an unbound data interface is transitional too.
        data = next(p for p in device.glob(device.name+':*')
                    if (p/'bInterfaceNumber').read_text().strip().lower() == '01')
        bound = (control/'driver').exists()
        if bound != (data/'driver').exists():
            return 'unknown'
        return 'bound' if bound else 'unbound'
    except (OSError, ValueError, StopIteration):
        return 'unknown'


def inspect_modem():
    try:
        usb = modem_device()
        device = usb.name
        generation = f"{(usb/'busnum').read_text().strip()}:{(usb/'devnum').read_text().strip()}"
        cdc_state = cdc_driver_state(usb)
    except FileNotFoundError:
        device = None
        generation = None
        cdc_state = 'unknown'
    base = Path('/sys/class/net')/INTERFACE
    present = (base/'address').exists()
    stats = {}
    for key in ('rx_bytes', 'tx_bytes', 'rx_errors', 'tx_errors'):
        try:
            stats[key] = int((base/'statistics'/key).read_text())
        except OSError:
            stats[key] = 0
    try:
        carrier = (base/'carrier').read_text().strip() == '1'
    except OSError:
        carrier = False
    if device is None:
        # There is no identified USB target to repair. An NM activation cannot
        # recreate a USB device, and probing a missing interface cannot add
        # useful transport evidence. Keep polling only the passive identity.
        return {**stats, 'nm_state': None, 'nm_profile': None, 'ipv4': [],
                'gateway': False, 'internet': False, 'public': False,
                'dns': False, 'dns_proxy': False, 'carrier': carrier,
                'device': None, 'usb_generation': None,
                'interface_present': present, 'probe_interface': INTERFACE,
                'cdc_driver_state': cdc_state}
    with ThreadPoolExecutor(max_workers=9) as pool:
        nm = pool.submit(network_manager_state)
        probes = [pool.submit(connect, '192.168.125.1', 80, interface=INTERFACE),
                  pool.submit(connect, '1.1.1.1', 443, True, interface=INTERFACE),
                  pool.submit(connect, '8.8.8.8', 443, True, interface=INTERFACE),
                  pool.submit(dns_ready, '127.0.0.53'),
                  pool.submit(dns_ready, '1.1.1.1', interface=INTERFACE),
                  pool.submit(dns_ready, '8.8.8.8', interface=INTERFACE),
                  pool.submit(public_ready, interface=INTERFACE),
                  pool.submit(dns_ready, '127.0.0.55')]
        gateway, cf, google, stub, dns_cf, dns_google, public, proxy = [p.result() for p in probes]
    internet = bool((cf or google or public) and device and present and carrier)
    return {**stats, **nm.result(), 'gateway': gateway, 'internet': internet, 'public': public,
            'dns': stub and (proxy or dns_cf or dns_google), 'dns_proxy': proxy, 'carrier': carrier,
            'device': device, 'usb_generation': generation,
            'interface_present': present, 'probe_interface': INTERFACE,
            'cdc_driver_state': cdc_state}


class RecoveryPolicy:
    def __init__(self, state=None):
        state = state or {}
        self.count = state.get('count', 0)
        self.next_action = state.get('next_action', 0)
        self.baseline = None
        self.stalled_since = None
        self.failed_since = None
        self.healthy_since = None
        self.recent_driver_stalls = list(state.get('recent_driver_stalls', []))[-8:]
        self.pending_driver_stall = False
        self.usb_count = state.get('usb_count', 0)
        self.next_usb_action = state.get('next_usb_action', 0)
        self.local_since = None
        self.local_key = None
        self.local_count = state.get('local_count', 0)
        self.next_local_action = state.get('next_local_action', 0)
        self.pending_local_fault = False
        self.unbound_since = None
        self.unbound_key = None
        self.pending_cdc_unbound = False
        self.waiting_for_usb = False

    def observe(self, sample, now):
        self.pending_driver_stall = False
        self.pending_local_fault = False
        self.pending_cdc_unbound = False
        self.recent_driver_stalls = [at for at in self.recent_driver_stalls
                                     if 0 <= now-at <= 300]
        if sample is None:  # Identity/inspection error: do not act on another device.
            self.baseline = None
            self.stalled_since = None
            self.failed_since = None
            self.healthy_since = None
            self.local_since = None
            self.local_key = None
            self.unbound_since = self.unbound_key = None
            return None
        if sample['device'] is None:
            # The card can still provide its onboard AP while its USB function
            # is absent. Do not turn an old outage deadline into endless NM
            # activations, or mutate an unidentifiable port/hub/controller.
            self.waiting_for_usb = True
            self.baseline = None
            self.stalled_since = self.failed_since = self.healthy_since = None
            self.local_since = self.local_key = None
            self.unbound_since = self.unbound_key = None
            return None
        internet = bool(sample['internet'] and sample['device'] and
                        sample['interface_present'] and sample['carrier'])
        if internet and sample['dns']:
            self.waiting_for_usb = False
            self.failed_since = None
            self.baseline = None
            self.stalled_since = None
            self.local_since = None
            self.local_key = None
            self.unbound_since = self.unbound_key = None
            if self.healthy_since is None:
                self.healthy_since = now
            if now-self.healthy_since >= 30:
                self.count = 0
                self.next_action = 0
                self.local_count = 0
                self.next_local_action = 0
                self.usb_count = 0
                self.next_usb_action = 0
            return None
        self.healthy_since = None
        if self.failed_since is None:
            self.baseline = sample.copy()
            self.failed_since = now
            self.stalled_since = now
        elif (sample['device'] != self.baseline['device'] or
              sample.get('usb_generation') != self.baseline.get('usb_generation') or
              sample['rx_bytes'] != self.baseline['rx_bytes'] or
              sample['tx_bytes'] != self.baseline['tx_bytes'] or
              sample['rx_errors'] < self.baseline['rx_errors'] or
              sample['tx_errors'] < self.baseline['tx_errors']):
            # Refresh the transport baseline independently of NM/DHCP state.
            self.baseline = sample.copy()
            self.stalled_since = now
        errors = sample['rx_errors'] + sample['tx_errors']
        old_errors = self.baseline['rx_errors'] + self.baseline['tx_errors']
        driver_stall = (not internet and sample['device'] is not None and
                        sample['device'] == self.baseline['device'] and
                        sample.get('cdc_driver_state', 'bound') == 'bound' and
                        not sample['gateway'] and errors-old_errors >= 50 and
                        sample['rx_bytes'] == self.baseline['rx_bytes'] and
                        sample['tx_bytes'] == self.baseline['tx_bytes'])
        if driver_stall and now-self.stalled_since >= STALL_CONFIRM_SECONDS:
            # Direct USB fault evidence takes priority over simultaneous DHCP
            # failure and does not inherit an unrelated radio retry deadline.
            if now < self.next_usb_action:
                return None
            self.pending_driver_stall = True
            return 'rebind' if self.usb_count >= 2 or self.recent_driver_stalls else 'reset'
        cdc_unbound = (not internet and not sample['gateway'] and sample['device']
                       and sample.get('usb_generation') and not sample['interface_present']
                       and sample.get('cdc_driver_state') == 'unbound'
                       and sample.get('nm_profile') in (None, PROFILE)
                       and sample.get('nm_state') not in (40, 50, 60, 70, 80, 90, 110))
        if cdc_unbound:
            key = (sample['device'], sample['usb_generation'])
            if self.unbound_key != key:
                self.unbound_key, self.unbound_since = key, now
            if now-self.unbound_since >= LOCAL_CONFIRM_SECONDS and now >= self.next_usb_action:
                self.pending_cdc_unbound = True
                return 'rebind'
            # NM cannot activate an absent interface. Preserve the USB retry
            # clock instead of restarting the 45-second upstream waiting path.
            return None
        self.unbound_since = self.unbound_key = None
        if (sample['device'] and not sample['interface_present']
                and 'cdc_driver_state' in sample):
            # Unknown/partial/foreign driver evidence cannot authorize repair.
            return None
        nm_state = sample.get('nm_state')
        profile = sample.get('nm_profile')
        local_fault = (not internet and sample['device'] and sample['interface_present']
                       and sample['carrier'] and profile in (None, PROFILE)
                       and (nm_state in (30, 120) or
                            (nm_state == 100 and profile == PROFILE and not sample.get('ipv4'))))
        if local_fault:
            key = (sample['device'], sample.get('usb_generation'), nm_state, profile)
            if (self.waiting_for_usb and sample.get('usb_generation')
                    and sample.get('cdc_driver_state') == 'bound'):
                # The identified card and its complete CDC pair returned. A
                # confirmed disconnected/failed fixed profile can be started
                # now; neither the absent-device period nor an old 45-second
                # deadline should delay that first safe activation.
                self.waiting_for_usb = False
                self.local_key, self.local_since = key, now
                self.next_local_action = 0
                self.pending_local_fault = True
                return 'reconnect'
            if self.local_key != key:
                self.local_key, self.local_since = key, now
            if (now-self.local_since >= LOCAL_CONFIRM_SECONDS and now >= self.next_local_action):
                self.pending_local_fault = True
                return 'reconnect'
            # A local profile failure has its own reconnect clock; never turn
            # this failure into USB reset, even after many unsuccessful tries.
            return None
        else:
            self.local_key = self.local_since = None
            if (self.waiting_for_usb and sample['interface_present']
                    and sample.get('cdc_driver_state') == 'bound'
                    and nm_state == 100 and profile == PROFILE and sample.get('ipv4')):
                self.waiting_for_usb = False
        if now < self.next_action:
            return None
        elapsed = now-self.failed_since
        if internet:
            return 'repair-dns' if elapsed >= 30 else None
        if elapsed < 45:
            return None
        if sample['gateway']:
            # Successful bidirectional LAN traffic demonstrates working USB.
            # A radio/APN/upstream outage does not justify USB reset/rebind.
            return 'reconnect'
        if sample['device'] and sample.get('cdc_driver_state', 'bound') != 'bound':
            # An explicit unknown/foreign/partial CDC state must not reach the
            # generic reset cycle, even when a network interface still exists.
            return None
        # First renew only this modem's connection. If that fails, reset the
        # matching USB modem. Continue indefinitely with bounded backoff.
        if self.count == 0:
            return 'reconnect'
        return ('reset', 'rebind', 'reconnect')[(self.count-1) % 3]

    def attempted(self, now):
        if self.pending_local_fault:
            self.local_count += 1
            self.next_local_action = now + LOCAL_RETRY_SECONDS
            self.pending_local_fault = False
            return self.next_local_action
        if self.pending_cdc_unbound:
            self.pending_cdc_unbound = False
            self.usb_count += 1
            self.next_usb_action = now + min(MAX_RETRY_SECONDS, 10 * 2**min(self.usb_count-1, 4))
            return self.next_usb_action
        if self.pending_driver_stall:
            self.recent_driver_stalls = (self.recent_driver_stalls + [now])[-8:]
            self.pending_driver_stall = False
            self.usb_count += 1
            self.next_usb_action = now + min(MAX_RETRY_SECONDS, 10 * 2**min(self.usb_count-1, 4))
            return self.next_usb_action
        self.count += 1
        self.next_action = now + min(MAX_RETRY_SECONDS, 10 * 2**min(self.count-1, 4))
        return self.next_action

    def state(self):
        return {'count': self.count, 'next_action': self.next_action,
                'recent_driver_stalls': self.recent_driver_stalls,
                'usb_count': self.usb_count, 'next_usb_action': self.next_usb_action,
                'local_count': self.local_count, 'next_local_action': self.next_local_action}


def matches_expected_usb(device, generation, expected_device, expected_generation):
    if expected_device is not None:
        target_matches = (device == expected_device if isinstance(expected_device, Path)
                          else device.name == expected_device)
        if not target_matches:
            return False
    return expected_generation is None or generation == expected_generation


def parse_usb_generation(value):
    if not isinstance(value, str) or not re.fullmatch(r'[1-9][0-9]*:[1-9][0-9]*', value):
        raise ValueError('Expected a positive USB bus:device generation')
    return tuple(int(part) for part in value.split(':'))


def recovery_command(action, sample):
    command = [sys.executable, '-I', str(INSTALLED), action]
    if action in ('reset', 'rebind'):
        device = sample.get('device')
        generation = sample.get('usb_generation')
        if not isinstance(device, str) or not re.fullmatch(r'[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*)*', device):
            raise ValueError('Observed USB modem path is unavailable')
        parse_usb_generation(generation)
        command += ['--expected-device', device, '--expected-generation', generation]
    return command


def reset_modem(expected_device=None, expected_generation=None):
    if onboard_hotspot_in_use():
        raise BlockingIOError(errno.EBUSY, 'Preserving the active onboard Wi-Fi rescue path')
    device = modem_device()
    bus = int((device/'busnum').read_text())
    number = int((device/'devnum').read_text())
    if not matches_expected_usb(device, (bus, number), expected_device, expected_generation):
        raise RuntimeError('USB identity changed before reset')
    if expected_generation is not None:
        cdc_control_interface(device, strict=True)
    path = Path(f'/dev/bus/usb/{bus:03d}/{number:03d}')
    with path.open('wb', buffering=0) as stream:
        # Revalidate before issuing USBDEVFS_RESET (_IO('U', 20)).
        if (modem_device() != device or int((device/'devnum').read_text()) != number
                or int((device/'busnum').read_text()) != bus):
            raise RuntimeError('USB identity changed before reset')
        if expected_generation is not None:
            cdc_control_interface(device, strict=True)
        if onboard_hotspot_in_use():
            raise BlockingIOError(errno.EBUSY, 'Onboard hotspot became active before USB reset')
        LOG.warning('Resetting verified modem only: %s (%s:%s), interface %s',
                    device.name, VENDOR, PRODUCT, INTERFACE)
        fcntl.ioctl(stream.fileno(), 0x5514, 0)


def modem_profile_check():
    # An administrator may have edited the profile: never activate eno1.
    result = subprocess.run(
        ['nmcli', '-g', 'connection.interface-name', 'connection', 'show', 'uuid', PROFILE],
        capture_output=True, text=True, timeout=5, check=True)
    if result.stdout.strip() != INTERFACE:
        raise ValueError('Mobile profile interface does not match; refusing operation')
    base = Path('/sys/class/net')/INTERFACE
    if (base/'address').exists() and (base/'address').read_text().strip().lower() != MAC:
        raise ValueError('Interface MAC changed; refusing operation')


def modem_interface_present(net_root=Path('/sys/class/net')):
    try:
        address = (net_root/INTERFACE/'address').read_text().strip().lower()
    except FileNotFoundError:
        return False
    if address != MAC:
        raise ValueError('Mobile interface MAC changed before activation')
    return True


def reconnect_modem():
    modem_profile_check()
    # Revalidate both identities immediately before activation. The card can
    # disappear between monitor observation and this child process starting.
    modem_device()
    if not modem_interface_present():
        raise FileNotFoundError('Mobile interface is unavailable before activation')
    LOG.warning('Reactivating mobile profile only: %s on %s', PROFILE, INTERFACE)
    subprocess.run(['nmcli', '--wait', '20', 'connection', 'up', 'uuid', PROFILE,
                    'ifname', INTERFACE], check=True, timeout=25)


def rebind_modem(driver=CDC_DRIVER, expected_device=None, expected_generation=None):
    device = modem_device()
    generation = ((int((device/'busnum').read_text()), int((device/'devnum').read_text()))
                  if (device/'busnum').exists() and (device/'devnum').exists() else None)
    if not matches_expected_usb(device, generation, expected_device, expected_generation):
        raise ValueError('Observed modem identity changed before driver recovery')

    def verify(strict=False):
        current = modem_device()
        if (current != device or (generation is not None and
                (int((current/'busnum').read_text()), int((current/'devnum').read_text())) != generation)):
            raise ValueError('Modem identity changed before driver recovery')
        if strict and generation is None:
            raise ValueError('Modem USB generation is unavailable for missing-interface recovery')
        return cdc_control_interface(current, driver, strict=strict)

    interface = verify(strict=expected_generation is not None)
    if not (interface/'driver').exists():
        # Missing CDC must be established by actual interface descriptors.
        interface = verify(strict=True)
    LOG.warning('Rebinding modem CDC Ethernet interface only: %s', interface.name)
    if (interface/'driver').exists():
        (driver/'unbind').write_text(interface.name)
    interface = verify()
    if (interface/'driver').exists():
        raise RuntimeError('Modem control interface remained bound after unbind')
    try:
        (driver/'bind').write_text(interface.name)
    except OSError as exc:
        if exc.errno != errno.EPROTO:
            raise
        # A failed probe leaves USB present but CDC unbound. Reset that exact
        # modem and explicitly bind once; USB reset alone will not bind it.
        interface = verify(strict=True)
        if (interface/'driver').exists():
            raise ValueError('Modem driver changed after failed bind') from exc
        if onboard_hotspot_in_use():
            LOG.warning('CDC bind failed with EPROTO; deferring USB reset to preserve onboard Wi-Fi')
            raise BlockingIOError(errno.EBUSY, 'Preserving the active onboard Wi-Fi rescue path') from exc
        LOG.warning('CDC bind failed with EPROTO; resetting verified modem and binding once')
        reset_modem(expected_device=device, expected_generation=generation)
        interface = verify(strict=True)
        if not (interface/'driver').exists():
            (driver/'bind').write_text(interface.name)


def repair_dns():
    modem_profile_check()
    if not dns_ready('127.0.0.55'):
        subprocess.run(['systemctl', 'restart', RESOLVER_UNIT], check=True, timeout=10)
    # Cache/feature reset and a DNS service recovery do not alter eno1, its
    # camera route, or its NetworkManager profile.
    for command in (['resolvectl', 'flush-caches'], ['resolvectl', 'reset-server-features'],
                    ['nmcli', 'device', 'reapply', INTERFACE]):
        try:
            subprocess.run(command, check=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            LOG.warning('DNS repair command failed: %s', command)
    if not dns_ready('127.0.0.53'):
        subprocess.run(['systemctl', 'restart', 'systemd-resolved.service'],
                       check=True, timeout=10)


def save_attempts(policy, boot):
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = STATE/'attempts.tmp'
    with temporary.open('w') as out:
        json.dump({'boot_id': boot, 'policy': policy.state()}, out)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(STATE/'attempts.json')


def notify(message):
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0'+address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), address)
    except OSError:
        LOG.warning('Unable to report recovery service watchdog status')


class RecoveryHealthReporter:
    """Report an absent USB function clearly without repeated probe/action spam."""
    def __init__(self):
        self.previous_unhealthy = None
        self.waiting_for_usb = False
        self.last_usb_wait_log = None

    def report(self, sample, now):
        unhealthy = sample is not None and not (sample['internet'] and sample['dns'])
        if sample is not None and sample['device'] is None:
            self.waiting_for_usb = True
            if self.last_usb_wait_log is None or now-self.last_usb_wait_log >= USB_WAIT_LOG_SECONDS:
                LOG.info('USB modem is not enumerated; waiting for USB re-enumeration. '
                         'No NetworkManager activation or USB reset; preserving onboard Wi-Fi.')
                self.last_usb_wait_log = now
        else:
            if sample is not None and self.waiting_for_usb:
                LOG.info('Verified USB modem re-enumerated: device=%s generation=%s CDC=%s; '
                         'checking the fixed mobile profile', sample['device'],
                         sample.get('usb_generation'), sample.get('cdc_driver_state'))
                self.waiting_for_usb = False
                self.last_usb_wait_log = None
            if unhealthy or self.previous_unhealthy != unhealthy:
                LOG.info('Modem health evidence: %s', json.dumps(sample))
        self.previous_unhealthy = unhealthy
        return unhealthy


def monitor():
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    state = {}
    try:
        saved = json.loads((STATE/'attempts.json').read_text())
        if saved['boot_id'] == boot:
            state = saved.get('policy', {})
    except (OSError, ValueError, KeyError, TypeError):
        LOG.info('No usable saved recovery state; starting fresh')
    # A pre-update retry could have retained the old five-minute deadline.
    state['next_action'] = min(state.get('next_action', 0), time.monotonic()+MAX_RETRY_SECONDS)
    policy = RecoveryPolicy(state)
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    reporter = RecoveryHealthReporter()
    notify('READY=1')
    while not stopped.is_set():
        try:
            sample = inspect_modem()
        except (OSError, ValueError) as exc:
            sample = None
            LOG.info('Modem unavailable for recovery: %s', exc)
        now = time.monotonic()
        unhealthy = reporter.report(sample, now)
        action = policy.observe(sample, now)
        if action:
            if action == 'reset' and onboard_hotspot_in_use():
                # Rebind only CDC in the background. reset_modem rechecks this
                # guard too, including the EPROTO bind fallback.
                action = 'rebind'
                LOG.info('Using CDC recovery while retaining onboard Wi-Fi Internet')
            # Save before trying so daemon restarts cannot bypass the backoff.
            local_action = policy.pending_local_fault
            usb_action = policy.pending_driver_stall
            cdc_action = policy.pending_cdc_unbound
            deadline = policy.attempted(now)
            save_attempts(policy, boot)
            LOG.warning('Network recovery action=%s fault=%s attempt=%s next_retry_in=%.0fs',
                        action, 'local-profile' if local_action else 'cdc-unbound' if cdc_action else 'usb-stall' if usb_action else 'transport-or-upstream',
                        policy.local_count if local_action else policy.usb_count if usb_action or cdc_action else policy.count, deadline-now)
            try:
                subprocess.run(recovery_command(action, sample), timeout=30, check=True)
            except (OSError, ValueError, subprocess.SubprocessError):
                LOG.exception('Network recovery failed; will retry after backoff')
        elif not unhealthy and sample is not None:
            save_attempts(policy, boot)
        notify('WATCHDOG=1')
        stopped.wait(5)


def update_recovery():
    """Update an existing recovery service without restarting DNS or the tunnel."""
    modem_device()
    modem_profile_check()
    if not INSTALLED.is_file() or not (Path('/etc/systemd/system')/UNIT).is_file():
        raise RuntimeError('Recovery service is not installed; use install first')
    source = Path(__file__).read_bytes()
    compile(source, str(INSTALLED), 'exec')
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    (STATE/'modem_recovery.previous.py').write_bytes(INSTALLED.read_bytes())
    temporary = INSTALLED.with_suffix('.tmp')
    temporary.write_bytes(source)
    temporary.chmod(0o644)
    temporary.replace(INSTALLED)
    try:
        subprocess.run(['systemctl', 'restart', UNIT], check=True, timeout=20)
        subprocess.run(['systemctl', 'is-active', '--quiet', UNIT], check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        INSTALLED.write_bytes((STATE/'modem_recovery.previous.py').read_bytes())
        subprocess.run(['systemctl', 'restart', UNIT], check=True, timeout=20)
        raise
    print('Updated modem recovery only; DNS and application services retained.')


def install():
    modem_device()  # Fail before installing on another machine/device.
    modem_profile_check()
    INSTALLED.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if INSTALLED.exists():
        STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
        (STATE/'modem_recovery.previous.py').write_bytes(INSTALLED.read_bytes())
    INSTALLED.write_bytes(Path(__file__).read_bytes())
    INSTALLED.chmod(0o644)
    source_root = Path(__file__).resolve().parent.parent
    resolver = INSTALLED.parent/'tunnel_resolver.py'
    resolver.write_bytes((source_root/'scripts/tunnel_resolver.py').read_bytes())
    resolver.chmod(0o644)
    resolver_unit = f'''[Unit]
Description=Loopback UAV DNS with UDP/TCP and verified HTTPS fallback
After=NetworkManager.service
StartLimitIntervalSec=0

[Service]
Type=simple
User=hrc
Group=hrc
ExecStart={source_root}/.venv/bin/python {resolver}
Restart=always
RestartSec=5
TimeoutStopSec=5
UMask=0077
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=true
ProtectSystem=strict
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''
    resolver_path = Path('/etc/systemd/system')/RESOLVER_UNIT
    resolver_path.write_text(resolver_unit)
    resolver_path.chmod(0o644)
    unit = f'''[Unit]
Description=Persistent staged recovery for UAV mobile network only
After=NetworkManager.service {RESOLVER_UNIT}
Wants={RESOLVER_UNIT}
StartLimitIntervalSec=0

[Service]
Type=notify
ExecStart=/usr/bin/python3 -I {INSTALLED} monitor
Restart=always
RestartSec=15
WatchdogSec=120
NotifyAccess=main
TimeoutStopSec=5
UMask=0077
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=/sys/bus/usb/drivers/cdc_ether/bind /sys/bus/usb/drivers/cdc_ether/unbind
StateDirectory=laiting-uav-recovery
PrivateTmp=true

[Install]
WantedBy=multi-user.target
'''
    path = Path('/etc/systemd/system')/UNIT
    path.write_text(unit)
    path.chmod(0o644)
    subprocess.run(['systemd-analyze', 'verify', str(path), str(resolver_path)], check=True)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', 'enable', UNIT, RESOLVER_UNIT], check=True)
    subprocess.run(['systemctl', 'restart', RESOLVER_UNIT], check=True)
    for _ in range(5):
        if dns_ready('127.0.0.55'):
            break
        time.sleep(1)
    else:
        raise RuntimeError('DNS fallback failed validation; existing mobile DNS settings retained')
    # Older resolved versions bind per-link DNS to that physical interface;
    # loopback DNS belongs in the global resolver configuration instead.
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    original = RESOLVED_DOH.read_bytes() if RESOLVED_DOH.exists() else None
    backup = STATE/'resolved-doh-before.json'
    if not backup.exists():
        backup.write_text(json.dumps({'content': original.decode() if original is not None else None}))
    RESOLVED_DOH.parent.mkdir(parents=True, exist_ok=True)
    RESOLVED_DOH.write_text('[Resolve]\nDNS=\nDNS=127.0.0.55\n')
    RESOLVED_DOH.chmod(0o644)
    subprocess.run(['systemctl', 'restart', 'systemd-resolved.service'], check=True, timeout=10)
    if not dns_ready('127.0.0.53'):
        if original is None:
            RESOLVED_DOH.unlink()
        else:
            RESOLVED_DOH.write_bytes(original)
        subprocess.run(['systemctl', 'restart', 'systemd-resolved.service'], check=True, timeout=10)
        raise RuntimeError('Host DNS validation failed; original DNS configuration restored')
    subprocess.run(['systemctl', 'restart', UNIT], check=True)
    print('Installed persistent mobile-network recovery; camera interface excluded.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'install', 'update-recovery', 'monitor', 'reset',
                                           'reconnect', 'rebind', 'repair-dns'))
    parser.add_argument('--expected-device', help='USB path observed by the monitor, e.g. 1-2.2')
    parser.add_argument('--expected-generation', help='USB bus:device observed by the monitor, e.g. 1:18')
    args = parser.parse_args()
    if bool(args.expected_device) != bool(args.expected_generation):
        parser.error('Both expected USB identity arguments are required together.')
    expected_generation = None
    if args.expected_device:
        if args.action not in ('reset', 'rebind'):
            parser.error('Expected USB identity arguments apply only to reset/rebind.')
        try:
            expected_generation = parse_usb_generation(args.expected_generation)
        except ValueError as exc:
            parser.error(str(exc))
        if not re.fullmatch(r'[1-9][0-9]*-[1-9][0-9]*(?:\.[1-9][0-9]*)*', args.expected_device):
            parser.error('Invalid observed USB modem path.')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if args.action != 'check' and os.geteuid() != 0:
        parser.error('This action needs local administrator authentication (sudo).')
    if args.action == 'check':
        print(json.dumps(inspect_modem(), indent=2))
    elif args.action == 'install':
        install()
    elif args.action == 'update-recovery':
        update_recovery()
    elif args.action == 'reset':
        reset_modem(expected_device=args.expected_device, expected_generation=expected_generation)
    elif args.action == 'reconnect':
        reconnect_modem()
    elif args.action == 'rebind':
        rebind_modem(expected_device=args.expected_device, expected_generation=expected_generation)
    elif args.action == 'repair-dns':
        repair_dns()
    else:
        monitor()


if __name__ == '__main__':
    main()
