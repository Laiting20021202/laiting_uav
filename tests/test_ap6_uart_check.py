"""UART acceptance rejects false health and exercises real MAVLink 2 over a PTY."""
import copy
import json
import os
import pty
import threading
import tty
from types import SimpleNamespace

import pytest
from pymavlink.dialects.v20 import ardupilotmega as mav2
from scripts import ap6_uart_check as check


def healthy_sample():
    return {"connected": True, "complete": True, "system_id": 1,
            "connections": 1, "attempts": 1,
            "transport": {"receive_errors": 0, "bytes_received": 100, "packets_received": 10}}


WIRE2 = {"mavlink1": 0, "mavlink2": 10, "missing_v2_messages": []}


def fake_check(monkeypatch, samples, duration=2):
    clock = [0.0]
    monkeypatch.setattr(check.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(check.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    class Reader:
        def __init__(self, config):
            self.stopped = False
            self.thread = SimpleNamespace(is_alive=lambda: not self.stopped)
            self.index = 0
        def start(self):
            pass
        def snapshot(self):
            sample = copy.deepcopy(samples[min(self.index, len(samples) - 1)])
            self.index += 1
            return sample
        def wire_snapshot(self):
            return dict(WIRE2)
        def stop(self):
            self.stopped = True
    emitted = []
    report = check.run_check({"connection": check.UART, "baud": 115200}, duration, 3,
                             emitted.append, reader_factory=Reader)
    return report, emitted


def test_acceptance_requires_continuous_health_and_receive_progress(monkeypatch):
    initial = healthy_sample()
    progressed = copy.deepcopy(initial)
    progressed["transport"].update(bytes_received=1000, packets_received=100)
    report, emitted = fake_check(monkeypatch, [initial, progressed, progressed])
    assert report["accepted"] and report["observed_healthy_s"] == 2
    assert len(emitted) == 3 and report["production_switched"] is False
    report, _ = fake_check(monkeypatch, [initial])
    assert not report["accepted"] and report["failure"] == "no_receive_progress"


@pytest.mark.parametrize("fault,reason", [
    ({"connected": False}, "disconnected"),
    ({"complete": False}, "missing_or_stale_telemetry"),
    ({"connections": 2, "attempts": 2}, "transport_reconnected"),
    ({"transport": {"receive_errors": 1}}, "mavlink_parse_errors"),
])
def test_transient_fault_cannot_be_hidden_by_later_recovery(monkeypatch, fault, reason):
    initial = healthy_sample()
    bad = {**initial, **fault}
    report, emitted = fake_check(monkeypatch, [initial, bad, initial])
    assert not report["accepted"] and report["failure"] == reason
    assert len(emitted) == 2


def test_no_data_times_out_instead_of_claiming_acceptance(monkeypatch):
    report, _ = fake_check(monkeypatch, [{"connected": False}])
    assert not report["accepted"] and report["failure"] == "warmup_timeout"
    assert report["observed_healthy_s"] == 0


@pytest.mark.parametrize("wire", [
    {**WIRE2, "mavlink1": 1},
    {**WIRE2, "missing_v2_messages": ["HEARTBEAT"]},
])
def test_wire_v2_must_be_received_not_merely_enabled_locally(wire):
    assert check.sample_failure(healthy_sample(), wire) == "ap6_uart_not_verified_as_mavlink2"


def test_candidate_cannot_restore_usb_fallback_or_change_source(tmp_path):
    patch = json.loads(check.PENDING.read_text())
    path = tmp_path / "candidate.json"
    patch["usb_serial"] = "330032000D51333331333534"
    path.write_text(json.dumps(patch))
    with pytest.raises(ValueError, match="fall back"):
        check.load_candidate(path)
    patch["usb_serial"] = None
    patch["system_id"] = 0
    path.write_text(json.dumps(patch))
    with pytest.raises(ValueError, match="only the five"):
        check.load_candidate(path)


def test_real_uart_reader_accepts_complete_v2_and_ignores_forwarded_gcs_v1():
    master, slave = pty.openpty()
    tty.setraw(slave)
    config = check.load_candidate(check.PENDING)
    config.update(connection=os.ttyname(slave), retry_s=.1, heartbeat_timeout_s=2)
    stopped = threading.Event()
    readers = []
    writer_errors = []
    class Writer:
        def write(self, data):
            return os.write(master, data)
    encoder = mav2.MAVLink(Writer(), srcSystem=1, srcComponent=1)
    gcs = mav2.MAVLink(None, srcSystem=255, srcComponent=235)
    def feed():
        try:
            while not stopped.is_set():
                os.write(master, mav2.MAVLink_heartbeat_message(6, 8, 0, 0, 3, 3).pack(gcs, force_mavlink1=True))
                encoder.heartbeat_send(2, 3, 0, 0, 3)
                encoder.global_position_int_send(1000, 250000000, 1210000000, 60000, 60000, 0, 0, 0, 0)
                encoder.gps_raw_int_send(1000000, 3, 250000000, 1210000000, 60000, 100, 100, 0, 0, 10)
                encoder.attitude_send(1000, 0, 0, 0, 0, 0, 0)
                encoder.vfr_hud_send(0, 0, 0, 0, 60, 0)
                encoder.sys_status_send(0, 0, 0, 100, 24000, 100, 90, 0, 0, 0, 0, 0, 0)
                stopped.wait(.1)
        except Exception as exc:
            writer_errors.append(exc)
    def factory(config):
        reader = check.UartCheckTelemetry(config)
        readers.append(reader)
        return reader
    sender = threading.Thread(target=feed)
    sender.start()
    try:
        report = check.run_check(config, 1, 5, lambda sample: None, reader_factory=factory)
        assert report["accepted"], report
        assert report["wire"]["mavlink2"] > 0 and report["wire"]["mavlink1"] == 0
        assert report["wire"]["missing_v2_messages"] == []
        assert report["final_telemetry"]["transport"]["receive_errors"] == 0
        assert not readers[0].thread.is_alive()
        assert not writer_errors
    finally:
        stopped.set()
        sender.join(timeout=2)
        os.close(master)
        os.close(slave)
