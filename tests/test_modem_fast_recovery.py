"""Recovery decisions for confirmed local link failures and working USB transport."""
import subprocess

import pytest

from scripts import modem_recovery as modem


def sample(**changes):
    return {
        'rx_bytes': 100, 'tx_bytes': 200, 'rx_errors': 0, 'tx_errors': 0,
        'gateway': True, 'internet': False, 'dns': False, 'carrier': True,
        'device': '1-2.2', 'usb_generation': '1:15', 'interface_present': True,
        'nm_state': 100, 'nm_profile': modem.PROFILE,
        'ipv4': ['192.168.125.100/24'], **changes,
    }


@pytest.mark.parametrize('output, expected', [
    ('100 (connected)\n'+modem.PROFILE+'\n192.168.125.100/24\n',
     {'nm_state': 100, 'nm_profile': modem.PROFILE, 'ipv4': ['192.168.125.100/24']}),
    ('100 (已連線)\n'+modem.PROFILE+'\n192.168.125.100/24\n192.168.125.101/24\n',
     {'nm_state': 100, 'nm_profile': modem.PROFILE,
      'ipv4': ['192.168.125.100/24', '192.168.125.101/24']}),
    ('30 (disconnected)\n--\n', {'nm_state': 30, 'nm_profile': None, 'ipv4': []}),
    ('120 (failed)\n\n\n', {'nm_state': 120, 'nm_profile': None, 'ipv4': []}),
])
def test_nm_state_parser_uses_only_the_fixed_modem_interface(monkeypatch, output, expected):
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, output, '')

    monkeypatch.setattr(modem.subprocess, 'run', run)
    assert modem.network_manager_state() == expected
    assert calls == [[
        'nmcli', '-t', '-g', 'GENERAL.STATE,GENERAL.CON-UUID,IP4.ADDRESS',
        'device', 'show', modem.INTERFACE,
    ]]


@pytest.mark.parametrize('failure', [
    OSError('nmcli unavailable'),
    subprocess.TimeoutExpired('nmcli', 2),
    subprocess.CalledProcessError(10, 'nmcli'),
])
def test_nm_read_failure_is_unknown_rather_than_disconnected(monkeypatch, failure):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(modem.subprocess, 'run', run)
    evidence = modem.network_manager_state()
    assert evidence['nm_state'] is None
    policy = modem.RecoveryPolicy()
    assert policy.observe(sample(**evidence), 0) is None
    assert policy.observe(sample(**evidence), 5) is None


@pytest.mark.parametrize('output, returncode', [
    ('', 0), ('connected\n'+modem.PROFILE+'\n', 0),
    ('30 (disconnected)\n--\n', 10),
])
def test_unusable_nm_output_cannot_confirm_a_fast_disconnect(monkeypatch, output, returncode):
    monkeypatch.setattr(
        modem.subprocess, 'run',
        lambda command, **kwargs: subprocess.CompletedProcess(command, returncode, output, ''),
    )
    evidence = modem.network_manager_state()
    assert evidence['nm_state'] is None
    policy = modem.RecoveryPolicy()
    assert policy.observe(sample(**evidence), 0) is None
    assert policy.observe(sample(**evidence), 5) is None


@pytest.mark.parametrize('local', [
    {'nm_state': 30, 'nm_profile': None, 'ipv4': []},
    {'nm_state': 30, 'nm_profile': modem.PROFILE, 'ipv4': []},
    {'nm_state': 120, 'nm_profile': None, 'ipv4': []},
    {'nm_state': 120, 'nm_profile': modem.PROFILE, 'ipv4': []},
    {'nm_state': 100, 'nm_profile': modem.PROFILE, 'ipv4': []},
])
def test_confirmed_local_failure_reconnects_after_five_seconds(local):
    policy = modem.RecoveryPolicy()
    evidence = sample(**local)
    assert policy.observe(evidence, 0) is None
    assert policy.observe(evidence, 4.99) is None
    assert policy.observe(evidence, 5) == 'reconnect'


