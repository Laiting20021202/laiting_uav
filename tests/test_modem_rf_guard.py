import ctypes
import errno
import json
import os

import pytest

from scripts import modem_rf_guard as rf


def device_tree(tmp_path):
    device = tmp_path/'1-2.2'
    interface = device/'1-2.2:1.4'
    interface.mkdir(parents=True)
    for name, value in {'busnum': '1', 'devnum': '15'}.items():
        (device/name).write_text(value)
    for name, value in {'bInterfaceNumber': '04', 'interface': 'Mobile AT Interface',
                        'bInterfaceClass': 'ff'}.items():
        (interface/name).write_text(value)
    for address, attributes in ((0x0a, 2), (0x81, 2), (0x88, 3)):
        endpoint = interface/f'ep_{address:02x}'
        endpoint.mkdir()
        (endpoint/'bEndpointAddress').write_text(f'{address:02x}')
        (endpoint/'bmAttributes').write_text(f'{attributes:02x}')
    return device, interface


def fake_usb(monkeypatch, tmp_path, replies=()):
    device, interface = device_tree(tmp_path)
    usbfs = tmp_path/'drivers'/'usbfs'
    usbfs.mkdir(parents=True)
    calls = []
    data = iter(replies)
    monkeypatch.setattr(rf, 'modem_device', lambda: device)
    monkeypatch.setattr(rf.os, 'open', lambda *args: 42)
    monkeypatch.setattr(rf.os, 'close', lambda descriptor: calls.append(('close', descriptor)))

    def ioctl(descriptor, operation, argument, *args):
        assert descriptor == 42
        assert operation in (rf.CLAIM, rf.RELEASE)
        assert list(argument) == [4]
        calls.append(('ioctl', operation, 4))
        if operation == rf.CLAIM:
            (interface/'driver').symlink_to(usbfs)
        else:
            (interface/'driver').unlink()

    def bulk_ioctl(descriptor, operation, argument):
        assert descriptor.value == 42
        assert operation.value == rf.BULK_IOCTL
        transfer = ctypes.cast(argument, ctypes.POINTER(rf.Bulk)).contents
        assert 1 <= transfer.timeout <= 800
        if transfer.ep == rf.OUT_ENDPOINT:
            sent = ctypes.string_at(transfer.data, transfer.length)
            calls.append(('write', sent))
            return transfer.length
        assert transfer.ep == rf.IN_ENDPOINT
        reply = next(data)
        if isinstance(reply, int):
            ctypes.set_errno(reply)
            return -1
        assert len(reply) <= transfer.length
        ctypes.memmove(transfer.data, reply, len(reply))
        calls.append(('read', len(reply)))
        return len(reply)

    monkeypatch.setattr(rf.fcntl, 'ioctl', ioctl)
    monkeypatch.setattr(rf.LIBC, 'ioctl', bulk_ioctl)
    return device, interface, calls


def test_claims_only_interface_four_and_accepts_its_own_usbfs_driver(monkeypatch, tmp_path):
    _, interface, calls = fake_usb(monkeypatch, tmp_path, [b'\r\n+CFUN: 4\r\nOK\r\n',
                                                       b'\r\n+CPIN: READY\r\nOK\r\n'])
    with rf.AtClient() as client:
        result = client.snapshot()
        assert (interface/'driver').resolve().name == 'usbfs'
    assert result == {'device': '1-2.2', 'usb_generation': '1:15', 'cfun': 4, 'sim_ready': True}
    assert [call[1] for call in calls if call[0] == 'write'] == [b'AT+CFUN?\r', b'AT+CPIN?\r']
    assert [call[1] for call in calls if call[0] == 'ioctl'] == [rf.CLAIM, rf.RELEASE]
    assert calls[-1] == ('close', 42)


