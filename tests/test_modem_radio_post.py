"""Offline checks for authenticated, device-verified modem maintenance POSTs."""
import hashlib
import re

import pytest

from scripts import modem_radio


def test_post_uses_canonical_digest_uri_and_sends_original_body_after_peer_checks(monkeypatch):
    events = []
    certificate = b'expected-modem-certificate'
    expected_body = b'<RGW><wan><connect_disconnect>cellular</connect_disconnect></wan></RGW>'
    path = '/xml_action.cgi?method=set&module=duster&file=wan'
    auth = {'username': 'admin', 'realm': 'Highwmg', 'ha1': 'offline-test-ha1',
            'tls_sha256': hashlib.sha256(certificate).hexdigest()}
    client = modem_radio.RadioClient(auth)
    client.nonce = 'offline-test-nonce'
    monkeypatch.setattr(modem_radio.secrets, 'token_hex', lambda *_: '0123456789abcdef')
    header = client.header(method='POST')
    values = dict(re.findall(r'(\w+)="([^"]*)"', header))
    ha2 = hashlib.md5(b'POST:/cgi/xml_action.cgi').hexdigest()
    expected = hashlib.md5(
        f'offline-test-ha1:offline-test-nonce:00000001:0123456789abcdef:auth:{ha2}'.encode(),
    ).hexdigest()
    assert values['uri'] == '/cgi/xml_action.cgi'
    assert values['response'] == expected

    def verified_device():
        events.append('usb-identity')

    class Reply:
        def getheaders(self):
            return []

        def read(self, *_):
            return b'<RGW><return_code>0</return_code></RGW>'

    class Connection:
        def __init__(self, host, **kwargs):
            assert host == modem_radio.HOST
            events.append('connection-created')
            self.sock = self

        def connect(self):
            events.append('tls-connected')

        def getpeercert(self, binary_form=False):
            assert binary_form
            events.append('certificate-read')
            return certificate

        def request(self, method, request_path, *, body, headers):
            assert events == ['usb-identity', 'connection-created', 'tls-connected', 'certificate-read']
            assert method == 'POST'
            assert request_path == path
            assert body == expected_body
            assert headers['Authorization'] == header
            assert headers['Content-Type'] == 'application/x-www-form-urlencoded; charset=UTF-8'
            assert headers['X-Requested-With'] == 'XMLHttpRequest'
            events.append('body-sent')

        def getresponse(self):
            return Reply()

        def close(self):
            events.append('closed')

    monkeypatch.setattr(modem_radio, 'modem_device', verified_device)
    monkeypatch.setattr(modem_radio.http.client, 'HTTPSConnection', Connection)
    response = client.request(path, header, method='POST', body=expected_body)
    assert response[1] == b'<RGW><return_code>0</return_code></RGW>'
    assert events[-2:] == ['body-sent', 'closed']


@pytest.mark.parametrize('failure', ['usb-identity', 'certificate-pin'])
def test_failed_peer_verification_blocks_maintenance_post_body(monkeypatch, failure):
    events = []
    auth = {'tls_sha256': hashlib.sha256(b'expected-certificate').hexdigest()}
    client = modem_radio.RadioClient(auth)

    def verify_device():
        events.append('usb-identity')
        if failure == 'usb-identity':
            raise ValueError('USB device identity does not match the modem')

    class Connection:
        def __init__(self, *args, **kwargs):
            events.append('connection-created')
            self.sock = self

        def connect(self):
            events.append('tls-connected')

        def getpeercert(self, **kwargs):
            return b'wrong-certificate'

        def request(self, *args, **kwargs):
            events.append('body-sent')
            raise AssertionError('A failed peer check must prevent sending the POST')

        def close(self):
            events.append('closed')

    monkeypatch.setattr(modem_radio, 'modem_device', verify_device)
    monkeypatch.setattr(modem_radio.http.client, 'HTTPSConnection', Connection)
    with pytest.raises(ValueError):
        client.request('/xml_action.cgi?method=set&module=duster&file=wan',
                       'offline-auth-header', method='POST', body=b'<RGW>maintenance</RGW>')
    assert 'body-sent' not in events
    if failure == 'usb-identity':
        assert 'connection-created' not in events
    else:
        assert events[-1] == 'closed'
