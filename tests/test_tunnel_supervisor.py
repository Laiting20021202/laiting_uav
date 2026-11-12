"""Exercise route changes with real child processes, without changing host routes."""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from scripts import tunnel_supervisor as tunnel


WIFI = (("-4", "wlan0", "192.168.0.1", "192.168.0.2"),)
CELL = (("-4", "usb0", "192.168.125.1", "192.168.125.2"),)


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate(), "Condition did not become true"


def process_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.mark.parametrize("new_route", [
    CELL,
    (("-4", "wlan0", "192.168.0.254", "192.168.0.2"),),
    (("-4", "wlan0", "192.168.0.1", "192.168.0.3"),),
])
def test_offline_boot_route_changes_and_reconnection(tmp_path, new_route):
    started = tmp_path / "pids"
    command = [sys.executable, "-c", (
        "import os, sys, time; "
        "f=open(sys.argv[1], 'a'); f.write(str(os.getpid())+'\\n'); f.close(); "
        "time.sleep(30)"
    ), str(started)]
    current = [()]
    stopped = threading.Event()
    result = []
    thread = threading.Thread(target=lambda: result.append(tunnel.supervise(
        command, stopped, read_route=lambda: current[0], interval=.01, stable_for=.06, offline_grace=.08,
    )), daemon=True)

    def pids():
        return [int(p) for p in started.read_text().splitlines()] if started.exists() else []

    thread.start()
    try:
        time.sleep(.09)
        assert not pids()  # No network: don't spin up repeatedly failing tunnels.
        current[0] = WIFI
        wait_until(lambda: len(pids()) == 1)
        old_pid = pids()[0]
        current[0] = new_route
        wait_until(lambda: len(pids()) == 2)
        assert not process_alive(old_pid)
        current[0] = ()
        wait_until(lambda: not process_alive(pids()[-1]))
        time.sleep(.09)
        assert len(pids()) == 2
        current[0] = new_route
        wait_until(lambda: len(pids()) == 3)
    finally:
        stopped.set()
        thread.join(3)
    assert not thread.is_alive()
    assert result == [0]
    assert all(not process_alive(pid) for pid in pids())


def test_start_waits_until_route_stops_changing(tmp_path):
    started = tmp_path / "started"
    stopped = threading.Event()
    routes = [WIFI, CELL, WIFI, CELL]
    samples = []

    def read_route():
        route = routes.pop(0) if routes else CELL
        samples.append(time.monotonic())
        return route

    command = [sys.executable, "-c", "import pathlib,sys; pathlib.Path(sys.argv[1]).touch()", str(started)]
    assert tunnel.supervise(command, stopped, read_route=read_route,
                            interval=.01, stable_for=.06) == 0
    assert started.exists()
    assert samples[-1] - samples[3] >= .06


def test_child_failure_returns_for_systemd_restart():
    assert tunnel.supervise([sys.executable, "-c", "raise SystemExit(17)"],
                            threading.Event(), read_route=lambda: WIFI,
                            interval=.01, stable_for=0) == 17


def test_hung_shutdown_is_killed(tmp_path):
    ready = tmp_path / "ready"
    child = subprocess.Popen([sys.executable, "-c", (
        "import pathlib,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "pathlib.Path(sys.argv[1]).touch(); time.sleep(30)"
    ), str(ready)])
    try:
        wait_until(ready.exists)
        tunnel.stop_child(child, timeout=.03)
        assert child.returncode == -9
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)


def test_route_signature_ignores_cache_and_supports_ipv6(monkeypatch):
    responses = [
        (0, [{"dev": "usb0", "gateway": "192.168.125.1", "prefsrc": "192.168.125.2", "cache": [], "uid": 1000}]),
        (2, []),
        (0, [{"dev": "usb0", "gateway": "192.168.125.1", "prefsrc": "192.168.125.2", "cache": [{"expires": 9}], "uid": 0}]),
        (2, []),
        (2, []),
        (0, [{"dev": "wlan0", "gateway": "fe80::1", "src": "2001:db8::2"}]),
        (2, []),
        (2, []),
    ]

    def run(command, **kwargs):
        code, data = responses.pop(0)
        return subprocess.CompletedProcess(command, code, json.dumps(data))

    monkeypatch.setattr(tunnel.subprocess, "run", run)
    assert tunnel.route_signature() == CELL
    assert tunnel.route_signature() == CELL
    assert tunnel.route_signature() == (("-6", "wlan0", "fe80::1", "2001:db8::2"),)
    assert tunnel.route_signature() == ()


