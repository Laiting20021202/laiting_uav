"""Route policy and broker safety without changing host networking."""
import json
from pathlib import Path
from unittest.mock import Mock
import uuid

import pytest

from scripts import modem_path_manager as paths
from scripts import modem_recovery as recovery


def healthy(usb=True, wifi=True, usb_connected=True, wifi_connected=True, gateway=True):
    return {name: {'available': True, 'internet': internet, 'connected': connected, 'gateway_ready': gateway,
                   'gateway': '192.168.125.1', 'interface': paths.INTERFACES[name]}
            for name, internet, connected in (('usb', usb, usb_connected), ('wifi', wifi, wifi_connected))}


def test_local_usb_failure_immediately_uses_ready_onboard_wifi():
    policy = paths.PathPolicy()
    assert policy.choose(healthy(usb=False, usb_connected=False), 'usb', 100) == 'wifi'
    assert policy.usb_retry_at == 400


def test_shared_lte_outage_does_not_switch_or_claim_a_working_backup():
    policy = paths.PathPolicy()
    for now in range(100):
        assert policy.choose(healthy(False, False), 'usb', now) is None


def test_one_external_probe_failure_with_working_gateway_keeps_path():
    policy = paths.PathPolicy()
    assert policy.choose(healthy(False, True), 'usb', 1) is None
    assert policy.choose(healthy(False, True), 'usb', 2) == 'wifi'


def test_failback_requires_usb_healthy_and_error_cooldown():
    policy = paths.PathPolicy()
    assert policy.choose(healthy(False, True, usb_connected=False), 'usb', 100) == 'wifi'
    policy.last_switch = 100
    assert policy.choose(healthy(), 'wifi', 101) is None
    assert policy.choose(healthy(), 'wifi', 399) is None
    assert policy.choose(healthy(), 'wifi', 400) is None
    assert policy.choose(healthy(), 'wifi', 401) == 'usb'


def test_usb_reenumeration_restarts_continuous_health_and_cooldown():
    policy = paths.PathPolicy()
    first = healthy()
    first['usb'].update(device='1-2.2', usb_generation='1:4')
    assert policy.choose(first, 'wifi', 1) is None
    returned = healthy()
    returned['usb'].update(device='1-2.2', usb_generation='1:5')
    assert policy.choose(returned, 'wifi', 29) is None
    assert policy.healthy_since['usb'] == 29
    assert policy.usb_retry_at == 329
    assert policy.choose(returned, 'wifi', 328) is None
    assert policy.choose(returned, 'wifi', 329) == 'usb'


def test_long_usb_outage_starts_cooldown_when_usb_actually_recovers():
    policy = paths.PathPolicy()
    assert policy.choose(healthy(False, True, usb_connected=False), 'usb', 1) == 'wifi'
    assert policy.choose(healthy(False, True, usb_connected=False), 'wifi', 500) is None
    assert policy.choose(healthy(), 'wifi', 501) is None
    assert policy.usb_retry_at == 801
    assert policy.choose(healthy(), 'wifi', 800) is None
    assert policy.choose(healthy(), 'wifi', 801) == 'usb'


def test_cooldown_survives_service_restart_but_health_is_measured_again():
    policy = paths.PathPolicy()
    policy.choose(healthy(False, True, usb_connected=False), 'usb', 100)
    policy.last_switch = 100
    restored = paths.PathPolicy(state=policy.state())
    assert restored.usb_retry_at == 400
    assert restored.last_switch == 100
    assert restored.choose(healthy(), 'wifi', 110) is None
    assert restored.healthy_since['usb'] == 110
    assert restored.choose(healthy(), 'wifi', 399) is None
    assert restored.choose(healthy(), 'wifi', 400) == 'usb'


@pytest.mark.parametrize('value', [float('inf'), float('nan'), -1, '500', True])
def test_invalid_persisted_cooldown_is_ignored(value):
    assert paths.PathPolicy(state={'usb_retry_at': value}).usb_retry_at == 0


def test_probe_success_from_old_usb_generation_cannot_authorize_route(monkeypatch):
    before = {**healthy()['usb'], 'device': '1-2.2', 'usb_generation': '1:4'}
    after = {**before, 'usb_generation': '1:5'}
    identities = iter((before, after))
    monkeypatch.setattr(paths, 'network_identity', lambda *args: next(identities).copy())
    monkeypatch.setattr(paths, 'bound_connect', lambda *args: True)
    status = paths.inspect_path('usb', {})
    assert status['usb_generation'] == '1:5'
    assert not status['internet'] and not status['gateway_ready']


