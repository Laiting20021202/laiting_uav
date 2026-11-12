import subprocess
import socket

import pytest

from scripts import modem_recovery as modem
from scripts.modem_recovery import INTERFACE, MAC, SERIAL, RecoveryPolicy, modem_device


def sample(**changes):
    return {'rx_bytes': 100, 'tx_bytes': 200, 'rx_errors': 0, 'tx_errors': 0,
            'gateway': False, 'internet': False, 'dns': False, 'carrier': True,
            'device': '1-2.1', 'interface_present': True, **changes}


def test_driver_stall_uses_fast_targeted_reset():
    policy = RecoveryPolicy()
    assert policy.observe(sample(), 0) is None
    assert policy.observe(sample(rx_errors=1754, tx_errors=24), 4) is None
    assert policy.observe(sample(rx_errors=4545, tx_errors=73), 5) == 'reset'


@pytest.mark.parametrize('changes', [
    {'gateway': True}, {'rx_bytes': 101}, {'tx_bytes': 201},
    {'device': '1-2.2'}, {'rx_errors': 49},
])
def test_fast_reset_requires_same_stalled_device_and_error_evidence(changes):
    policy = RecoveryPolicy()
    assert policy.observe(sample(), 0) is None
    assert policy.observe(sample(**{'rx_errors': 100, **changes}), 15) is None


@pytest.mark.parametrize('changes', [
    {}, {'carrier': False}, {'rx_bytes': 9000, 'tx_bytes': 8000},
    {'interface_present': False},
])
def test_other_network_outages_reconnect_then_escalate_without_error_counters(changes):
    policy = RecoveryPolicy()
    s = sample(**changes)
    assert policy.observe(s, 0) is None
    assert policy.observe(s, 44) is None
    assert policy.observe(s, 45) == 'reconnect'
    policy.attempted(45)
    assert policy.observe(s, 54) is None
    assert policy.observe(s, 55) == 'reset'


def test_missing_usb_waits_without_actions_then_recovers_when_device_returns():
    policy = RecoveryPolicy()
    absent = sample(device=None, interface_present=False, carrier=False)
    for now in range(0, 1201, 5):
        assert policy.observe(absent, now) is None
    assert policy.count == 0 and policy.next_action == 0
    assert policy.waiting_for_usb
    now = 1205
    assert policy.observe(sample(internet=True, dns=True), now) is None
    assert not policy.waiting_for_usb
    assert policy.observe(sample(internet=True, dns=True), now+30) is None
    assert policy.count == 0 and policy.next_action == 0


def test_dns_failure_never_resets_usb_or_reactivates_interface():
    policy = RecoveryPolicy()
    s = sample(internet=True, dns=False, gateway=True)
    assert policy.observe(s, 0) is None
    assert policy.observe(s, 30) == 'repair-dns'
    policy.attempted(30)
    assert policy.observe(s, 39) is None
    assert policy.observe(s, 40) == 'repair-dns'


def test_healthy_network_and_inspection_failure_cannot_trigger_actions():
    policy = RecoveryPolicy()
    for now in (0, 45, 500):
        assert policy.observe(sample(internet=True, dns=True, gateway=False), now) is None
    policy.observe(sample(), 510)
    assert policy.observe(None, 900) is None
    assert policy.observe(sample(), 910) is None
    assert policy.observe(sample(), 955) == 'reconnect'


def test_backoff_survives_daemon_restart():
    policy = RecoveryPolicy()
    policy.observe(sample(), 0)
    policy.attempted(45)
    restored = RecoveryPolicy(policy.state())
    assert restored.count == 1 and restored.next_action == 55
    assert restored.observe(sample(), 50) is None
    assert restored.observe(sample(), 90) is None
    assert restored.observe(sample(), 95) == 'reset'


def test_wifi_success_cannot_hide_missing_or_down_modem():
    for changes in ({'device': None}, {'interface_present': False}, {'carrier': False}):
        policy = RecoveryPolicy()
        apparent_wifi_success = sample(internet=True, dns=True, **changes)
        assert policy.observe(apparent_wifi_success, 0) is None
        expected = None if changes.get('device', 'present') is None else 'reconnect'
        assert policy.observe(apparent_wifi_success, 45) == expected
        assert policy.healthy_since is None


def test_recurring_stall_escalates_even_after_thirty_seconds_healthy():
    policy = RecoveryPolicy()
    policy.observe(sample(), 0)
    assert policy.observe(sample(rx_errors=100), 15) == 'reset'
    policy.attempted(15)
    healthy = sample(internet=True, dns=True, gateway=True)
    policy.observe(healthy, 25)
    policy.observe(healthy, 55)
    assert policy.count == 0
    policy = RecoveryPolicy(policy.state())
    policy.observe(sample(), 80)
    assert policy.observe(sample(rx_errors=100), 95) == 'rebind'
    policy.attempted(95)
    policy.observe(healthy, 105)
    policy.observe(healthy, 135)
    # A genuinely separate outage after the five-minute window starts at reset.
    policy.observe(sample(), 410)
    assert policy.observe(sample(rx_errors=100), 425) == 'reset'


def test_network_outage_that_later_stalls_uses_last_transport_progress():
    policy = RecoveryPolicy()
    policy.observe(sample(rx_bytes=1000, tx_bytes=2000, carrier=False), 0)
    policy.observe(sample(rx_bytes=400, tx_bytes=100, rx_errors=310, tx_errors=14), 14)
    assert policy.observe(sample(rx_bytes=400, tx_bytes=100, rx_errors=2381, tx_errors=126), 18) is None
    assert policy.observe(sample(rx_bytes=400, tx_bytes=100, rx_errors=4233, tx_errors=232), 21) == 'reset'


