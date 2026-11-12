"""Replay the missing CDC interface gap and guard its targeted USB repair."""
import errno
from pathlib import Path

import pytest

from scripts import modem_recovery as modem


def missing_sample(**changes):
    return {
        'rx_bytes': 0, 'tx_bytes': 0, 'rx_errors': 0, 'tx_errors': 0,
        'gateway': False, 'internet': False, 'dns': False, 'carrier': False,
        'device': '1-2.2', 'usb_generation': '1:18', 'interface_present': False,
        'nm_state': None, 'nm_profile': None, 'ipv4': [],
        'cdc_driver_state': 'unbound', **changes,
    }


def transport_sample(**changes):
    return missing_sample(**{
        'interface_present': True, 'carrier': True, 'cdc_driver_state': 'bound',
        'nm_state': 100, 'nm_profile': modem.PROFILE, 'ipv4': ['192.168.125.100/24'],
        'rx_bytes': 394490, 'tx_bytes': 2787254, **changes,
    })


def modem_tree(monkeypatch, tmp_path, bound=False):
    # These are synthetic USB identities; the host's current rescue route must
    # not decide whether the simulated reset reaches its identity guards.
    monkeypatch.setattr(modem, 'onboard_hotspot_in_use', lambda: False)
    device = tmp_path/'usb'/'1-2.2'
    device.mkdir(parents=True)
    for name, value in {
        'idVendor': modem.VENDOR, 'idProduct': modem.PRODUCT,
        'serial': modem.SERIAL, 'bDeviceClass': '00', 'busnum': '1', 'devnum': '18',
    }.items():
        (device/name).write_text(value)
    driver = tmp_path/'drivers'/'cdc_ether'
    driver.mkdir(parents=True)
    pair = []
    for number, klass, subclass in (('00', '02', '06'), ('01', '0a', '00'),
                                    ('03', 'ff', '00'), ('04', 'ff', '00')):
        interface = device/(device.name+':1.'+str(int(number, 16)))
        interface.mkdir()
        for name, value in {'bInterfaceNumber': number, 'bInterfaceClass': klass,
                            'bInterfaceSubClass': subclass}.items():
            (interface/name).write_text(value)
        if number in ('00', '01'):
            pair.append(interface)
            if bound:
                (interface/'driver').symlink_to(driver)
    original_device = modem.modem_device
    monkeypatch.setattr(modem, 'modem_device',
                        lambda: original_device(tmp_path/'net', tmp_path/'usb'))
    return device, driver, pair[0], pair[1]


def simulated_driver(monkeypatch, driver, control, data, fail=None):
    """Only fake sysfs writes; never issue an actual bind/unbind/reset."""
    original_write = Path.write_text
    calls = []
    bind_count = 0

    def write(path, value, *args, **kwargs):
        nonlocal bind_count
        if path not in (driver/'bind', driver/'unbind'):
            return original_write(path, value, *args, **kwargs)
        assert value == control.name
        calls.append(path.name)
        if path == driver/'unbind':
            for interface in (control, data):
                (interface/'driver').unlink(missing_ok=True)
        else:
            bind_count += 1
            if fail:
                fail(bind_count)
            for interface in (control, data):
                (interface/'driver').symlink_to(driver)
        return len(value)

    monkeypatch.setattr(Path, 'write_text', write)
    return calls


@pytest.mark.parametrize('bound, expected', [(False, 'unbound'), (True, 'bound')])
def test_driver_evidence_requires_the_exact_complete_cdc_pair(monkeypatch, tmp_path, bound, expected):
    device, driver, _, _ = modem_tree(monkeypatch, tmp_path, bound)
    assert modem.cdc_driver_state(device, driver) == expected


@pytest.mark.parametrize('target, name, value', [
    ('control', 'bInterfaceClass', 'ff'), ('control', 'bInterfaceSubClass', '02'),
    ('control', 'bInterfaceNumber', '04'), ('data', 'bInterfaceClass', 'ff'),
    ('data', 'bInterfaceNumber', '03'),
])
def test_wrong_descriptors_are_unknown_and_cannot_authorize_bind(monkeypatch, tmp_path, target, name, value):
    device, driver, control, data = modem_tree(monkeypatch, tmp_path)
    ((control if target == 'control' else data)/name).write_text(value)
    calls = simulated_driver(monkeypatch, driver, control, data)
    assert modem.cdc_driver_state(device, driver) == 'unknown'
    with pytest.raises((OSError, ValueError)):
        modem.rebind_modem(driver)
    assert calls == []


