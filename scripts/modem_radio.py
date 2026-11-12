#!/usr/bin/env python3
"""Read DWM-222W WAN signal only; digest auth follows its shipped dashboard JS."""
import argparse
import getpass
import hashlib
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import stat
import subprocess
import time
import urllib.parse
import xml.etree.ElementTree as ET

try:
    from .modem_recovery import modem_device
except ImportError:
    from modem_recovery import modem_device

AUTH_FILE = Path.home()/'.config/laiting-uav/modem-radio-auth.json'
HOST = '192.168.125.1'
WIFI_INTERFACE = 'wlP1p1s0'
WIFI_PROFILE = '7b8a7e5f-0e63-46ed-9c9c-9666959890b2'
WIFI_CONFIG = Path('/etc/laiting-uav/modem-paths.json')


def onboard_wifi_identity():
    """Verify only the root-configured onboard AP, independently of USB."""
    if os.geteuid() != 0:
        raise PermissionError('Onboard Wi-Fi modem management requires root')
    descriptor = os.open(WIFI_CONFIG, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            raise ValueError('Onboard Wi-Fi configuration must be root owned and private')
        raw = stream.read(32769)
    if len(raw) > 32768:
        raise ValueError('Onboard Wi-Fi configuration exceeds limit')
    config = json.loads(raw)
    if not isinstance(config, dict):
        raise ValueError('Invalid onboard Wi-Fi configuration')
    bssid = config.get('wifi_bssid')
    if (config.get('wifi_interface') != WIFI_INTERFACE or config.get('wifi_profile') != WIFI_PROFILE or
            not isinstance(bssid, str) or not re.fullmatch(r'(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}', bssid)):
        raise ValueError('Unexpected onboard Wi-Fi identity')

    def read(command):
        result = subprocess.run(command, capture_output=True, text=True, timeout=3,
                                env={**os.environ, 'LC_ALL': 'C'})
        if result.returncode:
            # Never include NetworkManager output, SSIDs, or credentials.
            raise ValueError('Onboard Wi-Fi identity observation failed')
        return result.stdout

    rows = read(['/usr/bin/nmcli', '-g', 'GENERAL.STATE,GENERAL.CON-UUID,IP4.GATEWAY',
                 'device', 'show', WIFI_INTERFACE]).splitlines()
    if len(rows) != 3 or not rows[0].startswith('100 ') or rows[1] != WIFI_PROFILE or rows[2] != HOST:
        raise ValueError('Onboard Wi-Fi profile or gateway is not connected')
    link = read(['/usr/sbin/iw', 'dev', WIFI_INTERFACE, 'link'])
    found = re.search(r'^Connected to ([0-9a-fA-F:]{17})(?:\s|$)', link, re.MULTILINE)
    if not found or found[1].lower() != bssid.lower():
        raise ValueError('Onboard Wi-Fi access point does not match')


class OnboardHTTPSConnection(http.client.HTTPSConnection):
    """A fixed TLS peer reached solely through the approved onboard interface."""
    def __init__(self, *, timeout, context):
        super().__init__(HOST, port=443, timeout=timeout, context=context)

    def connect(self):
        if os.geteuid() != 0 or self.host != HOST or self.port != 443 or self._tunnel_host:
            raise PermissionError('Only root may connect to the fixed onboard modem')
        raw = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            raw.settimeout(self.timeout)
            raw.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, WIFI_INTERFACE.encode()+b'\0')
            raw.connect((HOST, 443))
            self.sock = self._context.wrap_socket(raw, server_hostname=HOST)
        except BaseException:
            raw.close()
            raise


def md5(value):
    return hashlib.md5(value.encode()).hexdigest()


