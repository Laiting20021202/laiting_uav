#!/usr/bin/env python3
"""Observe the existing AP6 reader across boots; never open/reset a serial port.

Readiness is based on fresh production journal evidence, not merely lsusb or
web health. This observer records failures; Telemetry owns software reconnects.
"""
import argparse
import ast
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from uav.config import atomic_json
try:
    from .ap6_usb_recovery import IDENTITY, inspect
except ImportError:
    from ap6_usb_recovery import IDENTITY, inspect

BY_ID = "/dev/serial/by-id/usb-ALIGN_AP6-M490-ds_330032000D51333331333534-if00"
REQUIRED = ("GLOBAL_POSITION_INT", "GPS_RAW_INT", "ATTITUDE", "VFR_HUD", "SYS_STATUS")
CONNECTED = re.compile(r"Flight controller connected: (\S+) system=(\d+) component=(\d+), session (\d+)")
INTEGRITY = re.compile(r"Flight telemetry integrity: session=(\d+) complete=(True|False) missing=(\S+) ages_s=(\{.*\})")


class LinkEvidence:
    def __init__(self, boot_id, started, healthy_seconds=60):
        self.boot_id = boot_id.replace("-", "")
        self.started = started
        self.healthy_seconds = healthy_seconds
        self.context = None
        self.context_since = started
        self.source = None
        self.integrity = None
        self.healthy_since = None
        self.confirmations = set()
        self.faults = 0
        self.last_failure = None

    def invalidate(self, reason):
        self.healthy_since = None
        self.confirmations.clear()
        self.last_failure = reason

    def set_context(self, pid, generation, now):
        context = (pid, generation)
        if context != self.context:
            self.context, self.context_since = context, now
            self.source = self.integrity = None
            self.invalidate("reader_or_usb_generation_changed")

    def consume(self, entry, allowed_connections):
        if (entry.get("_BOOT_ID", "").replace("-", "") != self.boot_id
                or str(entry.get("_PID")) != str(self.context[0])):
            return
        at = int(entry["__MONOTONIC_TIMESTAMP"]) / 1e6
        message = entry.get("MESSAGE", "")
        if not isinstance(message, str):
            return
        connected = CONNECTED.search(message)
        if connected:
            connection, system, component, session = connected.groups()
            self.source = {"verified": connection in allowed_connections and (system, component) == ("1", "1"),
                           "connection": connection, "session": int(session), "at": at}
            self.integrity = None
            self.invalidate("new_reader_session")
            return
        if "Flight controller unavailable:" in message:
            self.integrity = None
            self.invalidate("reader_reported_unavailable")
            if at >= self.started:
                self.faults += 1
            return
        match = INTEGRITY.search(message)
        if match and at >= self.context_since:
            session, complete, missing, ages = match.groups()
            try:
                ages = ast.literal_eval(ages)
                if not isinstance(ages, dict):
                    raise ValueError("Invalid ages")
            except (ValueError, SyntaxError):
                self.invalidate("invalid_integrity_record")
                self.integrity = None
                return
            self.integrity = {"session": int(session), "complete": complete == "True",
                              "missing": missing, "ages_s": ages, "at": at,
                              "cursor": entry["__CURSOR"]}
            if complete != "True" or missing != "none":
                # Preserve a fault even when a later good entry is in the same batch.
                self.invalidate("missing_or_stale_telemetry")
                self.faults += 1

    def evaluate(self, now, device_ok, reader_ok, stale_s=2, heartbeat_s=8, journal_ok=True):
        reason = None
        info = self.integrity
        if not device_ok:
            reason = "ap6_usb_or_policy_unavailable"
        elif not reader_ok:
            reason = "production_reader_unavailable"
        elif not journal_ok:
            reason = "journal_unavailable"
        elif not self.source or not self.source["verified"]:
            reason = "waiting_verified_source"
        elif not info or not info["complete"] or info["missing"] != "none":
            reason = "waiting_complete_telemetry"
        elif info["session"] != self.source["session"]:
            reason = "integrity_session_mismatch"
        elif not 0 <= now - info["at"] <= 40:
            reason = "integrity_record_expired"
        else:
            ages = info["ages_s"]
            for name in ("HEARTBEAT", *REQUIRED):
                age = ages.get(name)
                limit = heartbeat_s if name == "HEARTBEAT" else stale_s
                if (isinstance(age, bool) or not isinstance(age, (int, float))
                        or not math.isfinite(age) or age < 0
                        or (age >= limit if name == "HEARTBEAT" else age > limit)):
                    reason = "invalid_or_stale_message_ages"
                    break
        if reason:
            self.invalidate(reason)
        else:
            if self.healthy_since is None:
                self.healthy_since = now
            self.confirmations.add(info["cursor"])
            self.last_failure = None
        healthy_s = 0 if self.healthy_since is None else max(0, now - self.healthy_since)
        accepted = reason is None and healthy_s >= self.healthy_seconds and len(self.confirmations) >= 3
        return {"accepted": accepted, "phase": reason or ("healthy" if accepted else "validating"),
                "observed_healthy_s": round(healthy_s, 3), "required_healthy_s": self.healthy_seconds,
                "fresh_integrity_records": len(self.confirmations), "fault_events_since_start": self.faults,
                "source": self.source, "integrity": info}


