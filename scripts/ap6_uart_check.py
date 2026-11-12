#!/usr/bin/env python3
"""Inspect native UART, or verify AP6 telemetry after confirmed physical wiring.

Uses the production telemetry reader alone. Does not change settings, services,
pinmux, network, autopilot parameters, or flight mode. Check mode sends the
reader's companion heartbeat and telemetry interval requests.
"""
import argparse
import json
import logging
import math
import os
from pathlib import Path
import stat
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from uav.config import atomic_json, load_settings
from uav.telemetry import REQUIRED_MESSAGES, Telemetry

PENDING = ROOT / "config/telemetry.uart.pending.json"
UART = "/dev/ttyTHS1"
TRANSPORT_KEYS = {"connection", "baud", "usb_serial", "usb_vid", "usb_pid"}


def load_candidate(path):
    """Merge only transport fields; retain the validated source and freshness policy."""
    patch = json.loads(Path(path).read_text())
    if not isinstance(patch, dict) or set(patch) != TRANSPORT_KEYS:
        raise ValueError("Candidate must contain only the five UART transport fields")
    if patch["connection"] != UART:
        raise ValueError(f"This host's verified native UART is {UART}")
    if any(patch[key] is not None for key in ("usb_serial", "usb_vid", "usb_pid")):
        raise ValueError("Native UART must not fall back to a USB device")
    if type(patch["baud"]) is not int or patch["baud"] not in (57600, 115200, 230400, 460800, 921600):
        raise ValueError("Unsupported candidate baud; it must match the confirmed AP6 UART baud")
    config = load_settings()["telemetry"]
    config.update(patch)
    if (config["system_id"], config.get("component_id", 1)) != (1, 1):
        raise ValueError("Expected the previously verified AP6 source 1/1")
    return config


def inspect_host(config):
    """Read device metadata without opening the serial port or changing its pins."""
    device = Path(config["connection"])
    sysdev = Path("/sys/class/tty") / device.name / "device"
    driver = (sysdev / "driver").resolve().name if (sysdev / "driver").exists() else None
    status_path = sysdev / "of_node/status"
    status = status_path.read_bytes().rstrip(b"\0").decode() if status_path.exists() else None
    exists = device.exists()
    character = exists and stat.S_ISCHR(device.stat().st_mode)
    consoles = [arg.split("=", 1)[1].split(",", 1)[0]
                for arg in Path("/proc/cmdline").read_text().split() if arg.startswith("console=")]
    production = load_settings()["telemetry"]["connection"]
    production_uses_uart = Path(production).resolve() == device.resolve() if production.startswith("/") else False
    available = character and driver == "serial-tegra" and status == "okay" and device.name not in consoles
    return {"connection": str(device), "baud": config["baud"], "character_device": bool(character),
            "driver": driver, "device_tree_status": status, "console": device.name in consoles,
            "host_uart_available": bool(available), "production_uses_uart": production_uses_uart,
            "read_only": True, "telemetry_verified": False,
            "ap6_pinout_voltage_baud_still_require_confirmation": True}


class UartCheckTelemetry(Telemetry):
    """Record actual inbound wire versions from the pinned AP6 source."""
    def __init__(self, config):
        super().__init__(config)
        self.wire_counts = {"mavlink1": 0, "mavlink2": 0}
        self.v2_messages = set()

    def consume(self, msg, mavutil):
        with self.lock:
            super().consume(msg, mavutil)
            if (msg.get_srcSystem(), msg.get_srcComponent()) != (self.system, self.component):
                return
            frame = msg.get_msgbuf()
            if frame and frame[0] == 0xFD:
                self.wire_counts["mavlink2"] += 1
                self.v2_messages.add(msg.get_type())
            elif frame and frame[0] == 0xFE:
                self.wire_counts["mavlink1"] += 1

    def wire_snapshot(self):
        with self.lock:
            return {**self.wire_counts,
                    "missing_v2_messages": sorted(set(("HEARTBEAT", *REQUIRED_MESSAGES)) - self.v2_messages)}


def sample_failure(state, wire, baseline=None):
    if not state.get("connected"):
        return "disconnected"
    if not state.get("complete"):
        return "missing_or_stale_telemetry"
    if state.get("system_id") != 1:
        return "unexpected_flight_controller"
    if state.get("transport", {}).get("receive_errors", 0):
        return "mavlink_parse_errors"
    if wire.get("mavlink1", 0) or wire.get("missing_v2_messages"):
        return "ap6_uart_not_verified_as_mavlink2"
    if baseline and (state["connections"], state["attempts"]) != (baseline["connections"], baseline["attempts"]):
        return "transport_reconnected"
    return None


