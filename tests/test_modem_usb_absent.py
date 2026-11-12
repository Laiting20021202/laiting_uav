"""A missing USB device is not repairable through NM or an arbitrary USB port."""
from pathlib import Path
from unittest.mock import Mock

import pytest

from scripts import modem_recovery as modem


def sample(**changes):
    return {
        'rx_bytes': 0, 'tx_bytes': 0, 'rx_errors': 0, 'tx_errors': 0,
        'gateway': False, 'internet': False, 'dns': False, 'carrier': False,
        'device': None, 'usb_generation': None, 'interface_present': False,
        'nm_state': None, 'nm_profile': None, 'ipv4': [],
        'cdc_driver_state': 'unknown', **changes,
    }


def returned_sample(**changes):
    return sample(**{
        'device': '1-2.2', 'usb_generation': '1:19', 'interface_present': True,
        'carrier': True, 'cdc_driver_state': 'bound',
        'nm_state': 30, 'nm_profile': None, **changes,
    })


@pytest.mark.parametrize('state', [
    {}, {'count': 2, 'next_action': 0},
    {'count': 10, 'next_action': 45, 'usb_count': 3, 'next_usb_action': 60},
])
def test_absent_usb_never_activates_nm_or_reaches_reset_cycles(state):
    policy = modem.RecoveryPolicy(state)
    for now in (0, 5, 44, 45, 60, 300, 3600):
        assert policy.observe(sample(), now) is None
        assert not policy.pending_local_fault
        assert not policy.pending_driver_stall
        assert not policy.pending_cdc_unbound
        assert policy.failed_since is None
    assert policy.state()['count'] == state.get('count', 0)
    assert policy.state()['usb_count'] == state.get('usb_count', 0)


def test_absence_discards_stale_outage_and_stall_evidence():
    policy = modem.RecoveryPolicy()
    policy.observe(returned_sample(nm_state=100, nm_profile=modem.PROFILE,
                                  ipv4=['192.168.125.100/24']), 0)
    assert policy.observe(sample(), 46) is None
    assert policy.baseline is None and policy.failed_since is None
    assert policy.observe(returned_sample(nm_state=100, nm_profile=modem.PROFILE,
                                         ipv4=['192.168.125.100/24']), 50) is None
    assert policy.observe(returned_sample(nm_state=100, nm_profile=modem.PROFILE,
                                         ipv4=['192.168.125.100/24']), 94) is None
    assert policy.observe(returned_sample(nm_state=100, nm_profile=modem.PROFILE,
                                         ipv4=['192.168.125.100/24']), 95) == 'reconnect'


@pytest.mark.parametrize('local', [
    {'nm_state': 30, 'nm_profile': None},
    {'nm_state': 120, 'nm_profile': modem.PROFILE},
    {'nm_state': 100, 'nm_profile': modem.PROFILE, 'ipv4': []},
])
def test_identified_interface_return_activates_fixed_profile_without_old_wait(local):
    policy = modem.RecoveryPolicy({'count': 4, 'next_action': 600,
                                   'local_count': 2, 'next_local_action': 600})
    assert policy.observe(sample(), 0) is None
    assert policy.observe(returned_sample(**local), 1) == 'reconnect'
    assert policy.pending_local_fault
    assert policy.attempted(1) == 16
    assert policy.count == 4 and policy.next_action == 600
    assert policy.observe(returned_sample(**local), 2) is None
    assert policy.observe(returned_sample(**local), 16) == 'reconnect'


def test_nm_transition_after_reenumeration_finishes_before_profile_activation():
    policy = modem.RecoveryPolicy()
    policy.observe(sample(), 0)
    for now in (1, 10, 20):
        assert policy.observe(returned_sample(nm_state=70), now) is None
    assert policy.observe(returned_sample(), 21) == 'reconnect'


@pytest.mark.parametrize('change', [
    {'carrier': False}, {'cdc_driver_state': 'unknown'},
    {'usb_generation': None}, {'nm_profile': 'foreign-profile'},
])
def test_return_activation_requires_identified_cdc_and_confirmed_local_failure(change):
    policy = modem.RecoveryPolicy()
    policy.observe(sample(), 0)
    assert policy.observe(returned_sample(**change), 1) is None


