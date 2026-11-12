"""USB policy deployment is scoped to AP6 and rolls back on udev failures."""
from pathlib import Path
import subprocess

import pytest
from scripts import deploy_ap6_usb_policy as policy
from scripts.ap6_usb_recovery import IDENTITY


@pytest.fixture
def host(tmp_path, monkeypatch):
    usb = tmp_path / "usb"
    device = usb / "devices/1-2.4"
    device.mkdir(parents=True)
    for name, value in zip(("idVendor", "idProduct", "serial"), IDENTITY):
        (device / name).write_text(value)
    for name, value in (("busnum", "1"), ("devnum", "18"), ("power/control", "on"),
                        ("power/runtime_status", "active"), ("power/autosuspend_delay_ms", "2000")):
        (device / name).parent.mkdir(exist_ok=True)
        (device / name).write_text(value)
    driver = usb / "drivers/cdc_acm"
    driver.mkdir(parents=True)
    interface = usb / "devices/1-2.4:1.0"
    interface.mkdir()
    (interface / "driver").symlink_to(driver)
    serial = tmp_path / "ttyACM0"
    serial.touch()
    by_id = tmp_path / "by-id"
    by_id.symlink_to(serial)
    tty_root = tmp_path / "tty"
    tty = tty_root / "ttyACM0"
    tty.mkdir(parents=True)
    (tty / "device").symlink_to(device / "interface")
    destination = tmp_path / "rules/policy.rules"
    destination.parent.mkdir()
    args = dict(sys_usb=usb, by_id=by_id, tty_root=tty_root, destination=destination,
                backup_root=tmp_path / "backups")
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if command[1] == "trigger" and command[-1] == str(device):
            (device / "power/autosuspend_delay_ms").write_text("-1")
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(policy.os, "geteuid", lambda: 0)
    monkeypatch.setattr(policy.subprocess, "run", run)
    return args, device, calls


def test_only_exact_device_and_tty_receive_change_events(host):
    args, device, calls = host
    result = policy.install("1:18", **args)
    assert result["policy_installed"] and not result["usb_reset_performed"]
    assert not result["services_restarted"] and not result["telemetry_verified"]
    triggers = [command for command in calls if command[1] == "trigger"]
    assert [command[-1] for command in triggers] == [str(device), str(args["tty_root"] / "ttyACM0")]
    assert all(command[2:4] == ["--action=change", "--settle"] for command in triggers)


@pytest.mark.parametrize("field,value", [("devnum", "19"), ("serial", "another-ap6")])
def test_stale_generation_or_different_device_refused_before_changes(host, field, value):
    args, device, calls = host
    (device / field).write_text(value)
    with pytest.raises(RuntimeError):
        policy.install("1:18", **args)
    assert not args["destination"].exists() and not calls


def test_non_root_refused_before_changes(host, monkeypatch):
    args, device, calls = host
    monkeypatch.setattr(policy.os, "geteuid", lambda: 1000)
    with pytest.raises(PermissionError):
        policy.install("1:18", **args)
    assert not args["destination"].exists() and not calls


@pytest.mark.parametrize("existed", [False, True])
def test_udev_failure_restores_prior_policy_and_device_attributes(host, monkeypatch, existed):
    args, device, calls = host
    if existed:
        args["destination"].write_text("prior policy\n")
    def fail_second_trigger(command, **kwargs):
        calls.append(command)
        if command[1] == "trigger":
            if command[-1] == str(device):
                (device / "power/autosuspend_delay_ms").write_text("-1")
            else:
                raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(policy.subprocess, "run", fail_second_trigger)
    with pytest.raises(subprocess.CalledProcessError):
        policy.install("1:18", **args)
    assert (device / "power/autosuspend_delay_ms").read_text().strip() == "2000"
    assert args["destination"].exists() == existed
    if existed:
        assert args["destination"].read_text() == "prior policy\n"


def test_unrelated_tty_refused(host):
    args, device, calls = host
    tty = args["tty_root"] / "ttyACM0/device"
    tty.unlink()
    tty.symlink_to(args["tty_root"] / "unrelated")
    with pytest.raises(RuntimeError, match="serial node"):
        policy.install("1:18", **args)
    assert not args["destination"].exists() and not calls
