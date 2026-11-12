#!/usr/bin/env python3
"""Bounded, read-only DNS/HTTPS evidence independent of tunnel restarts."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
import signal
import subprocess
import threading
import time

import dns.exception
import dns.message
import dns.query
import dns.rcode
import dns.resolver

LOG = logging.getLogger("uav.network.health")
DISCOVERY = "_v2-origintunneld._tcp.argotunnel.com"
DOH_SERVERS = ("https://1.1.1.1/dns-query", "https://8.8.8.8/dns-query")


def dns_check(name=DISCOVERY, kind="SRV", server=None, tcp=False, timeout=3, port=53):
    started = time.monotonic()
    try:
        if server is None:
            resolver = dns.resolver.Resolver()
            resolver.resolve(name, kind, lifetime=timeout)
        else:
            query = dns.message.make_query(name, kind)
            exchange = dns.query.tcp if tcp else dns.query.udp
            reply = exchange(query, server, timeout=timeout, port=port)
            if reply.rcode() != dns.rcode.NOERROR or not reply.answer:
                raise ValueError("DNS returned no successful answer")
        return {"ok": True, "seconds": round(time.monotonic()-started, 3)}
    except (OSError, EOFError, ValueError, dns.exception.DNSException) as exc:
        return {"ok": False, "error": str(exc)[:300],
                "seconds": round(time.monotonic()-started, 3)}


def doh_check(url, timeout=3):
    """Use literal IP HTTPS with certificate verification; no bootstrap DNS."""
    started = time.monotonic()
    query = dns.message.make_query(DISCOVERY, "SRV")
    try:
        result = subprocess.run(
            ["curl", "--noproxy", "*", "--fail", "--silent", "--show-error",
             "--connect-timeout", "2", "--max-time", str(timeout),
             "--header", "Content-Type: application/dns-message",
             "--header", "Accept: application/dns-message", "--data-binary", "@-", url],
            input=query.to_wire(), capture_output=True, timeout=timeout+1,
        )
        if result.returncode:
            raise ValueError(f"curl {result.returncode}: {result.stderr.decode(errors='replace')[:200]}")
        reply = dns.message.from_wire(result.stdout)
        if not query.is_response(reply) or reply.rcode() != dns.rcode.NOERROR or not reply.answer:
            raise ValueError("HTTPS DNS returned no matching successful answer")
        return {"ok": True, "seconds": round(time.monotonic()-started, 3)}
    except (OSError, ValueError, dns.exception.DNSException, subprocess.SubprocessError) as exc:
        return {"ok": False, "error": str(exc)[:300],
                "seconds": round(time.monotonic()-started, 3)}


def snapshot():
    checks = {
        "resolver_srv": lambda: dns_check(),
        "resolver_web": lambda: dns_check("uav.laiting1202.com", "A"),
        "resolver_ssh": lambda: dns_check("ssh-uav.laiting1202.com", "A"),
    }
    for server in ("1.1.1.1", "8.8.8.8"):
        for tcp in (False, True):
            checks[f"{server}_{'tcp' if tcp else 'udp'}"] = (
                lambda server=server, tcp=tcp: dns_check(server=server, tcp=tcp))
    for url in DOH_SERVERS:
        checks[url] = lambda url=url: doh_check(url)
    with ThreadPoolExecutor(max_workers=len(checks)) as pool:
        pending = {key: pool.submit(check) for key, check in checks.items()}
        results = {key: future.result() for key, future in pending.items()}
    return {
        "time": datetime.now(timezone.utc).isoformat(),
        "monotonic": round(time.monotonic(), 3),
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
        "checks": results,
    }


def restart_supported(report):
    """Restart only after proving DNS and an independent HTTPS path work."""
    checks = report["checks"]
    return (all(checks[key]["ok"] for key in ("resolver_srv", "resolver_web", "resolver_ssh"))
            and any(checks[url]["ok"] for url in DOH_SERVERS))


def upstream_ready():
    try:
        report = snapshot()
        LOG.info("Network upstream evidence: %s", json.dumps(report, sort_keys=True))
        return restart_supported(report)
    except Exception:
        # A diagnostic failure does not establish that killing live sockets helps.
        LOG.exception("Network upstream inspection failed; retaining tunnel")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopped = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stopped.set())
    while not stopped.is_set():
        healthy = upstream_ready()
        if args.once:
            return 0 if healthy else 1
        stopped.wait(60 if healthy else 15)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