def test_brief_outage_keeps_ssh_but_stuck_tunnel_recovers_on_same_route(tmp_path):
    started=tmp_path/'pids'
    command=[sys.executable,'-c',"import os,sys,time; f=open(sys.argv[1],'a'); f.write(str(os.getpid())+'\\n'); f.close(); time.sleep(30)",str(started)]
    current=[WIFI]; ready=[True]; stopped=threading.Event(); result=[]
    def route():
        if isinstance(current[0],Exception):raise current[0]
        return current[0]
    thread=threading.Thread(target=lambda:result.append(tunnel.supervise(
        command,stopped,read_route=route,read_ready=lambda:ready[0],
        interval=.01,stable_for=.03,offline_grace=.2,startup_grace=.15,unhealthy_for=.15)),daemon=True)
    def pids():return [int(p) for p in started.read_text().splitlines()] if started.exists() else []
    thread.start()
    try:
        wait_until(lambda:len(pids())==1)
        first=pids()[0]
        current[0]=();time.sleep(.06);current[0]=WIFI
        time.sleep(.1)
        assert pids()==[first] and process_alive(first)
        current[0]=OSError('temporary route query failure');time.sleep(.04);current[0]=WIFI
        time.sleep(.06)
        assert pids()==[first] and process_alive(first)
        ready[0]=False;time.sleep(.05);ready[0]=True;time.sleep(.08)
        assert pids()==[first]  # A single failed health sample must not drop SSH.
        ready[0]=False
        wait_until(lambda:len(pids())==2)
        ready[0]=True
        assert not process_alive(first)
        time.sleep(.2)
        assert len(pids())==2
    finally:
        stopped.set();thread.join(3)
    assert result==[0] and not thread.is_alive()
    assert all(not process_alive(pid) for pid in pids())


def test_readiness_checks_real_http_status_and_connection_count():
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    status=[200];body=[b'{"readyConnections":4}']
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status[0]);self.end_headers();self.wfile.write(body[0])
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    url=f'http://127.0.0.1:{server.server_port}/ready'
    try:
        assert tunnel.tunnel_ready(url)
        body[0]=b'{"readyConnections":0}'
        assert not tunnel.tunnel_ready(url)
        status[0]=503;body[0]=b'{"readyConnections":4}'
        assert not tunnel.tunnel_ready(url)
        status[0]=200;body[0]=b'invalid'
        assert not tunnel.tunnel_ready(url)
    finally:
        server.shutdown();server.server_close();thread.join(2)
    assert not tunnel.tunnel_ready(url)


def test_http_probe_checks_origin_status_and_public_body():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    replies = {"/local": (200, b'{"status":"ok"}'), "/public": (200, b'{"status":"ok"}')}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            status, body = replies[self.path]
            self.send_response(status)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        assert tunnel.http_transport_ready(base+'/public', base+'/local') is True
        replies['/public'] = (200, b'<html>proxy error</html>')
        assert tunnel.http_transport_ready(base+'/public', base+'/local') is False
        replies['/public'] = (503, b'{"status":"ok"}')
        assert tunnel.http_transport_ready(base+'/public', base+'/local') is False
        replies['/local'] = (503, b'not ready')
        count = requests.count('/public')
        assert tunnel.http_transport_ready(base+'/public', base+'/local') is None
        assert requests.count('/public') == count
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