def test_probe_success_with_unchanged_generation_is_accepted(monkeypatch):
    identity = {**healthy()['usb'], 'device': '1-2.2', 'usb_generation': '1:4'}
    monkeypatch.setattr(paths, 'network_identity', lambda *args: identity.copy())
    monkeypatch.setattr(paths, 'bound_connect', lambda *args: True)
    assert paths.path_ready(paths.inspect_path('usb', {}))


def test_manual_wifi_preference_survives_auto_and_still_fails_over():
    policy = paths.PathPolicy('wifi')
    assert policy.choose(healthy(), 'wifi', 1) is None
    assert policy.choose(healthy(wifi=False, wifi_connected=False), 'wifi', 2) == 'usb'


def test_offline_boot_selects_only_working_onboard_path():
    for active in ('none', 'unknown'):
        assert paths.PathPolicy().choose(healthy(False, True), active, 1) == 'wifi'
        assert paths.PathPolicy().choose(healthy(False, False), active, 1) is None


def test_nm_automatic_standby_promotion_is_pinned_before_usb_returns():
    policy = paths.PathPolicy()
    assert paths.managed_choice(policy, healthy(), 'usb', None, 1) == 'usb'
    # The kernel already selected wifi when NM removed the USB DHCP route.
    assert paths.managed_choice(policy, healthy(False, True, usb_connected=False), 'wifi', 'usb', 2) == 'wifi'
    assert policy.usb_retry_at == 302
    policy.last_switch = 2
    assert paths.managed_choice(policy, healthy(), 'wifi', 'wifi', 20) is None
    # Even an NM DHCP resync must not reclaim the primary during cooldown.
    assert paths.managed_choice(policy, healthy(), 'usb', 'wifi', 40) == 'wifi'


def test_boot_respects_saved_onboard_wifi_preference():
    assert paths.managed_choice(paths.PathPolicy('wifi'), healthy(), 'usb', None, 1) == 'wifi'


def test_startup_on_working_wifi_does_not_bypass_usb_health_wait():
    policy = paths.PathPolicy()
    assert paths.managed_choice(policy, healthy(), 'wifi', None, 1) == 'wifi'
    policy.last_switch = 1
    assert paths.managed_choice(policy, healthy(), 'wifi', 'wifi', 30) is None
    assert paths.managed_choice(policy, healthy(), 'wifi', 'wifi', 61) == 'usb'


def test_startup_does_not_bypass_saved_cooldown_even_without_managed_path():
    policy = paths.PathPolicy(state={'usb_retry_at': 500})
    assert paths.managed_choice(policy, healthy(), 'wifi', None, 100) == 'wifi'
    policy.last_switch = 100
    assert paths.managed_choice(policy, healthy(), 'wifi', 'wifi', 400) is None
    assert paths.managed_choice(policy, healthy(), 'wifi', 'wifi', 500) == 'usb'


def test_same_subnet_arp_settings_touch_only_verified_modem_interfaces(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'network_identity', lambda name, config: {'connected': name == 'wifi'})
    for interface in (paths.USB, paths.WIFI, 'eno1'):
        folder = tmp_path/interface
        folder.mkdir()
        (folder/'arp_ignore').write_text('0')
        (folder/'arp_announce').write_text('0')
    paths.isolate_arp('usb', {}, tmp_path)
    paths.isolate_arp('wifi', {}, tmp_path)
    assert (tmp_path/paths.WIFI/'arp_ignore').read_text() == '1'
    assert (tmp_path/paths.WIFI/'arp_announce').read_text() == '2'
    for interface in (paths.USB, 'eno1'):
        assert (tmp_path/interface/'arp_ignore').read_text() == '0'


