"""Real blocked checks must not hold up route handoffs or leak probe children."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import pytest

from scripts import tunnel_supervisor as tunnel


USB = (('-4', 'enxac0033aa9633', '192.168.125.1', '192.168.125.100'),)
WIFI = (('-4', 'wlP1p1s0', '192.168.125.1', '192.168.125.200'),)


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    assert predicate()


def running(pid):
    try:
        # An orphan that is already dead can briefly await the system reaper.
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1][0] != 'Z'
    except FileNotFoundError:
        return False


def sleeper(marker, seconds=8):
    return tunnel.CommandProbe([sys.executable, '-c',
        'import os,sys,time;open(sys.argv[1],"a").write(str(os.getpid())+"\\n");'
        'time.sleep(float(sys.argv[2]));print("false",flush=True)',
        str(marker), str(seconds)], timeout=seconds+2)


@pytest.mark.parametrize('blocked_check', ['transport', 'web', 'ready', 'upstream'])
def test_slow_health_checks_cannot_delay_verified_route_handoff(tmp_path, blocked_check):
    pids_path, marker = tmp_path/'tunnel-pids', tmp_path/'probe-pids'
    command = [sys.executable, '-c',
        'import os,sys,time;open(sys.argv[1],"a").write(str(os.getpid())+"\\n");time.sleep(30)',
        str(pids_path)]
    route, stop, results, beats = [USB], threading.Event(), [], []
    options = {'read_ready': lambda: True}
    options[{'transport': 'read_transport', 'web': 'read_web',
             'ready': 'read_ready', 'upstream': 'read_upstream'}[blocked_check]] = sleeper(marker)
    if blocked_check == 'upstream':
        options['read_ready'] = lambda: False
    worker = threading.Thread(target=lambda: results.append(tunnel.supervise(
        command, stop, read_route=lambda: route[0], read_handoff=lambda current: bool(current),
        interval=.01, stable_for=4, startup_grace=.02, unhealthy_for=.02,
        heartbeat=lambda: beats.append(time.monotonic()), **options)), daemon=True)
    pids = lambda: [int(p) for p in pids_path.read_text().splitlines()] if pids_path.exists() else []
    worker.start()
    try:
        wait_for(lambda: len(pids()) == 1)
        wait_for(marker.exists)
        beat_count = len(beats)
        time.sleep(.06)
        assert len(beats) >= beat_count+3
        before = time.monotonic()
        route[0] = WIFI
        wait_for(lambda: len(pids()) == 2, timeout=1)
        assert time.monotonic()-before < 1
        assert not running(pids()[0])
    finally:
        stop.set()
        worker.join(4)
    assert results == [0] and not worker.is_alive()
    assert all(not running(pid) for pid in pids())
    assert all(not running(int(pid)) for pid in marker.read_text().splitlines())


def test_stale_failure_is_discarded_and_blocked_callback_keeps_one_slot(tmp_path):
    pids_path = tmp_path/'pids'
    command = [sys.executable, '-c',
        'import os,sys,time;open(sys.argv[1],"a").write(str(os.getpid())+"\\n");time.sleep(30)',
        str(pids_path)]
    entered, release, stop = threading.Event(), threading.Event(), threading.Event()
    route, calls, results = [USB], [], []

    def check():
        calls.append(time.monotonic())
        if len(calls) == 1:
            entered.set()
            release.wait(2)
            return False
        return True

    worker = threading.Thread(target=lambda: results.append(tunnel.supervise(
        command, stop, read_route=lambda: route[0], read_ready=lambda: True,
        read_transport=check, read_handoff=lambda _: True, interval=.01,
        startup_grace=.02, probe_failures=1, probe_interval=.02)), daemon=True)
    pids = lambda: pids_path.read_text().splitlines() if pids_path.exists() else []
    worker.start()
    try:
        assert entered.wait(2)
        wait_for(lambda: len(pids()) == 1)
        route[0] = WIFI
        wait_for(lambda: len(pids()) == 2)
        time.sleep(.1)
        assert len(calls) == 1  # Cancelling a callback cannot spawn replacement copies.
        release.set()
        wait_for(lambda: len(calls) >= 3)
        assert len(pids()) == 2  # The stale False cannot kill the new connector.
    finally:
        release.set()
        stop.set()
        worker.join(3)
    assert results == [0] and not worker.is_alive()


def test_slow_ssh_probe_does_not_hold_up_web_failure_recovery(tmp_path):
    pids_path, marker = tmp_path/'pids', tmp_path/'ssh-probe-pids'
    command = [sys.executable, '-c',
        'import os,sys,time;open(sys.argv[1],"a").write(str(os.getpid())+"\\n");time.sleep(30)',
        str(pids_path)]
    stop, results, web = threading.Event(), [], [False]
    worker = threading.Thread(target=lambda: results.append(tunnel.supervise(
        command, stop, read_route=lambda: WIFI, read_ready=lambda: True,
        read_transport=sleeper(marker), read_web=lambda: web[0], read_upstream=lambda: True,
        interval=.01, stable_for=0, startup_grace=.05, probe_failures=3,
        probe_retry_interval=.02, probe_interval=.02)), daemon=True)
    pids = lambda: pids_path.read_text().splitlines() if pids_path.exists() else []
    worker.start()
    try:
        wait_for(marker.exists)
        before = time.monotonic()
        wait_for(lambda: len(pids()) == 2, timeout=1)
        assert time.monotonic()-before < 1  # No eight-second wait for the SSH job.
        web[0] = True
        time.sleep(.15)
        assert len(pids()) == 2
    finally:
        stop.set()
        worker.join(3)
    assert results == [0] and not worker.is_alive()
    assert all(not running(int(pid)) for pid in marker.read_text().splitlines())


def test_upstream_failure_respects_public_probe_pause(tmp_path):
    pids_path = tmp_path/'pids'
    command = [sys.executable, '-c',
        'import os,sys,time;open(sys.argv[1],"a").write(str(os.getpid())+"\\n");time.sleep(30)',
        str(pids_path)]
    stop, results, inspections = threading.Event(), [], []

    def upstream():
        inspections.append(time.monotonic())
        return False

    worker = threading.Thread(target=lambda: results.append(tunnel.supervise(
        command, stop, read_route=lambda: WIFI, read_ready=lambda: True,
        read_web=lambda: False, read_upstream=upstream, interval=.01,
        stable_for=0, startup_grace=.03, probe_failures=1,
        probe_retry_interval=.01, probe_interval=.3)), daemon=True)
    worker.start()
    try:
        wait_for(lambda: len(inspections) == 1)
        time.sleep(.15)
        assert len(inspections) == 1  # An old failed counter cannot bypass the pause.
        wait_for(lambda: len(inspections) == 2)
        assert inspections[1]-inspections[0] >= .3
        assert len(pids_path.read_text().splitlines()) == 1
    finally:
        stop.set()
        worker.join(3)
    assert results == [0] and not worker.is_alive()


@pytest.mark.parametrize('cancel', [True, False])
def test_command_probe_reaps_group_on_cancel_or_deadline(tmp_path, cancel):
    marker = tmp_path/'descendant'
    nested = ('import os,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
              'open(sys.argv[1],"w").write(str(os.getpid()));time.sleep(30)')
    helper = ('import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'
              + repr(nested) + ',sys.argv[1]]);time.sleep(30)')
    check = tunnel.CommandProbe([sys.executable, '-c', helper, str(marker)],
                                timeout=3 if cancel else .4)
    job = tunnel.BackgroundProbe(check)
    assert job.start(1)
    try:
        wait_for(marker.exists)
        pid = int(marker.read_text())
        if cancel:
            job.cancel()
        wait_for(lambda: not job.busy, timeout=2)
        wait_for(lambda: not running(pid), timeout=2)
        result = job.take(1)
        if cancel:
            assert result is None
        else:
            assert result.value is False
    finally:
        job.close()


def test_successful_helper_cannot_leave_descendant_using_old_route(tmp_path):
    marker = tmp_path/'descendant'
    nested = ('import os,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);'
              'open(sys.argv[1],"w").write(str(os.getpid()));time.sleep(30)')
    helper = ('import pathlib,subprocess,sys,time;subprocess.Popen([sys.executable,"-c",'
              + repr(nested) + ',sys.argv[1]]);'
              '\nwhile not pathlib.Path(sys.argv[1]).exists(): time.sleep(.01)'
              '\nprint("true",flush=True)')
    check = tunnel.CommandProbe([sys.executable, '-c', helper, str(marker)], timeout=3)
    assert check(threading.Event()) is True
    wait_for(lambda: not running(int(marker.read_text())))


def test_isolated_worker_dispatch_loads_only_adjacent_module(tmp_path):
    helper_dir = tmp_path/'helper'
    helper_dir.mkdir()
    helper = helper_dir/'tunnel_supervisor.py'
    shutil.copyfile(Path(tunnel.__file__), helper)
    (helper_dir/'network_health.py').write_text('def upstream_ready(): return False\n')
    (tmp_path/'network_health.py').write_text('raise RuntimeError("cwd must not be imported")\n')
    result = subprocess.run([sys.executable, '-I', str(helper), '--probe', 'upstream'],
                            cwd=tmp_path, capture_output=True, text=True, timeout=3, check=True)
    assert json.loads(result.stdout) is False
    assert result.stderr == ''


def test_isolated_ready_helper_has_bounded_failure_and_json_output():
    # Port 0 is not a listening endpoint. This avoids outside network traffic.
    result = subprocess.run([sys.executable, '-I', str(Path(tunnel.__file__).resolve()),
                             '--probe', 'ready', 'http://127.0.0.1:0/ready'],
                            capture_output=True, text=True, timeout=3, check=True)
    assert json.loads(result.stdout) is False
    assert result.stderr == ''


def test_internal_dispatch_rejects_extra_arguments():
    result = subprocess.run([sys.executable, '-I', str(Path(tunnel.__file__).resolve()),
                             '--probe', 'ready', 'http://127.0.0.1:0/ready', 'extra'],
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 2 and not result.stdout
