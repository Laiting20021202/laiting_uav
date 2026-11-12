"""Read-only expected kernel identity; no live /boot or hardware access."""
from types import SimpleNamespace

import pytest

from scripts import xhci_postboot_check as helper


@pytest.mark.parametrize('halt', [False, True])
def test_kernel_identity_uses_distinct_exact_image_version_and_required_markers(monkeypatch, halt):
    markers = 'uav_xhci_short_retry=1'+(' uav_xhci_halt_cleanup=1' if halt else '')
    monkeypatch.setattr(helper.Path, 'read_text', lambda *_args, **_kwargs: markers)
    version = helper.HALT_VERSION if halt else helper.EXPECTED_VERSION
    monkeypatch.setattr(helper.os, 'uname', lambda: SimpleNamespace(release='5.15.185-tegra', version=version))
    paths = []
    def sha(path):
        paths.append(str(path))
        return helper.HALT_IMAGE if halt else helper.EXPECTED_IMAGE
    monkeypatch.setattr(helper, 'sha256', sha)
    result = helper.kernel_identity(halt)
    assert result['expected_kernel_loaded'] is True
    assert paths == ['/boot/Image.uav-xhci-halt-cleanup' if halt else '/boot/Image.uav-xhci-retry']


@pytest.mark.parametrize('fault', ['missing_halt_marker', 'missing_short_marker', 'duplicate_marker', 'old_version', 'image', 'release'])
def test_halt_identity_never_claims_candidate_loaded_from_partial_or_mismatched_evidence(monkeypatch, fault):
    markers = 'uav_xhci_short_retry=1 uav_xhci_halt_cleanup=1'
    if fault == 'missing_halt_marker': markers = 'uav_xhci_short_retry=1'
    if fault == 'missing_short_marker': markers = 'uav_xhci_halt_cleanup=1'
    if fault == 'duplicate_marker': markers += ' uav_xhci_halt_cleanup=1'
    monkeypatch.setattr(helper.Path, 'read_text', lambda *_args, **_kwargs: markers)
    monkeypatch.setattr(helper.os, 'uname', lambda: SimpleNamespace(
        release='unexpected' if fault == 'release' else '5.15.185-tegra',
        version=helper.EXPECTED_VERSION if fault == 'old_version' else helper.HALT_VERSION))
    monkeypatch.setattr(helper, 'sha256', lambda _: 'unreviewed' if fault == 'image' else helper.HALT_IMAGE)
    assert helper.kernel_identity(True)['expected_kernel_loaded'] is False