@pytest.mark.parametrize('vanished_key', ['arp_ignore', 'arp_announce'])
def test_usb_interface_disappearing_during_arp_setup_keeps_wifi_inspection_possible(tmp_path, monkeypatch, vanished_key):
    monkeypatch.setattr(paths, 'network_identity', lambda name, config: {'connected': True})
    for interface in (paths.USB, paths.WIFI):
        folder = tmp_path/interface
        folder.mkdir()
        (folder/'arp_ignore').write_text('0')
        (folder/'arp_announce').write_text('0')
    (tmp_path/paths.USB/vanished_key).unlink()
    paths.isolate_arp('usb', {}, tmp_path)
    paths.isolate_arp('wifi', {}, tmp_path)
    assert (tmp_path/paths.WIFI/'arp_ignore').read_text() == '1'
    assert (tmp_path/paths.WIFI/'arp_announce').read_text() == '2'


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, 'STATE', tmp_path)
    monkeypatch.setattr(paths, 'RUNTIME', tmp_path)
    manager = paths.Manager({'user_uid': 1000, 'user_gid': 1000})
    manager.status.update(healthy())
    return manager


@pytest.mark.parametrize('message', [None, [], {'action': 'shell'},
    {'action': 'switch', 'target': 'lab', 'request_id': str(uuid.uuid4())},
    {'action': 'switch', 'target': 'wifi', 'request_id': str(uuid.uuid4()), 'profile': 'lab'},
    {'action': 'switch', 'target': 'wifi', 'request_id': '; reboot'},
    {'action': 'switch', 'target': 'wifi', 'request_id': None}])
def test_broker_rejects_arbitrary_profiles_commands_and_bad_ids(manager, message):
    assert manager.request(message)['ok'] is False
    assert manager.requests.empty()


def test_duplicate_request_is_idempotent_and_cannot_change_target(manager):
    request = {'action': 'switch', 'target': 'wifi', 'request_id': str(uuid.uuid4())}
    first = manager.request(request)
    assert first['accepted']
    assert manager.request(request) == first
    assert manager.requests.qsize() == 1
    assert not manager.request({**request, 'target': 'usb'})['ok']
    assert not manager.request({**request, 'request_id': str(uuid.uuid4())})['ok']


def test_tunnel_reconnection_does_not_lock_a_healthy_route_choice(manager):
    manager.status.update(switching=False, phase='recovering_tunnel')
    manager.switch_monotonic = 100
    manager.status['last_switch'] = {'request_id': 'previous', 'route_seconds': .05}
    message = {'action': 'switch', 'target': 'usb', 'request_id': str(uuid.uuid4())}
    assert manager.request(message)['accepted']
    assert manager.status['last_switch']['request_id'] == 'previous'
    assert manager.pending_id == message['request_id']


def test_manual_dead_path_retains_connection_and_preference(manager):
    manager.status['wifi']['internet'] = False
    assert not manager.request({'action': 'switch', 'target': 'wifi', 'request_id': str(uuid.uuid4())})['ok']
    assert manager.policy.preferred == 'auto'
    assert manager.requests.empty()


@pytest.mark.parametrize('field', ['available', 'connected', 'internet'])
@pytest.mark.parametrize('value', [False, None, 1])
def test_manual_path_requires_all_three_verified_health_flags(manager, field, value):
    manager.status['wifi'][field] = value
    assert not manager.request({'action': 'switch', 'target': 'wifi', 'request_id': str(uuid.uuid4())})['ok']
    assert manager.requests.empty()


def test_auto_request_with_no_healthy_onboard_path_is_rejected(manager):
    manager.status['usb']['internet'] = False
    manager.status['wifi']['internet'] = False
    assert not manager.request({'action': 'switch', 'target': 'auto', 'request_id': str(uuid.uuid4())})['ok']
    assert manager.requests.empty()


def test_request_outcome_survives_later_switch_and_is_bounded(manager):
    first = str(uuid.uuid4())
    manager.request_result(first, 'wifi', 'queued')
    manager.request_result(first, 'wifi', 'route_ready')
    manager.status['last_switch'] = {'request_id': str(uuid.uuid4()), 'target': 'usb'}
    assert manager.status['recent_requests'] == [{'request_id': first, 'target': 'wifi', 'state': 'route_ready'}]
    for _ in range(20):
        manager.request_result(str(uuid.uuid4()), 'usb', 'failed')
    assert len(manager.status['recent_requests']) == 16
    assert all(record['request_id'] != first for record in manager.status['recent_requests'])