@pytest.mark.parametrize('target, name', [
    ('control', 'bInterfaceClass'), ('control', 'bInterfaceSubClass'),
    ('data', 'bInterfaceClass'),
])
def test_missing_descriptors_cannot_authorize_missing_interface_repair(monkeypatch, tmp_path, target, name):
    device, driver, control, data = modem_tree(monkeypatch, tmp_path)
    ((control if target == 'control' else data)/name).unlink()
    calls = simulated_driver(monkeypatch, driver, control, data)
    assert modem.cdc_driver_state(device, driver) == 'unknown'
    with pytest.raises((OSError, ValueError)):
        modem.rebind_modem(driver)
    assert calls == []


@pytest.mark.parametrize('binding', ['foreign-control', 'foreign-data', 'only-control', 'only-data', 'dangling'])
def test_foreign_partial_and_dangling_drivers_are_never_mutated(monkeypatch, tmp_path, binding):
    device, driver, control, data = modem_tree(monkeypatch, tmp_path)
    foreign = tmp_path/'drivers'/'option'
    foreign.mkdir()
    if binding == 'foreign-control':
        (control/'driver').symlink_to(foreign)
    elif binding == 'foreign-data':
        (data/'driver').symlink_to(foreign)
    elif binding == 'only-control':
        (control/'driver').symlink_to(driver)
    elif binding == 'only-data':
        (data/'driver').symlink_to(driver)
    else:
        (control/'driver').symlink_to(tmp_path/'missing-driver')
    calls = simulated_driver(monkeypatch, driver, control, data)
    assert modem.cdc_driver_state(device, driver) == 'unknown'
    with pytest.raises((OSError, ValueError)):
        modem.rebind_modem(driver)
    assert calls == []


def test_verified_unbound_cdc_uses_five_seconds_and_no_nm_reconnect():
    policy = modem.RecoveryPolicy({'count': 5, 'next_action': 300})
    evidence = missing_sample()
    assert policy.observe(evidence, 0) is None
    assert policy.observe(evidence, 4.99) is None
    assert policy.observe(evidence, 5) == 'rebind'
    assert policy.pending_cdc_unbound and not policy.pending_local_fault
    assert policy.attempted(5) == 15
    assert policy.count == 5 and policy.next_action == 300


@pytest.mark.parametrize('change', [
    {'cdc_driver_state': 'unknown'}, {'cdc_driver_state': 'bound'},
    {'usb_generation': None}, {'nm_profile': 'another-profile'},
    {'nm_state': 40}, {'nm_state': 50}, {'nm_state': 90}, {'nm_state': 110},
])
def test_uncertain_or_transitioning_missing_cdc_never_escalates(change):
    policy = modem.RecoveryPolicy({'count': 2})
    evidence = missing_sample(**change)
    for now in (0, 5, 45, 300):
        assert policy.observe(evidence, now) is None


@pytest.mark.parametrize('change', [{'device': '1-2.1'}, {'usb_generation': '1:19'}])
def test_changed_generation_or_device_restarts_confirmation(change):
    policy = modem.RecoveryPolicy()
    assert policy.observe(missing_sample(), 0) is None
    changed = missing_sample(**change)
    assert policy.observe(changed, 4) is None
    assert policy.observe(changed, 5) is None
    assert policy.observe(changed, 9) == 'rebind'


def test_identity_inspection_failure_resets_confirmation():
    policy = modem.RecoveryPolicy()
    assert policy.observe(missing_sample(), 0) is None
    assert policy.observe(None, 4) is None
    assert policy.observe(missing_sample(), 5) is None
    assert policy.observe(missing_sample(), 10) == 'rebind'


def test_cdc_retries_persist_the_usb_clock_and_remain_bounded():
    policy = modem.RecoveryPolicy()
    policy.observe(missing_sample(), 0)
    assert policy.observe(missing_sample(), 5) == 'rebind'
    policy.attempted(5)
    restored = modem.RecoveryPolicy(policy.state())
    assert restored.usb_count == 1 and restored.next_usb_action == 15
    assert restored.observe(missing_sample(), 6) is None
    assert restored.observe(missing_sample(), 11) is None
    assert restored.observe(missing_sample(), 14.99) is None
    now = 15
    for delay in (20, 40, 60, 60, 60):
        assert restored.observe(missing_sample(), now) == 'rebind'
        assert restored.attempted(now)-now == delay
        assert restored.observe(missing_sample(), now+delay-0.01) is None
        now += delay
    assert restored.count == 0


