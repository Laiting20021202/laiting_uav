#!/usr/bin/env python3
"""Install/restart per-user systemd units; hardware app and tunnel fail independently."""
import argparse
import getpass
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def unit(description, command, watchdog=False, tunnel=False):
    extra = "Environment=UAV_MANAGED_SERVICE=1\nWatchdogSec=120\nNotifyAccess=main\n" if watchdog else ""
    if tunnel:
        extra += "WatchdogSec=120\nNotifyAccess=main\n"
    # A user manager's network-online.target does not wait for system networking.
    # Bound the NM wait: offline boot must still reach the reconnect supervisor.
    before = "ExecStartPre=-/usr/bin/nm-online -s -q -t 30\n" if tunnel else ""
    return f"""[Unit]
Description={description}
StartLimitIntervalSec=0

[Service]
Type=simple
WorkingDirectory={ROOT}
{before}ExecStart={command}
Restart=always
RestartSec=5
TimeoutStopSec={15 if tunnel else 80}
KillMode=control-group
UMask=0077
Environment=PYTHONDONTWRITEBYTECODE=1
{extra}\
NoNewPrivileges=true
RestrictSUIDSGID=true

[Install]
WantedBy=default.target
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tunnel", action="store_true", help="Also install the authenticated named tunnel")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--tunnel-only", action="store_true", help="Install/restart only the tunnel; keep camera and telemetry running")
    scope.add_argument("--diagnostics-only", action="store_true", help="Install/restart only read-only network/device diagnostics")
    args = parser.parse_args()
    units = Path.home()/".config/systemd/user"
    units.mkdir(parents=True, exist_ok=True)
    names = []
    if not (args.tunnel_only or args.diagnostics_only):
        (units/"uav-v2.service").write_text(unit("UAV V2 camera and telemetry dashboard", f"{ROOT}/.venv/bin/python -m uav", watchdog=True))
        names.append("uav-v2.service")
    if args.tunnel or args.tunnel_only:
        binary = Path.home()/".local/bin/cloudflared"
        config = ROOT/"config/cloudflared.yml"
        if not binary.exists() or not config.exists():
            parser.error("Run scripts/cloudflare.py install and setup first")
        command = (f"/bin/sh {ROOT}/scripts/tunnel_dns.sh "
                   f"{ROOT}/.venv/bin/python {ROOT}/scripts/tunnel_supervisor.py "
                   "--ssh-hostname ssh-uav.laiting1202.com "
                   "--public-health-url https://uav.laiting1202.com/healthz -- "
                   f"{binary} --no-autoupdate tunnel --config {config} --grace-period 5s run")
        (units/"uav-cloudflared.service").write_text(unit("UAV Cloudflare web and SSH tunnel", command, tunnel=True))
        names.append("uav-cloudflared.service")
    if args.tunnel or args.tunnel_only or args.diagnostics_only:
        (units/"uav-network-recorder.service").write_text(unit(
            "UAV persistent network diagnostics",
            f"{ROOT}/.venv/bin/python {ROOT}/scripts/network_recorder.py"))
        names.append("uav-network-recorder.service")
        (units/"uav-network-health.service").write_text(unit(
            "UAV independent DNS and Internet diagnostics (read-only)",
            f"/bin/sh {ROOT}/scripts/tunnel_dns.sh "
            f"{ROOT}/.venv/bin/python {ROOT}/scripts/network_health.py"))
        names.append("uav-network-health.service")
        (units/"uav-flight-network-monitor.service").write_text(unit(
            "UAV flight network observation (read-only, 10 second samples)",
            f"{ROOT}/.venv/bin/python {ROOT}/scripts/flight_network_monitor.py"))
        names.append("uav-flight-network-monitor.service")
        (units/"uav-device-evidence.service").write_text(unit(
            "UAV host power and USB evidence (read-only, one second samples)",
            f"{ROOT}/.venv/bin/python {ROOT}/scripts/device_evidence.py"))
        names.append("uav-device-evidence.service")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "--user", "enable", *names], check=True)
    subprocess.run(["systemctl", "--user", "restart", *names], check=True)
    result = subprocess.run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"], capture_output=True, text=True, check=True)
    print(result.stdout.strip())
    if "Linger=yes" not in result.stdout:
        print(f"要在未登入桌面時開機自啟，需主機管理員執行一次：sudo loginctl enable-linger {getpass.getuser()}")
    subprocess.run(["systemctl", "--user", "is-active", *names], check=True)


if __name__ == "__main__":
    main()