@pytest.mark.parametrize('state', [40, 50, 60, 70, 80, 90, 110, None])
def test_nm_transition_or_unknown_state_is_not_a_fast_failure(state):
    policy = modem.RecoveryPolicy()
    evidence = sample(nm_state=state, ipv4=[])
    assert policy.observe(evidence, 0) is None
    assert policy.observe(evidence, 5) is None
    assert policy.observe(evidence, 10) is None


@pytest.mark.parametrize('state', [30, 100, 120])
def test_foreign_profile_cannot_trigger_fast_profile_reactivation(state):
    policy = modem.RecoveryPolicy()
    evidence = sample(nm_state=state, nm_profile='unrelated-profile', ipv4=[])
    assert policy.observe(evidence, 0) is None
    assert policy.observe(evidence, 5) is None


@pytest.mark.parametrize('change', [
    {'device': None}, {'interface_present': False}, {'carrier': False},
])
def test_fast_local_reconnect_requires_present_usb_and_carrier(change):
    policy = modem.RecoveryPolicy()
    evidence = sample(nm_state=30, nm_profile=None, ipv4=[], **change)
    assert policy.observe(evidence, 0) is None
    assert policy.observe(evidence, 5) is None


@pytest.mark.parametrize('change', [
    {'device': '1-2.1'}, {'usb_generation': '1:16'},
    {'nm_state': 120}, {'nm_profile': modem.PROFILE},
])
def test_changed_local_fault_evidence_restarts_confirmation_window(change):
    policy = modem.RecoveryPolicy()
    initial = sample(nm_state=30, nm_profile=None, ipv4=[])
    changed = {**initial, **change}
    assert policy.observe(initial, 0) is None
    assert policy.observe(changed, 4) is None
    assert policy.observe(changed, 5) is None
    assert policy.observe(changed, 9) == 'reconnect'


def test_existing_generic_backoff_does_not_delay_a_new_confirmed_local_failure():
    policy = modem.RecoveryPolicy({'count': 5, 'next_action': 300})
    evidence = sample(nm_state=30, nm_profile=None, ipv4=[])
    assert policy.observe(evidence, 0) is None
    assert policy.observe(evidence, 5) == 'reconnect'
    policy.attempted(5)
    assert policy.state()['next_local_action'] == 20
    assert policy.observe(evidence, 19.99) is None
    assert policy.observe(evidence, 20) == 'reconnect'


def test_local_retry_clock_survives_service_restart_without_usb_escalation():
    policy = modem.RecoveryPolicy()
    evidence = sample(nm_state=30, nm_profile=None, ipv4=[])
    policy.observe(evidence, 0)
    assert policy.observe(evidence, 5) == 'reconnect'
    policy.attempted(5)
    restored = modem.RecoveryPolicy(policy.state())
    assert restored.state()['next_local_action'] == 20
    assert restored.observe(evidence, 10) is None
    assert restored.observe(evidence, 15) is None
    assert restored.observe(evidence, 20) == 'reconnect'
    restored.attempted(20)
    assert restored.observe(evidence, 34) is None
    assert restored.observe(evidence, 35) == 'reconnect'


def test_thirty_seconds_healthy_clears_both_retry_clocks():
    policy = modem.RecoveryPolicy({'count': 5, 'next_action': 300, 'next_local_action': 200})
    healthy = sample(internet=True, dns=True)
    assert policy.observe(healthy, 0) is None
    assert policy.observe(healthy, 29) is None
    assert policy.state()['next_action'] == 300
    assert policy.state()['next_local_action'] == 200
    assert policy.observe(healthy, 30) is None
    assert policy.state()['next_action'] == 0
    assert policy.state()['next_local_action'] == 0


def test_working_usb_with_ipv4_and_failed_lte_never_escalates_to_usb_reset():
    policy = modem.RecoveryPolicy()
    evidence = sample()
    assert policy.observe(evidence, 0) is None
    assert policy.observe(evidence, 5) is None
    assert policy.observe(evidence, 44) is None
    for _ in range(8):
        now = max(45, policy.next_action)
        assert policy.observe(evidence, now) == 'reconnect'
        policy.attempted(now)