def test_replay_1718_failed_bind_cannot_fall_back_to_nm_or_the_upstream_wait():
    policy = modem.RecoveryPolicy()
    first = transport_sample(rx_bytes=3994985, tx_bytes=28645631)
    policy.observe(first, 604)
    assert policy.observe({**first, 'rx_errors': 3873, 'tx_errors': 126}, 617.5) == 'reset'
    policy.attempted(617.5)
    assert policy.observe(transport_sample(internet=True, dns=True, gateway=True), 623) is None
    policy.observe(transport_sample(rx_errors=108, tx_errors=3), 649.3)
    assert policy.observe(transport_sample(rx_errors=3065, tx_errors=168), 661.1) == 'rebind'
    policy.attempted(661.1)
    assert policy.observe(missing_sample(), 666.8) is None
    assert policy.observe(missing_sample(), 671.8) is None
    assert policy.observe(missing_sample(), 681.1) == 'rebind'
    assert policy.pending_cdc_unbound
    assert policy.count == 0  # No meaningless NM reconnect for an absent device.


def test_healthy_rf_outage_and_stale_missing_cdc_evidence_never_reset_usb():
    policy = modem.RecoveryPolicy()
    rf_failure = transport_sample(gateway=True)
    for now in (0, 5, 44):
        assert policy.observe(rf_failure, now) is None
    for _ in range(8):
        now = max(45, policy.next_action)
        assert policy.observe(rf_failure, now) == 'reconnect'
        policy.attempted(now)
    assert policy.usb_count == 0
    stale = missing_sample(gateway=True)
    for now in (0, 5, 300):
        assert modem.RecoveryPolicy().observe(stale, now) is None


def test_thirty_seconds_healthy_clears_the_cdc_retry_clock():
    policy = modem.RecoveryPolicy({'usb_count': 4, 'next_usb_action': 100})
    healthy = transport_sample(internet=True, dns=True, gateway=True)
    policy.observe(healthy, 0)
    policy.observe(healthy, 29)
    assert policy.usb_count == 4
    policy.observe(healthy, 30)
    assert policy.usb_count == 0 and policy.next_usb_action == 0


@pytest.mark.parametrize('state', ['unknown', 'unbound', 'foreign-driver'])
def test_explicit_uncertain_cdc_with_present_iface_never_reaches_usb_mutation(state):
    policy = modem.RecoveryPolicy({'count': 2})
    evidence = transport_sample(cdc_driver_state=state)
    for now in (0, 5, 45, 60, 300):
        assert policy.observe({**evidence, 'rx_errors': now*100}, now) is None
    assert not policy.pending_driver_stall


def test_fully_absent_usb_waits_without_the_old_forty_five_second_reconnect():
    policy = modem.RecoveryPolicy()
    evidence = missing_sample(device=None, usb_generation=None, cdc_driver_state='unknown')
    for now in (0, 44, 45, 60, 300, 1200):
        assert policy.observe(evidence, now) is None
    assert policy.count == policy.usb_count == 0


@pytest.mark.parametrize('action', ['reset', 'rebind'])
def test_monitor_passes_the_observed_device_and_generation_to_the_child(action):
    command = modem.recovery_command(action, missing_sample())
    assert command == [modem.sys.executable, '-I', str(modem.INSTALLED), action,
                       '--expected-device', '1-2.2', '--expected-generation', '1:18']


@pytest.mark.parametrize('change', [
    {'device': None}, {'device': '../1-2.2'}, {'usb_generation': None},
    {'usb_generation': '1:0'}, {'usb_generation': '1:not-a-number'},
])
def test_monitor_refuses_usb_action_without_complete_observed_identity(change):
    with pytest.raises(ValueError):
        modem.recovery_command('rebind', missing_sample(**change))


