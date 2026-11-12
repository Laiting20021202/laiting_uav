#!/usr/bin/env python3
"""Configure only the authenticated DWM-222W's own SIM-backed Wi-Fi.

Run the root-owned copy with sudo; PSK never enters a command line or report.
The installer deliberately disables all existing Wi-Fi autoconnect profiles.
"""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET

if __package__:
    from .modem_radio import RadioClient
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from modem_radio import RadioClient

WIFI_INTERFACE = 'wlP1p1s0'
WIFI_UUID = '7b8a7e5f-0e63-46ed-9c9c-9666959890b2'
USB_INTERFACE = 'enxac0033aa9633'
USB_UUID = '8391cb7e-8a3a-317c-bf18-612aa9c5300e'
AUTH = Path('/home/hrc/.config/laiting-uav/modem-radio-auth.json')
KEYFILE = Path('/etc/NetworkManager/system-connections/uav-dwm222w-onboard-wifi.nmconnection')
ALLOW = Path('/etc/NetworkManager/conf.d/95-uav-onboard-wifi-only.conf')
PATHS = Path('/etc/laiting-uav/modem-paths.json')
BACKUPS = Path('/var/backups/laiting-uav')
GATEWAY = '192.168.125.1'


class NMCommandError(RuntimeError):
    def __init__(self, returncode, stderr):
        super().__init__('NetworkManager operation failed (exit %d)' % returncode)
        self.returncode = returncode
        self.stderr = stderr  # Used only for exact retry classification; never reported.


class SetupStageError(RuntimeError):
    def __init__(self, stage, cause_type):
        super().__init__('Onboard Wi-Fi setup failed at '+stage)
        self.stage = stage
        self.cause_type = cause_type


def command(arguments, timeout=15):
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, 'LC_ALL': 'C'})
    if result.returncode:
        # Never include command output: NM may repeat configuration values.
        raise NMCommandError(result.returncode, result.stderr)
    return result.stdout


def wait_wifi_ready(timeout=20, *, clock=time.monotonic, sleep=time.sleep):
    deadline = clock()+timeout
    while clock() < deadline:
        try:
            output = command(['nmcli', '-t', '-g', 'GENERAL.STATE', 'device', 'show',
                              WIFI_INTERFACE], timeout=min(3, max(.1, deadline-clock())))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise SetupStageError('wifi-readiness', type(exc).__name__) from None
        match = re.match(r'^(\d+)(?:\s|$)', output.strip())
        if not match:
            raise SetupStageError('wifi-readiness', 'InvalidState')
        if int(match[1]) in (30, 100):
            return
        sleep(min(.5, max(0, deadline-clock())))
    raise SetupStageError('wifi-readiness', 'Timeout')


def last_scan(device_path, timeout=3):
    if not re.fullmatch(r'/org/freedesktop/NetworkManager/Devices/[0-9]+', device_path):
        raise ValueError('Invalid NetworkManager Wi-Fi device path')
    output = command(['gdbus', 'call', '--system', '--dest', 'org.freedesktop.NetworkManager',
                      '--object-path', device_path, '--method', 'org.freedesktop.DBus.Properties.Get',
                      'org.freedesktop.NetworkManager.Device.Wireless', 'LastScan'], timeout=timeout)
    match = re.fullmatch(r'\s*\(<int64 (-?[0-9]+)>,\)\s*', output)
    if not match:
        raise ValueError('Unexpected NetworkManager scan completion response')
    return int(match[1])


