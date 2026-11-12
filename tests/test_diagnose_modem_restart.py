"""Offline USB / credential / one-shot checks; never sends a hardware command."""
import json
import os
from pathlib import Path
import stat
from types import SimpleNamespace
from uuid import uuid4

import pytest

from scripts import diagnose_modem_restart as tool, modem_recovery


def good_usb(generation=(1, 4), count=100, errors=0):
    return {'device': '1-2.2', 'identity_matched': True, 'interface_matched': True, 'generation': generation,
            'port_present': True, 'interface_present': True,
            'stats': {name: errors if name.endswith('_errors') else count for name in tool.COUNTERS}}


def good_probe(now, generation=(1, 4), internet=True):
    return {'device': '1-2.2', 'monotonic': now, 'generation': generation, 'internet': internet}


@pytest.fixture
def fake_sysfs(tmp_path, monkeypatch):
    usb, net = tmp_path/'usb', tmp_path/'net'
    usb.mkdir()
    net.mkdir()
    monkeypatch.setattr(tool, 'USB_ROOT', usb)
    monkeypatch.setattr(tool, 'NET_ROOT', net)
    monkeypatch.setattr(tool, 'MODEM_DEVICE', modem_recovery.modem_device)
    device = usb/'1-2.2'
    device.mkdir()
    for name, value in tool.USB_IDS.items():
        (device/name).write_text(value)
    (device/'bDeviceClass').write_text('00')
    for name, value in (('busnum', '1'), ('devnum', '4')):
        (device/name).write_text(value)
    (device/'power').mkdir()
    (device/'power/runtime_status').write_text('active')
    control = device/(device.name+':1.0')
    control.mkdir()
    (usb/control.name).symlink_to(control)
    driver = tmp_path/'drivers/cdc_ether'
    driver.mkdir(parents=True)
    (control/'driver').symlink_to(driver)
    interface = net/tool.USB_INTERFACE
    interface.mkdir()
    (interface/'address').write_text(tool.USB_MAC)
    (interface/'device').symlink_to(control)
    (interface/'statistics').mkdir()
    for name in tool.COUNTERS:
        (interface/'statistics'/name).write_text('0' if name.endswith('_errors') else '100')
    return device, interface


def test_usb_identity_interface_and_stats_are_read_only(fake_sysfs):
    sample = tool.usb_sample()
    assert sample['identity_matched'] and sample['interface_matched']
    assert sample['generation'] == (1, 4)
    assert sample['stats']['rx_bytes'] == 100
    assert sample['cdc_driver'] == 'cdc_ether' and sample['runtime_status'] == 'active'


@pytest.mark.parametrize('field', ['idVendor', 'idProduct', 'serial'])
def test_wrong_device_identity_cannot_supply_verified_counters(fake_sysfs, field):
    (fake_sysfs[0]/field).write_text('unapproved')
    sample = tool.usb_sample()
    assert sample['matching_devices'] == 0 and not sample['identity_matched']
    assert not sample['interface_matched'] and sample['stats'] is None


def test_wrong_mac_or_different_usb_parent_cannot_be_verified(fake_sysfs):
    (fake_sysfs[1]/'address').write_text('00:11:22:33:44:55')
    assert not tool.usb_sample()['interface_matched']
    (fake_sysfs[1]/'address').write_text(tool.USB_MAC)
    (fake_sysfs[1]/'device').unlink()
    (fake_sysfs[1]/'device').symlink_to(fake_sysfs[0])
    assert not tool.usb_sample()['interface_matched']


def test_absent_usb_is_not_a_sampling_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, 'USB_ROOT', tmp_path/'missing-usb')
    monkeypatch.setattr(tool, 'NET_ROOT', tmp_path/'missing-net')
    sample = tool.usb_sample()
    assert not sample['port_present'] and not sample['interface_present']
    assert sample['generation'] is None and sample['stats'] is None


def test_usb_replacement_during_counter_read_is_rejected(fake_sysfs, monkeypatch):
    answers = iter([(1, 4), (1, 8)])
    monkeypatch.setattr(tool, 'device_generation', lambda *_: next(answers))
    assert not tool.usb_sample()['interface_matched']


