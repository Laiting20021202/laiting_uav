"""Prevent unintended Wi-Fi connections and secret leakage during AP setup."""
import hashlib
import json
from pathlib import Path

import pytest

from scripts import configure_modem_hotspot as setup

SSID = 'Owned modem'
PSK = 'Sample-Key9!'
BSSID = '02:12:34:56:78:9A'
PIN = hashlib.sha256(b'test certificate').hexdigest()


def hotspot_xml(ssid=SSID, psk=PSK, mode='Mixed', enabled='1'):
    encoded = ssid.encode('utf-16-be').hex()
    return ('<RGW><wlan_settings><wlan_enable>'+enabled+'</wlan_enable></wlan_settings>'
            '<wlan_security><ssid>'+encoded+'</ssid><mode>'+mode+'</mode><pwd>'+psk+
            '</pwd></wlan_security><sysinfo><mac>'+BSSID+'</mac></sysinfo></RGW>').encode()


class Client:
    def __init__(self, body=None, failure=None):
        self.body = body if body is not None else hotspot_xml()
        self.failure = failure
        self.auth = {'tls_sha256': PIN}
        self.logins = 0
        self.requests = []

    def login(self):
        self.logins += 1
        if self.failure:
            raise self.failure

    def header(self):
        return 'fake digest'

    def request(self, path, header):
        self.requests.append((path, header))
        return {}, self.body


def scan_row(bssid=BSSID, ssid=SSID, security='WPA1 WPA2'):
    def escaped(text):
        return text.replace('\\', '\\\\').replace(':', '\\:')
    return ':'.join(escaped(value) for value in [bssid, ssid, security])+'\n'


def fake_install(monkeypatch, tmp_path, scan=None, gateway=setup.GATEWAY,
                 wifi_ip='192.168.125.101/24', peer_error=None):
    keyfile = tmp_path/'etc'/'system-connections'/'owned.nmconnection'
    auth = tmp_path/'auth.json'
    auth.write_text(json.dumps({'username': 'admin'}))
    auth.chmod(0o600)
    for name, value in {
        'AUTH': auth, 'KEYFILE': keyfile,
        'ALLOW': tmp_path/'etc'/'conf.d'/'95-onboard-only.conf',
        'PATHS': tmp_path/'etc'/'modem-paths.json', 'BACKUPS': tmp_path/'backups',
    }.items():
        monkeypatch.setattr(setup, name, value)
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(setup, 'RadioClient', lambda auth: Client())
    monkeypatch.setattr(setup.time, 'sleep', lambda seconds: None)
    calls, peers = [], []
    scan_reads = 0

    def command(arguments, **kwargs):
        nonlocal scan_reads
        assert PSK not in arguments
        calls.append(list(arguments))
        if arguments[1:4] == ['-g', 'connection.interface-name', 'connection']:
            return setup.USB_INTERFACE+'\n'
        if arguments[1:4] == ['-t', '-f', 'UUID,TYPE,AUTOCONNECT']:
            return ('11111111-1111-1111-1111-111111111111:802-11-wireless:yes\n'
                    '22222222-2222-2222-2222-222222222222:802-11-wireless:no\n'+
                    setup.USB_UUID+':802-3-ethernet:yes\n')
        if arguments[1:4] == ['-t', '-f', 'BSSID,SSID,SECURITY']:
            return scan if scan is not None else scan_row()
        if arguments[1:4] == ['-t', '-g', 'IP4.GATEWAY']:
            return gateway+'\n'
        if arguments[1:4] == ['-t', '-g', 'IP4.ADDRESS']:
            return (wifi_ip if arguments[-1] == setup.WIFI_INTERFACE else '192.168.125.100/24')+'\n'
        if arguments[1:4] == ['-t', '-g', 'GENERAL.STATE']:
            return '30 (disconnected)\n'
        if arguments[1:4] == ['-t', '-g', 'GENERAL.DBUS-PATH']:
            return '/org/freedesktop/NetworkManager/Devices/5\n'
        if arguments[0] == 'gdbus':
            scan_reads += 1
            return '(<int64 '+str(scan_reads)+'>,)\n'
        if arguments[1:4] == ['connection', 'load', str(keyfile)]:
            assert keyfile.stat().st_mode & 0o777 == 0o600
        return ''

    def peer(pin):
        peers.append(pin)
        if peer_error:
            raise peer_error

    monkeypatch.setattr(setup, 'command', command)
    monkeypatch.setattr(setup, 'verify_wifi_peer', peer)
    return calls, peers


