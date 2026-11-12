"""Interrupted staging must leave the installed daemon/service intact."""
from pathlib import Path

import pytest

from scripts import deploy_modem_paths as deploy


@pytest.mark.parametrize('contents', [b'', b'print(\n'+b'#'*200])
def test_empty_or_broken_source_preserves_installed_file(tmp_path, contents):
    source, installed = tmp_path/'source.py', tmp_path/'installed.py'
    source.write_bytes(contents)
    installed.write_bytes(b'old working daemon')
    with pytest.raises((ValueError, SyntaxError)):
        deploy.stage(source, installed, 0o755)
    assert installed.read_bytes() == b'old working daemon'


def test_interrupt_before_atomic_replace_keeps_installed_file(tmp_path, monkeypatch):
    source, installed = tmp_path/'source.py', tmp_path/'installed.py'
    source.write_bytes(b'#'+b' ' * 200+b'\nprint("valid")\n')
    installed.write_bytes(b'old working daemon')
    monkeypatch.setattr(deploy.os, 'fsync', lambda fd: (_ for _ in ()).throw(InterruptedError()))
    with pytest.raises(InterruptedError):
        deploy.stage(source, installed, 0o755)
    assert installed.read_bytes() == b'old working daemon'
    assert not list(tmp_path.glob('.installed.py-verified-*'))


def test_verified_stage_is_complete_but_does_not_overwrite_live_file(tmp_path):
    source, installed = tmp_path/'source.py', tmp_path/'installed.py'
    source.write_bytes(b'#'+b' '*200+b'\nprint("valid")\n')
    installed.write_bytes(b'old working daemon')
    temporary, digest = deploy.stage(source, installed, 0o755)
    assert temporary.read_bytes() == source.read_bytes()
    assert temporary.stat().st_mode & 0o777 == 0o755
    assert installed.read_bytes() == b'old working daemon'
    assert len(digest) == 64