def test_accepted_request_whose_path_dies_before_switch_reports_its_own_failure(manager, monkeypatch):
    request_id = str(uuid.uuid4())
    assert manager.request({'action': 'switch', 'target': 'wifi', 'request_id': request_id})['accepted']
    manager.status['last_switch'] = {'request_id': 'an-earlier-operation', 'target': 'usb'}
    # The monitor sees Wi-Fi lost after acceptance but before route mutation.
    fresh = healthy(wifi=False, wifi_connected=False)
    monkeypatch.setattr(paths, 'inspect_path', lambda name, config: fresh[name])
    monkeypatch.setattr(paths, 'isolate_arp', lambda *args: None)
    monkeypatch.setattr(paths, 'actual_route', lambda: 'usb')
    monkeypatch.setattr(paths, 'managed_choice', lambda *args: None)
    monkeypatch.setattr(manager, 'socket_loop', lambda: manager.stop.wait(5))
    monkeypatch.setattr(manager, 'standby_loop', lambda: manager.stop.wait(5))
    route_change = Mock()
    monkeypatch.setattr(paths, 'set_route', route_change)
    original_publish = manager.publish
    def publish_until_failed():
        original_publish()
        if any(record['state'] == 'failed' for record in manager.status['recent_requests']):
            manager.stop.set()
    monkeypatch.setattr(manager, 'publish', publish_until_failed)
    manager.monitor()
    assert manager.pending_id is None
    assert not manager.status['switching']
    assert manager.status['recent_requests'][-1] == {'request_id': request_id, 'target': 'wifi', 'state': 'failed'}
    route_change.assert_not_called()


def test_single_atomic_default_route_does_not_flush_other_routes(monkeypatch):
    command = Mock()
    monkeypatch.setattr(paths, 'run', command)
    monkeypatch.setattr(paths, 'actual_route', lambda: 'wifi')
    paths.set_route('wifi', healthy()['wifi'])
    command.assert_called_once_with('ip', '-4', 'route', 'replace', 'default', 'via',
                                   '192.168.125.1', 'dev', paths.WIFI, 'metric', '50', 'proto', '186')


def switch_health():
    verified = healthy()
    for name, status in verified.items():
        status['profile_uuid'] = paths.PROFILES[name]
    verified['usb'].update(device='1-2.2', usb_generation='1:4')
    verified['wifi']['bssid'] = '48:ee:0c:b3:9e:9d'
    return verified


@pytest.mark.parametrize('target,change', [
    ('usb', {'usb_generation': '1:5'}), ('usb', {'device': '1-2.3'}),
    ('usb', {'interface': 'eno1'}), ('usb', {'profile_uuid': 'other-profile'}),
    ('usb', {'gateway': '192.168.1.1'}), ('wifi', {'bssid': '00:11:22:33:44:55'}),
])
def test_switch_rejects_identity_change_after_successful_probe(manager, monkeypatch, target, change):
    verified = switch_health()
    current = {**verified[target], **change}
    monkeypatch.setattr(paths, 'network_identity', lambda *args: current)
    monkeypatch.setattr(paths, 'actual_route', lambda: 'wifi')
    monkeypatch.setattr(paths, 'tunnel_status', lambda: {'ready': True, 'connector': 'previous'})
    mutate = Mock()
    monkeypatch.setattr(paths, 'set_route', mutate)
    with pytest.raises(ValueError, match='identity changed'):
        manager.switch(target, verified, 'manual')
    mutate.assert_not_called()


@pytest.mark.parametrize('target,missing', [
    ('usb', 'device'), ('usb', 'usb_generation'), ('wifi', 'bssid'),
])
def test_switch_never_accepts_missing_identity_in_both_snapshots(manager, monkeypatch, target, missing):
    verified = switch_health()
    verified[target].pop(missing)
    monkeypatch.setattr(paths, 'network_identity', lambda *args: verified[target].copy())
    monkeypatch.setattr(paths, 'actual_route', lambda: 'wifi')
    monkeypatch.setattr(paths, 'tunnel_status', lambda: {'ready': True, 'connector': 'previous'})
    mutate = Mock()
    monkeypatch.setattr(paths, 'set_route', mutate)
    with pytest.raises(ValueError, match='identity changed'):
        manager.switch(target, verified, 'manual')
    mutate.assert_not_called()


