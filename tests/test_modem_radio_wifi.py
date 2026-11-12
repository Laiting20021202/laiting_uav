"""Offline, fixed-identity Wi-Fi modem access without requiring USB enumeration."""
import hashlib
import json
import os
import socket
import stat
from types import SimpleNamespace

import pytest

from scripts import modem_radio

CERTIFICATE = b'previously-verified-modem-certificate'
PIN = hashlib.sha256(CERTIFICATE).hexdigest()
BSSID = '48:ee:0c:b3:9e:9d'


@pytest.fixture
def wifi_config(tmp_path, monkeypatch):
    path = tmp_path / 'modem-paths.json'
    config = dict(wifi_interface=modem_radio.WIFI_INTERFACE, wifi_profile=modem_radio.WIFI_PROFILE,
                  wifi_bssid=BSSID)
    path.write_text(json.dumps(config))
    path.chmod(0o600)
    monkeypatch.setattr(modem_radio, 'WIFI_CONFIG', path)
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 0)
    original = os.fstat
    def root_stat(descriptor):
        info = original(descriptor)
        return SimpleNamespace(st_mode=info.st_mode, st_uid=0)
    monkeypatch.setattr(modem_radio.os, 'fstat', root_stat)
    calls = []
    def run(command, **options):
        assert options['timeout'] == 3
        assert options['env']['LC_ALL'] == 'C'
        calls.append(command)
        if command[0] == '/usr/bin/nmcli':
            assert command == ['/usr/bin/nmcli', '-g', 'GENERAL.STATE,GENERAL.CON-UUID,IP4.GATEWAY',
                               'device', 'show', modem_radio.WIFI_INTERFACE]
            output = f'100 (connected)\n{modem_radio.WIFI_PROFILE}\n{modem_radio.HOST}\n'
        else:
            assert command == ['/usr/sbin/iw', 'dev', modem_radio.WIFI_INTERFACE, 'link']
            output = f'Connected to {BSSID.upper()} (on {modem_radio.WIFI_INTERFACE})\n\tSSID: private\n'
        return SimpleNamespace(returncode=0, stdout=output)
    monkeypatch.setattr(modem_radio.subprocess, 'run', run)
    return path, config, calls


def test_approved_onboard_wifi_identity_does_not_require_usb(wifi_config, monkeypatch):
    monkeypatch.setattr(modem_radio, 'modem_device', lambda: pytest.fail('Wi-Fi must not require USB'))
    modem_radio.onboard_wifi_identity()
    assert len(wifi_config[2]) == 2


@pytest.mark.parametrize('field, value', [
    ('wifi_interface', 'laboratory0'), ('wifi_profile', 'laboratory-profile'),
    ('wifi_bssid', None), ('wifi_bssid', []), ('wifi_bssid', 'invalid'),
])
def test_unexpected_root_wifi_configuration_is_rejected_before_network_read(wifi_config, field, value):
    path, config, calls = wifi_config
    path.write_text(json.dumps({**config, field: value}))
    with pytest.raises(ValueError):
        modem_radio.onboard_wifi_identity()
    assert calls == []


@pytest.mark.parametrize('mode, owner', [(stat.S_IFREG | 0o644, 0), (stat.S_IFREG | 0o622, 0),
                                       (stat.S_IFREG | 0o600, 1000), (stat.S_IFIFO | 0o600, 0)])
def test_untrusted_configuration_mode_owner_or_type_is_rejected(wifi_config, monkeypatch, mode, owner):
    monkeypatch.setattr(modem_radio.os, 'fstat', lambda _: SimpleNamespace(st_mode=mode, st_uid=owner))
    with pytest.raises(ValueError):
        modem_radio.onboard_wifi_identity()
    assert wifi_config[2] == []


def test_symlink_configuration_is_rejected(wifi_config):
    path, _, calls = wifi_config
    target = path.with_name('replacement.json')
    path.rename(target)
    path.symlink_to(target)
    with pytest.raises(OSError):
        modem_radio.onboard_wifi_identity()
    assert calls == []


@pytest.mark.parametrize('body', ['[]', '{invalid', 'x' * 32769])
def test_invalid_or_oversized_config_is_rejected(wifi_config, body):
    path, _, calls = wifi_config
    path.write_text(body)
    with pytest.raises(ValueError):
        modem_radio.onboard_wifi_identity()
    assert calls == []


