#!/usr/bin/env python3
"""Keep only the onboard D-Link's USB and Wi-Fi paths ready; no external Wi-Fi.

This root service accepts fixed path names over a private local socket. It
changes only a dedicated IPv4 default route; camera/flight/VPN routes remain.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ipaddress
import json
import logging
import math
import os
from pathlib import Path
import queue
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
import uuid

USB = 'enxac0033aa9633'
WIFI = 'wlP1p1s0'
USB_PROFILE = '8391cb7e-8a3a-317c-bf18-612aa9c5300e'
WIFI_PROFILE = '7b8a7e5f-0e63-46ed-9c9c-9666959890b2'
INTERFACES = {'usb': USB, 'wifi': WIFI}
PROFILES = {'usb': USB_PROFILE, 'wifi': WIFI_PROFILE}
CONFIG = Path('/etc/laiting-uav/modem-paths.json')
RUNTIME = Path('/run/laiting-uav-network')
STATE = Path('/var/lib/laiting-uav-network')
METRIC = 50
PROTOCOL = 186
LOG = logging.getLogger('uav.network.paths')
if not __package__:
    # Isolated Python trusts only the root-owned installed sibling directory.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from modem_recovery import notify
else:
    from .modem_recovery import notify


def run(*args, timeout=3, check=True):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, 'LC_ALL': 'C'})
    if check and result.returncode:
        # Never log command output: NetworkManager may include SSIDs.
        raise RuntimeError('Network command failed: '+args[0])
    return result


def load_config(path=CONFIG):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise ValueError('Network configuration must be root owned and protected')
    config = json.loads(path.read_text())
    if (config.get('wifi_profile') != WIFI_PROFILE or
            config.get('usb_profile') != USB_PROFILE or
            config.get('wifi_interface') != WIFI or
            not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', config.get('wifi_bssid', '')) or
            config.get('user_uid') != 1000 or config.get('user_gid') != 1000):
        raise ValueError('Unexpected network identity; refusing arbitrary profile')
    return config


def atomic_json(path, value, mode=0o644):
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, 'w') as output:
        json.dump(value, output, separators=(',', ':'), allow_nan=False)
    temporary.chmod(mode)
    temporary.replace(path)


def bound_connect(interface, host, port, tls=False, timeout=.8):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode()+b'\0')
            sock.connect((host, port))
            if tls:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=host):
                    pass
        return True
    except (OSError, ValueError):
        return False


def actual_route():
    try:
        result = run('ip', '-j', '-4', 'route', 'get', '1.1.1.1', check=False)
        if result.returncode:
            return 'none'
        routes = json.loads(result.stdout)
        if not routes:
            return 'none'
        return next((name for name, interface in INTERFACES.items()
                     if routes[0].get('dev') == interface), 'unknown')
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
        return 'unknown'


def network_identity(path, config):
    interface = INTERFACES[path]
    base = {'interface': interface, 'profile_uuid': PROFILES[path], 'gateway': None,
            'available': False, 'connected': False, 'internet': False}
    if not Path('/sys/class/net', interface).exists():
        return base
    try:
        if path == 'usb':
            try:
                from modem_recovery import modem_device
            except ImportError:
                from scripts.modem_recovery import modem_device
            device = modem_device()
            generation = tuple((device/name).read_text().strip() for name in ('busnum', 'devnum'))
            if any(not value.isdecimal() or not 1 <= int(value) <= 999 for value in generation):
                return base
            base.update(device=device.name, usb_generation=':'.join(generation))
        base['available'] = True
        result = run('nmcli', '-g', 'GENERAL.STATE,GENERAL.CON-UUID,IP4.GATEWAY',
                     'device', 'show', interface)
        rows = result.stdout.splitlines()
        if len(rows) < 3 or not rows[0].startswith('100 ') or rows[1] != PROFILES[path]:
            return base
        gateway = str(ipaddress.IPv4Address(rows[2]))
        if gateway != '192.168.125.1':
            return base
        if path == 'wifi':
            # The NM profile locks both the SSID and the physically observed BSSID.
            link = run('iw', 'dev', WIFI, 'link').stdout
            found = re.search(r'Connected to ([0-9a-fA-F:]{17})', link)
            if not found or found[1].lower() != config['wifi_bssid'].lower():
                return base
            base['bssid'] = found[1].lower()
        base.update(available=True, connected=True, gateway=gateway)
    except (OSError, ValueError, subprocess.SubprocessError, RuntimeError):
        pass
    return base


def inspect_path(path, config):
    status = network_identity(path, config)
    if status['connected']:
        with ThreadPoolExecutor(max_workers=3) as pool:
            probes = [pool.submit(bound_connect, status['interface'], host, port, tls)
                      for host, port, tls in (('192.168.125.1', 80, False),
                                              ('1.1.1.1', 443, True), ('8.8.8.8', 443, True))]
            gateway, first, second = [probe.result() for probe in probes]
        # A successful probe belongs to the observed device generation. A USB
        # function may disappear and return on the same interface during TLS.
        current = network_identity(path, config)
        fields = ('interface', 'profile_uuid', 'gateway', 'device', 'usb_generation', 'bssid')
        if not current['connected'] or any(current.get(key) != status.get(key) for key in fields):
            current.update(gateway_ready=False, internet=False)
            return current
        status.update(gateway_ready=gateway, internet=first or second)
    return status


def path_ready(status):
    return all(status.get(field) is True for field in ('available', 'connected', 'internet'))


class PathPolicy:
    """Fail over quickly to verified standby; avoid switching on common LTE loss."""
    def __init__(self, preferred='auto', state=None):
        self.preferred = preferred if preferred in ('auto', 'usb', 'wifi') else 'auto'
        self.failed = {'usb': 0, 'wifi': 0}
        self.healthy_since = {'usb': None, 'wifi': None}
        self.health_keys = {'usb': None, 'wifi': None}
        self.last_switch = float('-inf')
        self.usb_retry_at = 0
        state = state if isinstance(state, dict) else {}
        for field in ('last_switch', 'usb_retry_at'):
            value = state.get(field)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                setattr(self, field, value)

    def state(self):
        return {'last_switch': self.last_switch if math.isfinite(self.last_switch) else None,
                'usb_retry_at': self.usb_retry_at}

    def choose(self, status, active, now):
        for path in ('usb', 'wifi'):
            healthy = path_ready(status[path])
            key = tuple(status[path].get(field) for field in
                        ('interface', 'profile_uuid', 'device', 'usb_generation', 'bssid'))
            changed = self.health_keys[path] is not None and key != self.health_keys[path]
            recovered = self.failed[path] > 0
            self.failed[path] = 0 if healthy else self.failed[path]+1
            if healthy:
                if self.healthy_since[path] is None or changed:
                    self.healthy_since[path] = now
                if path == 'usb' and active == 'wifi' and (recovered or changed):
                    # Count the cooldown from the latest recovery, so a card
                    # that repeatedly re-enumerates cannot briefly reclaim WAN.
                    self.usb_retry_at = max(self.usb_retry_at, now+300)
                self.health_keys[path] = key
            else:
                self.healthy_since[path] = None
        desired = 'usb' if self.preferred == 'auto' else self.preferred
        other = 'wifi' if desired == 'usb' else 'usb'
        if active not in ('usb', 'wifi'):
            return next((path for path in (desired, other) if path_ready(status[path])), None)
        backup = 'wifi' if active == 'usb' else 'usb'
        local_failure = (not status[active]['connected'] or
                         not status[active].get('gateway_ready', False))
        if (not path_ready(status[active]) and path_ready(status[backup]) and
                (local_failure or self.failed[active] >= 2)):
            if active == 'usb':
                self.usb_retry_at = now+300
            return backup
        if (active != desired and path_ready(status[desired]) and
                self.healthy_since[desired] is not None and
                now-self.healthy_since[desired] >= 30 and now-self.last_switch >= 60 and
                (desired != 'usb' or now >= self.usb_retry_at)):
            return desired
        return None


def set_route(path, status):
    # Recheck device/profile/BSSID immediately before the single atomic mutation.
    if path not in INTERFACES or not path_ready(status):
        raise ValueError('Target path has no verified Internet access')
    gateway = str(ipaddress.IPv4Address(status['gateway']))
    if gateway != '192.168.125.1' or status['interface'] != INTERFACES[path]:
        raise ValueError('Unexpected modem route')
    run('ip', '-4', 'route', 'replace', 'default', 'via', gateway, 'dev',
        INTERFACES[path], 'metric', str(METRIC), 'proto', str(PROTOCOL))
    if actual_route() != path:
        raise RuntimeError('Kernel did not select the verified target')


def managed_choice(policy, paths, actual, managed, now):
    # NM immediately promotes its DHCP standby when USB disappears. Judge the
    # fault against our last selected path, then pin the promoted path before
    # the repaired USB profile can reclaim the ordinary DHCP default route.
    chosen = policy.choose(paths, managed or actual, now)
    if chosen:
        return chosen
    if managed is None:
        desired = 'usb' if policy.preferred == 'auto' else policy.preferred
        if policy.preferred != 'auto' and path_ready(paths[desired]):
            return desired
        # Adopt an already working default route at startup. Picking desired
        # here would bypass the policy's continuous-health/cooldown check.
        return actual if actual in paths and path_ready(paths[actual]) else None
    if actual != managed and path_ready(paths[managed]):
        return managed
    return None


def isolate_arp(path, config, proc_root=Path('/proc/sys/net/ipv4/conf')):
    """Two modem LAN links share a subnet; answer only for each link's address."""
    if not network_identity(path, config)['connected']:
        return
    for key, expected in (('arp_ignore', '1'), ('arp_announce', '2')):
        target = proc_root/INTERFACES[path]/key
        try:
            if target.read_text().strip() != expected:
                target.write_text(expected)
        except FileNotFoundError:
            # A verified USB interface may disappear between the identity
            # check and this procfs access. Keep inspecting the other path.
            return


