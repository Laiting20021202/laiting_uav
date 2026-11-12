import hashlib

import pytest

from scripts.modem_radio import RadioClient, parse_radio
from scripts import modem_radio


def test_lte_signal_conversion_uses_device_encoding_and_limits_saved_fields():
    radio = parse_radio(b'''<RGW><wan><sys_mode>17</sys_mode><rssi>71</rssi>
        <rsrp>46</rsrp><rsrq>18</rsrq><sinr>7.5</sinr><wifi_password>secret</wifi_password>
        <wan_conn_status>connected</wan_conn_status></wan></RGW>''')
    assert radio['rssi_dbm'] == -70
    assert radio['rsrp_dbm'] == -95
    assert radio['rsrq_db'] == -11
    assert radio['sinr_db'] == 7.5
    assert 'secret' not in str(radio)


def test_missing_or_unknown_signal_is_not_reported_as_healthy_lte():
    radio = parse_radio(b'<RGW><wan><sys_mode>17</sys_mode><rsrp>255</rsrp><sinr>NaN</sinr></wan></RGW>')
    assert all(radio[k] is None for k in ('rssi_dbm', 'rsrp_dbm', 'rsrq_db', 'sinr_db'))
    radio = parse_radio(b'<RGW><wan><sys_mode>5</sys_mode><rsrp>46</rsrp></wan></RGW>')
    assert radio['rsrp_dbm'] is None


def test_active_pdp_address_overrides_placeholder_wan_ip_without_exposing_sim_credentials():
    radio = parse_radio(b'''<RGW><wan><ip>0.0.0.0</ip><sys_mode>17</sys_mode>
        <cellular><pdp_context_list><Item><rulename>PDN1</rulename><connnum>1</connnum>
        <success>1</success><default>1</default><ipv4>10.197.148.3</ipv4><ipv6>NA</ipv6>
        <password>private-password</password><IMSI>private-sim-id</IMSI>
        </Item></pdp_context_list></cellular></wan></RGW>''')
    assert radio['cellular_connected'] is True
    assert radio['wan_ip'] == '10.197.148.3'
    assert radio['pdp'][0]['ipv6'] is None
    assert 'private-' not in str(radio)


def test_failed_pdp_and_placeholder_addresses_do_not_imply_cellular_connection():
    radio = parse_radio(b'''<RGW><wan><sys_mode>17</sys_mode><cellular><pdp_context_list>
        <Item><success>0</success><default>1</default><ipv4>10.1.2.3</ipv4></Item>
        <Item><success>1</success><default>0</default><ipv4>0.0.0.0</ipv4><ipv6>::</ipv6></Item>
        </pdp_context_list></cellular></wan></RGW>''')
    assert radio['cellular_connected'] is False
    assert radio['wan_ip'] is None


@pytest.mark.parametrize('status', ['UNAUTHORIZED', 'KICKOFF'])
def test_authentication_errors_are_not_parsed_as_signal(status):
    with pytest.raises(PermissionError):
        parse_radio(f'<RGW><login_status>{status}</login_status></RGW>'.encode())


def test_rejected_credential_is_not_retried_on_every_monitor_sample(monkeypatch):
    client = RadioClient({'username': 'admin', 'realm': 'Highwmg', 'ha1': 'test-digest'})
    requests = []
    def request(path, header=None):
        requests.append(path)
        if path == '/login.cgi':
            return {'WWW-Authenticate': 'Digest realm="Highwmg", nonce="123", qop="auth"'}, b''
        return {}, b'500 OK'
    monkeypatch.setattr(client, 'request', request)
    for _ in range(3):
        with pytest.raises(PermissionError):
            client.sample()
    assert len(requests) == 2


def test_certificate_mismatch_stops_before_sending_digest(monkeypatch):
    client = RadioClient({'tls_sha256': hashlib.sha256(b'expected-cert').hexdigest()})
    sent = []
    class FakeConnection:
        def __init__(self, *args, **kwargs):
            self.sock = self
        def connect(self):
            pass
        def getpeercert(self, binary_form=False):
            return b'different-cert'
        def request(self, *args, **kwargs):
            sent.append(args)
        def close(self):
            pass
    monkeypatch.setattr(modem_radio, 'modem_device', lambda: None)
    monkeypatch.setattr(modem_radio.http.client, 'HTTPSConnection', FakeConnection)
    with pytest.raises(ValueError, match='certificate changed'):
        client.request('/xml_action.cgi', 'private-digest-header')
    assert sent == []


def test_digest_challenge_does_not_wait_for_unframed_cgi_body(monkeypatch):
    requests = []
    class Reply:
        def getheaders(self):
            return [('WWW-Authenticate', 'Digest realm="Highwmg", nonce="123", qop="auth"')]
        def read(self, *_):
            raise AssertionError('The challenge has no framed body')
    class Connection:
        def __init__(self, *args, **kwargs):
            self.sock = self
        def connect(self):
            pass
        def getpeercert(self, binary_form=False):
            return b'modem-certificate'
        def request(self, method, path, headers):
            requests.append((method, path, headers))
        def getresponse(self):
            return Reply()
        def close(self):
            pass
    monkeypatch.setattr(modem_radio, 'modem_device', lambda: None)
    monkeypatch.setattr(modem_radio.http.client, 'HTTPSConnection', Connection)
    client = RadioClient({'username': 'admin'})
    client.challenge()
    assert client.nonce == '123'
    assert requests[0][1] == '/login.cgi'
    assert requests[0][2]['X-Requested-With'] == 'XMLHttpRequest'
    assert requests[0][2]['Cache-Control'] == 'no-store, no-cache, must-revalidate'
    assert requests[0][2]['Pragma'] == 'no-cache'


def test_radio_reads_live_dashboard_status_instead_of_editable_wan_config(monkeypatch):
    client = RadioClient({'username': 'admin', 'realm': 'Highwmg', 'ha1': 'test-digest'})
    client.nonce = '123'
    client.logged_in_at = modem_radio.time.monotonic()
    requested = []
    def request(path, header=None):
        requested.append(path)
        return {}, b'<RGW><wan><sys_mode>17</sys_mode><rssi>71</rssi></wan></RGW>'
    monkeypatch.setattr(client, 'request', request)
    assert client.sample()['rssi_dbm'] == -70
    assert requested == ['/xml_action.cgi?method=get&module=duster&file=status1']