@pytest.mark.parametrize('observation', [
    'disconnected', 'wrong_profile', 'wrong_gateway', 'wrong_bssid', 'iw_disconnected', 'command_failed',
])
def test_changed_live_identity_is_rejected_without_exposing_observation(wifi_config, monkeypatch, observation):
    def run(command, **options):
        if command[0] == '/usr/bin/nmcli':
            rows = ['100 (connected)', modem_radio.WIFI_PROFILE, modem_radio.HOST]
            if observation == 'disconnected': rows[0] = '30 (disconnected)'
            if observation == 'wrong_profile': rows[1] = 'laboratory-profile'
            if observation == 'wrong_gateway': rows[2] = '192.168.0.1'
            output = '\n'.join(rows)
        else:
            actual = '00:11:22:33:44:55' if observation == 'wrong_bssid' else BSSID
            output = 'Not connected. private-ssid' if observation == 'iw_disconnected' else f'Connected to {actual}\nSSID: private-ssid'
        if observation == 'command_failed': output = 'private-secret-output'
        return SimpleNamespace(returncode=1 if observation == 'command_failed' else 0, stdout=output)
    monkeypatch.setattr(modem_radio.subprocess, 'run', run)
    with pytest.raises(ValueError) as error:
        modem_radio.onboard_wifi_identity()
    assert 'private' not in str(error.value) and 'laboratory-profile' not in str(error.value)


def fake_connection(monkeypatch, events, certificate=CERTIFICATE):
    class Reply:
        def getheaders(self): return []
        def read(self, *_): return b'<RGW><return_code>0</return_code></RGW>'
    class Connection:
        def __init__(self, **kwargs):
            assert kwargs['timeout'] == 3
            self.sock = self
            events.append('created')
        def connect(self): events.append('connected')
        def getpeercert(self, binary_form=False):
            assert binary_form
            events.append('certificate')
            return certificate
        def request(self, method, path, **kwargs):
            events.append(('sent', method, path, kwargs))
        def getresponse(self): return Reply()
        def close(self): events.append('closed')
    monkeypatch.setattr(modem_radio, 'OnboardHTTPSConnection', Connection)


def test_explicit_wifi_request_checks_ap_and_pin_before_sending_digest_and_body(monkeypatch):
    events = []
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(modem_radio, 'modem_device', lambda: pytest.fail('Must not require USB'))
    monkeypatch.setattr(modem_radio, 'onboard_wifi_identity', lambda: events.append('wifi-identity'))
    fake_connection(monkeypatch, events)
    auth = dict(tls_sha256=PIN)
    client = modem_radio.RadioClient(auth, transport='onboard-wifi')
    reply = client.request('/xml_action.cgi', 'private-digest', method='POST', body=b'private-maintenance')
    assert reply[1] == b'<RGW><return_code>0</return_code></RGW>'
    assert events[:5] == ['wifi-identity', 'created', 'connected', 'certificate', 'wifi-identity']
    assert events[5][0:3] == ('sent', 'POST', '/xml_action.cgi')
    assert events[5][3]['headers']['Authorization'] == 'private-digest'
    assert events[5][3]['body'] == b'private-maintenance'
    assert events[-1] == 'closed'
    assert auth == {'tls_sha256': PIN}


@pytest.mark.parametrize('pin', [None, '', 'x' * 64, PIN.upper(), PIN[:-1], [], True])
def test_wifi_never_creates_a_new_certificate_pin(monkeypatch, pin):
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(modem_radio, 'onboard_wifi_identity', lambda: pytest.fail('Invalid pin must stop before connection'))
    auth = {'tls_sha256': pin}
    with pytest.raises(ValueError, match='previously verified'):
        modem_radio.RadioClient(auth, transport='onboard-wifi').request('/login.cgi')
    assert auth['tls_sha256'] == pin


def test_wifi_certificate_change_blocks_digest_body_and_never_replaces_pin(monkeypatch):
    events = []
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(modem_radio, 'onboard_wifi_identity', lambda: events.append('wifi-identity'))
    fake_connection(monkeypatch, events, certificate=b'replacement-modem-certificate')
    auth = dict(tls_sha256=PIN)
    with pytest.raises(ValueError, match='certificate changed'):
        modem_radio.RadioClient(auth, transport='onboard-wifi').request('/xml_action.cgi', 'private-digest',
                                                                     method='POST', body=b'private-maintenance')
    assert not any(isinstance(event, tuple) for event in events)
    assert auth['tls_sha256'] == PIN and events[-1] == 'closed'


def test_wifi_identity_change_during_tls_blocks_digest_body(monkeypatch):
    events = []
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 0)
    def identity():
        events.append('wifi-identity')
        if events.count('wifi-identity') == 2:
            raise ValueError('Onboard Wi-Fi changed')
    monkeypatch.setattr(modem_radio, 'onboard_wifi_identity', identity)
    fake_connection(monkeypatch, events)
    with pytest.raises(ValueError, match='Wi-Fi changed'):
        modem_radio.RadioClient({'tls_sha256': PIN}, transport='onboard-wifi').request('/xml_action.cgi', 'private-digest')
    assert not any(isinstance(event, tuple) for event in events)
    assert events[-1] == 'closed'


def test_each_request_revalidates_onboard_identity(monkeypatch):
    events = []
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 0)
    checks = []
    monkeypatch.setattr(modem_radio, 'onboard_wifi_identity', lambda: checks.append(True))
    fake_connection(monkeypatch, events)
    client = modem_radio.RadioClient({'tls_sha256': PIN}, transport='onboard-wifi')
    client.request('/xml_action.cgi')
    client.request('/xml_action.cgi')
    assert len(checks) == 4