def tunnel_status():
    try:
        result = run('curl', '--noproxy', '*', '-fsS', '--max-time', '.8',
                     'http://127.0.0.1:20241/ready', timeout=1, check=False)
        body = json.loads(result.stdout) if result.returncode == 0 else {}
        return {'ready': body.get('readyConnections', 0) > 0, 'connector': body.get('connectorId')}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return {'ready': False, 'connector': None}


def website_ready():
    try:
        result = run('curl', '--noproxy', '*', '-fsS', '--connect-timeout', '1', '--max-time', '2',
                     'https://uav.laiting1202.com/healthz', timeout=3, check=False)
        return result.returncode == 0 and json.loads(result.stdout) == {'status': 'ok'}
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
        return False


class Manager:
    def __init__(self, config):
        self.config = config
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.requests = queue.Queue(maxsize=1)
        self.completed = {}
        self.pending_id = None
        self.queued_started = None
        self.status = {'active_path': 'unknown', 'preferred_path': 'auto', 'phase': 'preparing',
                       'switching': False, 'last_switch': None, 'error': '', 'tunnel_ready': False,
                       'recent_requests': [],
                       'usb': {}, 'wifi': {}, 'updated_at': time.time()}
        self.status['boot_id'] = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        try:
            saved = json.loads((STATE/'preference.json').read_text())
        except (OSError, ValueError):
            saved = {}
        if not isinstance(saved, dict):
            saved = {}
        try:
            routing = json.loads((STATE/'routing.json').read_text())
            if not isinstance(routing, dict) or routing.get('boot_id') != self.status['boot_id']:
                routing = {}
        except (OSError, ValueError):
            routing = {}
        self.policy = PathPolicy(saved.get('preferred', 'auto'), routing.get('policy'))
        self.saved_routing = None
        self.switch_monotonic = None
        self.next_web_probe = 0
        self.managed_path = routing.get('managed_path') if routing.get('managed_path') in INTERFACES else None
        self.before_connector = None
        self.require_new_connector = False
        self.inspection_error = False

    def publish(self):
        with self.lock:
            routing = {'boot_id': self.status['boot_id'], 'managed_path': self.managed_path,
                       'policy': self.policy.state()}
            if routing != self.saved_routing:
                atomic_json(STATE/'routing.json', routing, 0o600)
                self.saved_routing = routing
            self.status['updated_at'] = time.time()
            self.status['monotonic'] = time.monotonic()
            self.status['preferred_path'] = self.policy.preferred
            atomic_json(RUNTIME/'status.json', self.status)

    def request_result(self, request_id, target, state):
        if not request_id:
            return
        with self.lock:
            records = [record for record in self.status['recent_requests']
                       if record['request_id'] != request_id]
            records.append({'request_id': request_id, 'target': target, 'state': state})
            self.status['recent_requests'] = records[-16:]

    def request(self, request):
        if not isinstance(request, dict):
            return {'ok': False, 'error': '無效的切換請求'}
        if request.get('action') == 'status' and set(request) == {'action'}:
            with self.lock:
                return {'ok': True, 'status': json.loads(json.dumps(self.status))}
        if (set(request) != {'action', 'target', 'request_id'} or request.get('action') != 'switch'
                or request.get('target') not in ('auto', 'usb', 'wifi')):
            return {'ok': False, 'error': '只允許機上網卡的固定連線方式'}
        try:
            request_id = str(uuid.UUID(request['request_id']))
        except (ValueError, TypeError, AttributeError):
            return {'ok': False, 'error': '無效的切換識別碼'}
        with self.lock:
            if request_id in self.completed:
                old = self.completed[request_id]
                return old if old['target'] == request['target'] else {'ok': False, 'error': '識別碼已用於另一個操作'}
            if self.pending_id is not None or self.status.get('switching') is True:
                return {'ok': False, 'error': '目前正在切換，請等待連線恢復'}
            target = request['target']
            if target != 'auto' and not path_ready(self.status.get(target, {})):
                return {'ok': False, 'error': '指定路徑尚未驗證可上網，保留目前連線'}
            if target == 'auto' and not any(path_ready(self.status.get(path, {})) for path in INTERFACES):
                return {'ok': False, 'error': '機上網卡路徑尚未驗證可上網，系統持續恢復'}
            response = {'ok': True, 'accepted': True, 'request_id': request_id, 'target': target}
            self.completed[request_id] = response
            while len(self.completed) > 16:
                self.completed.pop(next(iter(self.completed)))
            self.pending_id = request_id
            self.queued_started = time.time()
            self.status.update(switching=True, phase='preparing', error='')
            self.requests.put_nowait((target, request_id))
            self.request_result(request_id, target, 'queued')
            self.publish()
            return response

    def socket_loop(self):
        path = RUNTIME/'control.sock'
        if path.exists() or path.is_symlink():
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
            listener.bind(str(path))
            os.chown(path, 0, self.config['user_gid'])
            path.chmod(0o660)
            listener.listen(8)
            listener.settimeout(.5)
            while not self.stop.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                with client:
                    client.settimeout(.5)
                    try:
                        peer = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
                        if peer not in (0, self.config['user_uid']):
                            continue
                        raw = bytearray()
                        while b'\n' not in raw and len(raw) <= 2048:
                            part = client.recv(2049-len(raw))
                            if not part:
                                break
                            raw.extend(part)
                        if len(raw) > 2048 or b'\n' not in raw:
                            continue
                        response = self.request(json.loads(raw.split(b'\n')[0]))
                        client.sendall(json.dumps(response, separators=(',', ':')).encode()+b'\n')
                    except (OSError, ValueError, TypeError, queue.Full):
                        continue

    def standby_loop(self):
        """Rejoin only the pinned profile after AP restart; never search alternatives."""
        policy = Path('/etc/NetworkManager/conf.d/95-uav-onboard-wifi-only.conf')
        while not self.stop.is_set():
            try:
                info = policy.lstat()
                allowed = ('allowed-connections=uuid:'+WIFI_PROFILE)
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022 or
                        allowed not in policy.read_text()):
                    raise ValueError('Onboard Wi-Fi allowlist is missing')
                if not network_identity('wifi', self.config)['connected']:
                    run('nmcli', 'radio', 'wifi', 'on')
                    run('nmcli', '--wait', '5', 'connection', 'up', 'uuid', WIFI_PROFILE,
                        'ifname', WIFI, timeout=7, check=False)
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                LOG.warning('Pinned onboard Wi-Fi standby unavailable; retrying without alternatives')
            self.stop.wait(15)

    def switch(self, target, paths, reason, request_id=None):
        with self.lock:
            self.status.update(switching=True, phase='switching', error='')
            started = self.queued_started if request_id and self.queued_started else time.time()
            self.status['last_switch'] = {'target': target, 'reason': reason, 'request_id': request_id,
                                          'started_at': started, 'route_seconds': None,
                                          'website_seconds': None, 'finished_at': None}
            self.publish()
        previous_path = actual_route()
        self.before_connector = tunnel_status()['connector']
        self.require_new_connector = (previous_path in INTERFACES and previous_path != target or
                                      self.managed_path is not None and self.managed_path != target)
        before = time.monotonic()
        identity = network_identity(target, self.config)
        expected = paths[target]
        fields = ('interface', 'profile_uuid', 'gateway', 'device', 'usb_generation', 'bssid')
        required = ('interface', 'profile_uuid', 'gateway') + (
            ('device', 'usb_generation') if target == 'usb' else ('bssid',))
        if (identity.get('available') is not True or identity.get('connected') is not True or
                any(not isinstance(expected.get(field), str) or not expected[field] for field in required) or
                any(identity.get(field) != expected.get(field) for field in fields)):
            raise ValueError('Target identity changed; retaining route')
        set_route(target, paths[target])
        elapsed = time.monotonic()-before
        self.policy.last_switch = time.monotonic()
        self.managed_path = target
        self.switch_monotonic = before
        self.next_web_probe = 0
        with self.lock:
            self.status.update(active_path=target, switching=False, phase='recovering_tunnel')
            self.status['route_changed_at'] = self.policy.last_switch
            self.status['last_switch']['route_seconds'] = round(elapsed, 4)
            accepted = self.completed.get(request_id)
            self.request_result(request_id, accepted['target'] if accepted else target, 'route_ready')
            self.pending_id = None
            self.queued_started = None
            self.publish()
        LOG.info('Onboard path selected: %s, route seconds %.4f, reason %s', target, elapsed, reason)

    def monitor(self):
        thread = threading.Thread(target=self.socket_loop, daemon=True)
        thread.start()
        standby = threading.Thread(target=self.standby_loop, daemon=True)
        standby.start()
        notify('READY=1')
        with ThreadPoolExecutor(max_workers=2) as pool:
            while not self.stop.is_set():
                if not thread.is_alive():
                    raise RuntimeError('Network control socket stopped; restarting service')
                if not standby.is_alive():
                    raise RuntimeError('Onboard Wi-Fi standby stopped; restarting service')
                started = time.monotonic()
                request_id = None
                try:
                    for path in INTERFACES:
                        isolate_arp(path, self.config)
                    futures = {path: pool.submit(inspect_path, path, self.config) for path in INTERFACES}
                    paths = {path: future.result() for path, future in futures.items()}
                    active = actual_route()
                    if active in INTERFACES and not paths[active]['connected']:
                        active = 'unknown'
                    choice = managed_choice(self.policy, paths, active, self.managed_path, time.monotonic())
                    with self.lock:
                        for path in INTERFACES:
                            since = self.policy.healthy_since[path]
                            paths[path]['healthy_for'] = round(time.monotonic()-since, 2) if since is not None else 0
                        self.status.update(active_path=active, **paths)
                        if self.inspection_error:
                            self.status['error'] = ''
                            self.inspection_error = False
                    try:
                        preference, request_id = self.requests.get_nowait()
                    except queue.Empty:
                        preference = request_id = None
                    if preference:
                        desired = ('usb' if path_ready(paths['usb']) else 'wifi') if preference == 'auto' else preference
                        if not path_ready(paths[desired]):
                            raise ValueError('Requested path became unavailable; retained current preference')
                        self.switch(desired, paths, '手動切換', request_id)
                        self.policy.preferred = preference
                        atomic_json(STATE/'preference.json', {'preferred': preference}, 0o600)
                    elif choice:
                        self.switch(choice, paths, '自動備援' if active in INTERFACES else '開機選擇')
                    tunnel = tunnel_status()
                    switched_tunnel = (not self.require_new_connector or
                                       (tunnel['connector'] is not None and tunnel['connector'] != self.before_connector))
                    with self.lock:
                        self.status['tunnel_ready'] = tunnel['ready'] and switched_tunnel
                    if self.switch_monotonic is not None and time.monotonic() >= self.next_web_probe:
                        self.next_web_probe = time.monotonic()+1
                        observed_switch = self.status['last_switch']
                        if tunnel['ready'] and switched_tunnel and website_ready():
                            with self.lock:
                                if self.pending_id is None and self.status['last_switch'] is observed_switch:
                                    self.status['last_switch'].update(
                                        website_seconds=round(time.monotonic()-self.switch_monotonic, 3),
                                        finished_at=time.time())
                                    self.status['phase'] = 'stable'
                                    self.switch_monotonic = None
                        elif time.monotonic()-self.switch_monotonic >= 30:
                            with self.lock:
                                self.status.update(error='出口已切換，網頁尚未恢復；系統持續重連', switching=False)
                                self.pending_id = None
                            self.switch_monotonic = None
                    elif self.switch_monotonic is None:
                        with self.lock:
                            self.status['phase'] = 'stable' if any(p['internet'] for p in paths.values()) else 'offline'
                    self.publish()
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError):
                    LOG.exception('Onboard path inspection/switch failed; retrying')
                    with self.lock:
                        self.inspection_error = not bool(request_id)
                        if request_id:
                            accepted = self.completed.get(request_id)
                            self.request_result(request_id, accepted['target'] if accepted else preference, 'failed')
                            if self.pending_id == request_id:
                                self.pending_id = None
                                self.queued_started = None
                        waiting = self.pending_id is not None
                        self.status.update(switching=waiting,
                                           phase='preparing' if waiting else 'stable' if actual_route() in INTERFACES else 'offline',
                                           error='切換尚未完成，保留可用路徑並持續重試')
                        self.publish()
                notify('WATCHDOG=1')
                self.stop.wait(max(.1, 1-(time.monotonic()-started)))
        thread.join(2)
        standby.join(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('check', 'monitor'))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = load_config()
    if args.action == 'check':
        print(json.dumps({path: inspect_path(path, config) for path in INTERFACES}))
        return
    os.umask(0o077)
    RUNTIME.mkdir(exist_ok=True, mode=0o755)
    STATE.mkdir(exist_ok=True, mode=0o700)
    manager = Manager(config)
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: manager.stop.set())
    manager.monitor()


if __name__ == '__main__':
    main()
