import threading
import time

import dns.flags
import dns.message
import dns.query
import dns.rcode
import dns.rrset
import pytest

from scripts.tunnel_resolver import Resolver, create_servers


def answer(query):
    reply = dns.message.make_response(query)
    reply.answer.append(dns.rrset.from_text(query.question[0].name, 60, 'IN', 'A', '192.0.2.5'))
    return reply


def test_https_fallback_when_both_udp_upstreams_are_unavailable():
    calls = []
    def exchange(query, server, https):
        calls.append((server, https))
        if not https:
            raise OSError('Port 53 blocked')
        return answer(query)
    resolver = Resolver(exchange)
    try:
        query = dns.message.make_query('example.test', 'A')
        reply = resolver.resolve(query)
        assert query.is_response(reply) and str(reply.answer[0][0]) == '192.0.2.5'
        assert any(https for _, https in calls)
    finally:
        resolver.close()


def test_dead_primary_does_not_delay_healthy_secondary():
    def exchange(query, server, https):
        if server == '1.1.1.1':
            time.sleep(.3)
            raise OSError('dead primary')
        return answer(query)
    resolver = Resolver(exchange)
    try:
        started = time.monotonic()
        assert resolver.resolve(dns.message.make_query('example.test', 'A')).answer
        assert time.monotonic()-started < .2
    finally:
        resolver.close()


def test_failures_not_cached_and_recovery_works_without_restart():
    failed = [True]
    def exchange(query, *args):
        if failed[0]:
            raise OSError('no network')
        return answer(query)
    resolver = Resolver(exchange)
    try:
        query = dns.message.make_query('example.test', 'A')
        assert resolver.resolve(query).rcode() == dns.rcode.SERVFAIL
        failed[0] = False
        assert resolver.resolve(query).answer
    finally:
        resolver.close()


def test_cache_preserves_new_transaction_id_and_decrements_ttl(monkeypatch):
    calls = []
    def exchange(query, *args):
        calls.append(query.id)
        return answer(query)
    clock = [100.0]
    monkeypatch.setattr('scripts.tunnel_resolver.time.monotonic', lambda: clock[0])
    resolver = Resolver(exchange)
    try:
        query = dns.message.make_query('example.test', 'A')
        query.id = 1
        assert resolver.resolve(query).answer[0].ttl == 60
        count = len(calls)
        clock[0] = 110.0
        query.id = 2
        result = resolver.resolve(query)
        assert result.id == 2 and result.answer[0].ttl == 50
        assert len(calls) == count
    finally:
        resolver.close()


@pytest.mark.parametrize('tcp', [False, True])
def test_loopback_server_with_real_dns_transport(tcp):
    resolver = Resolver(lambda query, *args: answer(query))
    udp, tcp_server = create_servers(resolver, '127.0.0.1', 0)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in (udp, tcp_server)]
    for thread in threads:
        thread.start()
    try:
        query = dns.message.make_query('example.test', 'A')
        exchange = dns.query.tcp if tcp else dns.query.udp
        result = exchange(query, '127.0.0.1', port=udp.server_address[1], timeout=2)
        assert query.is_response(result) and str(result.answer[0][0]) == '192.0.2.5'
    finally:
        for server in (udp, tcp_server):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(2)
        resolver.close()


def test_https_query_keeps_certificate_verification_and_checks_reply(monkeypatch):
    from scripts import tunnel_resolver
    import subprocess
    wrong_id = [False]
    def run(command, **kwargs):
        assert '--insecure' not in command and '-k' not in command
        query = dns.message.from_wire(kwargs['input'])
        reply = answer(query)
        if wrong_id[0]:
            reply.id ^= 1
        return subprocess.CompletedProcess(command, 0, reply.to_wire(), b'')
    monkeypatch.setattr(tunnel_resolver.subprocess, 'run', run)
    query = dns.message.make_query('example.test', 'A')
    assert tunnel_resolver.exchange(query, '1.1.1.1', True).answer
    wrong_id[0] = True
    with pytest.raises(ValueError):
        tunnel_resolver.exchange(query, '1.1.1.1', True)