def read_journal(cursor=None):
    command = ["journalctl", "--boot=0", "_SYSTEMD_USER_UNIT=uav-v2.service",
               "--grep=uav.telemetry: (Flight controller|Flight telemetry)",
               "--output=json", "--no-pager"]
    if cursor:
        command.append("--after-cursor=" + cursor)
    result = subprocess.run(command, capture_output=True, text=True, timeout=8, check=True)
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


def read_reader():
    result = subprocess.run(["systemctl", "--user", "show", "uav-v2.service",
                             "--property=MainPID,ActiveState,NRestarts,UnitFileState"],
                            capture_output=True, text=True, timeout=5, check=True)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def observe(state, cursor):
    now = time.monotonic()
    config = json.loads((ROOT / "config/settings.json").read_text())["telemetry"]
    configured = (config["connection"] == BY_ID and config.get("usb_serial") == IDENTITY[2]
                  and config.get("usb_vid") == int(IDENTITY[0], 16)
                  and config.get("usb_pid") == int(IDENTITY[1], 16)
                  and config["system_id"] == 1 and config.get("component_id", 1) == 1)
    reader = read_reader()
    try:
        device = inspect()
        base = Path("/sys/bus/usb/devices") / device["device"]
        device["autosuspend_delay_ms"] = (base / "power/autosuspend_delay_ms").read_text().strip()
    except (OSError, RuntimeError) as exc:
        device = {"error": str(exc)}
    state.set_context(int(reader.get("MainPID", 0)), device.get("generation"), now)
    if getattr(state, "cursor_context", None) != state.context:
        cursor = None  # Find this process's connection entry even if it predates the observer.
        state.cursor_context = state.context
    entries = read_journal(cursor)
    allowed = {BY_ID, str(Path(BY_ID).resolve())}
    for entry in entries:
        state.consume(entry, allowed)
        cursor = entry.get("__CURSOR", cursor)
    # Do not carry evidence across a device replacement during this observation.
    try:
        after = inspect()
        same_generation = (after["device"], after["generation"]) == (device.get("device"), device.get("generation"))
    except (OSError, RuntimeError):
        same_generation = False
    device_ok = (configured and Path(BY_ID).exists() and device.get("power_control") == "on"
                 and device.get("runtime_status") == "active" and device.get("autosuspend_delay_ms") == "-1"
                 and same_generation)
    report = state.evaluate(time.monotonic(), device_ok,
                            reader.get("ActiveState") == "active" and int(reader.get("MainPID", 0)) > 0,
                            config["stale_s"], config["heartbeat_timeout_s"])
    report.update(boot_id=Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                  observer_started_boot_s=state.started, sampled_boot_s=time.monotonic(),
                  realtime=time.time(), device=device, reader=reader,
                  method="production_reader_journal", serial_port_opened=False,
                  transport_counters_verified=False, cold_boot_cycles_tested=0,
                  permanent_reliability_proven=False)
    return report, cursor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Read one sample; cannot prove a continuous window")
    parser.add_argument("--directory", type=Path, default=Path.home() / ".local/state/laiting-uav/ap6")
    args = parser.parse_args()
    os.umask(0o077)
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    state = LinkEvidence(boot_id, time.monotonic())
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    cursor = previous = None
    written_at = -math.inf
    if not args.once:
        args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    while not stopped.is_set():
        try:
            report, cursor = observe(state, cursor)
        except Exception as exc:
            cursor = None  # Recover if journald rotated away the previous cursor.
            state.invalidate("observation_failed")
            report = {"accepted": False, "phase": "observation_failed", "boot_id": boot_id,
                      "realtime": time.time(), "observer_started_boot_s": state.started,
                      "error": f"{type(exc).__name__}: {exc}", "serial_port_opened": False,
                      "permanent_reliability_proven": False}
        if args.once:
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if (report.get("source") or {}).get("verified") else 1
        signature = (report["accepted"], report["phase"], report.get("device", {}).get("generation"))
        if signature != previous or time.monotonic() - written_at >= 10:
            atomic_json(args.directory / (boot_id + ".json"), report)
            atomic_json(args.directory / "latest.json", report)
            written_at = time.monotonic()
        if signature != previous:
            print(json.dumps({"accepted": report["accepted"], "phase": report["phase"],
                              "boot_id": boot_id}, ensure_ascii=False), flush=True)
            previous = signature
        stopped.wait(5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