@pytest.mark.parametrize('file, value', [
    ('bInterfaceNumber', '03'), ('interface', 'Diagnostic Interface'),
    ('bInterfaceClass', '02'), ('ep_0a/bEndpointAddress', '0f'),
    ('ep_81/bmAttributes', '03'), ('ep_88/bEndpointAddress', '89'),
])
def test_wrong_interface_or_endpoints_are_rejected_before_claim(monkeypatch, tmp_path, file, value):
    _, interface, calls = fake_usb(monkeypatch, tmp_path)
    (interface/file).write_text(value)
    with pytest.raises(ValueError):
        with rf.AtClient():
            pass
    assert calls == []


def test_even_an_existing_usbfs_driver_cannot_be_claimed_as_ours(monkeypatch, tmp_path):
    _, interface, calls = fake_usb(monkeypatch, tmp_path)
    driver = tmp_path/'other-driver'/'usbfs'
    driver.mkdir(parents=True)
    (interface/'driver').symlink_to(driver)
    with pytest.raises(ValueError, match='already has a driver'):
        with rf.AtClient():
            pass
    assert calls == []


def test_fixed_modem_identity_failure_prevents_usb_operations(monkeypatch):
    calls = []
    def changed_device():
        raise ValueError('Modem identity mismatch')
    monkeypatch.setattr(rf, 'modem_device', changed_device)
    monkeypatch.setattr(rf.os, 'open', lambda *args: calls.append(args))
    with pytest.raises(ValueError):
        with rf.AtClient():
            pass
    assert calls == []


def test_changed_generation_blocks_every_bulk_and_uses_close_for_release(monkeypatch, tmp_path):
    device, _, calls = fake_usb(monkeypatch, tmp_path)
    with rf.AtClient() as client:
        (device/'devnum').write_text('16')
        with pytest.raises(RuntimeError, match='generation changed'):
            client.query('AT+CFUN?')
    assert not any(call[0] in ('write', 'read') for call in calls)
    assert [call[1] for call in calls if call[0] == 'ioctl'] == [rf.CLAIM]
    assert calls[-1] == ('close', 42)


def test_context_releases_claim_when_a_query_raises(monkeypatch, tmp_path):
    _, _, calls = fake_usb(monkeypatch, tmp_path, [errno.EPROTO])
    with pytest.raises(OSError):
        with rf.AtClient() as client:
            client.query('AT+CFUN?')
    assert [call[1] for call in calls if call[0] == 'ioctl'] == [rf.CLAIM, rf.RELEASE]
    assert calls[-1] == ('close', 42)


@pytest.mark.parametrize('command', ['AT+CFUN=1', 'AT+CFUN=0', 'AT+CPIN=1234', 'AT+CGDCONT=1'])
def test_public_query_api_rejects_all_mutating_commands(command):
    with pytest.raises(ValueError, match='read-only'):
        rf.AtClient().query(command)


@pytest.mark.parametrize('cfun, pin, enabled', [
    (4, 'READY', True), (1, 'READY', False), (4, 'SIM PIN', False), (0, 'READY', False),
])
def test_enable_independently_rechecks_function_and_sim_before_only_cfun_one(monkeypatch, tmp_path, cfun, pin, enabled):
    replies = [f'\r\n+CFUN: {cfun}\r\nOK\r\n'.encode(),
               f'\r\n+CPIN: {pin}\r\nOK\r\n'.encode()]
    if enabled:
        replies.append(b'\r\nOK\r\n')
    _, _, calls = fake_usb(monkeypatch, tmp_path, replies)
    with rf.AtClient() as client:
        result = client.enable_if_disabled()
    commands = [call[1] for call in calls if call[0] == 'write']
    assert commands == [b'AT+CFUN?\r', b'AT+CPIN?\r'] + ([b'AT+CFUN=1\r'] if enabled else [])
    assert result['command_acknowledged'] is enabled


def test_query_read_timeout_is_bounded_and_releases_the_claim(monkeypatch, tmp_path):
    _, _, calls = fake_usb(monkeypatch, tmp_path, [errno.ETIMEDOUT]*20)
    tick = [0.0]
    def monotonic():
        tick[0] += 0.35
        return tick[0]
    monkeypatch.setattr(rf.time, 'monotonic', monotonic)
    with pytest.raises(TimeoutError):
        with rf.AtClient() as client:
            client.query('AT+CFUN?')
    assert tick[0] < 4
    assert [call[1] for call in calls if call[0] == 'ioctl'] == [rf.CLAIM, rf.RELEASE]


