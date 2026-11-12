#!/usr/bin/env python3
"""Repair this UAV host's DNS ownership; preserve routes and running tunnels.

Apply: sudo python3 scripts/fix_host_dns.py apply
Undo:  sudo python3 scripts/fix_host_dns.py rollback /var/backups/laiting-uav/dns/<backup>
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile


PROFILE = "8391cb7e-8a3a-317c-bf18-612aa9c5300e"
INTERFACE = "enxac0033aa9633"
STUB = "/run/systemd/resolve/stub-resolv.conf"
RESOLVED = Path("/etc/systemd/resolved.conf.d/90-uav-dns.conf")
NETWORK_MANAGER = Path("/etc/NetworkManager/conf.d/90-uav-dns.conf")
WIREGUARD = Path("/etc/wireguard/wg0.conf")
PROPERTIES = {
    "ipv4.dns": "1.1.1.1,8.8.8.8",
    "ipv4.ignore-auto-dns": "yes",
    "ipv6.dns": "",
    "ipv6.ignore-auto-dns": "yes",
}
ENV = {**os.environ, "LC_ALL": "C", "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}


def run(*args, timeout=30):
    return subprocess.check_output(args, text=True, timeout=timeout, env=ENV).strip()


def atomic_write(path, content, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".uav-dns-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def without_wireguard_dns(content):
    """Remove only Interface DNS; preserve keys, peers, routes and comments."""
    interface = False
    lines = []
    for line in content.splitlines(keepends=True):
        section = re.match(r"\s*\[([^]]+)\]", line)
        if section:
            interface = section[1].lower() == "interface"
        if interface and re.match(r"\s*DNS\s*=", line, re.IGNORECASE):
            lines.append("# Host DNS is managed by NetworkManager and systemd-resolved.\n")
        else:
            lines.append(line)
    return "".join(lines)


def set_profile(properties):
    arguments = [part for pair in properties.items() for part in pair]
    run("nmcli", "connection", "modify", "uuid", PROFILE, *arguments)


def refresh():
    # Reload settings and reapply DNS on the existing link; never bring it down.
    run("systemctl", "reload", "NetworkManager")
    run("nmcli", "device", "reapply", INTERFACE)
    run("resolvectl", "flush-caches")


def check():
    code = (
        "import socket,time,json; t=time.monotonic(); "
        "a=socket.getaddrinfo('uav.laiting1202.com',443,type=socket.SOCK_STREAM); "
        "print(json.dumps({'addresses':len(a),'seconds':round(time.monotonic()-t,3)}))"
    )
    lookup = json.loads(run("/usr/bin/python3", "-c", code, timeout=10))
    health = json.loads(run(
        "curl", "--noproxy", "*", "-fsS", "--connect-timeout", "5", "--max-time", "10",
        "https://uav.laiting1202.com/healthz", timeout=12,
    ))
    if health != {"status": "ok"}:
        raise RuntimeError("Public health endpoint did not return the expected JSON")
    if Path("/etc/resolv.conf").resolve() != Path(STUB):
        raise RuntimeError("Host resolver is not the systemd-resolved stub")
    return {"lookup": lookup, "health": health, "resolver": STUB}


def rollback(backup):
    manifest = json.loads((backup / "manifest.json").read_text())
    for entry in manifest["files"]:
        target = Path(entry["path"])
        if target.exists() or target.is_symlink():
            target.unlink()
        if entry["present"]:
            shutil.copy2(backup / entry["saved"], target, follow_symlinks=False)
    set_profile(manifest["profile"])
    # Restore the resolver contribution without restarting WireGuard.
    if manifest["wireguard_record"] is not None:
        subprocess.run(
            ["resolvconf", "-a", "tun.wg0"], input=manifest["wireguard_record"],
            text=True, check=True, timeout=10, env=ENV,
        )
    run("systemctl", "restart", "systemd-resolved")
    refresh()
    print("Restored DNS backup:", backup)


def apply():
    if run("nmcli", "-g", "GENERAL.CON-UUID", "device", "show", INTERFACE) != PROFILE:
        raise RuntimeError("Expected mobile-network profile is not active; no changes made")
    if run("systemctl", "is-active", "systemd-resolved") != "active":
        raise RuntimeError("systemd-resolved must already be running")
    before_routes = run("ip", "-j", "route", "show")
    base = Path("/var/backups/laiting-uav/dns")
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = Path(tempfile.mkdtemp(prefix=datetime.now().strftime("%Y%m%d-%H%M%S-"), dir=base))
    paths = [Path("/etc/resolv.conf"), RESOLVED, NETWORK_MANAGER, WIREGUARD]
    entries = []
    for index, path in enumerate(paths):
        present = path.exists() or path.is_symlink()
        entry = {"path": str(path), "present": present, "saved": str(index)}
        if present:
            shutil.copy2(path, backup / str(index), follow_symlinks=False)
        entries.append(entry)
    record = Path("/run/resolvconf/interface/tun.wg0")
    manifest = {
        "files": entries,
        "profile": {key: run("nmcli", "-g", key, "connection", "show", "uuid", PROFILE) for key in PROPERTIES},
        "wireguard_record": record.read_text() if record.exists() else None,
        "routes": json.loads(before_routes),
    }
    atomic_write(backup / "manifest.json", json.dumps(manifest, indent=2) + "\n", 0o600)
    atomic_write(backup / "original-resolv.conf", Path("/etc/resolv.conf").read_text(), 0o600)
    print("DNS backup:", backup, flush=True)
    try:
        atomic_write(RESOLVED,
                     "# UAV host: local stub, with fallback when no link supplies DNS.\n"
                     "[Resolve]\nDNS=\nFallbackDNS=\n"
                     "FallbackDNS=1.1.1.1 1.0.0.1 8.8.8.8\nDNSStubListener=yes\n")
        atomic_write(NETWORK_MANAGER,
                     "# Give systemd-resolved exclusive ownership of host DNS.\n"
                     "[main]\ndns=systemd-resolved\nrc-manager=unmanaged\n")
        set_profile(PROPERTIES)
        if WIREGUARD.exists():
            atomic_write(WIREGUARD, without_wireguard_dns(WIREGUARD.read_text()), 0o600)
        # Make the local stub ready before switching applications to it.
        run("systemctl", "restart", "systemd-resolved")
        refresh()
        temporary = Path("/etc/.uav-resolv.conf")
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("Temporary resolver link already exists")
        temporary.symlink_to(STUB)
        os.replace(temporary, "/etc/resolv.conf")
        if record.exists():
            run("resolvconf", "-d", "tun.wg0")
        # Clear DNS imported from the former foreign resolv.conf.
        run("systemctl", "restart", "systemd-resolved")
        refresh()
        result = check()
        if json.loads(run("ip", "-j", "route", "show")) != json.loads(before_routes):
            raise RuntimeError("Route table changed during DNS repair")
        atomic_write(backup / "result.json", json.dumps(result, indent=2) + "\n", 0o600)
        print(json.dumps(result, indent=2))
        print("DNS repair applied. Rollback: sudo python3", Path(__file__).resolve(), "rollback", backup)
    except BaseException:
        print("DNS repair failed; restoring the previous configuration", flush=True)
        rollback(backup)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["apply", "check", "rollback"])
    parser.add_argument("backup", nargs="?", type=Path)
    args = parser.parse_args()
    if args.action != "check" and os.geteuid() != 0:
        parser.error("apply/rollback require root; use sudo locally")
    if args.action == "apply":
        apply()
    elif args.action == "check":
        print(json.dumps(check(), indent=2))
    elif args.backup is None:
        parser.error("rollback requires the backup directory printed by apply")
    else:
        rollback(args.backup)


if __name__ == "__main__":
    main()