def diagnostic(state):
    keys = ("connected", "complete", "system_id", "armed", "phase", "connections", "attempts",
            "missing_messages", "ages_s", "transport", "rate_requests", "error", "last_error")
    return {key: state[key] for key in keys if key in state}


def run_check(config, duration, warmup, emit, reader_factory=UartCheckTelemetry):
    """Require one continuous healthy session; stop at the first observed fault."""
    reader = reader_factory(config)
    started = time.monotonic()
    healthy_at = baseline = None
    state, wire = {}, {}
    report = {"accepted": False, "connection": config["connection"], "baud": config["baud"],
              "requested_healthy_s": duration, "warmup_limit_s": warmup, "samples": 0,
              "failure": None, "observed_healthy_s": 0}
    try:
        reader.start()
        while True:
            now = time.monotonic()
            state, wire = reader.snapshot(), reader.wire_snapshot()
            failure = sample_failure(state, wire, baseline)
            report["samples"] += 1
            emit({"elapsed_s": round(now - started, 3), "telemetry": diagnostic(state), "wire": wire})
            if not reader.thread.is_alive():
                report["failure"] = "reader_stopped"
                break
            if healthy_at is None:
                if failure is None:
                    healthy_at, baseline = now, state
                elif now - started >= warmup:
                    report.update(failure="warmup_timeout", warmup_failure=failure)
                    break
            if healthy_at is not None:
                report["observed_healthy_s"] = round(now - healthy_at, 3)
                if failure:
                    report["failure"] = failure
                    break
                if now - healthy_at >= duration:
                    before, after = baseline["transport"], state["transport"]
                    progressed = all(after.get(key, 0) > before.get(key, 0)
                                     for key in ("bytes_received", "packets_received"))
                    report.update(accepted=progressed, failure=None if progressed else "no_receive_progress")
                    break
            time.sleep(1)
    except KeyboardInterrupt:
        report["failure"] = "interrupted"
    except Exception as exc:
        report.update(failure="check_exception", error=f"{type(exc).__name__}: {exc}")
    finally:
        reader.stop()
        if reader.thread and reader.thread.is_alive():
            report.update(accepted=False, failure="reader_did_not_stop")
    report.update(elapsed_s=round(time.monotonic() - started, 3),
                  final_telemetry=diagnostic(state), wire=wire,
                  permanent_reliability_proven=False, production_switched=False)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("inspect", "check"))
    parser.add_argument("--config", type=Path, default=PENDING)
    parser.add_argument("--duration", type=float, default=600, help="continuous healthy seconds, minimum 30")
    parser.add_argument("--warmup", type=float, default=30, help="maximum seconds waiting for complete MAVLink 2")
    parser.add_argument("--output", type=Path, help="new evidence directory for check mode")
    args = parser.parse_args()
    try:
        config = load_candidate(args.config)
        host = inspect_host(config)
        if args.action == "inspect":
            print(json.dumps(host, ensure_ascii=False, indent=2))
            return 0 if host["host_uart_available"] else 1
        if not math.isfinite(args.duration) or not 30 <= args.duration <= 86400:
            raise ValueError("Duration must be 30..86400 seconds")
        if not math.isfinite(args.warmup) or not 5 <= args.warmup <= 120:
            raise ValueError("Warmup must be 5..120 seconds")
        if not host["host_uart_available"] or host["production_uses_uart"]:
            raise ValueError("UART is unavailable or selected by production; do not open a second reader")
        logging.basicConfig(level=logging.INFO, stream=sys.stderr)
        output = args.output or ROOT / "data/maintenance" / time.strftime("%Y%m%d-ap6-uart-check-%H%M%S")
        output.mkdir(mode=0o700, parents=True, exist_ok=False)
        atomic_json(output / "host.json", host)
        fd = os.open(output / "samples.jsonl", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", buffering=1) as stream:
            report = run_check(config, args.duration, args.warmup,
                               lambda sample: stream.write(json.dumps(sample, allow_nan=False) + "\n"))
        atomic_json(output / "report.json", report)
        print(json.dumps({"accepted": report["accepted"], "failure": report["failure"],
                          "observed_healthy_s": report["observed_healthy_s"],
                          "report": str(output / "report.json"), "production_switched": False}, indent=2))
        return 0 if report["accepted"] else 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"accepted": False, "error": str(exc), "production_switched": False}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
