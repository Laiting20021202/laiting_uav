"""Unprivileged dashboard client for the fixed 4G path service.

The web process never changes NetworkManager, runs sudo, or accepts a profile,
SSID, command, or credential from the browser. Only the root-owned local
service can accept one of the three fixed path choices.
"""
import json
import math
import os
from pathlib import Path
import socket
import stat
import struct
import subprocess
import time
import uuid

SOCKET = Path('/run/laiting-uav-network/control.sock')
STATUS = SOCKET.parent / 'status.json'
USB_INTERFACE = 'enxac0033aa9633'
USB_MAC = 'ac:00:33:aa:96:33'
USB_PROFILE = '8391cb7e-8a3a-317c-bf18-612aa9c5300e'
WIFI_INTERFACE = 'wlP1p1s0'
WIFI_PROFILE = '7b8a7e5f-0e63-46ed-9c9c-9666959890b2'
TARGETS = frozenset(('auto', 'usb', 'wifi'))
MAX_RESPONSE = 32768
MAX_STATUS_AGE = 15


class NetworkUnavailable(RuntimeError):
    pass


class NetworkRejected(RuntimeError):
    pass


def _seconds(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def _boolean(value):
    return value if isinstance(value, bool) else None


def _request_id(value):
    """Only the broker's canonical UUID can identify a completed operation."""
    if not isinstance(value, str) or len(value) != 36:
        return None
    try:
        canonical = str(uuid.UUID(value))
    except ValueError:
        return None
    return canonical if value == canonical else None


def _target(value):
    return value if isinstance(value, str) and value in TARGETS else None


def sanitize_status(value):
    """Explicit fields only: a broker response must never expose credentials."""
    if not isinstance(value, dict):
        raise ValueError('Invalid network status')
    result = {
        'active_path': value.get('active_path') if value.get('active_path') in ('usb', 'wifi', 'none', 'unknown') else 'unknown',
        'preferred_path': _target(value.get('preferred_path')),
        'phase': value.get('phase') if value.get('phase') in ('stable', 'preparing', 'switching', 'recovering_tunnel', 'offline') else 'offline',
        'switching': value.get('switching') is True,
        'tunnel_ready': _boolean(value.get('tunnel_ready')),
        'updated_at': _seconds(value.get('updated_at')),
        'error': str(value.get('error') or '')[:240],
        'recent_requests': [],
    }
    records = value.get('recent_requests')
    if isinstance(records, list):
        for record in records[-16:]:
            if not isinstance(record, dict):
                continue
            identity = _request_id(record.get('request_id'))
            target, state = _target(record.get('target')), record.get('state')
            if identity is None or target is None or state not in ('queued', 'route_ready', 'failed'):
                continue
            result['recent_requests'] = [old for old in result['recent_requests'] if old['request_id'] != identity]
            result['recent_requests'].append({'request_id': identity, 'target': target, 'state': state})
    for name in ('usb', 'wifi'):
        source = value.get(name) if isinstance(value.get(name), dict) else {}
        result[name] = {key: _boolean(source.get(key)) for key in ('available', 'internet', 'connected')}
        result[name]['healthy_for'] = _seconds(source.get('healthy_for'))
    source = value.get('last_switch')
    result['last_switch'] = None
    if isinstance(source, dict):
        result['last_switch'] = {
            'target': _target(source.get('target')),
            'request_id': _request_id(source.get('request_id')),
            'reason': str(source.get('reason') or '')[:160],
            **{key: _seconds(source.get(key)) for key in
               ('started_at', 'finished_at', 'route_seconds', 'website_seconds', 'seconds')},
        }
    return result


class NetworkClient:
    def __init__(self, socket_path=SOCKET, status_path=STATUS, clock=time.time):
        self.socket_path = Path(socket_path)
        self.status_path = Path(status_path)
        self.clock = clock

    @staticmethod
    def _trusted_parent(path):
        info = path.parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise NetworkUnavailable('網路切換服務的權限不正確')

    def _request(self, value):
        try:
            self._trusted_parent(self.socket_path)
            info = self.socket_path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0:
                raise NetworkUnavailable('網路切換服務的權限不正確')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(1)
                client.connect(str(self.socket_path))
                credentials = client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
                if struct.unpack('3i', credentials)[1] != 0:
                    raise NetworkUnavailable('網路切換服務的身分不正確')
                client.sendall(json.dumps(value, separators=(',', ':')).encode('utf-8') + b'\n')
                chunks = bytearray()
                while b'\n' not in chunks:
                    chunk = client.recv(min(4096, MAX_RESPONSE + 1 - len(chunks)))
                    if not chunk:
                        raise NetworkUnavailable('網路切換服務未回覆完整狀態')
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE:
                        raise NetworkUnavailable('網路切換服務的回覆過大')
                result = json.loads(bytes(chunks).split(b'\n', 1)[0])
                if not isinstance(result, dict):
                    raise ValueError('Invalid broker response')
                return result
        except NetworkUnavailable:
            raise
        except (OSError, ValueError, UnicodeError) as error:
            raise NetworkUnavailable('網路切換服務暫時無法使用，正在重新連線') from error

    def _fresh(self, value):
        updated = _seconds(value.get('updated_at'))
        return updated is not None and -5 <= self.clock() - updated <= MAX_STATUS_AGE

    def _file_status(self):
        self._trusted_parent(self.status_path)
        descriptor = os.open(self.status_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise NetworkUnavailable('網路狀態檔的權限不正確')
            raw = stream.read(MAX_RESPONSE + 1)
        if len(raw) > MAX_RESPONSE:
            raise ValueError('Network status is too large')
        value = json.loads(raw)
        if not isinstance(value, dict) or not self._fresh(value):
            raise ValueError('Network status is stale')
        return value

    @staticmethod
    def _read_command(command):
        result = subprocess.run(command, capture_output=True, text=True, timeout=1)
        if result.returncode:
            raise OSError('Network observation failed')
        return result.stdout

    def _observed_path(self, value):
        """An unavailable broker cannot turn an old Wi-Fi report into fact."""
        routes = json.loads(self._read_command(['/usr/sbin/ip', '-j', '-4', 'route', 'get', '1.1.1.1']))
        if not routes:
            return 'none'
        route = routes[0]
        interface = route.get('dev')
        if interface == USB_INTERFACE and route.get('gateway') == '192.168.125.1':
            base = Path('/sys/class/net') / USB_INTERFACE
            device = (base / 'device').resolve(strict=True).parent
            if ((base / 'address').read_text().strip().lower() == USB_MAC and
                    all((device / key).read_text().strip() == expected for key, expected in
                        (('idVendor', '1286'), ('idProduct', '4e31'), ('serial', '200806006809080000')))):
                return 'usb'
        wifi = value.get('wifi') if isinstance(value.get('wifi'), dict) else {}
        approved_interface, approved_profile = wifi.get('interface'), wifi.get('profile_uuid')
        if (interface == approved_interface == WIFI_INTERFACE and
                approved_profile == WIFI_PROFILE and route.get('gateway') == '192.168.125.1'):
            actual = self._read_command(['/usr/bin/nmcli', '-t', '-g', 'GENERAL.CON-UUID', 'device', 'show', interface]).strip()
            if actual == approved_profile:
                return 'wifi'
        return 'unknown'

    def status(self):
        try:
            response = self._request({'action': 'status'})
            value = response.get('status', response)
            if response.get('ok') is False or not isinstance(value, dict) or not self._fresh(value):
                raise ValueError('Network status is unavailable')
            result = sanitize_status(value)
            result.update(controller_available=True, state_source='service')
            return result
        except (NetworkUnavailable, ValueError):
            pass
        try:
            value = self._file_status()
            result = sanitize_status(value)
            result['active_path'] = self._observed_path(value)
            for name in ('usb', 'wifi'):
                result[name].update(internet=None, healthy_for=None, connected=result['active_path'] == name)
        except (OSError, ValueError, NetworkUnavailable, subprocess.SubprocessError):
            result = sanitize_status({})
        result.update(controller_available=False, state_source='fallback', switching=False,
                      phase='offline', tunnel_ready=None,
                      recent_requests=[],
                      error='網路切換服務暫時無法使用，等待重新連線')
        return result

    def switch(self, target, request_id):
        if target not in TARGETS:
            raise ValueError('僅可選擇自動、USB 4G 或網卡 Wi-Fi 4G')
        canonical = str(uuid.UUID(str(request_id)))
        response = self._request({'action': 'switch', 'target': target, 'request_id': canonical})
        if response.get('ok') is not True or response.get('accepted') is not True:
            raise NetworkRejected('目前無法接受切換，請確認網卡連線後重試')
        if response.get('request_id') != canonical:
            raise NetworkUnavailable('切換回覆無法確認，正在查詢連線狀態')
        return {'accepted': True, 'request_id': canonical, 'target': target}
