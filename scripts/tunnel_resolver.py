#!/usr/bin/env python3
"""Loopback-only DNS with two conventional and two verified HTTPS upstreams."""
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import socketserver
import struct
import subprocess
import threading
import time

import dns.exception
import dns.flags
import dns.message
import dns.query
import dns.rcode

LOG = logging.getLogger('uav.tunnel.resolver')
ADDRESS = '127.0.0.55'
UPSTREAMS = ('1.1.1.1', '8.8.8.8')


def exchange(query, upstream, https=False):
    if not https:
        reply = dns.query.udp(query, upstream, timeout=.6)
        if reply.flags & dns.flags.TC:
            reply = dns.query.tcp(query, upstream, timeout=1)
        return reply
    result = subprocess.run(
        ['curl', '--noproxy', '*', '-fsS', '--connect-timeout', '2', '--max-time', '3',
         '--header', 'Content-Type: application/dns-message',
         '--header', 'Accept: application/dns-message', '--data-binary', '@-',
         f'https://{upstream}/dns-query'], input=query.to_wire(), capture_output=True, timeout=4)
    result.check_returncode()
    reply = dns.message.from_wire(result.stdout)
    if not query.is_response(reply):
        raise ValueError('HTTPS resolver returned an unrelated answer')
    return reply


class Resolver:
    def __init__(self, query_upstream=exchange):
        self.query_upstream = query_upstream
        self.cache = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=16)

    def resolve(self, query):
        if len(query.question) != 1 or query.opcode() != 0 or query.flags & dns.flags.QR:
            reply = dns.message.make_response(query)
            reply.set_rcode(dns.rcode.REFUSED)
            return reply
        key = query.to_wire()[2:]
        now = time.monotonic()
        with self.lock:
            cached = self.cache.get(key)
        if cached and now < cached[0]:
            reply = dns.message.from_wire(query.id.to_bytes(2, 'big')+cached[2][2:])
            age = int(now-cached[1])
            for group in (reply.answer, reply.authority, reply.additional):
                for rrset in group:
                    rrset.ttl = max(0, rrset.ttl-age)
            return reply
        reply = None
        for https in (False, True):
            pending = [self.pool.submit(self.query_upstream, query, server, https)
                       for server in UPSTREAMS]
            try:
                for future in as_completed(pending):
                    try:
                        candidate = future.result()
                        if (query.is_response(candidate) and candidate.rcode() in
                                (dns.rcode.NOERROR, dns.rcode.NXDOMAIN)):
                            reply = candidate
                            break
                    except (OSError, ValueError, EOFError, dns.exception.DNSException,
                            subprocess.SubprocessError):
                        continue
            finally:
                for future in pending:
                    future.cancel()
            if reply is not None:
                break
        if reply is None:
            reply = dns.message.make_response(query)
            reply.set_rcode(dns.rcode.SERVFAIL)
            LOG.warning('Tunnel DNS unavailable via UDP/TCP and HTTPS upstreams')
            return reply
        ttl = min([rr.ttl for rr in reply.answer+reply.authority] or [0])
        if ttl > 0:
            with self.lock:
                if len(self.cache) >= 256:
                    self.cache.pop(next(iter(self.cache)))
                stored_at = time.monotonic()
                self.cache[key] = (stored_at+min(ttl, 30), stored_at, reply.to_wire())
        return reply

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


class BoundedThreads(socketserver.ThreadingMixIn):
    daemon_threads = True
    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class UDPServer(BoundedThreads, socketserver.UDPServer):
    pass


class TCPServer(BoundedThreads, socketserver.TCPServer):
    allow_reuse_address = True


def recv_exact(sock, size):
    chunks = bytearray()
    while len(chunks) < size:
        part = sock.recv(size-len(chunks))
        if not part:
            raise EOFError('DNS TCP peer closed the connection')
        chunks.extend(part)
    return bytes(chunks)


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data, sock = self.request
        try:
            query = dns.message.from_wire(data)
            reply = self.server.resolver.resolve(query)
            sock.sendto(reply.to_wire(max_size=max(512, min(query.payload, 4096)),
                                     prefer_truncation=True), self.client_address)
        except (OSError, ValueError, dns.exception.DNSException):
            return


class TCPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(5)
        try:
            for _ in range(20):
                size = struct.unpack('!H', recv_exact(self.request, 2))[0]
                if size > 4096:
                    return
                query = dns.message.from_wire(recv_exact(self.request, size))
                wire = self.server.resolver.resolve(query).to_wire()
                self.request.sendall(struct.pack('!H', len(wire))+wire)
        except (OSError, EOFError, ValueError, dns.exception.DNSException):
            return


def create_servers(resolver, address=ADDRESS, port=53):
    udp = UDPServer((address, port), UDPHandler)
    try:
        tcp = TCPServer((address, udp.server_address[1]), TCPHandler)
    except BaseException:
        udp.server_close()
        raise
    slots = threading.BoundedSemaphore(16)
    for server in (udp, tcp):
        server.resolver = resolver
        server.slots = slots
    return udp, tcp


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    resolver = Resolver()
    udp, tcp = create_servers(resolver)
    threading.Thread(target=tcp.serve_forever, daemon=True).start()
    LOG.info('Tunnel DNS resolver listening on loopback %s:53', ADDRESS)
    udp.serve_forever()


if __name__ == '__main__':
    main()
