"""Verify the native ABI and prove this probe cannot send hub state changes."""
import ctypes
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
from types import SimpleNamespace

import pytest

from scripts import usb_hub_readonly_probe as hub


DESCRIPTOR = bytes((9, 0x29, 4, 0x09, 0, 50, 0, 0, 0xff))
PORT_STATUS = {2: struct.pack('<HH', 0x0101, 0x0002),
               4: struct.pack('<HH', 0x0103, 0)}


def make_hub(tmp_path, name=hub.HUB_NAME, **changes):
    device = tmp_path/name
    device.mkdir()
    for key, value in {
        'idVendor': hub.VENDOR, 'idProduct': hub.PRODUCT, 'bDeviceClass': '09',
        'busnum': '1', 'devnum': '2', 'speed': '480', **changes,
    }.items():
        (device/key).write_text(value)
    return device


@pytest.fixture
def fake_usbfs(monkeypatch, tmp_path):
    identity = hub.HubIdentity(tmp_path/hub.HUB_NAME, 1, 2, 50)
    monkeypatch.setattr(hub, 'require_root', lambda: None)
    monkeypatch.setattr(hub, 'hub_identity', lambda expected_generation: identity)
    node_stat = SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_ino=51,
                                st_rdev=os.makedev(189, 1))
    opened, closed, requests = [], [], []
    monkeypatch.setattr(hub.os, 'open', lambda path, flags: opened.append((path, flags)) or 61)
    monkeypatch.setattr(hub.os, 'close', closed.append)
    monkeypatch.setattr(hub.os, 'fstat', lambda fd: node_stat)
    original_stat = hub.os.stat

    def node_info(path, *args, **kwargs):
        return node_stat if str(path).startswith('/dev/bus/usb/') else original_stat(path, *args, **kwargs)

    monkeypatch.setattr(hub.os, 'stat', node_info)

    def ioctl(fd, command, pointer):
        transfer = ctypes.cast(pointer, ctypes.POINTER(hub.ControlTransfer)).contents
        request = (transfer.bRequestType, transfer.bRequest, transfer.wValue,
                   transfer.wIndex, transfer.wLength)
        assert fd == 61 and command == hub.USBDEVFS_CONTROL
        assert request in hub.APPROVED_READS and request[0] & 0x80
        assert transfer.timeout == 250
        requests.append(request)
        payload = DESCRIPTOR if request == hub.GET_HUB_DESCRIPTOR else PORT_STATUS[request[3]]
        ctypes.memmove(transfer.data, payload, len(payload))
        return len(payload)

    monkeypatch.setattr(hub.LIBC, 'ioctl', ioctl)
    return SimpleNamespace(identity=identity, node_stat=node_stat, opened=opened,
                           closed=closed, requests=requests)


def test_ctypes_abi_matches_the_local_kernel_uapi_header(tmp_path):
    compiler = shutil.which('cc')
    if not compiler or not Path('/usr/include/linux/usbdevice_fs.h').is_file():
        pytest.skip('The local Linux UAPI C compiler is unavailable')
    source = tmp_path/'abi.c'
    source.write_text('''#include <stdio.h>
#include <stddef.h>
#include <sys/ioctl.h>
#include <linux/usbdevice_fs.h>
int main(void) {
    printf("%zu %zu %zu %lu\\n", sizeof(struct usbdevfs_ctrltransfer),
           offsetof(struct usbdevfs_ctrltransfer, timeout),
           offsetof(struct usbdevfs_ctrltransfer, data),
           (unsigned long)USBDEVFS_CONTROL);
    return 0;
}
''')
    executable = tmp_path/'abi'
    subprocess.run([compiler, str(source), '-o', str(executable)], check=True, timeout=10,
                   capture_output=True)
    result = subprocess.run([str(executable)], check=True, timeout=2, capture_output=True, text=True)
    native = tuple(map(int, result.stdout.split()))
    hub.check_abi()
    assert native == (24, 8, 16, hub.USBDEVFS_CONTROL)
    assert ctypes.sizeof(hub.ControlTransfer) == native[0]
    assert hub.ControlTransfer.data.offset == native[2]


def test_incorrect_control_abi_is_rejected_before_open(fake_usbfs, monkeypatch):
    original_sizeof = hub.ctypes.sizeof
    monkeypatch.setattr(hub.ctypes, 'sizeof', lambda value: 16 if value is hub.ControlTransfer else original_sizeof(value))
    with pytest.raises(RuntimeError, match='Unsupported native'):
        with hub.HubReadOnlyProbe():
            pass
    assert fake_usbfs.opened == []