def test_exact_card_moving_to_another_port_is_found_by_identity_and_net_owner(fake_sysfs):
    device, interface = fake_sysfs
    first = tool.usb_sample()
    assert first['device'] == '1-2.2' and first['interface_matched']
    (device.parent/(device.name+':1.0')).unlink()
    replacement = device.with_name('1-2.3')
    device.rename(replacement)
    old_control = replacement/'1-2.2:1.0'
    control = replacement/'1-2.3:1.0'
    old_control.rename(control)
    (replacement.parent/'1-2.3:1.0').symlink_to(control)
    (interface/'device').unlink()
    (interface/'device').symlink_to(control)
    # Even if the numeric generation happens to match, the port is explicit.
    second = tool.usb_sample()
    assert second['device'] == '1-2.3' and second['interface_matched']
    assert second['generation'] == first['generation']
    assert second['stats'] == first['stats']


def test_duplicate_exact_serials_are_ambiguous_even_with_one_matching_net_owner(fake_sysfs):
    device, _ = fake_sysfs
    duplicate = device.with_name('1-2.3')
    duplicate.mkdir()
    for field in (*tool.USB_IDS, 'bDeviceClass', 'busnum', 'devnum'):
        (duplicate/field).write_text((device/field).read_text())
    sample = tool.usb_sample()
    assert sample['matching_devices'] == 2
    assert sample['device'] is None and not sample['identity_matched'] and sample['stats'] is None


def test_exact_ids_on_a_hub_cannot_be_treated_as_the_modem(fake_sysfs):
    (fake_sysfs[0]/'bDeviceClass').write_text('09')
    sample = tool.usb_sample()
    assert not sample['identity_matched'] and not sample['interface_matched']


def test_actual_device_key_prevents_combining_health_from_different_ports():
    stable = tool.StableUSB()
    stable.update(good_usb(), good_probe(0), 0)
    usb, probe = good_usb(count=500), good_probe(29)
    usb['device'] = probe['device'] = '1-2.3'
    assert not stable.update(usb, probe, 29)
    assert stable.stable_seconds == 0
    assert not stable.update(usb, good_probe(30), 30)


def test_kernel_messages_follow_confirmed_card_ports_without_foreign_port_attribution(monkeypatch):
    rows = [{'__MONOTONIC_TIMESTAMP': str((index+1)*1000000), 'MESSAGE': message}
            for index, message in enumerate(('usb 1-2.2: USB disconnect', 'usb 1-2.3: device descriptor error -71',
                                             'usb 1-2.5: unrelated device error -71'))]
    monkeypatch.setattr(tool.subprocess, 'run', lambda *_, **__: SimpleNamespace(returncode=0,
                       stdout='\n'.join(json.dumps(row) for row in rows)))
    result = tool.kernel_observation(0, ('1-2.2', '1-2.3'))
    assert len(result['events']) == 2
    assert '1-2.3' in result['events'][1]['message'] and '1-2.5' not in json.dumps(result)


@pytest.mark.parametrize('change', ['errors', 'generation', 'stale_probe', 'external_down', 'no_interface', 'counters_reset'])
def test_stability_window_resets_on_real_failure_or_changed_generation(change):
    stable = tool.StableUSB()
    assert not stable.update(good_usb(), good_probe(0), 0)
    assert not stable.update(good_usb(count=200), good_probe(20), 20)
    usb, probe = good_usb(count=300), good_probe(25)
    if change == 'errors': usb['stats']['rx_errors'] = 1
    if change == 'generation': usb['generation'] = probe['generation'] = (1, 8)
    if change == 'stale_probe': probe['monotonic'] = 20
    if change == 'external_down': probe['internet'] = False
    if change == 'no_interface': usb['interface_matched'] = False
    if change == 'counters_reset': usb['stats']['rx_bytes'] = 1
    assert not stable.update(usb, probe, 25)
    assert stable.stable_seconds == 0


def test_enumeration_or_frozen_counters_alone_never_prove_recovery():
    stable = tool.StableUSB()
    assert not stable.update(good_usb(), None, 0)
    for now in range(40):
        assert not stable.update(good_usb(), good_probe(now), now)


def test_thirty_seconds_requires_bound_external_traffic_and_both_directions():
    stable = tool.StableUSB()
    for now in range(30):
        assert not stable.update(good_usb(count=100+now), good_probe(now), now)
    assert stable.update(good_usb(count=130), good_probe(30), 30)
    usb = good_usb(count=100)
    usb['stats']['tx_bytes'] = 500
    fresh = tool.StableUSB()
    fresh.update(good_usb(), good_probe(0), 0)
    assert not fresh.update(usb, good_probe(30), 30)


