"""Trusted onboard handoffs recreate TCP sockets without general route delay."""
import json
from pathlib import Path
import stat
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from scripts import tunnel_supervisor as tunnel


def test_verified_onboard_route_restarts_child_without_debounce(tmp_path):
    started = tmp_path/'pids'
    command = [sys.executable, '-c',
               'import os,sys,time;open(sys.argv[1],"a").write(str(os.getpid())+"\\n");time.sleep(20)', str(started)]
    usb = (('-4', 'enxac0033aa9633', '192.168.125.1', '192.168.125.100'),)
    wifi = (('-4', 'wlP1p1s0', '192.168.125.1', '192.168.125.101'),)
    route = [usb]
    stop = threading.Event()
    worker = threading.Thread(target=tunnel.supervise, args=(command, stop),
                              kwargs={'read_route': lambda: route[0], 'interval': .01,
                                      'stable_for': 4, 'read_handoff': lambda current: current in (usb, wifi)}, daemon=True)
    worker.start()
    def count():
        return len(started.read_text().splitlines()) if started.exists() else 0
    def wait_for(count_needed):
        deadline = time.monotonic()+2
        while time.monotonic() < deadline and count() < count_needed:
            time.sleep(.01)
        assert count() == count_needed
    try:
        wait_for(1)
        before = time.monotonic()
        route[0] = wifi
        wait_for(2)
        assert time.monotonic()-before < 1  # The ordinary 4-second wait was bypassed.
    finally:
        stop.set()
        worker.join(3)
    assert not worker.is_alive()


@pytest.fixture
def handoff(tmp_path, monkeypatch):
    path = tmp_path/'status.json'
    original = Path.lstat
    monkeypatch.setattr(Path, 'lstat', lambda self: SimpleNamespace(st_uid=0, st_mode=
                        stat.S_IFREG | 0o644 if self == path else stat.S_IFDIR | 0o755)
                        if self in (path, path.parent) else original(self))
    monkeypatch.setattr(tunnel.time, 'monotonic', lambda: 100)
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    value = {'boot_id': boot, 'monotonic': 99, 'route_changed_at': 95, 'active_path': 'wifi',
             'wifi': {'internet': True, 'connected': True, 'interface': 'wlP1p1s0',
                      'profile_uuid': '7b8a7e5f-0e63-46ed-9c9c-9666959890b2'}}
    route = (('-4', 'wlP1p1s0', '192.168.125.1', '192.168.125.101'),)
    return path, value, route


def test_fresh_root_verified_status_authorizes_short_handoff(handoff):
    path, value, route = handoff
    path.write_text(json.dumps(value))
    assert tunnel.onboard_handoff_ready(route, path)


@pytest.mark.parametrize('change', [{'boot_id': 'old'}, {'monotonic': 80},
    {'monotonic': 101}, {'route_changed_at': 80}, {'active_path': 'lab'},
    {'wifi': {'internet': False}}, {'wifi': {'internet': True, 'connected': True,
        'interface': 'wlP1p1s0', 'profile_uuid': 'lab-profile'}}])
def test_stale_or_unverified_path_keeps_normal_debounce(handoff, change):
    path, value, route = handoff
    path.write_text(json.dumps({**value, **change}))
    assert not tunnel.onboard_handoff_ready(route, path)


def test_other_actual_route_cannot_use_onboard_status(handoff):
    path, value, route = handoff
    path.write_text(json.dumps(value))
    assert not tunnel.onboard_handoff_ready((('-4', 'lab0', '192.168.1.1', '192.168.1.2'),), path)