def request_wifi_scan(ssid, timeout=20, *, clock=time.monotonic, sleep=time.sleep):
    deadline = clock()+timeout
    try:
        path = command(['nmcli', '-t', '-g', 'GENERAL.DBUS-PATH', 'device', 'show',
                        WIFI_INTERFACE], timeout=3).strip()
        previous = last_scan(path)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        raise SetupStageError('wifi-scan-state', type(exc).__name__) from None
    while clock() < deadline:
        wait_wifi_ready(max(.1, deadline-clock()), clock=clock, sleep=sleep)
        try:
            command(['nmcli', 'device', 'wifi', 'rescan', 'ifname', WIFI_INTERFACE, 'ssid', ssid],
                    timeout=min(4, max(.1, deadline-clock())))
            break
        except NMCommandError as exc:
            # These two errors come from the installed NM 1.36.6 Wi-Fi plugin.
            if not any(reason in exc.stderr for reason in (
                    'Scanning not allowed while unavailable', 'Scanning not allowed at this time')):
                raise SetupStageError('wifi-scan-request', type(exc).__name__) from None
            sleep(min(1, max(0, deadline-clock())))
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            raise SetupStageError('wifi-scan-request', type(exc).__name__) from None
    else:
        raise SetupStageError('wifi-scan-request', 'Timeout')
    while clock() < deadline:
        try:
            current = last_scan(path, timeout=min(3, max(.1, deadline-clock())))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            raise SetupStageError('wifi-scan-completion', type(exc).__name__) from None
        if current > previous:
            return
        sleep(min(.5, max(0, deadline-clock())))
    raise SetupStageError('wifi-scan-completion', 'Timeout')


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError('Refusing a symlink configuration destination')
    descriptor, name = tempfile.mkstemp(prefix=path.name+'.', dir=path.parent)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as out:
            out.write(content)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def decode_ssid(value):
    if not value or len(value) % 4 or not re.fullmatch(r'[0-9a-fA-F]+', value):
        raise ValueError('Unsupported vendor SSID encoding')
    ssid = bytes.fromhex(value).decode('utf-16-be')
    if not 1 <= len(ssid.encode('utf-8')) <= 32 or '\0' in ssid:
        raise ValueError('Invalid Wi-Fi SSID')
    return ssid


def read_hotspot(client):
    client.login()  # One authenticated session; never guess or retry a rejection.
    _, body = client.request('/xml_action.cgi?method=get&module=duster&file=status1', client.header())
    root = ET.fromstring(body)
    if root.findtext('.//wlan_settings/wlan_enable') != '1':
        raise ValueError('The D-Link access point is disabled')
    security = root.find('.//wlan_security')
    if security is None or security.findtext('mode') != 'Mixed':
        raise ValueError('Expected authenticated WPA/WPA2 PSK access point')
    ssid = decode_ssid(security.findtext('ssid'))
    psk = security.findtext('pwd')
    if not psk:
        raise ValueError('Authenticated AP password is unavailable')
    if not ((8 <= len(psk) <= 63 and all(32 <= ord(c) <= 126 for c in psk))
            or re.fullmatch(r'[0-9a-fA-F]{64}', psk)):
        raise ValueError('Unsupported AP PSK format')
    fingerprint = client.auth.get('tls_sha256')
    if not fingerprint or not re.fullmatch(r'[0-9a-f]{64}', fingerprint):
        raise ValueError('Authenticated modem TLS identity is unavailable')
    return {'ssid': ssid, 'psk': psk, 'tls_sha256': fingerprint,
            'router_mac': root.findtext('.//sysinfo/mac') or ''}


def escape_keyfile(value):
    return (value.replace('\\', '\\\\').replace('\n', '\\n')
            .replace('\r', '\\r').replace('\t', '\\t').replace(' ', '\\s'))


def render_keyfile(hotspot, bssid=None, autoconnect=False):
    if bssid is not None and not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', bssid):
        raise ValueError('Invalid access point BSSID')
    return '\n'.join([
        '[connection]', 'id=DWM-222W onboard Wi-Fi', 'uuid='+WIFI_UUID,
        'type=wifi', 'interface-name='+WIFI_INTERFACE,
        'autoconnect='+('true' if autoconnect else 'false'),
        'autoconnect-priority=100', 'autoconnect-retries=0', '',
        '[wifi]', 'mode=infrastructure', 'ssid='+escape_keyfile(hotspot['ssid']),
        *(['bssid='+bssid] if bssid else []), 'powersave=2', '',
        '[wifi-security]', 'key-mgmt=wpa-psk', 'proto=rsn;',
        'pairwise=ccmp;', 'group=ccmp;', 'psk-flags=0',
        'psk='+escape_keyfile(hotspot['psk']), '',
        '[ipv4]', 'method=auto', 'route-metric=600', 'may-fail=false',
        'ignore-auto-dns=true', 'dns=1.1.1.1;8.8.8.8;', '',
        '[ipv6]', 'method=disabled', '',
    ])