class FakeClient:
    def __init__(self, body=None, exception=None):
        self.calls = []
        self.body = body if body is not None else b'<RGW><return_code>0</return_code></RGW>'
        self.exception = exception
    def login(self): self.calls.append('login')
    def header(self): return 'private-digest'
    def request(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.exception:
            raise self.exception
        return {}, self.body


def version_xml(device='DWM-222W', hardware='B1', version='1.01.00', unauthorized=False):
    return (f'<RGW><login_status>{"UNAUTHORIZED" if unauthorized else "OK"}</login_status><sysinfo>'
            f'<device_name>{device}</device_name><hardware_version>{hardware}</hardware_version>'
            f'<version_num>{version}</version_num><password>private-password</password>'
            f'</sysinfo></RGW>').encode()


def test_preflight_whitelists_only_authenticated_exact_device_version():
    client = FakeClient(body=version_xml())
    assert tool.authenticated_device(client) == {
        'device_name': 'DWM-222W', 'hardware_version': 'B1', 'firmware_version': '1.01.00'}
    assert len(client.calls) == 2 and client.calls[0] == 'login'
    assert client.calls[1][0][0] == tool.STATUS_PATH


@pytest.mark.parametrize('changes', [dict(device='DWM-222'), dict(hardware='A1'),
                                    dict(version='1.00.00'), dict(unauthorized=True)])
def test_wrong_or_unauthenticated_version_is_rejected_without_a_reset(changes):
    client = FakeClient(body=version_xml(**changes))
    with pytest.raises((ValueError, PermissionError)):
        tool.authenticated_device(client)
    assert not any(isinstance(call, tuple) and call[0][0] == tool.RESET_PATH for call in client.calls)


@pytest.mark.parametrize('exception', [OSError('private-http-details'), TimeoutError('private-digest'),
                                       ValueError('private-XML'), PermissionError('private-auth')])
def test_uncertain_reboot_response_never_repeats_request_or_leaks_message(exception):
    client = FakeClient(exception=exception)
    result = tool.request_one_reboot(client)
    assert len(client.calls) == 1
    assert client.calls[0] == ((tool.RESET_PATH, 'private-digest'), {'method': 'GET'})
    assert result['result'] == 'exception_unconfirmed'
    assert 'private' not in json.dumps(result)


@pytest.mark.parametrize('body, expected', [(b'<RGW><return_code>0</return_code></RGW>', 'accepted'),
    (b'<RGW><return_code>1</return_code></RGW>', 'reply_unconfirmed'),
    (b'<RGW><return_code>0</return_code><login_status>UNAUTHORIZED</login_status></RGW>', 'reply_unconfirmed'),
    (b'bare EOF', 'exception_unconfirmed')])
def test_vendor_reboot_reply_is_recorded_without_raw_xml(body, expected):
    client = FakeClient(body=body)
    result = tool.request_one_reboot(client)
    assert len(client.calls) == 1 and result['result'] == expected
    assert '<RGW>' not in json.dumps(result)


def test_private_output_is_exclusive_and_never_overwrites_action(tmp_path):
    path = tmp_path/'action.json'
    tool.write_json(path, {'attempted': True}, exclusive=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        tool.write_json(path, {'attempted': True}, exclusive=True)


def test_private_auth_rejects_symlink_and_public_permissions(tmp_path):
    path = tmp_path/'auth.json'
    path.write_text('{"tls_sha256":"private-test"}')
    path.chmod(0o644)
    with pytest.raises(ValueError): tool.private_json(path)
    path.chmod(0o600)
    assert tool.private_json(path) == {'tls_sha256': 'private-test'}
    link = tmp_path/'link'
    link.symlink_to(path)
    with pytest.raises(OSError): tool.private_json(link)


class ImmediatePool:
    def __init__(self, **kwargs): pass
    def submit(self, function, *args):
        result = function(*args)
        return SimpleNamespace(done=lambda: True, result=lambda: result)
    def shutdown(self, **kwargs): pass


def test_bounded_observer_records_absence_fc_change_and_never_logs_credentials(tmp_path, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(tool, 'ThreadPoolExecutor', ImmediatePool)
    missing = dict(identity_matched=False, interface_matched=False, generation=None, stats=None)
    monkeypatch.setattr(tool, 'usb_sample', lambda: missing)
    monkeypatch.setattr(tool, 'network_sample', lambda *_: {'fresh': False, 'active_path': 'unknown'})
    monkeypatch.setattr(tool, 'device_generation', lambda *_: (1, 8) if now[0] >= 2 else (1, 5))
    monkeypatch.setattr(tool, 'public_health', lambda: True)
    monkeypatch.setattr(tool, 'kernel_observation', lambda after, _ports: {'events': [], 'after_monotonic': after})
    summary = tool.observe(tmp_path, 4, 'fake-boot', missing, (1, 5),
                           lambda *_: pytest.fail('No external USB probe without exact interface'),
                           clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0]+seconds))
    rows = [json.loads(line) for line in (tmp_path/'samples.jsonl').read_text().splitlines()]
    assert len(rows) == 4 and summary['observation_seconds'] == 4
    assert [row['elapsed'] for row in rows] == [0, 1, 2, 3]
    assert not summary['flight_generation_unchanged']
    assert not summary['usb_verified_30s'] and summary['permanent_fix_proven'] is False
    assert stat.S_IMODE((tmp_path/'samples.jsonl').stat().st_mode) == 0o600


def test_generation_change_during_bound_probe_invalidates_result_and_stops_second_probe(tmp_path, monkeypatch):
    now, current, calls = [0.0], [(1, 4)], []
    monkeypatch.setattr(tool, 'ThreadPoolExecutor', ImmediatePool)
    monkeypatch.setattr(tool, 'usb_sample', lambda: good_usb(generation=current[0]))
    monkeypatch.setattr(tool, 'network_sample', lambda *_: {'fresh': False})
    monkeypatch.setattr(tool, 'device_generation', lambda *_: (1, 5))
    monkeypatch.setattr(tool, 'public_health', lambda: True)
    monkeypatch.setattr(tool, 'kernel_observation', lambda after, _ports: {'events': [], 'after_monotonic': after})
    def bound(interface, destination, port, tls, **options):
        assert interface == tool.USB_INTERFACE and port == 443 and tls is True
        calls.append(destination)
        if len(calls) == 1:
            current[0] = (1, 8)
        return True
    summary = tool.observe(tmp_path, 2, 'fake-boot', good_usb(), (1, 5), bound,
                           clock=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0]+seconds))
    rows = [json.loads(line) for line in (tmp_path/'samples.jsonl').read_text().splitlines()]
    failed_probe = rows[1]['usb_probe']
    assert failed_probe['generation'] == [1, 4]
    assert failed_probe['identity_unchanged'] is False and failed_probe['internet'] is False
    assert calls[:2] == ['1.1.1.1', '1.1.1.1']
    assert not summary['usb_verified_30s']