@pytest.mark.parametrize('action', ['reset', 'rebind'])
@pytest.mark.parametrize('change', ['generation', 'port'])
def test_child_rejects_stale_monitored_usb_identity_before_any_write(monkeypatch, tmp_path, action, change):
    device, driver, control, data = modem_tree(monkeypatch, tmp_path)
    calls = simulated_driver(monkeypatch, driver, control, data)
    if change == 'generation':
        (device/'devnum').write_text('19')
    expected_device = '1-2.1' if change == 'port' else '1-2.2'
    monkeypatch.setattr(modem.fcntl, 'ioctl', lambda *args: calls.append('ioctl'))
    with pytest.raises((ValueError, RuntimeError), match='identity changed'):
        if action == 'reset':
            modem.reset_modem(expected_device=expected_device, expected_generation=(1, 18))
        else:
            modem.rebind_modem(driver, expected_device=expected_device, expected_generation=(1, 18))
    assert calls == []


def test_cli_hands_observed_identity_to_the_rebind_guard(monkeypatch):
    calls = []
    monkeypatch.setattr(modem.sys, 'argv', ['modem_recovery.py', 'rebind',
                        '--expected-device', '1-2.2', '--expected-generation', '1:18'])
    monkeypatch.setattr(modem.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(modem, 'rebind_modem', lambda **kwargs: calls.append(kwargs))
    modem.main()
    assert calls == [{'expected_device': '1-2.2', 'expected_generation': (1, 18)}]


@pytest.mark.parametrize('args', [
    ['rebind', '--expected-device', '1-2.2'],
    ['reset', '--expected-generation', '1:18'],
    ['rebind', '--expected-device', '../1-2.2', '--expected-generation', '1:18'],
    ['rebind', '--expected-device', '1-2.2', '--expected-generation', '1:0'],
    ['reconnect', '--expected-device', '1-2.2', '--expected-generation', '1:18'],
])
def test_cli_rejects_partial_invalid_or_inapplicable_observed_identity(monkeypatch, args):
    monkeypatch.setattr(modem.sys, 'argv', ['modem_recovery.py', *args])
    with pytest.raises(SystemExit) as exc:
        modem.main()
    assert exc.value.code == 2


@pytest.mark.parametrize('bound', [False, True])
def test_eproto_bind_resets_the_same_modem_then_binds_once(monkeypatch, tmp_path, bound):
    device, driver, control, data = modem_tree(monkeypatch, tmp_path, bound)

    def fail_first(count):
        if count == 1:
            raise OSError(errno.EPROTO, 'Protocol error')

    calls = simulated_driver(monkeypatch, driver, control, data, fail_first)

    def reset(**kwargs):
        assert kwargs == {'expected_device': device, 'expected_generation': (1, 18)}
        assert not (control/'driver').exists() and not (data/'driver').exists()
        calls.append('reset')

    monkeypatch.setattr(modem, 'reset_modem', reset)
    modem.rebind_modem(driver)
    assert calls == (['unbind'] if bound else [])+['bind', 'reset', 'bind']
    assert modem.cdc_driver_state(device, driver) == 'bound'


@pytest.mark.parametrize('error', [errno.EIO, errno.EBUSY, errno.EPERM, errno.ENOENT])
def test_other_bind_errors_cannot_authorize_a_usb_reset(monkeypatch, tmp_path, error):
    _, driver, control, data = modem_tree(monkeypatch, tmp_path)

    def fail(count):
        raise OSError(error, 'failure')

    calls = simulated_driver(monkeypatch, driver, control, data, fail)
    monkeypatch.setattr(modem, 'reset_modem', lambda **kwargs: calls.append('reset'))
    with pytest.raises(OSError) as exc:
        modem.rebind_modem(driver)
    assert exc.value.errno == error and calls == ['bind']


def test_second_bind_failure_is_bounded_without_another_reset(monkeypatch, tmp_path):
    _, driver, control, data = modem_tree(monkeypatch, tmp_path)

    def fail(count):
        raise OSError(errno.EPROTO, 'Protocol error')

    calls = simulated_driver(monkeypatch, driver, control, data, fail)
    monkeypatch.setattr(modem, 'reset_modem', lambda **kwargs: calls.append('reset'))
    with pytest.raises(OSError):
        modem.rebind_modem(driver)
    assert calls == ['bind', 'reset', 'bind']


@pytest.mark.parametrize('changed', ['devnum', 'busnum', 'serial', 'descriptor', 'foreign-driver'])
def test_identity_generation_or_driver_change_after_failed_bind_prevents_reset(monkeypatch, tmp_path, changed):
    device, driver, control, data = modem_tree(monkeypatch, tmp_path)
    foreign = tmp_path/'drivers'/'option'
    foreign.mkdir()

    def fail(count):
        if changed in ('devnum', 'busnum', 'serial'):
            (device/changed).write_text({'devnum': '19', 'busnum': '2', 'serial': 'other-device'}[changed])
        elif changed == 'descriptor':
            (control/'bInterfaceClass').write_text('ff')
        else:
            (control/'driver').symlink_to(foreign)
        raise OSError(errno.EPROTO, 'Protocol error')

    calls = simulated_driver(monkeypatch, driver, control, data, fail)
    monkeypatch.setattr(modem, 'reset_modem', lambda **kwargs: calls.append('reset'))
    with pytest.raises((OSError, ValueError)):
        modem.rebind_modem(driver)
    assert calls == ['bind']


def test_generation_change_during_reset_prevents_the_second_bind(monkeypatch, tmp_path):
    device, driver, control, data = modem_tree(monkeypatch, tmp_path)

    def fail(count):
        raise OSError(errno.EPROTO, 'Protocol error')

    calls = simulated_driver(monkeypatch, driver, control, data, fail)

    def reset(**kwargs):
        calls.append('reset')
        (device/'devnum').write_text('19')

    monkeypatch.setattr(modem, 'reset_modem', reset)
    with pytest.raises(ValueError, match='identity changed'):
        modem.rebind_modem(driver)
    assert calls == ['bind', 'reset']


@pytest.mark.parametrize('attribute, value', [
    ('idVendor', '2a58'), ('idProduct', 'a002'), ('serial', 'flight-controller'),
    ('bDeviceClass', '09'),
])
def test_foreign_device_and_hub_identity_are_rejected_before_any_write(monkeypatch, tmp_path, attribute, value):
    device, driver, control, data = modem_tree(monkeypatch, tmp_path)
    (device/attribute).write_text(value)
    calls = simulated_driver(monkeypatch, driver, control, data)
    monkeypatch.setattr(modem, 'reset_modem', lambda **kwargs: calls.append('reset'))
    with pytest.raises((OSError, ValueError)):
        modem.rebind_modem(driver)
    assert calls == []


def test_unbind_eproto_does_not_enter_the_bind_failure_fallback(monkeypatch, tmp_path):
    _, driver, control, _ = modem_tree(monkeypatch, tmp_path, True)
    original_write = Path.write_text
    calls = []

    def write(path, value, *args, **kwargs):
        if path == driver/'unbind':
            calls.append('unbind')
            raise OSError(errno.EPROTO, 'Protocol error')
        return original_write(path, value, *args, **kwargs)

    monkeypatch.setattr(Path, 'write_text', write)
    monkeypatch.setattr(modem, 'reset_modem', lambda **kwargs: calls.append('reset'))
    with pytest.raises(OSError):
        modem.rebind_modem(driver)
    assert calls == ['unbind'] and (control/'driver').exists()


@pytest.mark.parametrize('generation', [(2, 18), (1, 19)])
def test_reset_rejects_a_different_expected_usb_generation_before_open(monkeypatch, tmp_path, generation):
    device, _, _, _ = modem_tree(monkeypatch, tmp_path)
    calls = []
    original_open = Path.open
    monkeypatch.setattr(Path, 'open',
                        lambda path, *args, **kwargs: calls.append('open')
                        if str(path).startswith('/dev/bus/usb/')
                        else original_open(path, *args, **kwargs))
    with pytest.raises(RuntimeError, match='identity changed'):
        modem.reset_modem(expected_device=device, expected_generation=generation)
    assert calls == []


@pytest.mark.parametrize('attribute, value', [('busnum', '2'), ('devnum', '19')])
def test_reset_revalidates_bus_and_device_number_after_open(monkeypatch, tmp_path, attribute, value):
    device, _, _, _ = modem_tree(monkeypatch, tmp_path)
    calls = []

    class Stream:
        def __enter__(self):
            (device/attribute).write_text(value)
            return self

        def __exit__(self, *args):
            return False

        def fileno(self):
            return 42

    original_open = Path.open
    monkeypatch.setattr(Path, 'open',
                        lambda path, *args, **kwargs: Stream() if str(path).startswith('/dev/bus/usb/')
                        else original_open(path, *args, **kwargs))
    monkeypatch.setattr(modem.fcntl, 'ioctl', lambda *args: calls.append(args))
    with pytest.raises(RuntimeError, match='identity changed'):
        modem.reset_modem(expected_device=device, expected_generation=(1, 18))
    assert calls == []
