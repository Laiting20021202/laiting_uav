"""Only trusted local status and fixed, authenticated 4G switch requests."""
import json
from pathlib import Path
import socket
import stat
import struct
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from uav.auth import password_hash
from uav.config import load_settings
from uav.network import NetworkClient, NetworkRejected, NetworkUnavailable, WIFI_PROFILE, sanitize_status
from uav.web import create_app


def status(**updates):
    value = dict(updated_at=100, active_path='usb', preferred_path='auto', phase='stable',
                 switching=False, tunnel_ready=True, error='',
                 usb=dict(available=True, internet=True, connected=True, healthy_for=30),
                 wifi=dict(available=True, internet=True, connected=True, healthy_for=30), last_switch=None)
    return {**value, **updates}


@pytest.fixture
def client(tmp_path):
    users = tmp_path / 'users.json'
    users.write_text(json.dumps({'test': password_hash('test-password')}))
    config = load_settings()
    app = create_app(config, data_dir=tmp_path / 'data', users_path=users, start_workers=False)
    app.state.network = SimpleNamespace(status=lambda: status(), switch=lambda target, request_id:
                                       {'accepted': True, 'target': target, 'request_id': str(request_id)})
    with TestClient(app, base_url=config['web']['origin']) as value:
        yield value


def login(client):
    origin = str(client.base_url).rstrip('/')
    assert client.post('/api/login', json={'username': 'test', 'password': 'test-password'},
                       headers={'Origin': origin}).status_code == 200
    return {'Origin': origin, 'X-CSRF-Token': client.get('/api/me').json()['csrf']}


def test_network_reads_and_actions_require_session_and_csrf(client):
    body = {'target': 'wifi', 'request_id': str(uuid4())}
    origin = str(client.base_url).rstrip('/')
    assert client.get('/api/network').status_code == 401
    assert client.post('/api/network/switch', json=body, headers={'Origin': origin}).status_code == 401
    headers = login(client)
    assert client.get('/api/network').status_code == 200
    assert client.post('/api/network/switch', json=body, headers={'Origin': origin}).status_code == 403
    assert client.post('/api/network/switch', json=body, headers={**headers, 'Origin': 'https://foreign.invalid'}).status_code == 403


@pytest.mark.parametrize('target', ['auto', 'usb', 'wifi'])
def test_fixed_switch_request_is_accepted_without_running_network_commands(client, target):
    headers = login(client)
    identity = str(uuid4())
    calls = []
    def switch(mode, request):
        calls.append((mode, str(request)))
        return {'accepted': True, 'target': mode, 'request_id': str(request)}
    client.app.state.network.switch = switch
    result = client.post('/api/network/switch', json={'target': target, 'request_id': identity}, headers=headers)
    assert result.status_code == 202 and result.json()['request_id'] == identity
    assert calls == [(target, identity)]
    assert 'no-store' in result.headers['Cache-Control']


@pytest.mark.parametrize('body', [
    {'target': 'lab_wifi', 'request_id': str(uuid4())},
    {'target': 'wifi', 'request_id': 'not-an-id'},
    {'target': 'wifi', 'request_id': str(uuid4()), 'ssid': 'lab'},
    {'target': 'usb', 'request_id': str(uuid4()), 'command': 'reboot'},
    {'target': 'wifi', 'request_id': str(uuid4()), 'password': 'unwanted'},
])
def test_browser_cannot_supply_network_identity_credentials_or_commands(client, body):
    client.app.state.network.switch = lambda *_: pytest.fail('Invalid input reached broker')
    assert client.post('/api/network/switch', json=body, headers=login(client)).status_code == 422


@pytest.mark.parametrize('error, expected', [(NetworkUnavailable('暫時不可用'), 503), (NetworkRejected('請稍後再試'), 409)])
def test_broker_failures_do_not_end_login_or_restart_application(client, error, expected):
    headers = login(client)
    def fail(*_):
        raise error
    client.app.state.network.switch = fail
    result = client.post('/api/network/switch', json={'target': 'wifi', 'request_id': str(uuid4())}, headers=headers)
    assert result.status_code == expected
    assert client.get('/api/me').status_code == 200
    assert client.get('/api/status').status_code == 200