def test_kernel_window_keeps_usb_events_and_drops_unrelated_private_logs(monkeypatch):
    rows = [{'__MONOTONIC_TIMESTAMP': '1000000', 'MESSAGE': 'usb 1-2.2: USB disconnect, device number 4'},
            {'__MONOTONIC_TIMESTAMP': '2000000', 'MESSAGE': 'private-password unrelated modem login'},
            {'__MONOTONIC_TIMESTAMP': '3000000', 'MESSAGE': 'xhci-hcd controller Transfer error'}]
    monkeypatch.setattr(tool.subprocess, 'run', lambda *_, **__: SimpleNamespace(returncode=0,
                       stdout='\n'.join(json.dumps(row) for row in rows)))
    result = tool.kernel_observation(0, ('1-2.2',))
    assert len(result['events']) == 2 and result['after_monotonic'] == 3
    assert 'private-password' not in json.dumps(result)


def test_default_cli_observes_without_loading_auth_or_requesting_reboot(tmp_path, monkeypatch):
    boot = tmp_path/'boot'
    boot.write_text(str(uuid4()))
    report = tmp_path/'report'
    monkeypatch.setattr(tool, 'BOOT', boot)
    monkeypatch.setattr(tool.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(tool, 'private_directory', lambda path: path.mkdir(mode=0o700, exist_ok=True))
    monkeypatch.setattr(tool, 'installed_dependencies', lambda: (SimpleNamespace(
        RadioClient=lambda *_args, **_kwargs: pytest.fail('Read-only must not authenticate')), lambda *_: False))
    monkeypatch.setattr(tool, 'private_json', lambda *_: pytest.fail('Read-only must not read credentials'))
    monkeypatch.setattr(tool, 'usb_sample', lambda: {'identity_matched': False})
    monkeypatch.setattr(tool, 'device_generation', lambda *_: None)
    monkeypatch.setattr(tool, 'observe', lambda *_, **__: {'samples': 1, 'usb_verified_30s': False,
                                                       'flight_generation_unchanged': False})
    monkeypatch.setattr(tool.sys, 'argv', ['diagnose', '--directory', str(report), '--seconds', '1'])
    tool.main()
    assert json.loads((report/'summary.json').read_text())['reboot']['attempted'] is False
    with pytest.raises(FileExistsError): tool.main()


def test_boot_base_creates_uuid_directory_and_cannot_perform_reset(tmp_path, monkeypatch):
    boot = tmp_path/'boot'
    identifier = str(uuid4())
    boot.write_text(identifier)
    monkeypatch.setattr(tool, 'BOOT', boot)
    monkeypatch.setattr(tool.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(tool, 'private_directory', lambda path: path.mkdir(mode=0o700, exist_ok=True))
    monkeypatch.setattr(tool, 'installed_dependencies', lambda: (None, None))
    monkeypatch.setattr(tool, 'usb_sample', lambda: {})
    monkeypatch.setattr(tool, 'device_generation', lambda *_: None)
    monkeypatch.setattr(tool, 'observe', lambda *_, **__: {'samples': 1, 'usb_verified_30s': False,
                                                       'flight_generation_unchanged': False})
    base = tmp_path/'reports'
    monkeypatch.setattr(tool.sys, 'argv', ['diagnose', '--boot-observe-base', str(base), '--seconds', '1'])
    tool.main()
    assert (base/identifier/'run.json').is_file()
    monkeypatch.setattr(tool.sys, 'argv', ['diagnose', '--boot-observe-base', str(base), '--reboot'])
    with pytest.raises(SystemExit): tool.main()


@pytest.mark.parametrize('seconds', ['0', '181', '-1'])
def test_cli_observation_duration_is_bounded_before_output_or_actions(tmp_path, monkeypatch, seconds):
    monkeypatch.setattr(tool.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(tool.sys, 'argv', ['diagnose', '--directory', str(tmp_path/'report'), '--seconds', seconds])
    with pytest.raises(SystemExit): tool.main()
    assert not (tmp_path/'report').exists()


def test_nonroot_cli_cannot_observe_or_request_a_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(tool.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(tool.sys, 'argv', ['diagnose', '--directory', str(tmp_path/'report'), '--reboot'])
    with pytest.raises(SystemExit): tool.main()
    assert not (tmp_path/'report').exists()


@pytest.mark.parametrize('wrong_version', [False, True])
def test_reboot_cli_authenticates_exact_card_once_and_directory_blocks_repeated_action(tmp_path, monkeypatch, wrong_version):
    boot = tmp_path/'boot'
    boot.write_text(str(uuid4()))
    report = tmp_path/'report'
    monkeypatch.setattr(tool, 'BOOT', boot)
    monkeypatch.setattr(tool.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(tool, 'private_directory', lambda path: path.mkdir(mode=0o700, exist_ok=True))
    client = FakeClient()
    original_request = client.request
    def request(path, header, **kwargs):
        client.body = version_xml(version='bad' if wrong_version else '1.01.00') if path == tool.STATUS_PATH else b'<RGW><return_code>0</return_code></RGW>'
        return original_request(path, header, **kwargs)
    client.request = request
    def factory(auth, *, transport):
        assert auth == {'tls_sha256': 'test-private-pin'} and transport == 'onboard-wifi'
        return client
    monkeypatch.setattr(tool, 'installed_dependencies', lambda: (SimpleNamespace(RadioClient=factory), None))
    monkeypatch.setattr(tool, 'private_json', lambda *_: {'tls_sha256': 'test-private-pin'})
    monkeypatch.setattr(tool, 'usb_sample', lambda: {'identity_matched': False})
    monkeypatch.setattr(tool, 'device_generation', lambda *_: (1, 5))
    monkeypatch.setattr(tool, 'observe', lambda *_, **__: {'samples': 1, 'usb_verified_30s': False,
                                                       'flight_generation_unchanged': True})
    monkeypatch.setattr(tool.sys, 'argv', ['diagnose', '--directory', str(report), '--reboot', '--seconds', '1'])
    if wrong_version:
        with pytest.raises(ValueError): tool.main()
        assert not (report/'action.json').exists()
    else:
        tool.main()
        assert json.loads((report/'action-result.json').read_text())['result'] == 'accepted'
        assert json.loads((report/'action.json').read_text())['version']['firmware_version'] == '1.01.00'
    reset_calls = [call for call in client.calls if isinstance(call, tuple) and call[0][0] == tool.RESET_PATH]
    assert len(reset_calls) == (0 if wrong_version else 1)
    before = list(client.calls)
    with pytest.raises(FileExistsError): tool.main()
    assert client.calls == before
