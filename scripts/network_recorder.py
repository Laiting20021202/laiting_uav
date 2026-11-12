#!/usr/bin/env python3
"""Keep bounded network evidence with boot IDs and monotonic timestamps."""
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import subprocess


FIELDS = ("__REALTIME_TIMESTAMP", "__MONOTONIC_TIMESTAMP", "_BOOT_ID", "_TRANSPORT",
          "_SYSTEMD_UNIT", "_SYSTEMD_USER_UNIT", "_COMM", "PRIORITY", "MESSAGE")
KERNEL_EVENTS = re.compile(
    r"usb|cdc_ether|Booting Linux|panic|watchdog|thermal|voltage|out of memory|oom|killed process",
    re.IGNORECASE,
)
TUNNEL_EVENTS = re.compile(
    r"Internet route|tunnel|Tunnel|Public SSH|Public HTTP|Network upstream|Waiting for a stable", re.IGNORECASE,
)


def select_record(record):
    """Store diagnostic fields only; ignore unrelated kernel/application output."""
    message = record.get("MESSAGE", "")
    if not isinstance(message, str):
        return None
    if record.get("_TRANSPORT") == "kernel":
        if not KERNEL_EVENTS.search(message):
            return None
    elif record.get("_SYSTEMD_USER_UNIT") == "uav-cloudflared.service":
        if not TUNNEL_EVENTS.search(message):
            return None
    elif record.get("_SYSTEMD_USER_UNIT") == "uav-network-health.service":
        if "Network upstream" not in message:
            return None
    elif record.get("_SYSTEMD_USER_UNIT") == "uav-v2.service":
        # Keep every transport transition, including rapid disconnects that
        # the ten-second sampler can miss, across host restarts.
        if "uav.telemetry:" not in message and "watchdog" not in message.lower():
            return None
    elif record.get("_SYSTEMD_UNIT") not in {
        "NetworkManager.service", "systemd-timesyncd.service", "systemd-logind.service",
        "systemd-resolved.service",
        "uav-modem-recovery.service",
        "uav-modem-rf-guard.service", "uav-modem-paths.service",
        "uav-tunnel-resolver.service",
    }:
        return None
    return {key: record[key] for key in FIELDS if key in record}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path,
                        default=Path.home()/".local/state/laiting-uav/network")
    args = parser.parse_args()
    os.umask(0o077)
    args.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    handler = RotatingFileHandler(args.directory/"events.jsonl", maxBytes=4*1024*1024,
                                  backupCount=3, encoding="utf-8")
    logger = logging.getLogger("uav.network.recorder")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    # Replay this boot when starting, then follow live events. Replays preserve
    # their original timestamps and boot ID; no dependence on the wall clock.
    command = ["journalctl", "--boot=0", "--follow", "--no-tail", "--output=json",
               "--no-pager", "_TRANSPORT=kernel", "+",
               "_SYSTEMD_UNIT=NetworkManager.service", "+",
               "_SYSTEMD_UNIT=systemd-timesyncd.service", "+",
               "_SYSTEMD_UNIT=systemd-logind.service", "+",
               "_SYSTEMD_UNIT=systemd-resolved.service", "+",
               "_SYSTEMD_UNIT=uav-modem-recovery.service", "+",
               "_SYSTEMD_UNIT=uav-modem-rf-guard.service", "+",
               "_SYSTEMD_UNIT=uav-modem-paths.service", "+",
               "_SYSTEMD_UNIT=uav-tunnel-resolver.service", "+",
               "_SYSTEMD_USER_UNIT=uav-cloudflared.service", "+",
               "_SYSTEMD_USER_UNIT=uav-network-health.service", "+",
               "_SYSTEMD_USER_UNIT=uav-v2.service"]
    with subprocess.Popen(command, stdout=subprocess.PIPE, text=True) as child:
        try:
            for line in child.stdout:
                record = select_record(json.loads(line))
                if record is not None:
                    logger.info(json.dumps(record, ensure_ascii=False))
                    # Abrupt host resets lost part of syslog in this incident.
                    # Sync the small, filtered stream for the next investigation.
                    os.fsync(handler.stream.fileno())
            return child.wait() or 1  # Unexpected EOF: let systemd restart us.
        finally:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            handler.close()


if __name__ == "__main__":
    raise SystemExit(main())