def escaped_fields(line):
    fields, current, escaped = [], [], False
    for char in line:
        if escaped:
            current.append(char)
            escaped = False
        elif char == '\\':
            escaped = True
        elif char == ':':
            fields.append(''.join(current))
            current = []
        else:
            current.append(char)
    if escaped:
        raise ValueError('Invalid escaped NetworkManager output')
    fields.append(''.join(current))
    return fields


def select_bssid(output, expected_ssid):
    matches = set()
    for line in output.splitlines():
        fields = escaped_fields(line)
        if len(fields) != 3 or fields[1] != expected_ssid:
            continue
        bssid, _, security = fields
        bssid = bssid.upper()
        if not re.fullmatch(r'(?:[0-9A-F]{2}:){5}[0-9A-F]{2}', bssid):
            raise ValueError('Invalid advertised BSSID')
        if int(bssid[:2], 16) & 1 or bssid == '00:00:00:00:00:00':
            raise ValueError('Invalid advertised AP identity')
        if 'WPA2' not in security:
            raise ValueError('Expected WPA2 access point security')
        matches.add(bssid)
    if len(matches) != 1:
        raise ValueError('Expected exactly one matching D-Link access point; refusing to guess')
    return matches.pop()


def wifi_profiles(output):
    profiles = []
    for line in output.splitlines():
        fields = line.split(':')
        if len(fields) == 3 and fields[1] in ('wifi', '802-11-wireless'):
            if not re.fullmatch(r'[0-9a-f-]{36}', fields[0]):
                raise ValueError('Invalid saved Wi-Fi profile identity')
            profiles.append({'uuid': fields[0], 'autoconnect': fields[2]})
    return profiles


def backup_configuration(profiles):
    BACKUPS.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix='onboard-wifi-', dir=BACKUPS))
    backup.chmod(0o700)
    paths = [ALLOW, PATHS, *KEYFILE.parent.glob('*.nmconnection')]
    manifest = {'profiles': profiles, 'files': [], 'created_realtime': time.time()}
    for source in paths:
        if source.is_symlink():
            raise ValueError('Refusing a symlink in private network configuration')
        if source.is_file():
            target = backup/str(source).lstrip('/')
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(source, target)
            target.chmod(0o600)
            manifest['files'].append(str(source))
    atomic_write(backup/'manifest.json', json.dumps(manifest, indent=2)+'\n')
    return backup


def verify_wifi_peer(fingerprint):
    # Same subnet can otherwise cause an unbound management probe to use USB.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(4)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE,
                        WIFI_INTERFACE.encode('ascii')+b'\0')
        sock.connect((GATEWAY, 443))
        with ssl._create_unverified_context().wrap_socket(sock, server_hostname=GATEWAY) as tls:
            actual = hashlib.sha256(tls.getpeercert(binary_form=True)).hexdigest()
            if actual != fingerprint:
                raise ValueError('Wi-Fi peer does not match the authenticated modem')