def test_usb_reenumeration_restarts_stall_evidence_without_resetting_outage_window():
    policy = RecoveryPolicy()
    policy.observe(sample(usb_generation='1:4'), 0)
    assert policy.observe(sample(rx_errors=100, usb_generation='1:5'), 15) is None
    assert policy.observe(sample(rx_errors=200, usb_generation='1:5'), 19) is None
    assert policy.observe(sample(rx_errors=300, usb_generation='1:5'), 20) == 'reset'


def test_bound_probe_cannot_fall_back_to_working_loopback_connection():
    # A real reachable TCP listener represents a working alternate route.
    # Binding to a missing mobile interface must fail instead of using it.
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(2)
        port = listener.getsockname()[1]
        assert modem.connect('127.0.0.1', port)
        assert not modem.connect('127.0.0.1', port, interface='uav-missing0')


def make_device(tmp_path):
    net = tmp_path/'net'/INTERFACE
    net.mkdir(parents=True)
    usb = tmp_path/'usb'/'1-2.1'
    interface = usb/'1-2.1:1.0'
    interface.mkdir(parents=True)
    (net/'device').symlink_to(interface)
    (net/'address').write_text(MAC)
    for name, value in {'idVendor': '1286', 'idProduct': '4e31',
                        'serial': SERIAL, 'bDeviceClass': '00'}.items():
        (usb/name).write_text(value)
    return net, usb


def test_exact_modem_identity_and_hub_rejection(tmp_path):
    net, usb = make_device(tmp_path)
    assert modem_device(tmp_path/'net') == usb
    for file, bad in [('bDeviceClass', '09'), ('idProduct', '0001'), ('serial', 'other-device')]:
        before = (usb/file).read_text()
        (usb/file).write_text(bad)
        with pytest.raises(ValueError):
            modem_device(tmp_path/'net')
        (usb/file).write_text(before)
    (net/'address').write_text('00:00:00:00:00:00')
    with pytest.raises(ValueError):
        modem_device(tmp_path/'net')


def test_missing_interface_finds_only_same_unique_serial(tmp_path):
    net, usb = make_device(tmp_path)
    (net/'address').unlink()
    (net/'device').unlink()
    assert modem_device(tmp_path/'net', tmp_path/'usb') == usb
    (usb/'serial').write_text('another-device')
    with pytest.raises(FileNotFoundError):
        modem_device(tmp_path/'net', tmp_path/'usb')


def test_camera_profile_must_never_be_activated(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, 'eno1\n', '')
    monkeypatch.setattr(modem.subprocess, 'run', run)
    with pytest.raises(ValueError):
        modem.reconnect_modem()
    assert len(calls) == 1


def test_reconnect_command_is_fixed_to_mobile_interface_and_uuid(monkeypatch):
    monkeypatch.setattr(modem, 'modem_profile_check', lambda: None)
    monkeypatch.setattr(modem, 'modem_device', lambda: 'verified-modem')
    monkeypatch.setattr(modem, 'modem_interface_present', lambda: True)
    calls = []
    monkeypatch.setattr(modem.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    modem.reconnect_modem()
    assert calls == [['nmcli', '--wait', '20', 'connection', 'up', 'uuid', modem.PROFILE,
                      'ifname', INTERFACE]]


def test_dns_repair_never_changes_camera_profile(monkeypatch):
    monkeypatch.setattr(modem, 'modem_profile_check', lambda: None)
    monkeypatch.setattr(modem, 'dns_ready', lambda *args: True)
    calls = []
    monkeypatch.setattr(modem.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    modem.repair_dns()
    assert calls == [['resolvectl', 'flush-caches'], ['resolvectl', 'reset-server-features'],
                     ['nmcli', 'device', 'reapply', INTERFACE]]


def test_repeated_outage_cycles_reconnect_reset_and_rebind_indefinitely():
    policy = RecoveryPolicy()
    policy.observe(sample(), 0)
    actions = []
    for _ in range(20):
        now = max(45, policy.next_action)
        actions.append(policy.observe(sample(), now))
        policy.attempted(now)
    assert actions[:7] == ['reconnect', 'reset', 'rebind', 'reconnect', 'reset', 'rebind', 'reconnect']
    assert actions[-1] is not None and policy.count == 20


def test_rebind_only_touches_cdc_interface_and_rebinds_after_unbind(monkeypatch, tmp_path):
    _, device = make_device(tmp_path)
    driver = tmp_path/'drivers'/'cdc_ether'
    driver.mkdir(parents=True)
    interface = device/(device.name+':1.0')
    (interface/'bInterfaceNumber').write_text('00')
    (interface/'driver').symlink_to(driver)
    monkeypatch.setattr(modem, 'modem_device', lambda: device)
    from pathlib import Path
    original = Path.write_text
    writes = []
    def write(path, data, *args, **kwargs):
        writes.append((path, data))
        if path == driver/'unbind':
            (interface/'driver').unlink()
        return original(path, data, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', write)
    modem.rebind_modem(driver)
    assert writes == [(driver/'unbind', '1-2.1:1.0'), (driver/'bind', '1-2.1:1.0')]