def test_status_drops_credentials_and_invalid_measurements():
    value = status(password='secret', ssid='private', wifi={'available': True, 'password': 'secret'},
                   last_switch={'target': 'wifi', 'request_id': str(uuid4()), 'website_seconds': float('nan'), 'route_seconds': -1})
    result = sanitize_status(value)
    assert 'secret' not in json.dumps(result) and 'private' not in json.dumps(result)
    assert result['last_switch']['website_seconds'] is None and result['last_switch']['route_seconds'] is None


def test_recent_request_results_are_bounded_fixed_and_secret_free():
    identities = [str(uuid4()) for _ in range(20)]
    records = [{'request_id': identity, 'target': 'wifi', 'state': 'route_ready',
                'password': 'secret', 'ssid': 'private'} for identity in identities]
    result = sanitize_status(status(recent_requests=records))
    assert result['recent_requests'] == [dict(request_id=identity, target='wifi', state='route_ready')
                                         for identity in identities[-16:]]
    assert 'secret' not in json.dumps(result) and 'private' not in json.dumps(result)


@pytest.mark.parametrize('record', [
    None, [], 'invalid', {},
    {'request_id': 'bad-id', 'target': 'wifi', 'state': 'failed'},
    {'request_id': str(uuid4()).upper(), 'target': 'wifi', 'state': 'failed'},
    {'request_id': uuid4().hex, 'target': 'wifi', 'state': 'failed'},
    {'request_id': str(uuid4()), 'target': 'laboratory', 'state': 'failed'},
    {'request_id': str(uuid4()), 'target': ['wifi'], 'state': 'failed'},
    {'request_id': str(uuid4()), 'target': 'wifi', 'state': ['failed']},
    {'request_id': str(uuid4()), 'target': 'wifi', 'state': 'complete'},
])
def test_malformed_recent_request_result_cannot_acknowledge_a_switch(record):
    assert sanitize_status(status(recent_requests=[record]))['recent_requests'] == []


def test_recent_result_retains_only_latest_record_for_each_identity():
    identity = str(uuid4())
    records = [dict(request_id=identity, target='auto', state=state) for state in ('queued', 'route_ready')]
    assert sanitize_status(status(recent_requests=records))['recent_requests'] == records[-1:]


@pytest.mark.parametrize('records', [None, {}, 'invalid', False])
def test_nonlist_recent_results_are_ignored(records):
    assert sanitize_status(status(recent_requests=records))['recent_requests'] == []


def test_last_switch_requires_canonical_request_identity():
    result = sanitize_status(status(last_switch={'target': ['wifi'], 'request_id': str(uuid4()).upper()}))
    assert result['last_switch']['request_id'] is None
    assert result['last_switch']['target'] is None


@pytest.mark.parametrize('updated', [0, 84, 106, float('nan'), '100', None])
def test_old_or_invalid_socket_status_never_claims_current_healthy_path(monkeypatch, updated):
    network = NetworkClient(clock=lambda: 100)
    monkeypatch.setattr(network, '_request', lambda _: {'ok': True, 'status': status(updated_at=updated)})
    monkeypatch.setattr(network, '_file_status', lambda: (_ for _ in ()).throw(ValueError('missing')))
    result = network.status()
    assert result['active_path'] == 'unknown' and result['controller_available'] is False
    assert result['usb']['internet'] is None


def test_fresh_fallback_observes_route_and_never_reuses_old_health(monkeypatch):
    network = NetworkClient(clock=lambda: 100)
    monkeypatch.setattr(network, '_request', lambda _: (_ for _ in ()).throw(NetworkUnavailable('offline')))
    monkeypatch.setattr(network, '_file_status', lambda: status(active_path='wifi',
                        recent_requests=[dict(request_id=str(uuid4()), target='wifi', state='route_ready')]))
    monkeypatch.setattr(network, '_observed_path', lambda _: 'usb')
    result = network.status()
    assert result['active_path'] == 'usb' and result['controller_available'] is False
    assert result['wifi']['connected'] is False and result['usb']['connected'] is True
    assert result['usb']['internet'] is None and result['tunnel_ready'] is None
    assert result['recent_requests'] == []