def parse_radio(body):
    root = ET.fromstring(body)
    if root.findtext('.//login_status') in ('UNAUTHORIZED', 'KICKOFF'):
        raise PermissionError('Modem management login required')
    wan = root if root.tag == 'wan' else root.find('.//wan')
    if wan is None:
        raise ValueError('Modem did not return WAN status')
    raw = {key: wan.findtext('.//'+key) for key in (
        'rssi', 'rsrp', 'rsrq', 'sinr', 'sys_mode', 'wan_link_status', 'wan_conn_status',
        'band', 'cellid', 'pci', 'sim_status', 'network_name')}

    def address(value, version):
        try:
            ip = ipaddress.ip_address(value)
            if (ip.version == version and not ip.is_unspecified and not ip.is_multicast
                    and not ip.is_loopback and not ip.is_link_local):
                return str(ip)
        except (ValueError, TypeError):
            pass
        return None

    # The shipped dashboard's setDetailPDPInfo reads these live addresses.
    # Top-level wan/ip may remain 0.0.0.0 even with an active LTE bearer.
    pdp = [{key: item.findtext(key) for key in ('rulename', 'connnum', 'success', 'default')}
           | {'ipv4': address(item.findtext('ipv4'), 4),
              'ipv6': address(item.findtext('ipv6'), 6)}
           for item in wan.findall('.//pdp_context_list/Item')]
    active = [item for item in pdp if item['success'] == '1' and (item['ipv4'] or item['ipv6'])]
    default = next((item for item in active if item['default'] == '1'), None)

    def number(key, low, high):
        try:
            value = float(raw[key])
            return value if math.isfinite(value) and low <= value <= high else None
        except (ValueError, TypeError):
            return None
    mode = number('sys_mode', 0, 100)
    offset = {3: 111, 5: 121, 15: 121, 17: 141}.get(mode)
    rssi = number('rssi', 1, 97 if mode == 17 else 96 if mode in (5, 15) else 63)
    # These transformations match this firmware's dashboard.js. Preserve raw
    # values and exclude unavailable/out-of-range encodings from the estimates.
    rsrp = number('rsrp', 0, 97) if mode == 17 else None
    rsrq = number('rsrq', 0, 34) if mode == 17 else None
    sinr = number('sinr', -100, 100) if mode == 17 else None
    return {'ok': True, 'raw': raw, 'pdp': pdp,
            'wan_ip': default['ipv4'] or default['ipv6'] if default else None,
            'cellular_connected': mode in (3, 5, 15, 17) and bool(active),
            'rssi_dbm': rssi-offset if rssi is not None and offset else None,
            'rsrp_dbm': rsrp-141 if rsrp is not None else None,
            'rsrq_db': rsrq*.5-20 if rsrq is not None else None, 'sinr_db': sinr}


class RadioClient:
    def __init__(self, auth, *, transport='usb'):
        if transport not in ('usb', 'onboard-wifi'):
            raise ValueError('Unsupported modem management transport')
        self.auth = auth
        self.transport = transport
        self.nonce = None
        self.count = 1
        self.logged_in_at = 0
        self.rejected = False

    def request(self, path, header=None, *, method='GET', body=None):
        # Fixed LAN peer, no redirects or proxy, pinned self-signed certificate.
        transport = self.transport
        if transport == 'onboard-wifi':
            if os.geteuid() != 0:
                raise PermissionError('Onboard Wi-Fi modem management requires root')
            pin = self.auth.get('tls_sha256')
            if not isinstance(pin, str) or not re.fullmatch(r'[0-9a-f]{64}', pin):
                raise ValueError('Onboard Wi-Fi requires a previously verified certificate pin')
            onboard_wifi_identity()
        elif transport == 'usb':
            modem_device()
        else:
            raise ValueError('Unsupported modem management transport')
        context = ssl._create_unverified_context()
        # This modem CGI sometimes ends a response with bare TCP EOF. The
        # fixed LAN peer is verified by USB identity and certificate pinning.
        context.options |= getattr(ssl, 'OP_IGNORE_UNEXPECTED_EOF', 0)
        conn = (OnboardHTTPSConnection(timeout=3, context=context) if transport == 'onboard-wifi' else
                http.client.HTTPSConnection(HOST, timeout=3, context=context))
        try:
            conn.connect()
            fingerprint = hashlib.sha256(conn.sock.getpeercert(binary_form=True)).hexdigest()
            expected_pin = pin if transport == 'onboard-wifi' else self.auth.get('tls_sha256')
            if expected_pin and fingerprint != expected_pin:
                raise ValueError('Modem TLS certificate changed; reconfigure locally')
            if transport == 'onboard-wifi':
                # Recheck association before sending a digest header or body.
                onboard_wifi_identity()
            else:
                self.auth['tls_sha256'] = fingerprint
            headers = {'User-Agent': 'Mozilla/5.0', 'Connection': 'close',
                       'X-Requested-With': 'XMLHttpRequest',
                       'Cache-Control': 'no-store, no-cache, must-revalidate', 'Pragma': 'no-cache'}
            if header:
                headers['Authorization'] = header
            # This direct client has no response cache. Some modem CGI builds
            # close before sending headers when login.cgi has an extra _ query;
            # preserve the exact endpoint/query and use the no-cache headers.
            request_path = path
            if body is None:
                conn.request(method, request_path, headers=headers)
            else:
                headers['Content-Type'] = 'application/x-www-form-urlencoded; charset=UTF-8'
                conn.request(method, request_path, body=body, headers=headers)
            reply = conn.getresponse()
            response_headers = dict(reply.getheaders())
            if (header is None and path == '/login.cgi' and
                    any(k.lower() == 'www-authenticate' for k in response_headers)):
                # getAuthType uses only the header. This CGI's challenge has
                # no framed body, so waiting for it can fail on TLS shutdown.
                return response_headers, b''
            body = reply.read(1024*1024+1)
            if len(body) > 1024*1024:
                raise ValueError('Modem response exceeds limit')
            return response_headers, body
        finally:
            conn.close()

    def digest(self, uri, count=1, method='GET'):
        cnonce = secrets.token_hex(8)
        nc = f'{count:08X}'
        response = md5(f"{self.auth['ha1']}:{self.nonce}:{nc}:{cnonce}:auth:{md5(method+':'+uri)}")
        return cnonce, nc, response

    def header(self, method='GET'):
        cnonce, nc, response = self.digest('/cgi/xml_action.cgi', self.count, method)
        self.count += 1
        return (f'Digest username="{self.auth["username"]}", realm="{self.auth["realm"]}", '
                f'nonce="{self.nonce}", uri="/cgi/xml_action.cgi", response="{response}", '
                f'qop=auth, nc={nc}, cnonce="{cnonce}"')

    def challenge(self):
        headers, _ = self.request('/login.cgi')
        challenge = next((v for k, v in headers.items() if k.lower() == 'www-authenticate'), '')
        values = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if values.get('realm') != 'Highwmg' or values.get('qop') != 'auth' or not values.get('nonce'):
            raise ValueError('Unsupported modem authentication challenge')
        self.nonce = values['nonce']
        self.auth['realm'] = values['realm']
        self.count = 1

    def login(self):
        if self.rejected:
            raise PermissionError('Saved modem credential was rejected; reconfigure locally')
        self.challenge()
        cnonce, _, response = self.digest('/cgi/protected.cgi')
        query = urllib.parse.urlencode({
            'Action': 'Digest', 'username': self.auth['username'], 'realm': self.auth['realm'],
            'nonce': self.nonce, 'response': response, 'qop': 'auth', 'cnonce': cnonce, 'temp': 'MIFI'})
        _, body = self.request('/login.cgi?'+query, self.header())
        if b'200 OK' not in body:
            self.rejected = True
            if b'500 OK' not in body:
                raise ValueError('Modem returned an unrecognized login response')
            raise PermissionError('Modem credential rejected; automatic login retries disabled')
        self.logged_in_at = time.monotonic()

    def sample(self):
        if not self.nonce or time.monotonic()-self.logged_in_at > 240:
            self.login()
        # wan is editable configuration, including placeholder signal values.
        # The shipped dashboard reads live radio/connection data from status1.
        _, body = self.request('/xml_action.cgi?method=get&module=duster&file=status1', self.header())
        try:
            return parse_radio(body)
        except PermissionError:
            # A modem reset invalidates the old session. One authenticated retry
            # recovers that session; a rejected credential disables further attempts.
            self.login()
            _, body = self.request('/xml_action.cgi?method=get&module=duster&file=status1', self.header())
            return parse_radio(body)