@pytest.mark.parametrize('web_result', [False, None])
def test_web_failure_recovers_even_when_ssh_and_local_readiness_succeed(tmp_path, web_result):
    started = tmp_path/'pids'
    command = [sys.executable, '-c',
               "import os,sys,time; f=open(sys.argv[1],'a'); f.write(str(os.getpid())+'\\n'); f.close(); time.sleep(30)",
               str(started)]
    stopped = threading.Event()
    web = [web_result]
    calls = []
    results = []

    def pids():
        return [int(p) for p in started.read_text().splitlines()] if started.exists() else []

    def check_web():
        calls.append(web[0])
        return web[0]

    thread = threading.Thread(target=lambda: results.append(tunnel.supervise(
        command, stopped, read_route=lambda: WIFI, read_ready=lambda: True,
        read_transport=lambda: True, read_web=check_web, interval=.005,
        stable_for=.01, startup_grace=.05, probe_interval=.02, probe_retry_interval=.02,
    )), daemon=True)
    thread.start()
    try:
        wait_until(lambda: len(calls) >= 2)
        assert len(pids()) == 1  # Two failures cannot drop healthy sessions.
        first = pids()[0]
        if web_result is False:
            wait_until(lambda: len(pids()) == 2)
            web[0] = True
            assert not process_alive(first)
            wait_until(lambda: True in calls)
            time.sleep(.08)
            assert len(pids()) == 2
        else:
            wait_until(lambda: len(calls) >= 5)
            assert pids() == [first] and process_alive(first)
    finally:
        stopped.set()
        thread.join(3)
    assert results == [0] and not thread.is_alive()
    assert all(not process_alive(pid) for pid in pids())


def test_dns_namespace_is_private_and_preserves_arguments():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    original = Path('/etc/resolv.conf').read_bytes()
    result = subprocess.run(
        ['/bin/sh', str(root/'scripts/tunnel_dns.sh'), sys.executable, '-c',
         "import pathlib,sys,json; print(json.dumps([pathlib.Path('/etc/resolv.conf').read_text(),sys.argv[1]]))",
         'argument with spaces; literal $value'], capture_output=True, text=True, timeout=5, check=True,
    )
    content, arg = json.loads(result.stdout)
    assert content == (root/'config/tunnel-resolv.conf').read_text()
    assert arg == 'argument with spaces; literal $value'
    assert Path('/etc/resolv.conf').read_bytes() == original


def test_public_ssh_watchdog_recovers_even_when_ready_endpoint_is_stale(tmp_path):
    started=tmp_path/'pids'
    command=[sys.executable,'-c',"import os,sys,time; f=open(sys.argv[1],'a'); f.write(str(os.getpid())+'\\n'); f.close(); time.sleep(30)",str(started)]
    stopped=threading.Event();results=[];probes=[];reachable=[None]
    def probe():probes.append(reachable[0]);return reachable[0]
    def pids():return started.read_text().splitlines() if started.exists() else []
    thread=threading.Thread(target=lambda:results.append(tunnel.supervise(
        command,stopped,read_route=lambda:WIFI,read_ready=lambda:True,read_transport=probe,
        interval=.005,stable_for=0,startup_grace=.03,probe_interval=.03,probe_failures=3,
        probe_retry_interval=.03)),daemon=True)
    thread.start()
    try:
        wait_until(lambda:len(probes)>=4)
        assert len(pids())==1  # Local sshd failure cannot be fixed by restarting tunnel.
        reachable[0]=False
        wait_until(lambda:False in probes)
        assert len(pids())==1  # No reset on a single failure.
        wait_until(lambda:len(pids())==2)
        reachable[0]=True
        time.sleep(.15)
        assert len(pids())==2
    finally:
        stopped.set();thread.join(3)
    assert results==[0]
    assert all(not process_alive(int(pid)) for pid in pids())