@pytest.mark.parametrize('profile,gateway', [('laboratory-profile', '192.168.125.1'),
                                           (WIFI_PROFILE, '192.168.0.1')])
def test_wifi_interface_alone_never_identifies_modem_wifi(monkeypatch, profile, gateway):
    network = NetworkClient()
    value = status(wifi={'interface': 'wlP1p1s0', 'profile_uuid': WIFI_PROFILE})
    def read(command):
        return json.dumps([{'dev': 'wlP1p1s0', 'gateway': gateway}]) if command[0].endswith('/ip') else profile
    monkeypatch.setattr(network, '_read_command', read)
    assert network._observed_path(value) == 'unknown'


def test_verified_wifi_profile_and_route_match_fallback(monkeypatch):
    network = NetworkClient()
    value = status(wifi={'interface': 'wlP1p1s0', 'profile_uuid': WIFI_PROFILE})
    monkeypatch.setattr(network, '_read_command', lambda command:
                        json.dumps([{'dev': 'wlP1p1s0', 'gateway': '192.168.125.1'}])
                        if command[0].endswith('/ip') else WIFI_PROFILE)
    assert network._observed_path(value) == 'wifi'


def test_nonroot_status_file_is_rejected_without_reading_contents(tmp_path, monkeypatch):
    network = NetworkClient(status_path=tmp_path / 'status.json')
    network.status_path.write_text(json.dumps(status()))
    monkeypatch.setattr(network, '_trusted_parent', lambda _: None)
    monkeypatch.setattr('uav.network.os.fstat', lambda _: SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=1000))
    with pytest.raises(NetworkUnavailable):
        network._file_status()


class FakeSocket:
    def __init__(self, chunks, uid=0):
        self.chunks, self.uid, self.sent = list(chunks), uid, []
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def settimeout(self, *_): pass
    def connect(self, *_): pass
    def getsockopt(self, *_): return struct.pack('3i', 42, self.uid, 0)
    def sendall(self, raw): self.sent.append(raw)
    def recv(self, *_): return self.chunks.pop(0) if self.chunks else b''


def broker(monkeypatch, fake):
    network = NetworkClient()
    monkeypatch.setattr(network, '_trusted_parent', lambda _: None)
    monkeypatch.setattr(Path, 'lstat', lambda _: SimpleNamespace(st_mode=stat.S_IFSOCK | 0o660, st_uid=0))
    monkeypatch.setattr(socket, 'socket', lambda *_: fake)
    return network


def test_peer_credentials_checked_before_any_request(monkeypatch):
    fake = FakeSocket([b'{"ok":true}\n'], uid=1000)
    with pytest.raises(NetworkUnavailable):
        broker(monkeypatch, fake)._request({'action': 'status'})
    assert fake.sent == []


@pytest.mark.parametrize('chunks', [[b'x' * 32769], [b'{"ok":true}'], [b'[]\n'], [b'not-json\n']])
def test_invalid_oversized_or_unframed_socket_response_is_rejected(monkeypatch, chunks):
    with pytest.raises(NetworkUnavailable):
        broker(monkeypatch, FakeSocket(chunks))._request({'action': 'status'})


def test_switch_sends_only_fixed_action_target_and_canonical_identity(monkeypatch):
    identity = str(uuid4())
    fake = FakeSocket([json.dumps({'ok': True, 'accepted': True, 'request_id': identity}).encode() + b'\n'])
    result = broker(monkeypatch, fake).switch('wifi', identity)
    assert result['accepted'] is True
    assert json.loads(fake.sent[0]) == {'action': 'switch', 'target': 'wifi', 'request_id': identity}


def test_mismatched_reply_identity_is_not_an_accepted_switch(monkeypatch):
    fake = FakeSocket([json.dumps({'ok': True, 'accepted': True, 'request_id': str(uuid4())}).encode() + b'\n'])
    with pytest.raises(NetworkUnavailable):
        broker(monkeypatch, fake).switch('usb', str(uuid4()))