def test_only_the_fixed_hub_file_and_three_read_requests_are_used(fake_usbfs, monkeypatch):
    clock = SimpleNamespace(now=100.0)
    monkeypatch.setattr(hub.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(hub.time, 'sleep', lambda delay: setattr(clock, 'now', clock.now+delay))
    report = hub.collect()
    assert fake_usbfs.opened == [('/dev/bus/usb/001/002', os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)]
    assert fake_usbfs.closed == [61]
    assert fake_usbfs.requests == [hub.GET_HUB_DESCRIPTOR]+[
        request for _ in range(20) for request in (hub.GET_PORT_STATUS[2], hub.GET_PORT_STATUS[4])]
    assert report['read_only'] and report['stop_reason'] == 'sample_limit'
    assert len(report['samples']) == 20 and report['elapsed_seconds'] == pytest.approx(3.8)
    assert report['hub']['descriptor']['power_switching'] == 'individual'
    assert report['samples'][0]['ports']['2']['connected'] is True
    assert report['samples'][0]['ports']['2']['enabled'] is False
    assert report['samples'][0]['ports']['4']['speed'] == 'full'


@pytest.mark.parametrize('transfer_request', [
    (0x23, 3, 8, 2, 0),   # SET_FEATURE(PORT_POWER)
    (0x23, 1, 8, 2, 0),   # CLEAR_FEATURE(PORT_POWER)
    (0x23, 3, 4, 2, 0),   # SET_FEATURE(PORT_RESET)
    (0x00, 9, 1, 0, 0),   # SET_CONFIGURATION
    (0xa3, 0, 0, 1, 4),   # Unapproved port
    (0xa3, 0, 0, 4, 8),   # Unapproved size/extended request
    (0xa0, 6, 0x0100, 0, 18),  # Device/string/data requests are not available
    [0xa3, 0, 0, 2, 4],
    (0xa3, False, 0, 2, 4),
])
def test_nonapproved_requests_never_reach_any_ioctl(fake_usbfs, transfer_request):
    with hub.HubReadOnlyProbe() as probe:
        with pytest.raises(ValueError, match='three fixed hub IN requests'):
            probe.read(transfer_request, 1e20)
    assert fake_usbfs.requests == []


def test_nonroot_is_rejected_before_any_hub_open(monkeypatch):
    monkeypatch.setattr(hub.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(hub.os, 'open', lambda *args: pytest.fail('Nonroot must not open usbfs'))
    with pytest.raises(PermissionError, match='root privileges'):
        with hub.HubReadOnlyProbe():
            pass
    with pytest.raises(PermissionError):
        hub.collect()


@pytest.mark.parametrize('changes', [
    {'idVendor': '1286'}, {'idProduct': '4e31'}, {'bDeviceClass': '00'},
    {'busnum': '2'}, {'devnum': '3'}, {'busnum': 'bad'}, {'devnum': '0'},
    {'speed': '5000'},
])
def test_fixed_hub_identity_and_generation_are_required(tmp_path, changes):
    make_hub(tmp_path, **changes)
    with pytest.raises(hub.HubIdentityError):
        hub.hub_identity(usb_root=tmp_path)


def test_missing_fixed_hub_never_falls_back_to_an_identical_hub(tmp_path):
    make_hub(tmp_path, '1-3')
    make_hub(tmp_path, '1-4')
    with pytest.raises(hub.HubIdentityError, match='unavailable'):
        hub.hub_identity(usb_root=tmp_path)


def test_fixed_topology_wins_even_when_similar_hubs_exist(tmp_path):
    target = make_hub(tmp_path)
    make_hub(tmp_path, '1-3')
    identity = hub.hub_identity(usb_root=tmp_path)
    assert identity.device == target and identity.bus == 1 and identity.number == 2


def test_symlink_to_a_different_hub_path_is_not_accepted(tmp_path):
    other = make_hub(tmp_path, '1-3')
    (tmp_path/hub.HUB_NAME).symlink_to(other)
    with pytest.raises(hub.HubIdentityError, match='topology'):
        hub.hub_identity(usb_root=tmp_path)


@pytest.mark.parametrize('change', ['generation', 'inode', 'node'])
def test_identity_change_before_request_stops_without_ioctl(fake_usbfs, monkeypatch, change):
    with hub.HubReadOnlyProbe() as probe:
        if change == 'node':
            fake_usbfs.node_stat.st_ino += 1
        else:
            before = fake_usbfs.identity
            replacement = hub.HubIdentity(before.device, before.bus,
                before.number+1 if change == 'generation' else before.number,
                before.sysfs_inode+1 if change == 'inode' else before.sysfs_inode)
            monkeypatch.setattr(hub, 'hub_identity', lambda *args: replacement)
        with pytest.raises(hub.HubIdentityError):
            probe.read(hub.GET_PORT_STATUS[2], 1e20)
    assert fake_usbfs.requests == [] and fake_usbfs.closed == [61]


def test_changed_hub_after_response_is_not_published_as_current(fake_usbfs, monkeypatch):
    original_ioctl = hub.LIBC.ioctl

    def changed(*args):
        result = original_ioctl(*args)
        fake_usbfs.node_stat.st_ino += 1
        return result

    monkeypatch.setattr(hub.LIBC, 'ioctl', changed)
    with hub.HubReadOnlyProbe() as probe:
        with pytest.raises(hub.HubIdentityError):
            probe.read(hub.GET_PORT_STATUS[2], 1e20)
    assert len(fake_usbfs.requests) == 1


def test_unexpected_usbfs_node_is_rejected_before_any_request(fake_usbfs):
    fake_usbfs.node_stat.st_rdev = os.makedev(189, 4)  # Flight controller 001/005.
    with pytest.raises(hub.HubIdentityError, match='not the fixed hub'):
        with hub.HubReadOnlyProbe():
            pass
    assert fake_usbfs.requests == [] and fake_usbfs.closed == [61]


def test_time_limit_bounds_even_repeated_control_timeouts(fake_usbfs, monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(hub.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(hub.time, 'sleep', lambda delay: setattr(clock, 'now', clock.now+delay))

    def timeout(fd, command, pointer):
        transfer = ctypes.cast(pointer, ctypes.POINTER(hub.ControlTransfer)).contents
        assert transfer.timeout == 250
        clock.now += 0.25
        ctypes.set_errno(errno.ETIMEDOUT)
        return -1

    monkeypatch.setattr(hub.LIBC, 'ioctl', timeout)
    report = hub.collect()
    assert report['stop_reason'] == 'time_limit'
    assert report['elapsed_seconds'] <= 10
    assert len(report['samples']) < 20
    assert fake_usbfs.closed == [61]


def test_disappearing_hub_is_not_reopened_or_replaced(fake_usbfs, monkeypatch):
    def unavailable(*args):
        ctypes.set_errno(errno.ENODEV)
        return -1

    monkeypatch.setattr(hub.LIBC, 'ioctl', unavailable)
    report = hub.collect()
    assert report['stop_reason'] == 'hub_identity_unavailable_or_changed'
    assert len(fake_usbfs.opened) == 1 and fake_usbfs.closed == [61]
    assert len(report['samples']) == 1


@pytest.mark.parametrize('length', [0, 3, 8, 10])
def test_short_or_long_descriptor_is_refused(length):
    with pytest.raises(ValueError):
        hub.decode_descriptor(bytes(length))


@pytest.mark.parametrize('length', [0, 2, 8])
def test_extended_or_short_port_status_is_refused(length):
    with pytest.raises(ValueError):
        hub.decode_port_status(bytes(length))


@pytest.mark.parametrize('status, expected', [
    (0x0100, 'disconnected'), (0x0101, 'not_enabled'),
    (0x0103, 'full'), (0x0303, 'low'), (0x0503, 'high'), (0x0703, 'invalid'),
])
def test_enabled_connection_is_required_to_claim_a_usb_speed(status, expected):
    result = hub.decode_port_status(struct.pack('<HH', status, 0x001f))
    assert result['speed'] == expected
    assert all(result['change'][name] for name in ('connection', 'enable', 'suspend', 'overcurrent', 'reset'))


@pytest.mark.parametrize('limits', [
    (21, .2, 10), (0, .2, 10), (20, .1, 10), (20, 2, 10),
    (20, .2, 11), (20, .2, float('nan')),
])
def test_sampling_limits_cannot_be_overridden_unboundedly(limits):
    with pytest.raises(ValueError):
        hub.validate_limits(*limits)


def test_reports_are_atomic_private_and_contain_no_payload(tmp_path):
    directory = tmp_path/'private'
    report = {'read_only': True, 'hub': {'descriptor': hub.decode_descriptor(DESCRIPTOR)},
              'samples': [{'ports': {'2': hub.decode_port_status(PORT_STATUS[2])}}]}
    location = hub.save_report(report, directory)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(location.stat().st_mode) == 0o600
    assert json.loads(location.read_text()) == report
    assert list(directory.iterdir()) == [location]
    assert 'raw' not in location.read_text() and 'payload' not in location.read_text()


def test_public_or_symlinked_report_directory_is_refused(tmp_path):
    directory = tmp_path/'public'
    directory.mkdir(mode=0o755)
    with pytest.raises(PermissionError):
        hub.save_report({}, directory)
    (tmp_path/'symlink').symlink_to(directory)
    with pytest.raises(PermissionError):
        hub.save_report({}, tmp_path/'symlink')
