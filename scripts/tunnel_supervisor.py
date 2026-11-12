#!/usr/bin/env python3
"""Recreate tunnel sockets when the Internet interface, gateway or address changes."""
import argparse
from dataclasses import dataclass
import importlib.util
import json
import logging
import os
from pathlib import Path
import selectors
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from urllib.request import build_opener, ProxyHandler

LOG = logging.getLogger("uav.tunnel")


def notify_watchdog():
    address = os.environ.get('NOTIFY_SOCKET')
    if not address:
        return
    if address.startswith('@'):
        address = '\0'+address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(b'WATCHDOG=1', address)
    except OSError:
        LOG.warning('Cannot report tunnel supervisor watchdog status')


def tunnel_ready(url):
    """Bounded, local-only readiness check; ignore proxy environment variables."""
    try:
        with build_opener(ProxyHandler({})).open(url, timeout=2) as response:
            body = json.load(response)
            return response.status == 200 and isinstance(body, dict) and body.get("readyConnections", 0) > 0
    except (OSError, ValueError, TypeError):
        return False


def http_transport_ready(public_url, local_url="http://127.0.0.1:8080/healthz", timeout=5,
                         doh_servers=()):
    """Verify the public web path; a failed origin must not restart the tunnel."""
    try:
        with build_opener(ProxyHandler({})).open(local_url, timeout=2) as response:
            if response.status != 200 or json.load(response) != {"status": "ok"}:
                return None
    except (OSError, ValueError, TypeError):
        return None
    try:
        # urllib is rejected by this zone's bot policy; use the same curl client
        # used to validate deployment. Keep certificate verification enabled.
        for doh_url in (None, *doh_servers):
            command = ["curl", "--noproxy", "*", "--fail", "--silent", "--show-error",
                       "--connect-timeout", "3", "--max-time", str(timeout)]
            if doh_url:
                command += ["--doh-url", doh_url]
            result = subprocess.run(command + [public_url], capture_output=True,
                                    text=True, timeout=timeout + 1)
            if result.returncode == 0:
                return json.loads(result.stdout) == {"status": "ok"}
            LOG.warning("Public HTTP probe failed via %s: curl=%s %s",
                        doh_url or "system DNS", result.returncode, result.stderr.strip()[:300])
            # DNS failure and connect timeout can benefit from alternate DNS.
            # HTTP/origin/certificate failures must not be hidden by retries.
            if result.returncode not in (6, 28):
                break
        return False
    except (OSError, ValueError, subprocess.SubprocessError):
        return False


def ssh_transport_ready(binary, hostname, timeout=8):
    """Check public SSH transport without credentials or a login attempt.

    None means the local origin itself is unavailable; restarting the tunnel
    cannot repair sshd. This deployment's hostname has no Access login policy.
    """
    try:
        with socket.create_connection(("127.0.0.1", 22), timeout=2) as local:
            if not local.recv(256).startswith(b"SSH-"):
                return None
    except OSError:
        return None
    child = None
    try:
        child = subprocess.Popen([binary, "access", "ssh", "--hostname", hostname],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 env={**os.environ, "BROWSER": "/bin/false"})
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            if selector.select(timeout):
                return os.read(child.stdout.fileno(), 256).startswith(b"SSH-")
        return False
    except OSError:
        return False
    finally:
        if child is not None:
            stop_child(child, timeout=1)
            child.stdin.close()
            child.stdout.close()


def route_signature():
    """Ask the kernel for Internet egress; no DNS lookup or packets are needed.

    Check both families so IPv6-only networks also work. Ignore transient route
    metadata (cache, expiry, metrics) that does not change the connection path.
    """
    routes = []
    for family, target in (("-4", "1.1.1.1"), ("-6", "2606:4700:4700::1111")):
        result = subprocess.run(
            ["ip", "-j", family, "route", "get", target],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 2:  # No route in this address family.
            continue
        result.check_returncode()
        for route in json.loads(result.stdout):
            if route.get("dev") and route.get("type", "unicast") == "unicast":
                routes.append((family, route["dev"], route.get("gateway", ""),
                               route.get("prefsrc", route.get("src", ""))))
    return tuple(routes)


def onboard_handoff_ready(route, status_path=Path('/run/laiting-uav-network/status.json')):
    """Skip general route debounce only for a freshly verified onboard handoff."""
    try:
        parent, info = status_path.parent.lstat(), status_path.lstat()
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022 or
                not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022):
            return False
        status = json.loads(status_path.read_text())
        now = time.monotonic()
        if (status.get('boot_id') != Path('/proc/sys/kernel/random/boot_id').read_text().strip() or
                not 0 <= now-status['monotonic'] <= 5 or
                not 0 <= now-status['route_changed_at'] <= 15):
            return False
        active = status['active_path']
        interface, profile = {'usb': ('enxac0033aa9633', '8391cb7e-8a3a-317c-bf18-612aa9c5300e'),
                              'wifi': ('wlP1p1s0', '7b8a7e5f-0e63-46ed-9c9c-9666959890b2')}[active]
        health = status[active]
        return (health['internet'] is True and health['connected'] is True and
                health['profile_uuid'] == profile and health['interface'] == interface and
                any(family == '-4' and device == interface and gateway == '192.168.125.1'
                    for family, device, gateway, source in route))
    except (OSError, ValueError, TypeError, KeyError):
        return False