def save_auth(auth):
    os.umask(0o077)
    AUTH_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = AUTH_FILE.with_suffix('.tmp')
    temporary.write_text(json.dumps(auth))
    temporary.chmod(0o600)
    temporary.replace(AUTH_FILE)


class RadioProbe:
    """Reload locally configured credentials; never guess or repeatedly try rejects."""
    def __init__(self):
        self.client = None
        self.modified = None

    def __call__(self):
        try:
            stat = AUTH_FILE.stat()
        except FileNotFoundError:
            return {'ok': False, 'configured': False, 'error': 'Modem management login not configured'}
        if stat.st_mode & 0o077:
            return {'ok': False, 'error': 'Modem credential file must be private (0600)'}
        if self.modified != stat.st_mtime_ns:
            self.client = RadioClient(json.loads(AUTH_FILE.read_text()))
            self.modified = stat.st_mtime_ns
        try:
            return self.client.sample()
        except (OSError, ValueError, PermissionError, http.client.HTTPException, ET.ParseError) as exc:
            # Do not log a request URL, digest header, or the raw XML response.
            return {'ok': False, 'configured': True, 'error': type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--configure', action='store_true', help='Prompt locally; save digest credential, never plaintext password')
    parser.add_argument('--transport', choices=('usb', 'onboard-wifi'), default='usb',
                        help='Onboard Wi-Fi is an explicit root-only maintenance path, with an existing TLS pin')
    parser.add_argument('--auth-file', type=Path, default=AUTH_FILE,
                        help='Existing private digest credential file; no credentials are printed')
    args = parser.parse_args()
    if args.transport == 'onboard-wifi' and (args.configure or os.geteuid() != 0):
        parser.error('onboard-wifi requires root and an existing verified credential; configure over USB first')
    if args.configure and args.auth_file != AUTH_FILE:
        parser.error('--auth-file selects an existing credential; --configure writes only the default credential')
    if args.configure:
        username = input('Modem username [admin]: ').strip() or 'admin'
        if not re.fullmatch(r'[\w.-]{1,64}', username):
            raise ValueError('Unsupported username')
        client = RadioClient({'username': username})
        client.challenge()
        client.auth['ha1'] = md5(username+':'+client.auth['realm']+':'+getpass.getpass('Modem password: '))
        radio = client.sample()
        save_auth(client.auth)
        print(json.dumps(radio, indent=2))
    else:
        descriptor = os.open(args.auth_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
                raise ValueError('Modem credential file must be private (0600)')
            raw = stream.read(32769)
        if len(raw) > 32768:
            raise ValueError('Modem credential file exceeds limit')
        print(json.dumps(RadioClient(json.loads(raw), transport=args.transport).sample(), indent=2))


if __name__ == '__main__':
    main()