@pytest.mark.parametrize("recovered", [True, False])
def test_same_route_recovery_probes_promptly_and_only_restarts_if_broken(tmp_path, recovered):
    started = tmp_path / "pids"
    command = [sys.executable, "-c", (
        "import os,sys,time; "
        "f=open(sys.argv[1],'a'); f.write(str(os.getpid())+'\\n'); f.close(); time.sleep(30)"
    ), str(started)]
    current = [WIFI]
    reachable = [True]
    samples = []
    probes = []
    stopped = threading.Event()
    results = []

    def route():
        samples.append(current[0])
        return current[0]

    def probe():
        probes.append((time.monotonic(), reachable[0]))
        return reachable[0]

    def pids():
        return [int(p) for p in started.read_text().splitlines()] if started.exists() else []

    thread = threading.Thread(target=lambda: results.append(tunnel.supervise(
        command, stopped, read_route=route, read_ready=lambda: True, read_transport=probe,
        interval=.005, stable_for=.03, offline_grace=.3, startup_grace=.1,
        probe_interval=60, probe_retry_interval=.03,
    )), daemon=True)
    thread.start()
    try:
        wait_until(lambda: len(probes) == 1 and len(pids()) == 1)
        first = pids()[0]
        current[0] = ()
        wait_until(lambda: () in samples)
        reachable[0] = recovered
        current[0] = WIFI
        wait_until(lambda: len(probes) >= 2)
        if recovered:
            time.sleep(.15)
            assert len(probes) == 2  # Return to the normal 60s interval.
            assert pids() == [first] and process_alive(first)
        else:
            wait_until(lambda: len(pids()) == 2)
            reachable[0] = True
            assert [value for _, value in probes[:4]] == [True, False, False, False]
            assert probes[3][0] - probes[1][0] < 1
            assert not process_alive(first)
    finally:
        stopped.set()
        thread.join(3)
    assert results == [0] and not thread.is_alive()
    assert all(not process_alive(pid) for pid in pids())


@pytest.mark.parametrize("edge_ready", [True, False])
def test_upstream_outage_keeps_process_then_repairs_tunnel_after_upstream_recovers(tmp_path, edge_ready):
    started = tmp_path/'pids'
    command = [sys.executable, '-c',
               "import os,sys,time; f=open(sys.argv[1],'a'); f.write(str(os.getpid())+'\\n'); f.close(); time.sleep(30)",
               str(started)]
    stopped = threading.Event()
    upstream = [False]
    repaired = [False]
    inspections = []
    results = []

    def pids():
        return [int(p) for p in started.read_text().splitlines()] if started.exists() else []

    def inspect():
        inspections.append(upstream[0])
        return upstream[0]

    thread = threading.Thread(target=lambda: results.append(tunnel.supervise(
        command, stopped, read_route=lambda: WIFI,
        read_ready=lambda: edge_ready or repaired[0], read_web=lambda: repaired[0],
        read_upstream=inspect, interval=.005, stable_for=0, startup_grace=.03,
        unhealthy_for=.04, probe_interval=.02, probe_retry_interval=.02,
    )), daemon=True)
    thread.start()
    try:
        wait_until(lambda: len(inspections) >= 3)
        assert len(pids()) == 1
        first = pids()[0]
        assert process_alive(first)
        upstream[0] = True
        wait_until(lambda: len(pids()) == 2)
        repaired[0] = True
        assert not process_alive(first)
        time.sleep(.12)
        assert len(pids()) == 2
    finally:
        stopped.set()
        thread.join(3)
    assert results == [0] and not thread.is_alive()
    assert all(not process_alive(pid) for pid in pids())


def test_http_dns_failure_retries_with_verified_https_dns(monkeypatch):
    import io
    class LocalResponse(io.BytesIO):
        status = 200
    class Opener:
        def open(self, *args, **kwargs):
            return LocalResponse(b'{"status":"ok"}')
    monkeypatch.setattr(tunnel, 'build_opener', lambda *args: Opener())
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert '--insecure' not in command and '-k' not in command
        if '--doh-url' not in command:
            return subprocess.CompletedProcess(command, 6, '', 'Could not resolve host')
        return subprocess.CompletedProcess(command, 0, '{"status":"ok"}', '')
    monkeypatch.setattr(tunnel.subprocess, 'run', run)
    assert tunnel.http_transport_ready('https://example.test/healthz',
                                       doh_servers=('https://1.1.1.1/dns-query',))
    assert len(calls) == 2 and '--doh-url' in calls[1]