def test_healthy_internet_cannot_trigger_no_ip_fast_lane_from_inconsistent_nm_sample():
    policy = modem.RecoveryPolicy()
    evidence = sample(internet=True, dns=True, nm_state=30, nm_profile=None, ipv4=[])
    for now in (0, 5, 45):
        assert policy.observe(evidence, now) is None


def test_confirmed_usb_errors_and_stopped_counters_use_five_second_threshold():
    policy = modem.RecoveryPolicy()
    initial = sample(gateway=False)
    assert policy.observe(initial, 0) is None
    assert policy.observe(sample(gateway=False, rx_errors=100), 4.99) is None
    assert policy.observe(sample(gateway=False, rx_errors=200), 5) == 'reset'


@pytest.mark.parametrize('change', [
    {'rx_errors': 49}, {'rx_errors': 100, 'rx_bytes': 101},
    {'rx_errors': 100, 'gateway': True},
])
def test_five_second_usb_reset_still_requires_transport_stall_evidence(change):
    policy = modem.RecoveryPolicy()
    initial = sample(gateway=False)
    assert policy.observe(initial, 0) is None
    assert policy.observe({**initial, **change}, 5) is None


@pytest.mark.parametrize('local', [
    {'nm_state': 30, 'nm_profile': None, 'ipv4': []},
    {'nm_state': 120, 'nm_profile': None, 'ipv4': []},
    {'nm_state': 100, 'nm_profile': modem.PROFILE, 'ipv4': []},
])
def test_proven_usb_stall_takes_priority_over_coexisting_local_profile_failure(local):
    policy = modem.RecoveryPolicy()
    initial = sample(gateway=False, **local)
    assert policy.observe(initial, 0) is None
    assert policy.observe({**initial, 'rx_errors': 100}, 5) == 'reset'
    assert policy.pending_driver_stall
    assert not policy.pending_local_fault


def test_no_ip_and_unreachable_gateway_without_usb_errors_stay_reconnect_only():
    policy = modem.RecoveryPolicy()
    evidence = sample(gateway=False, nm_state=30, nm_profile=None, ipv4=[])
    assert policy.observe(evidence, 0) is None
    for now in (5, 20, 35, 50):
        assert policy.observe(evidence, now) == 'reconnect'
        policy.attempted(now)
    assert not policy.state()['recent_driver_stalls']


def test_old_generic_backoff_does_not_delay_first_proven_usb_stall():
    policy = modem.RecoveryPolicy({'count': 5, 'next_action': 300})
    initial = sample(gateway=False)
    assert policy.observe(initial, 0) is None
    assert policy.observe({**initial, 'rx_errors': 100}, 5) == 'reset'
    policy.attempted(5)
    state = policy.state()
    assert state['next_usb_action'] == 15
    assert state['usb_count'] == 1
    assert state['count'] == 5 and state['next_action'] == 300
    assert policy.observe({**initial, 'rx_errors': 200}, 14.99) is None
    assert policy.observe({**initial, 'rx_errors': 300}, 15) == 'rebind'


def test_proven_usb_retry_clock_survives_restart_and_keeps_its_own_backoff():
    policy = modem.RecoveryPolicy()
    initial = sample(gateway=False)
    assert policy.observe(initial, 0) is None
    assert policy.observe({**initial, 'rx_errors': 100}, 5) == 'reset'
    policy.attempted(5)
    assert policy.observe({**initial, 'rx_errors': 200}, 15) == 'rebind'
    policy.attempted(15)
    assert policy.state()['next_usb_action'] == 35
    restored = modem.RecoveryPolicy(policy.state())
    assert restored.state()['usb_count'] == 2
    assert restored.state()['next_usb_action'] == 35
    assert restored.observe({**initial, 'rx_errors': 300}, 29) is None
    assert restored.observe({**initial, 'rx_errors': 400}, 34) is None
    assert restored.observe({**initial, 'rx_errors': 500}, 35) == 'rebind'
    restored.attempted(35)
    assert restored.state()['next_usb_action'] == 75