def test_authenticated_read_uses_one_session_and_active_security_only():
    client = Client()
    result = setup.read_hotspot(client)
    assert result == {'ssid': SSID, 'psk': PSK, 'tls_sha256': PIN, 'router_mac': BSSID}
    assert client.logins == 1
    assert client.requests == [('/xml_action.cgi?method=get&module=duster&file=status1', 'fake digest')]


def test_rejected_login_is_not_retried():
    client = Client(failure=PermissionError('rejected'))
    with pytest.raises(PermissionError):
        setup.read_hotspot(client)
    assert client.logins == 1 and client.requests == []


@pytest.mark.parametrize('changes', [
    {'enabled': '0'}, {'mode': 'Open'}, {'mode': 'WPA3-SAE'}, {'psk': ''},
    {'psk': 'short'}, {'psk': 'g'*64},
])
def test_unusable_or_unexpected_ap_metadata_fails_closed(changes):
    with pytest.raises(ValueError):
        setup.read_hotspot(Client(hotspot_xml(**changes)))


def test_valid_64_hex_psk_is_preserved_without_interpreting_it_as_a_passphrase():
    key = 'aB'*32
    assert setup.read_hotspot(Client(hotspot_xml(psk=key)))['psk'] == key


@pytest.mark.parametrize('ssid', ['Owned modem', 'D-Link_123456', '飛行:備援', 'a'*32])
def test_ssid_decoding_follows_vendor_utf16_code_units(ssid):
    assert setup.decode_ssid(ssid.encode('utf-16-be').hex()) == ssid


@pytest.mark.parametrize('encoded', ['', 'not-hex', '123', '0000', ('x'*33).encode('utf-16-be').hex(), 'd800'])
def test_invalid_or_oversized_ssid_cannot_be_used(encoded):
    with pytest.raises((ValueError, UnicodeError)):
        setup.decode_ssid(encoded)


def test_scan_ignores_external_ssids_and_pins_the_unique_secure_onboard_ap():
    output = scan_row('02:AA:BB:CC:DD:EE', 'Laboratory')+scan_row(ssid='Owned:modem')
    assert setup.select_bssid(output, 'Owned:modem') == BSSID


@pytest.mark.parametrize('output', [
    '', scan_row(ssid='Laboratory'), scan_row()+scan_row('02:AA:BB:CC:DD:EE'),
    scan_row(security='--'), scan_row('01:12:34:56:78:9A'), scan_row('00:00:00:00:00:00'),
])
def test_absent_ambiguous_open_or_invalid_ap_is_never_selected(output):
    with pytest.raises(ValueError):
        setup.select_bssid(output, SSID)


def test_keyfile_escapes_secrets_and_cannot_inject_sections():
    value = {'ssid': 'Owned\n[ipv4]', 'psk': ' x\\y\n[connection] '}
    result = setup.render_keyfile(value, BSSID)
    assert '\n[ipv4]\n' in result  # Only the intended ipv4 section.
    assert result.count('\n[connection]') == 0
    assert 'ssid=Owned\\n[ipv4]' in result
    assert 'psk=\\sx\\\\y\\n[connection]\\s' in result
    assert 'autoconnect=false' in result and 'powersave=2' in result


def test_success_disables_existing_profiles_before_radio_and_pins_before_association(monkeypatch, tmp_path):
    calls, peers = fake_install(monkeypatch, tmp_path)
    report = setup.install()
    assert report['configured'] and report['wifi_ipv4'] == ['192.168.125.101/24']
    assert PSK not in json.dumps(report)
    config = json.loads(setup.PATHS.read_text())
    assert config['wifi_profile'] == setup.WIFI_UUID and config['wifi_bssid'] == BSSID
    assert PSK not in setup.PATHS.read_text()
    assert setup.PATHS.stat().st_mode & 0o777 == 0o600
    assert 'allowed-connections=uuid:'+setup.WIFI_UUID in setup.ALLOW.read_text()
    radio_on = calls.index(['nmcli', 'radio', 'wifi', 'on'])
    reload_conf = calls.index(['nmcli', 'general', 'reload', 'conf'])
    disable_existing = [i for i, c in enumerate(calls) if 'connection.autoconnect' in c and c[-1] == 'no']
    assert disable_existing and max(disable_existing) < reload_conf < radio_on
    assert calls.index(['nmcli', 'connection', 'load', str(setup.KEYFILE)]) < radio_on
    up = next(i for i, c in enumerate(calls) if c[1:4] == ['--wait', '25', 'connection'])
    assert 'bssid='+BSSID in setup.KEYFILE.read_text()
    assert radio_on < up
    assert peers == [PIN]
    assert calls[-1] == ['nmcli', 'connection', 'modify', 'uuid', setup.WIFI_UUID, 'connection.autoconnect', 'yes']
    assert not any('down' in c or (USB_INTERFACE in c and 'up' in c) for c in calls for USB_INTERFACE in [setup.USB_INTERFACE])


