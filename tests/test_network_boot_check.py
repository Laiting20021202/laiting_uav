import pytest

from scripts import network_boot_check as boot


def status(path='usb'):
    return {'boot_id': 'current', 'monotonic': 100, 'active_path': path, 'tunnel_ready': True,
            path: {'available': True, 'connected': True, 'internet': True,
                   'interface': boot.INTERFACES[path], 'profile_uuid': boot.PROFILES[path],
                   'gateway': '192.168.125.1', 'device': '1-2.2', 'usb_generation': '1:4'}}


@pytest.mark.parametrize('path', ['usb', 'wifi'])
def test_accepts_only_verified_onboard_route_with_local_and_public_health(path):
    assert boot.qualified(status(path), 'current', 101, path, True, True)


@pytest.mark.parametrize('change', [
    {'boot_id': 'previous'}, {'monotonic': 90}, {'monotonic': 102},
    {'monotonic': None}, {'active_path': 'wifi'}, {'tunnel_ready': False},
])
def test_stale_or_mismatched_status_cannot_pass_boot(change):
    assert not boot.qualified({**status(), **change}, 'current', 101, 'usb', True, True)


@pytest.mark.parametrize('field,value', [
    ('internet', False), ('connected', False), ('available', False),
    ('interface', 'eno1'), ('profile_uuid', 'lab'), ('gateway', '192.168.1.1'),
    ('usb_generation', None), ('device', None),
])
def test_other_networks_and_missing_usb_generation_cannot_pass(field, value):
    report = status()
    report['usb'][field] = value
    assert not boot.qualified(report, 'current', 101, 'usb', True, True)


@pytest.mark.parametrize('route,local,public', [
    ('unknown', True, True), ('none', True, True), ('usb', False, True), ('usb', True, False),
])
def test_route_and_both_http_probes_are_required(route, local, public):
    assert not boot.qualified(status(), 'current', 101, route, local, public)


def sample(now, ready=True, path='usb', generation='1:4'):
    return {'boot_seconds': now, 'ready': ready, 'active_path': path,
            'paths': {path: {'device': '1-2.2', 'usb_generation': generation}}}


def test_single_success_or_reenumeration_does_not_pass_continuous_acceptance():
    acceptance = boot.Acceptance()
    assert not acceptance.observe(sample(0))
    assert not acceptance.observe(sample(19))
    assert not acceptance.observe(sample(20, generation='1:5'))
    assert not acceptance.observe(sample(39, generation='1:5'))
    assert acceptance.observe(sample(40, generation='1:5'))


def test_outage_or_route_switch_restarts_twenty_second_acceptance():
    acceptance = boot.Acceptance()
    assert not acceptance.observe(sample(0))
    assert not acceptance.observe(sample(20, ready=False))
    assert not acceptance.observe(sample(21, path='wifi'))
    assert acceptance.observe(sample(41, path='wifi'))