@pytest.mark.parametrize('target', ['usb', 'wifi'])
def test_switch_with_unchanged_verified_identity_keeps_manual_paths_available(manager, monkeypatch, target):
    verified = switch_health()
    monkeypatch.setattr(paths, 'network_identity', lambda *args: verified[target].copy())
    monkeypatch.setattr(paths, 'actual_route', lambda: target)
    monkeypatch.setattr(paths, 'tunnel_status', lambda: {'ready': True, 'connector': 'previous'})
    mutate = Mock()
    monkeypatch.setattr(paths, 'set_route', mutate)
    manager.switch(target, verified, 'manual')
    mutate.assert_called_once_with(target, verified[target])
    assert manager.status['active_path'] == target and not manager.status['switching']


@pytest.mark.parametrize('change', [{'internet': False}, {'connected': False},
    {'gateway': '192.168.1.1'}, {'interface': 'lab0'}])
def test_route_mutation_requires_verified_fixed_modem_path(monkeypatch, change):
    command = Mock()
    monkeypatch.setattr(paths, 'run', command)
    with pytest.raises(ValueError):
        paths.set_route('wifi', {**healthy()['wifi'], **change})
    command.assert_not_called()


def test_wifi_wrong_profile_and_bssid_never_pass_identity(monkeypatch):
    monkeypatch.setattr(Path, 'exists', lambda self: True)
    config = {'wifi_bssid': 'ac:00:33:aa:96:34'}
    reply = Mock(returncode=0, stdout='100 (connected)\nwrong-profile\n192.168.125.1\n')
    monkeypatch.setattr(paths, 'run', lambda *a, **k: reply)
    assert not paths.network_identity('wifi', config)['connected']
    def answer(*args, **kwargs):
        return Mock(stdout=('100 (connected)\n'+paths.WIFI_PROFILE+'\n192.168.125.1\n')
                    if args[0] == 'nmcli' else 'Connected to 00:11:22:33:44:55 (on wlan0)')
    monkeypatch.setattr(paths, 'run', answer)
    assert not paths.network_identity('wifi', config)['connected']


def test_status_snapshot_cannot_mutate_manager(manager):
    report = manager.request({'action': 'status'})['status']
    report['usb']['internet'] = False
    assert manager.status['usb']['internet'] is True


def test_manager_restores_only_same_boot_routing_state(manager):
    manager.managed_path = 'wifi'
    manager.policy.usb_retry_at = 500
    manager.policy.last_switch = 200
    manager.publish()
    restored = paths.Manager(manager.config)
    assert restored.managed_path == 'wifi'
    assert restored.policy.usb_retry_at == 500
    file = paths.STATE/'routing.json'
    saved = json.loads(file.read_text())
    saved['boot_id'] = 'previous-boot'
    file.write_text(json.dumps(saved))
    fresh_boot = paths.Manager(manager.config)
    assert fresh_boot.managed_path is None
    assert fresh_boot.policy.usb_retry_at == 0


def test_unchanged_routing_state_does_not_write_flash_every_sample(manager, monkeypatch):
    manager.publish()
    writes = Mock(wraps=paths.atomic_json)
    monkeypatch.setattr(paths, 'atomic_json', writes)
    manager.publish()
    assert [call.args[0] for call in writes.call_args_list] == [paths.RUNTIME/'status.json']


def test_wifi_active_during_status_service_restart_blocks_usb_reset(monkeypatch, tmp_path):
    def response(command, **kwargs):
        return Mock(stdout=json.dumps([{'dev': paths.WIFI}]) if command[0] == 'ip' else paths.WIFI_PROFILE+'\n')
    monkeypatch.setattr(recovery.subprocess, 'run', response)
    assert recovery.onboard_hotspot_in_use(tmp_path/'missing.json')
    device = Mock()
    monkeypatch.setattr(recovery, 'modem_device', device)
    with pytest.raises(BlockingIOError):
        recovery.reset_modem()
    device.assert_not_called()


def test_unrelated_wifi_profile_does_not_claim_onboard_backup(monkeypatch, tmp_path):
    def response(command, **kwargs):
        return Mock(stdout=json.dumps([{'dev': paths.WIFI}]) if command[0] == 'ip' else 'lab-profile\n')
    monkeypatch.setattr(recovery.subprocess, 'run', response)
    assert not recovery.onboard_hotspot_in_use(tmp_path/'missing.json')
