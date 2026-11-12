import socketserver
import struct
import subprocess
import threading

import dns.message
import dns.rcode
import dns.rrset
import pytest

from scripts import network_health as health


def response(query):
    reply = dns.message.make_response(query)
    reply.answer.append(dns.rrset.from_text(health.DISCOVERY+'.', 60, 'IN', 'SRV',
                                            '1 1 7844 region1.v2.argotunnel.com.'))
    return reply


@pytest.mark.parametrize('tcp', [False, True])
def test_dns_transport_checks_real_packets_and_rejects_failure(tcp):
    failed = [False]
    class Handler(socketserver.BaseRequestHandler):
        def handle(self):
            if tcp:
                size = struct.unpack('!H', self.request.recv(2))[0]
                data = b''
                while len(data) < size:
                    data += self.request.recv(size-len(data))
            else:
                data = self.request[0]
            query = dns.message.from_wire(data)
            reply = response(query)
            if failed[0]:
                reply.answer.clear()
                reply.set_rcode(dns.rcode.SERVFAIL)
            wire = reply.to_wire()
            if tcp:
                self.request.sendall(struct.pack('!H', len(wire))+wire)
            else:
                self.request[1].sendto(wire, self.client_address)
    server_class = socketserver.TCPServer if tcp else socketserver.UDPServer
    with server_class(('127.0.0.1', 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            assert health.dns_check(server='127.0.0.1', port=server.server_address[1], tcp=tcp)['ok']
            failed[0] = True
            assert not health.dns_check(server='127.0.0.1', port=server.server_address[1], tcp=tcp)['ok']
        finally:
            server.shutdown()
            thread.join(2)


def test_doh_rejects_unrelated_reply_and_requires_certificate_verification(monkeypatch):
    corrupt = [False]
    def run(command, **kwargs):
        assert '--insecure' not in command and '-k' not in command
        query = dns.message.from_wire(kwargs['input'])
        reply = response(query)
        if corrupt[0]:
            reply.id ^= 1
        return subprocess.CompletedProcess(command, 0, reply.to_wire(), b'')
    monkeypatch.setattr(health.subprocess, 'run', run)
    assert health.doh_check(health.DOH_SERVERS[0])['ok']
    corrupt[0] = True
    assert not health.doh_check(health.DOH_SERVERS[0])['ok']


def test_restart_requires_dns_and_independent_https():
    checks = {key: {'ok': True} for key in (
        'resolver_srv', 'resolver_web', 'resolver_ssh', *health.DOH_SERVERS)}
    report = {'checks': checks}
    assert health.restart_supported(report)
    for key in ('resolver_srv', 'resolver_web', 'resolver_ssh'):
        checks[key]['ok'] = False
        assert not health.restart_supported(report)
        checks[key]['ok'] = True
    checks[health.DOH_SERVERS[0]]['ok'] = False
    assert health.restart_supported(report)
    checks[health.DOH_SERVERS[1]]['ok'] = False
    assert not health.restart_supported(report)


def test_inspection_exception_does_not_trigger_restart(monkeypatch):
    def fail():
        raise OSError('temporary diagnostic failure')
    monkeypatch.setattr(health, 'snapshot', fail)
    assert health.upstream_ready() is False