def test_query_response_limit_blocks_unbounded_or_unsolicited_output(monkeypatch, tmp_path):
    fake_usb(monkeypatch, tmp_path, [b'x'*4096])
    with pytest.raises(ValueError, match='exceeds limit'):
        with rf.AtClient() as client:
            client.query('AT+CFUN?')


def health(**changes):
    return {'device': '1-2.2', 'usb_generation': '1:15', 'gateway': True,
            'internet': False, 'interface_present': True, 'carrier': True, **changes}


def test_working_onboard_wifi_proves_shared_rf_without_usb_at(monkeypatch):
    monkeypatch.setattr(rf, 'onboard_hotspot_in_use', lambda: True)
    calls = []
    monkeypatch.setattr(rf, 'connect', lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    assert rf.onboard_wifi_healthy()
    assert calls == [(('1.1.1.1', 443), {'tls': True, 'interface': 'wlP1p1s0'})]


def test_common_lte_loss_still_allows_rf_recovery(monkeypatch):
    monkeypatch.setattr(rf, 'onboard_hotspot_in_use', lambda: True)
    monkeypatch.setattr(rf, 'connect', lambda *args, **kwargs: False)
    assert not rf.onboard_wifi_healthy()


def radio(**changes):
    return {'device': '1-2.2', 'usb_generation': '1:15', 'cfun': 4,
            'sim_ready': True, **changes}


def test_rf_debounce_requires_same_generation_then_persists_fifteen_second_backoff():
    policy = rf.RFPolicy()
    assert not policy.observe(health(), radio(), 0)
    assert not policy.observe(health(), radio(), 4.99)
    assert policy.observe(health(), radio(), 5)
    policy.attempted(5)
    assert policy.state() == {'count': 1, 'next_action': 20}
    restored = rf.RFPolicy(policy.state())
    assert not restored.observe(health(), radio(), 10)
    assert not restored.observe(health(), radio(), 15)
    assert restored.observe(health(), radio(), 20)


def test_new_usb_generation_or_interrupted_evidence_restarts_confirmation():
    policy = rf.RFPolicy()
    assert not policy.observe(health(), radio(), 0)
    current_health = health(usb_generation='1:16')
    current_radio = radio(usb_generation='1:16')
    assert not policy.observe(current_health, current_radio, 5)
    assert not policy.observe(current_health, current_radio, 9)
    assert policy.observe(current_health, current_radio, 10)
    assert not policy.observe(current_health, None, 11)
    assert not policy.observe(current_health, current_radio, 15)
    assert policy.observe(current_health, current_radio, 20)


@pytest.mark.parametrize('changes', [
    {'internet': True}, {'gateway': False}, {'interface_present': False},
    {'carrier': False}, {'device': None},
])
def test_internet_or_nonworking_usb_path_never_triggers_rf_action(changes):
    policy = rf.RFPolicy()
    for now in (0, 5, 60):
        assert not rf.eligible(health(**changes))
        assert not policy.observe(health(**changes), radio(), now)


@pytest.mark.parametrize('changes', [
    {'cfun': 1}, {'cfun': 0}, {'sim_ready': False}, {'usb_generation': '1:99'},
])
def test_rf_on_no_service_sim_locked_or_stale_generation_never_triggers_rf_reset(changes):
    policy = rf.RFPolicy()
    for now in (0, 5, 60):
        assert not policy.observe(health(), radio(**changes), now)


def test_private_atomic_status_file_has_no_raw_at_or_credentials(tmp_path):
    path = tmp_path/'state'/'status.json'
    rf.save_json(path, {'radio': radio(), 'error': 'TimeoutError'})
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert json.loads(path.read_text())['radio']['cfun'] == 4
