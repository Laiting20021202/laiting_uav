#!/usr/bin/env python3
"""Persist this AP6's awake/permission policy; no reset or service restart."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

try:
    from .ap6_usb_recovery import SYS_USB, find_ap6, inspect, verify
except ImportError:
    from ap6_usb_recovery import SYS_USB, find_ap6, inspect, verify

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/udev/99-align-ap6-stability.rules"
DESTINATION = Path("/etc/udev/rules.d/99-align-ap6-stability.rules")
BY_ID = Path("/dev/serial/by-id/usb-ALIGN_AP6-M490-ds_330032000D51333331333534-if00")


def install(expected_generation, *, source=SOURCE, destination=DESTINATION, sys_usb=SYS_USB,
            by_id=BY_ID, tty_root=Path("/sys/class/tty"), backup_root=Path("/var/backups/laiting-uav")):
    if os.geteuid() != 0:
        raise PermissionError("Installing the AP6 udev policy requires root")
    target = find_ap6(sys_usb)
    if target["generation"] != expected_generation:
        raise RuntimeError("AP6 generation changed; inspect again before installing")
    verify(target, sys_usb)
    before = inspect(sys_usb)
    device = sys_usb / "devices" / target["device"]
    before["autosuspend_delay_ms"] = (device / "power/autosuspend_delay_ms").read_text().strip()
    tty_device = tty_root / by_id.resolve(strict=True).name
    if device.resolve() not in (tty_device / "device").resolve().parents:
        raise RuntimeError("The configured serial node is not part of the verified AP6")
    if destination.is_symlink():
        raise RuntimeError("Refusing to replace a symlinked udev policy")
    backup = backup_root / time.strftime("ap6-usb-policy-%Y%m%d-%H%M%S")
    backup.mkdir(mode=0o700, parents=True, exist_ok=False)
    existed = destination.exists()
    if existed:
        shutil.copy2(destination, backup / destination.name)
    (backup / "before.json").write_text(json.dumps(before, indent=2) + "\n")
    (backup / "before.json").chmod(0o600)
    contents = source.read_bytes()
    fd, temporary = tempfile.mkstemp(prefix=".ap6-policy-", dir=destination.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        verify(target, sys_usb)
        os.replace(temporary, destination)
        subprocess.run(["udevadm", "control", "--reload-rules"], check=True, timeout=8)
        # Only two exact nodes. No blanket trigger, unbind, or USB reset.
        for node in (device.resolve(), tty_device.resolve()):
            verify(target, sys_usb)
            subprocess.run(["udevadm", "trigger", "--action=change", "--settle", str(node)],
                           check=True, timeout=8)
        verify(target, sys_usb)
        after = inspect(sys_usb)
        after["autosuspend_delay_ms"] = (device / "power/autosuspend_delay_ms").read_text().strip()
        if after["power_control"] != "on" or after["autosuspend_delay_ms"] != "-1":
            raise RuntimeError("The AP6 awake policy did not apply")
    except Exception:
        if existed:
            shutil.copy2(backup / destination.name, destination)
        elif destination.exists():
            destination.unlink()
        subprocess.run(["udevadm", "control", "--reload-rules"], timeout=8, check=False)
        # Restore attributes only if the original device generation still exists.
        try:
            for name, value in (("autosuspend_delay_ms", before["autosuspend_delay_ms"]),
                                ("control", before["power_control"])):
                verify(target, sys_usb)
                (device / "power" / name).write_text(value + "\n")
        except (OSError, RuntimeError):
            pass
        raise
    finally:
        Path(temporary).unlink(missing_ok=True)
    result = {"policy_installed": True, "destination": str(destination), "backup": str(backup),
              "sha256": hashlib.sha256(contents).hexdigest(), "before": before, "after": after,
              "usb_reset_performed": False, "services_restarted": False, "telemetry_verified": False}
    (backup / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    (backup / "result.json").chmod(0o600)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-generation", required=True)
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise PermissionError("Installing the AP6 udev policy requires root")
        fd = os.open("/run/laiting-ap6-rebind.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            print(json.dumps(install(args.expected_generation), indent=2))
        finally:
            os.close(fd)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"{type(exc).__name__}: {exc}\n")


if __name__ == "__main__":
    main()
