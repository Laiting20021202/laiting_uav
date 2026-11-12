"""Fixed service restart command, shared cooldown, and systemd watchdog notifications."""
import os
import socket
import subprocess
import threading
import time


class ServiceControl:
    def __init__(self, demo=False):
        self.enabled = not demo and os.environ.get("UAV_MANAGED_SERVICE") == "1"
        self.lock = threading.Lock()
        self.last_restart = -float("inf")

    def restart(self):
        if not self.enabled:
            raise ValueError("Service restart is available only in the managed live service")
        with self.lock:
            if time.monotonic()-self.last_restart < 30:
                raise RuntimeError("Restart already requested; please wait")
            # A systemd-owned timer returns the HTTP response before replacing this process.
            subprocess.run(["systemd-run", "--user", "--collect", "--unit=uav-v2-restart-request",
                            "--on-active=2s", "--timer-property=RemainAfterElapse=no",
                            "/usr/bin/systemctl", "--user", "--no-block", "restart", "uav-v2.service"],
                           check=True, capture_output=True, timeout=5)
            self.last_restart = time.monotonic()


def notify_watchdog():
    path = os.environ.get("NOTIFY_SOCKET")
    if not path:
        return
    if path.startswith("@"):
        path = "\0"+path[1:]
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        sock.settimeout(1)
        sock.sendto(b"WATCHDOG=1", path)
