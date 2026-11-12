import json
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from scripts import deploy_network_stability as deploy
from scripts import modem_path_manager as paths
from scripts import modem_recovery as recovery


def test_late_service_failure_restores_files_and_preexisting_service_settings(tmp_path, monkeypatch):
    # A real atomic replacement is used in a fake filesystem. Simulate failure
    # only after all files changed and systemd enable/restart already happened.
    target = tmp_path/'modem_path_manager.py'
    target.write_bytes(b'previous daemon')
    new = tmp_path/'network_boot_check.py'
    backup_root = tmp_path/'backups'
    backup_root.mkdir()
    original_mkdtemp = deploy.tempfile.mkdtemp
    monkeypatch.setattr(deploy.tempfile, 'mkdtemp',
                        lambda **kw: original_mkdtemp(prefix=kw['prefix'], dir=backup_root))
    monkeypatch.setattr(recovery, 'modem_profile_check', lambda: None)
    monkeypatch.setattr(paths, 'load_config', lambda: {})
    original_mkdir = Path.mkdir
    def mkdir(path, *args, **kwargs):
        if str(path) == '/var/backups/laiting-uav':
            return
        return original_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'mkdir', mkdir)
    commands = []
    def run(*args, check=True):
        commands.append(args)
        if args[:2] == ('systemctl', 'is-enabled'):
            return Mock(stdout='disabled' if args[-1] == deploy.BOOT_UNIT else 'enabled')
        if args[:2] == ('systemctl', 'is-active'):
            return Mock(returncode=1 if args[-1] == deploy.BOOT_UNIT else 0)
        if args == ('systemctl', 'restart', deploy.SERVICES[1]):
            assert target.read_bytes() == b'new daemon'
            assert new.exists()
            raise subprocess.CalledProcessError(1, args)
        return Mock(returncode=0, stdout='')
    monkeypatch.setattr(deploy, 'run', run)
    with pytest.raises(subprocess.CalledProcessError):
        deploy.deploy([(target, b'new daemon'), (new, b'new boot checker')])
    assert target.read_bytes() == b'previous daemon'
    assert not new.exists()
    assert ('systemctl', 'disable', deploy.BOOT_UNIT) in commands
    assert ('systemctl', 'stop', '--no-block', deploy.BOOT_UNIT) in commands
    for unit in deploy.SERVICES:
        assert ('systemctl', 'restart', '--no-block', unit) in commands
    assert not any(args[:2] == ('systemctl', 'disable') and args[-1] in deploy.SERVICES for args in commands)
    backup = next(backup_root.iterdir())
    assert (backup/target.name).read_bytes() == b'previous daemon'
    assert json.loads((backup/'services.json').read_text())['active'][deploy.BOOT_UNIT] is False