def test_login_digest_query_is_preserved_without_extra_cachebuster(monkeypatch):
    events = []
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(modem_radio, 'onboard_wifi_identity', lambda: None)
    fake_connection(monkeypatch, events)
    path = '/login.cgi?Action=Digest&username=offline-test'
    modem_radio.RadioClient({'tls_sha256': PIN}, transport='onboard-wifi').request(path, 'offline-digest')
    sent = next(event for event in events if isinstance(event, tuple))
    assert sent[2] == path and '_=' not in sent[2]
    assert sent[3]['headers']['Cache-Control'] == 'no-store, no-cache, must-revalidate'
    assert sent[3]['headers']['Pragma'] == 'no-cache'


def test_nonroot_wifi_transport_is_rejected_before_identity_or_socket(monkeypatch):
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(modem_radio, 'onboard_wifi_identity', lambda: pytest.fail('Root check must come first'))
    with pytest.raises(PermissionError, match='requires root'):
        modem_radio.RadioClient({'tls_sha256': PIN}, transport='onboard-wifi').request('/login.cgi')


@pytest.mark.parametrize('transport', ['wifi', 'laboratory', None, []])
def test_transport_must_be_explicit_fixed_choice(transport):
    with pytest.raises(ValueError):
        modem_radio.RadioClient({}, transport=transport)


def test_bound_tls_socket_connects_only_to_fixed_wifi_peer(monkeypatch):
    events = []
    class RawSocket:
        def settimeout(self, timeout): events.append(('timeout', timeout))
        def setsockopt(self, *args): events.append(('bind', args))
        def connect(self, address): events.append(('connect', address))
        def close(self): events.append('closed')
    raw = RawSocket()
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 0)
    def create(family, kind):
        assert (family, kind) == (socket.AF_INET, socket.SOCK_STREAM)
        return raw
    monkeypatch.setattr(modem_radio.socket, 'socket', create)
    class Context:
        check_hostname = False
        verify_mode = modem_radio.ssl.CERT_NONE
        def wrap_socket(self, actual, *, server_hostname):
            assert actual is raw and server_hostname == modem_radio.HOST
            events.append('tls')
            return object()
    connection = modem_radio.OnboardHTTPSConnection(timeout=3, context=Context())
    connection.connect()
    assert events == [('timeout', 3), ('bind', (socket.SOL_SOCKET, socket.SO_BINDTODEVICE, b'wlP1p1s0\0')),
                      ('connect', ('192.168.125.1', 443)), 'tls']


def test_bound_socket_failure_closes_socket_without_falling_back(monkeypatch):
    events = []
    class RawSocket:
        def settimeout(self, _): pass
        def setsockopt(self, *_):
            events.append('bind')
            raise OSError('Interface unavailable')
        def connect(self, *_): pytest.fail('Bind failure must never fall back')
        def close(self): events.append('closed')
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(modem_radio.socket, 'socket', lambda *_: RawSocket())
    context = modem_radio.ssl._create_unverified_context()
    with pytest.raises(OSError):
        modem_radio.OnboardHTTPSConnection(timeout=3, context=context).connect()
    assert events == ['bind', 'closed']


@pytest.mark.parametrize('change', ['user', 'host', 'port', 'proxy'])
def test_bound_connection_rejects_other_user_destination_or_tunnel_before_socket(monkeypatch, change):
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 1000 if change == 'user' else 0)
    monkeypatch.setattr(modem_radio.socket, 'socket', lambda *_: pytest.fail('Refusal must precede socket creation'))
    connection = modem_radio.OnboardHTTPSConnection(timeout=3, context=modem_radio.ssl._create_unverified_context())
    if change == 'host': connection.host = '192.168.0.1'
    if change == 'port': connection.port = 444
    if change == 'proxy': connection._tunnel_host = 'external.example'
    with pytest.raises(PermissionError):
        connection.connect()


def test_wifi_request_retains_original_required_pin_if_auth_changes_during_connect(monkeypatch):
    events = []
    auth = {'tls_sha256': PIN}
    monkeypatch.setattr(modem_radio.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(modem_radio, 'onboard_wifi_identity', lambda: None)
    class Connection:
        def __init__(self, **kwargs): self.sock = self
        def connect(self): auth.clear()
        def getpeercert(self, **kwargs): return b'replacement-modem-certificate'
        def request(self, *args, **kwargs): pytest.fail('Clearing auth must not permit unpinned credentials')
        def close(self): events.append('closed')
    monkeypatch.setattr(modem_radio, 'OnboardHTTPSConnection', Connection)
    with pytest.raises(ValueError, match='certificate changed'):
        modem_radio.RadioClient(auth, transport='onboard-wifi').request('/xml_action.cgi', 'private-digest')
    assert events == ['closed']