def stop_child(child, timeout=5):
    if child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        # A dead HTTP/2 connection can also hang cloudflared's graceful shutdown.
        LOG.warning("Tunnel shutdown timed out; killing stale process")
        child.kill()
        child.wait(timeout=2)


class ProbeCancelled(Exception):
    pass


class CommandProbe:
    """Hard-bounded, cancellable process group for production health checks.

    The helper's curl/access children inherit its private process group. Route
    changes cancel the whole group; no blocked DNS/socket thread can survive it.
    """
    def __init__(self, command, timeout):
        self.command = command
        self.timeout = timeout

    def __call__(self, cancelled):
        child = subprocess.Popen(self.command, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
        try:
            deadline = time.monotonic() + self.timeout
            while child.poll() is None:
                if cancelled.wait(.025):
                    raise ProbeCancelled()
                if time.monotonic() >= deadline:
                    LOG.warning('Health probe reached its bounded deadline')
                    return False
            if cancelled.is_set():
                raise ProbeCancelled()
            if child.returncode:
                return False
            # The trusted helper writes one JSON boolean/null, never response
            # bodies. Do not wait for EOF from a stray descendant holding pipes.
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                if not selector.select(0):
                    return False
                result = json.loads(os.read(child.stdout.fileno(), 64))
            if result is not None and type(result) is not bool:
                return False
            return result
        except (OSError, ValueError):
            return False
        finally:
            # Even a helper that successfully printed its result must not leave
            # a descendant behind holding a pipe or using the old route.
            try:
                os.killpg(child.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                child.wait(timeout=.25)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait(timeout=1)
            child.stdout.close()


@dataclass
class ProbeResult:
    generation: int
    value: object


class BackgroundProbe:
    """At most one in-flight job per check; stale jobs never count as failures.

    Production checks use CommandProbe for hard cancellation. The injectable
    zero-argument callbacks used by tests must be bounded; a cancelled callback
    keeps its one occupied slot until it returns, rather than launching copies.
    """
    def __init__(self, check):
        self.check = check
        self.thread = None
        self.cancelled = threading.Event()
        self.result = None

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()

    def start(self, generation):
        if self.busy:
            return False
        self.result = None
        self.cancelled = threading.Event()
        cancellation = self.cancelled

        def run():
            try:
                value = (self.check(cancellation) if isinstance(self.check, CommandProbe)
                         else self.check())
            except ProbeCancelled:
                return
            except Exception:
                LOG.warning('Health check failed without usable evidence', exc_info=True)
                value = None
            if not cancellation.is_set():
                self.result = ProbeResult(generation, value)

        self.thread = threading.Thread(target=run, name='tunnel-health-probe', daemon=True)
        self.thread.start()
        return True

    def take(self, generation):
        if self.busy or self.result is None:
            return None
        result, self.result = self.result, None
        return result if result.generation == generation else None

    def cancel(self):
        self.cancelled.set()
        self.result = None

    def close(self):
        self.cancel()
        if self.thread is not None:
            self.thread.join(timeout=1.5)


def probe_command(kind, *arguments, timeout):
    return CommandProbe([sys.executable, '-I', str(Path(__file__).resolve()),
                         '--probe', kind, *arguments], timeout)


def supervise(command, stopped, *, read_route=route_signature, interval=2,
              stable_for=4, stop_timeout=5, offline_grace=12, read_ready=None,
              startup_grace=20, unhealthy_for=30, read_transport=None, read_web=None,
              probe_interval=15, probe_failures=3, probe_retry_interval=5,
              read_upstream=None, heartbeat=None, read_handoff=None):
    child = None
    previous = ()
    running_route = ()
    changed_at = time.monotonic()
    started_at = changed_at
    unhealthy_since = None
    was_ready = None
    checks = {name: check for name, check in
              (("SSH transport", read_transport), ("HTTP health", read_web)) if check is not None}
    probes = {name: BackgroundProbe(check) for name, check in checks.items()}
    ready_probe = BackgroundProbe(read_ready) if read_ready is not None else None
    upstream_probe = BackgroundProbe(read_upstream) if read_upstream is not None else None
    jobs = [*probes.values(), *([ready_probe] if ready_probe else []),
            *([upstream_probe] if upstream_probe else [])]
    failed_probes = dict.fromkeys(probes, 0)
    next_probes = dict.fromkeys(probes, changed_at)
    next_ready = changed_at
    ready = None
    generation = 0
    restart_pending = None
    recovery_pending = False

    def invalidate():
        # Route and child generations share the same fence. A check that began
        # before either changed cannot justify killing the new connection.
        nonlocal generation, ready, was_ready, unhealthy_since, restart_pending
        generation += 1
        for job in jobs:
            job.cancel()
        ready = was_ready = None
        unhealthy_since = restart_pending = None

    def pending_still_broken():
        if restart_pending == 'readiness':
            return ready is False and unhealthy_since is not None
        return any(count >= probe_failures for count in failed_probes.values())

    LOG.info("Waiting for a stable Internet route")
    try:
        while not stopped.is_set():
            if child is not None and child.poll() is not None:
                LOG.warning("Tunnel exited with status %s", child.returncode)
                return child.returncode
            try:
                route = read_route()
            except (OSError, ValueError, subprocess.SubprocessError):
                # A transient inspection failure is not proof of a route change.
                LOG.warning("Cannot inspect Internet route; retaining current tunnel", exc_info=True)
                if heartbeat is not None:
                    heartbeat()
                stopped.wait(interval)
                continue
            now = time.monotonic()
            handoff = read_handoff(route) if read_handoff is not None else False
            settle_for = 0 if handoff else stable_for
            if route != previous:
                LOG.info("Internet route changed: %s -> %s", previous, route)
                previous = route
                changed_at = now
                invalidate()
                next_ready = now
                # A USB modem can return with the same IP but lose its TCP/NAT
                # state. Verify it promptly without dropping a healthy tunnel.
                recovery_pending = child is not None
                failed_probes = dict.fromkeys(probes, 0)
            # Do not destroy every active SSH stream on one missing-route sample.
            # Recovered identical routes keep their sockets; readiness handles a
            # stuck process even when the interface/IP/gateway did not change.
            route_changed = route and route != running_route and now-changed_at >= settle_for
            offline = not route and now-changed_at >= offline_grace
            if child is not None and (route_changed or offline):
                LOG.info("Rebuilding tunnel after sustained route change/offline period")
                if handoff:
                    LOG.info("Verified onboard handoff: debounce bypassed, shutdown budget %.1fs",
                             min(stop_timeout, 1))
                invalidate()
                stop_child(child, min(stop_timeout, 1) if handoff else stop_timeout)
                child = None
            if child is None and route and now - changed_at >= settle_for:
                LOG.info("Starting tunnel on %s", route)
                child = subprocess.Popen(command)
                invalidate()
                running_route = route
                started_at = time.monotonic()
                unhealthy_since = None
                was_ready = None
                next_probes = dict.fromkeys(probes, started_at + startup_grace)
                next_ready = started_at
                failed_probes = dict.fromkeys(probes, 0)
                recovery_pending = False
            if child is not None and ready_probe is not None:
                result = ready_probe.take(generation)
                if result is not None:
                    ready = result.value
                    next_ready = now + interval
                    if ready != was_ready:
                        LOG.info("Tunnel readiness: %s", {True: 'ready', False: 'not ready',
                                                         None: 'unknown'}.get(ready, 'unknown'))
                        was_ready = ready
                if now >= next_ready and ready_probe.start(generation):
                    next_ready = float('inf')
                if ready is not False or not route or now-started_at < startup_grace:
                    unhealthy_since = None
                elif unhealthy_since is None:
                    unhealthy_since = now
                elif now-unhealthy_since >= unhealthy_for and restart_pending is None:
                    restart_pending = 'readiness'
            if recovery_pending and route and now-changed_at >= settle_for:
                next_probes = {name: min(deadline, now) for name, deadline in next_probes.items()}
                recovery_pending = False
            if child is not None and route and now-changed_at >= settle_for:
                new_failure_evidence = False
                for name, job in probes.items():
                    result = job.take(generation)
                    if result is not None:
                        reachable = result.value
                        LOG.info("Public %s: %s", name, {True: "ready", False: "unreachable",
                                 None: "local origin unavailable"}.get(reachable, 'unknown'))
                        failed_probes[name] = failed_probes[name] + 1 if reachable is False else 0
                        new_failure_evidence |= reachable is False and failed_probes[name] >= probe_failures
                        next_probes[name] = now + (probe_retry_interval if reachable is False else probe_interval)
                    if restart_pending is None and now >= next_probes[name] and job.start(generation):
                        next_probes[name] = float('inf')
                # A retained failure count is not a new reason to inspect the
                # upstream on every loop during its normal retry pause.
                if new_failure_evidence and restart_pending is None:
                    restart_pending = 'public'
            if restart_pending is not None:
                if not pending_still_broken():
                    restart_pending = None
                    if upstream_probe is not None:
                        upstream_probe.cancel()
                else:
                    upstream = upstream_probe.take(generation) if upstream_probe is not None else ProbeResult(generation, True)
                    if upstream is None:
                        upstream_probe.start(generation)
                    elif upstream.value is not True:
                        LOG.warning("Network upstream unavailable; retaining tunnel for native reconnection")
                        unhealthy_since = now if ready is False else None
                        next_probes = dict.fromkeys(probes, now + probe_interval)
                        restart_pending = None
                    else:
                        if restart_pending == 'readiness':
                            LOG.warning("Tunnel has no ready edge connection for %.1fs; restarting", now-unhealthy_since)
                        else:
                            broken = [name for name, count in failed_probes.items() if count >= probe_failures]
                            LOG.warning("Public %s failed %d consecutive probes with local origin healthy; restarting tunnel",
                                        ', '.join(broken), probe_failures)
                        invalidate()
                        stop_child(child, stop_timeout)
                        child = None
            if heartbeat is not None:
                heartbeat()
            stopped.wait(interval)
        return 0
    finally:
        for job in jobs:
            job.cancel()
        if child is not None:
            stop_child(child, stop_timeout)
        for job in jobs:
            job.close()


def network_health_module():
    """Load the adjacent module explicitly, including in isolated helpers.

    Python -I removes the script directory from sys.path. A fixed file beside
    this script avoids relying on cwd or PYTHONPATH for the helper's imports.
    """
    spec = importlib.util.spec_from_file_location(
        'laiting_uav_network_health', Path(__file__).resolve().with_name('network_health.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe', choices=('ready', 'ssh', 'http', 'upstream'),
                        help=argparse.SUPPRESS)
    parser.add_argument("--ready-url", default="http://127.0.0.1:20241/ready")
    parser.add_argument("--ssh-hostname", help="Optional public SSH banner probe; requires a hostname without interactive Access authentication")
    parser.add_argument("--public-health-url", help="Public /healthz URL returning the local application's health JSON")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command
    if command[:1] == ["--"]:
        command = command[1:]
    if args.probe:
        expected = {'ready': 1, 'ssh': 2, 'http': 1, 'upstream': 0}[args.probe]
        if len(command) != expected:
            parser.error('Invalid internal probe arguments')
        if args.probe == 'ready':
            value = tunnel_ready(command[0])
        elif args.probe == 'ssh':
            value = ssh_transport_ready(*command)
        elif args.probe == 'http':
            value = http_transport_ready(command[0], doh_servers=network_health_module().DOH_SERVERS)
        else:
            value = network_health_module().upstream_ready()
        print(json.dumps(value), flush=True)
        return 0
    if not command:
        parser.error("Provide the cloudflared command after --")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    probe = probe_command('ssh', command[0], args.ssh_hostname, timeout=12) if args.ssh_hostname else None
    web_probe = probe_command('http', args.public_health_url, timeout=22) if args.public_health_url else None
    return supervise(command, stopped, read_ready=probe_command('ready', args.ready_url, timeout=4),
                     read_transport=probe, read_web=web_probe,
                     read_upstream=probe_command('upstream', timeout=6),
                     heartbeat=notify_watchdog, interval=.5, read_handoff=onboard_handoff_ready)


if __name__ == "__main__":
    raise SystemExit(main())