def install():
    if os.geteuid() != 0:
        raise PermissionError('This installer requires local administrator authentication')
    auth_stat = AUTH.stat()
    if auth_stat.st_mode & 0o077 or auth_stat.st_uid != 1000:
        raise ValueError('Modem authentication file must be private and owned by the UAV user')
    hotspot = read_hotspot(RadioClient(json.loads(AUTH.read_text())))
    if command(['nmcli', '-g', 'connection.interface-name', 'connection', 'show',
                'uuid', USB_UUID]).strip() != USB_INTERFACE:
        raise ValueError('USB profile no longer matches the verified modem interface')
    profiles = wifi_profiles(command(['nmcli', '-t', '-f', 'UUID,TYPE,AUTOCONNECT', 'connection', 'show']))
    backup = backup_configuration(profiles)
    stage = 'wifi-disable-existing'
    try:
        command(['nmcli', 'radio', 'wifi', 'off'])
        for profile in profiles:
            command(['nmcli', 'connection', 'modify', 'uuid', profile['uuid'], 'connection.autoconnect', 'no'])
        atomic_write(ALLOW, '[device-uav-onboard-wifi]\nmatch-device=type:wifi\n'
                     'allowed-connections=uuid:'+WIFI_UUID+'\nkeep-configuration=no\n', mode=0o644)
        # Only persistent settings change; do not down/up the active USB link.
        command(['nmcli', 'connection', 'modify', 'uuid', USB_UUID, 'ipv6.method', 'disabled'])
        atomic_write(KEYFILE, render_keyfile(hotspot))
        stage = 'network-config-reload'
        command(['nmcli', 'general', 'reload', 'conf'])
        command(['nmcli', 'connection', 'load', str(KEYFILE)])
        stage = 'wifi-radio-enable'
        command(['nmcli', 'radio', 'wifi', 'on'])
        wait_wifi_ready()
        request_wifi_scan(hotspot['ssid'])
        stage = 'wifi-select-bssid'
        scan = command(['nmcli', '-t', '-f', 'BSSID,SSID,SECURITY', 'device', 'wifi', 'list',
                        'ifname', WIFI_INTERFACE, '--rescan', 'no'])
        bssid = select_bssid(scan, hotspot['ssid'])
        atomic_write(KEYFILE, render_keyfile(hotspot, bssid=bssid))
        command(['nmcli', 'connection', 'load', str(KEYFILE)])
        stage = 'wifi-associate'
        command(['nmcli', '--wait', '25', 'connection', 'up', 'uuid', WIFI_UUID,
                 'ifname', WIFI_INTERFACE], timeout=30)
        addresses = command(['nmcli', '-t', '-g', 'IP4.ADDRESS', 'device', 'show', WIFI_INTERFACE])
        gateway = command(['nmcli', '-t', '-g', 'IP4.GATEWAY', 'device', 'show', WIFI_INTERFACE]).strip()
        stage = 'wifi-ipv4-validation'
        if gateway != GATEWAY:
            raise ValueError('D-Link Wi-Fi gateway differs from the configured recovery gateway')
        valid = [ipaddress.ip_interface(line) for line in addresses.splitlines() if line]
        if not valid or any(ip.version != 4 or ip.ip.is_unspecified for ip in valid):
            raise ValueError('D-Link Wi-Fi did not obtain a valid IPv4 lease')
        usb_addresses = command(['nmcli', '-t', '-g', 'IP4.ADDRESS', 'device', 'show', USB_INTERFACE])
        usb_ips = {ipaddress.ip_interface(line).ip for line in usb_addresses.splitlines() if line}
        if any(ip.ip in usb_ips for ip in valid):
            raise ValueError('USB and Wi-Fi obtained the same IPv4 address; refusing ambiguous routing')
        stage = 'wifi-peer-validation'
        verify_wifi_peer(hotspot['tls_sha256'])
        stage = 'wifi-commit'
        command(['nmcli', 'connection', 'modify', 'uuid', WIFI_UUID, 'connection.autoconnect', 'yes'])
        config = {'wifi_interface': WIFI_INTERFACE, 'wifi_profile': WIFI_UUID, 'wifi_bssid': bssid,
                  'usb_interface': USB_INTERFACE, 'usb_profile': USB_UUID,
                  'user_uid': 1000, 'user_gid': 1000}
        atomic_write(PATHS, json.dumps(config, indent=2)+'\n')
    except Exception as exc:
        # Fail closed: preserve the external-network prohibition and USB route.
        # Do not restore laboratory Wi-Fi autoconnect as an automatic rollback.
        try:
            command(['nmcli', 'connection', 'modify', 'uuid', WIFI_UUID, 'connection.autoconnect', 'no'])
            command(['nmcli', 'radio', 'wifi', 'off'])
        except (OSError, RuntimeError, subprocess.SubprocessError):
            pass
        if isinstance(exc, SetupStageError):
            raise
        raise SetupStageError(stage, type(exc).__name__) from None
    return {'configured': True, 'wifi_profile': WIFI_UUID, 'wifi_bssid': bssid,
            'gateway': gateway, 'wifi_ipv4': [str(ip) for ip in valid],
            'backup': str(backup), 'psk_exposed': False}


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    try:
        print(json.dumps(install(), indent=2))
    except Exception as exc:
        print(json.dumps({'configured': False, 'error_type': type(exc).__name__,
                          'stage': getattr(exc, 'stage', 'setup-preparation'),
                          'cause_type': getattr(exc, 'cause_type', type(exc).__name__)}), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