@pytest.mark.parametrize('failure', ['ambiguous', 'gateway', 'duplicate-ip', 'peer'])
def test_failed_setup_keeps_external_wifi_prohibited_and_never_restores_old_profiles(monkeypatch, tmp_path, failure):
    kwargs = {
        'ambiguous': {'scan': scan_row()+scan_row('02:AA:BB:CC:DD:EE')},
        'gateway': {'gateway': '192.168.0.1'},
        'duplicate-ip': {'wifi_ip': '192.168.125.100/24'},
        'peer': {'peer_error': ValueError('mismatched modem identity')},
    }[failure]
    calls, _ = fake_install(monkeypatch, tmp_path, **kwargs)
    with pytest.raises(setup.SetupStageError):
        setup.install()
    assert calls[-1] == ['nmcli', 'radio', 'wifi', 'off']
    assert 'allowed-connections=uuid:'+setup.WIFI_UUID in setup.ALLOW.read_text()
    assert not setup.PATHS.exists()
    assert not any('connection.autoconnect' in c and c[-1] == 'yes' for c in calls)


def test_private_backup_preserves_configuration_without_printing_secrets(monkeypatch, tmp_path):
    fake_install(monkeypatch, tmp_path)
    setup.KEYFILE.parent.mkdir(parents=True)
    setup.KEYFILE.write_text('psk='+PSK+'\n')
    setup.KEYFILE.chmod(0o600)
    backup = setup.backup_configuration([{'uuid': setup.WIFI_UUID, 'autoconnect': 'no'}])
    assert backup.stat().st_mode & 0o777 == 0o700
    copied = backup/str(setup.KEYFILE).lstrip('/')
    assert copied.read_bytes() == setup.KEYFILE.read_bytes()
    assert copied.stat().st_mode & 0o777 == 0o600
    assert PSK not in (backup/'manifest.json').read_text()


def test_configuration_symlinks_are_rejected(monkeypatch, tmp_path):
    fake_install(monkeypatch, tmp_path)
    target = tmp_path/'other'
    target.write_text('keep')
    setup.PATHS.parent.mkdir(parents=True)
    setup.PATHS.symlink_to(target)
    with pytest.raises(ValueError):
        setup.atomic_write(setup.PATHS, 'replace')
    assert target.read_text() == 'keep'


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        assert 0 < seconds <= 1
        self.sleeps.append(seconds)
        self.now += seconds


def test_radio_enable_waits_through_unavailable_and_activating_states(monkeypatch):
    clock = Clock()
    replies = iter(['20 (unavailable)\n', '40 (prepare)\n', '30 (disconnected)\n'])
    calls = []

    def command(arguments, **kwargs):
        calls.append(arguments)
        return next(replies)

    monkeypatch.setattr(setup, 'command', command)
    setup.wait_wifi_ready(3, clock=clock.time, sleep=clock.sleep)
    assert clock.now == 1
    assert len(calls) == 3
    assert all(c[3] == 'GENERAL.STATE' for c in calls)


def test_unavailable_wifi_times_out_without_issuing_a_scan(monkeypatch):
    clock = Clock()
    calls = []

    def command(arguments, **kwargs):
        calls.append(arguments)
        return '20 (unavailable)\n'

    monkeypatch.setattr(setup, 'command', command)
    with pytest.raises(setup.SetupStageError) as exc:
        setup.wait_wifi_ready(2, clock=clock.time, sleep=clock.sleep)
    assert exc.value.stage == 'wifi-readiness' and exc.value.cause_type == 'Timeout'
    assert clock.now == 2 and len(calls) == 4


