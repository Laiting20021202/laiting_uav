"""AP6 driver rebind is pinned to identity and current USB generation."""
from pathlib import Path
import stat
from types import SimpleNamespace
import pytest
from scripts import ap6_usb_recovery as recovery


@pytest.fixture
def usb(tmp_path):
    root = tmp_path / 'usb'
    device = root / 'devices/1-2.4'
    driver = root / 'drivers/cdc_acm'
    device.mkdir(parents=True)
    driver.mkdir(parents=True)
    for name, value in zip(('idVendor', 'idProduct', 'serial'), recovery.IDENTITY):
        (device / name).write_text(value)
    for name, value in (('busnum', '1'), ('devnum', '13'),
                        ('dev', '189:12'),
                        ('power/control', 'on'), ('power/runtime_status', 'active')):
        (device / name).parent.mkdir(exist_ok=True)
        (device / name).write_text(value)
    interface = root / 'devices/1-2.4:1.0'
    interface.mkdir()
    (interface / 'driver').symlink_to(driver)
    (driver / 'unbind').touch()
    (driver / 'bind').touch()
    return root


def test_inspect_is_read_only_and_pins_exact_ap6(usb):
    result = recovery.inspect(usb)
    assert result['generation'] == '1:13' and result['interface'] == '1-2.4:1.0'
    assert (usb / 'drivers/cdc_acm/unbind').read_text() == ''
    assert (usb / 'drivers/cdc_acm/bind').read_text() == ''


@pytest.mark.parametrize('field,value', [('serial', 'other'), ('idVendor', '1234'), ('idProduct', '5678')])
def test_other_device_is_never_rebound(usb, field, value):
    (usb / 'devices/1-2.4' / field).write_text(value)
    with pytest.raises(RuntimeError, match='found 0'):
        recovery.rebind('1:13', usb)
    assert (usb / 'drivers/cdc_acm/unbind').read_text() == ''


def test_stale_generation_is_refused_before_mutation(usb):
    with pytest.raises(RuntimeError, match='no longer exists'):
        recovery.rebind('1:12', usb)
    assert (usb / 'drivers/cdc_acm/unbind').read_text() == ''


def test_single_control_interface_rebind_does_not_claim_telemetry_recovery(usb, monkeypatch):
    def unbound(_):
        (usb / 'devices/1-2.4:1.0/driver').unlink()
    monkeypatch.setattr(recovery.time, 'sleep', unbound)
    result = recovery.rebind('1:13', usb)
    assert result['rebound'] and not result['telemetry_verified']
    assert (usb / 'drivers/cdc_acm/unbind').read_text() == '1-2.4:1.0'
    assert (usb / 'drivers/cdc_acm/bind').read_text() == '1-2.4:1.0'


def test_reenumeration_after_unbind_does_not_bind_new_device(usb, monkeypatch):
    def replacement(_):
        (usb / 'devices/1-2.4:1.0/driver').unlink()
        (usb / 'devices/1-2.4/devnum').write_text('14')
    monkeypatch.setattr(recovery.time, 'sleep', replacement)
    with pytest.raises(RuntimeError, match='generation changed'):
        recovery.rebind('1:13', usb)
    assert (usb / 'drivers/cdc_acm/bind').read_text() == ''


def test_cancelled_wait_still_restores_same_interface(usb, monkeypatch):
    def cancelled(_):
        (usb / 'devices/1-2.4:1.0/driver').unlink()
        raise KeyboardInterrupt
    monkeypatch.setattr(recovery.time, 'sleep', cancelled)
    with pytest.raises(KeyboardInterrupt):
        recovery.rebind('1:13', usb)
    assert (usb / 'drivers/cdc_acm/bind').read_text() == '1-2.4:1.0'


@pytest.fixture
def usbfs_calls(monkeypatch):
    calls = []
    def open_node(path, flags):
        calls.append(('open', str(path), flags))
        return 42
    monkeypatch.setattr(recovery.os, 'open', open_node)
    monkeypatch.setattr(recovery.os, 'fstat', lambda fd: SimpleNamespace(
        st_mode=stat.S_IFCHR | 0o600, st_rdev=recovery.os.makedev(189, 12)))
    monkeypatch.setattr(recovery.os, 'close', lambda fd: calls.append(('close', fd)))
    monkeypatch.setattr(recovery.fcntl, 'ioctl', lambda *args: calls.append(('ioctl', *args)))
    return calls


def test_reset_uses_only_verified_ap6_usb_node(usb, usbfs_calls):
    result = recovery.reset('1:13', usb)
    assert result['usb_reset_returned_success'] and not result['telemetry_verified']
    assert usbfs_calls == [
        ('open', '/dev/bus/usb/001/013', recovery.os.O_RDWR | recovery.os.O_NOFOLLOW | recovery.os.O_CLOEXEC),
        ('ioctl', 42, recovery.USBDEVFS_RESET, 0), ('close', 42)]
    assert (usb / 'drivers/cdc_acm/unbind').read_text() == ''
    assert (usb / 'drivers/cdc_acm/bind').read_text() == ''


def test_stale_reset_generation_does_not_open_any_usb_node(usb, usbfs_calls):
    with pytest.raises(RuntimeError, match='no longer exists'):
        recovery.reset('1:12', usb)
    assert not usbfs_calls


def test_reset_rechecks_generation_after_open(usb, usbfs_calls, monkeypatch):
    original = recovery.os.open
    def changed(path, flags):
        fd = original(path, flags)
        (usb / 'devices/1-2.4/devnum').write_text('14')
        return fd
    monkeypatch.setattr(recovery.os, 'open', changed)
    with pytest.raises(RuntimeError, match='generation changed'):
        recovery.reset('1:13', usb)
    assert [call[0] for call in usbfs_calls] == ['open', 'close']


@pytest.mark.parametrize('mode,rdev', [(stat.S_IFREG, 0), (stat.S_IFCHR, 189 * 256 + 11)])
def test_reset_refuses_wrong_node_and_closes_fd(usb, usbfs_calls, monkeypatch, mode, rdev):
    monkeypatch.setattr(recovery.os, 'fstat', lambda fd: SimpleNamespace(st_mode=mode, st_rdev=rdev))
    with pytest.raises(RuntimeError, match='not the verified AP6'):
        recovery.reset('1:13', usb)
    assert [call[0] for call in usbfs_calls] == ['open', 'close']


def test_reset_refuses_wrong_sysfs_device_number(usb, usbfs_calls):
    (usb / 'devices/1-2.4/dev').write_text('189:11')
    with pytest.raises(RuntimeError, match='sysfs USB node identity mismatch'):
        recovery.reset('1:13', usb)
    assert not usbfs_calls


def test_reset_ioctl_error_closes_fd_without_retry(usb, usbfs_calls, monkeypatch):
    def failed(*args):
        usbfs_calls.append(('ioctl', *args))
        raise OSError('USB reset failed')
    monkeypatch.setattr(recovery.fcntl, 'ioctl', failed)
    with pytest.raises(OSError, match='USB reset failed'):
        recovery.reset('1:13', usb)
    assert [call[0] for call in usbfs_calls] == ['open', 'ioctl', 'close']