def test_missing_interface_still_requires_strict_cdc_bind_and_confirmation():
    policy = modem.RecoveryPolicy()
    policy.observe(sample(), 0)
    unbound = returned_sample(interface_present=False, carrier=False,
                              cdc_driver_state='unbound', nm_state=None)
    assert policy.observe(unbound, 1) is None
    assert policy.observe(unbound, 5.99) is None
    assert policy.observe(unbound, 6) == 'rebind'
    assert policy.pending_cdc_unbound
    policy.attempted(6)
    assert policy.observe(returned_sample(), 7) == 'reconnect'


def test_absent_usb_inspection_is_passive_and_does_not_launch_network_probes(monkeypatch):
    def missing():
        raise FileNotFoundError('No matching USB device')

    def forbidden(*args, **kwargs):
        pytest.fail('Absent USB must not run NetworkManager or network probes')

    monkeypatch.setattr(modem, 'modem_device', missing)
    for name in ('network_manager_state', 'connect', 'dns_ready', 'public_ready'):
        monkeypatch.setattr(modem, name, forbidden)
    evidence = modem.inspect_modem()
    assert evidence['device'] is None and evidence['usb_generation'] is None
    assert not evidence['internet'] and not evidence['gateway']
    assert evidence['nm_state'] is None


def test_waiting_log_is_limited_and_reenumeration_is_reported(monkeypatch):
    log = Mock()
    monkeypatch.setattr(modem, 'LOG', log)
    reporter = modem.RecoveryHealthReporter()
    for now in range(0, 121, 5):
        reporter.report(sample(), now)
    reporter.report(returned_sample(), 121)
    reporter.report(sample(), 122)
    messages = [call.args[0] % call.args[1:] for call in log.info.call_args_list]
    waits = [message for message in messages if 'waiting for USB re-enumeration' in message]
    assert len(waits) == 4  # 0, 60, 120, and the new disappearance at 122.
    assert any('Verified USB modem re-enumerated' in message for message in messages)
    assert len([message for message in messages if 'Modem health evidence:' in message]) == 1


def test_reconnect_revalidates_presence_before_nm_activation(monkeypatch):
    monkeypatch.setattr(modem, 'modem_profile_check', lambda: None)
    monkeypatch.setattr(modem, 'modem_device', lambda: Path('/verified-modem'))
    monkeypatch.setattr(modem, 'modem_interface_present', lambda: False)
    calls = []
    monkeypatch.setattr(modem.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    with pytest.raises(FileNotFoundError, match='unavailable before activation'):
        modem.reconnect_modem()
    assert calls == []


def test_reconnect_rejects_unidentified_usb_before_nm_activation(monkeypatch):
    monkeypatch.setattr(modem, 'modem_profile_check', lambda: None)

    def missing():
        raise FileNotFoundError('USB gone before child activation')

    monkeypatch.setattr(modem, 'modem_device', missing)
    calls = []
    monkeypatch.setattr(modem.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    with pytest.raises(FileNotFoundError):
        modem.reconnect_modem()
    assert calls == []


def test_network_presence_uses_only_fixed_mac(tmp_path):
    assert not modem.modem_interface_present(tmp_path)
    interface = tmp_path/modem.INTERFACE
    interface.mkdir()
    (interface/'address').write_text(modem.MAC)
    assert modem.modem_interface_present(tmp_path)
    (interface/'address').write_text('00:00:00:00:00:00')
    with pytest.raises(ValueError, match='MAC changed'):
        modem.modem_interface_present(tmp_path)


def test_monitor_does_not_spawn_recovery_children_for_an_absent_device(monkeypatch):
    class Stopped:
        def __init__(self):
            self.iterations = 0

        def is_set(self):
            return self.iterations >= 5

        def set(self):
            self.iterations = 5

        def wait(self, delay):
            assert delay == 5
            self.iterations += 1

    stopped = Stopped()
    monkeypatch.setattr(modem.threading, 'Event', lambda: stopped)
    monkeypatch.setattr(modem.signal, 'signal', lambda *args: None)
    monkeypatch.setattr(modem.time, 'monotonic', lambda: stopped.iterations*60)
    monkeypatch.setattr(modem, 'inspect_modem', sample)
    monkeypatch.setattr(modem, 'notify', lambda *args: None)
    calls = []
    monkeypatch.setattr(modem.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    monkeypatch.setattr(modem, 'save_attempts', lambda *args: pytest.fail('No absent-device attempts'))
    modem.monitor()
    assert calls == []