def test_scan_retries_only_the_two_installed_nm_transient_reasons_then_waits_for_completion(monkeypatch):
    clock = Clock()
    scan_replies = iter([
        setup.NMCommandError(1, 'Error: Scanning not allowed while unavailable.'),
        setup.NMCommandError(1, 'Error: Scanning not allowed at this time.'), '',
    ])
    completion = iter([10, 10, 11])
    requests = []

    def command(arguments, **kwargs):
        if 'GENERAL.DBUS-PATH' in arguments:
            return '/org/freedesktop/NetworkManager/Devices/5\n'
        if 'GENERAL.STATE' in arguments:
            return '30 (disconnected)\n'
        if arguments[0] == 'gdbus':
            return '(<int64 '+str(next(completion))+'>,)\n'
        requests.append(arguments)
        reply = next(scan_replies)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(setup, 'command', command)
    setup.request_wifi_scan(SSID, 5, clock=clock.time, sleep=clock.sleep)
    assert len(requests) == 3 and clock.now == 2.5
    assert all(c == ['nmcli', 'device', 'wifi', 'rescan', 'ifname', setup.WIFI_INTERFACE,
                     'ssid', SSID] for c in requests)


def test_nontransient_scan_failure_is_immediate_and_does_not_disclose_stderr(monkeypatch):
    clock = Clock()
    scans = []

    def command(arguments, **kwargs):
        if 'GENERAL.DBUS-PATH' in arguments:
            return '/org/freedesktop/NetworkManager/Devices/5\n'
        if 'GENERAL.STATE' in arguments:
            return '30 (disconnected)\n'
        if arguments[0] == 'gdbus':
            return '(<int64 10>,)\n'
        scans.append(arguments)
        raise setup.NMCommandError(1, 'Permission denied '+PSK)

    monkeypatch.setattr(setup, 'command', command)
    with pytest.raises(setup.SetupStageError) as exc:
        setup.request_wifi_scan(SSID, 5, clock=clock.time, sleep=clock.sleep)
    assert exc.value.stage == 'wifi-scan-request'
    assert PSK not in str(exc.value) and len(scans) == 1 and clock.now == 0


def test_accepted_scan_cannot_succeed_from_a_stale_last_scan_counter(monkeypatch):
    clock = Clock()
    scans = []

    def command(arguments, **kwargs):
        if 'GENERAL.DBUS-PATH' in arguments:
            return '/org/freedesktop/NetworkManager/Devices/5\n'
        if 'GENERAL.STATE' in arguments:
            return '30 (disconnected)\n'
        if arguments[0] == 'gdbus':
            return '(<int64 10>,)\n'
        scans.append(arguments)
        return ''

    monkeypatch.setattr(setup, 'command', command)
    with pytest.raises(setup.SetupStageError) as exc:
        setup.request_wifi_scan(SSID, 3, clock=clock.time, sleep=clock.sleep)
    assert exc.value.stage == 'wifi-scan-completion' and exc.value.cause_type == 'Timeout'
    assert len(scans) == 1 and clock.now == 3


def test_transient_scan_rejection_is_also_bounded(monkeypatch):
    clock = Clock()
    scans = []

    def command(arguments, **kwargs):
        if 'GENERAL.DBUS-PATH' in arguments:
            return '/org/freedesktop/NetworkManager/Devices/5\n'
        if 'GENERAL.STATE' in arguments:
            return '30 (disconnected)\n'
        if arguments[0] == 'gdbus':
            return '(<int64 -1>,)\n'
        scans.append(arguments)
        raise setup.NMCommandError(1, 'Scanning not allowed at this time')

    monkeypatch.setattr(setup, 'command', command)
    with pytest.raises(setup.SetupStageError) as exc:
        setup.request_wifi_scan(SSID, 3, clock=clock.time, sleep=clock.sleep)
    assert exc.value.stage == 'wifi-scan-request' and exc.value.cause_type == 'Timeout'
    assert len(scans) == 3 and clock.now == 3


def test_stage_reporting_never_prints_private_exception_text(monkeypatch, capsys):
    monkeypatch.setattr(setup.sys, 'argv', ['configure_modem_hotspot.py'])

    def install():
        raise setup.SetupStageError('wifi-scan-request', 'NMCommandError') from RuntimeError(PSK)

    monkeypatch.setattr(setup, 'install', install)
    assert setup.main() == 1
    output = capsys.readouterr().err
    assert PSK not in output
    assert json.loads(output)['stage'] == 'wifi-scan-request'
