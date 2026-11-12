import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import xhci_kernel_stage as helper


STOCK = """TIMEOUT 30
DEFAULT primary

MENU TITLE L4T boot options

LABEL primary
      MENU LABEL primary kernel
      LINUX /boot/Image
      INITRD /boot/initrd
      FDT /boot/board.dtb
      APPEND ${cbootargs} root=PARTUUID=123 rw rootwait console=ttyTCU0,115200

# LABEL backup
#   LINUX /boot/Image.backup
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    # The production CLI has no filesystem-root override. Imported operations
    # below use an isolated synthetic Jetson filesystem and never touch /boot.
    paths = helper.Paths(tmp_path / "host", tmp_path / "repo")
    monkeypatch.setattr(helper, "require_root", lambda: None)
    monkeypatch.setattr(helper.os, "uname", lambda: SimpleNamespace(release=helper.RELEASE, machine="aarch64"))

    def put(absolute, data):
        destination = paths.system(absolute)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return destination

    stock_image = put("/boot/Image", b"stock kernel image")
    initrd = put("/boot/initrd", b"stock initrd")
    put("/boot/extlinux/extlinux.conf", STOCK.encode())
    symbols = b"0x11111111\tbuiltin_one\tvmlinux\tEXPORT_SYMBOL\n0x22222222\tbuiltin_two\tvmlinux\tEXPORT_SYMBOL_GPL\n0x33333333\tmodule_only\tdrivers/net/vendor\tEXPORT_SYMBOL\n"
    installed_symbols = put(f"/lib/modules/{helper.RELEASE}/build/Module.symvers", symbols)
    config = b"CONFIG_USB_XHCI_HCD=y\nCONFIG_MODVERSIONS=y\n"
    put("/proc/config.gz", gzip.compress(config))
    secure_boot = put("/sys/firmware/efi/efivars/SecureBoot-example", b"\x07\x00\x00\x00\x00")
    build = paths.workspace / "data/maintenance/test-kernel/obj"
    image = build / "arch/arm64/boot/Image"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"patched built image")
    built_symbols = build / "vmlinux.symvers"
    built_symbols.write_bytes(symbols.split(b"0x33333333")[0])
    validation = build.parent / "validation.json"
    metadata = {
        "release": helper.RELEASE,
        "upstream_commit": helper.COMMIT,
        "image": str(image),
        "image_sha256": helper.sha256(image),
        "installed_module_symvers_sha256": helper.sha256(installed_symbols),
        "built_vmlinux_symvers_sha256": helper.sha256(built_symbols),
        "running_config_sha256": helper.digest(config),
        "crc_differences": [],
        "installed_builtin_exports_missing_from_build": [],
        "installed_exports_missing_from_build": [{"symbol": "module_only", "origin": "drivers/net/vendor"}],
    }
    validation.write_text(json.dumps(metadata))
    return SimpleNamespace(paths=paths, validation=validation, metadata=metadata,
                           image=image, built_symbols=built_symbols,
                           installed_symbols=installed_symbols, config=config,
                           secure_boot=secure_boot, stock_image=stock_image, initrd=initrd)


def save_metadata(host, **changes):
    host.metadata.update(changes)
    host.validation.write_text(json.dumps(host.metadata))


def test_stage_preserves_default_stock_files_and_all_primary_settings(host):
    before = host.paths.extlinux.read_bytes()
    result = helper.stage(host.validation, host.paths)
    text = host.paths.extlinux.read_text()
    default, entries = helper.settings(text)
    assert default == "primary" and result["default"] == "primary"
    assert text.startswith(before.decode())
    assert helper.directive(entries[helper.LABEL], "LINUX") == helper.IMAGE
    assert helper.directive(entries[helper.LABEL], "INITRD") == "/boot/initrd"
    assert helper.directive(entries[helper.LABEL], "FDT") == "/boot/board.dtb"
    assert helper.directive(entries[helper.LABEL], "APPEND") == helper.directive(entries["primary"], "APPEND") + " " + helper.MARKER
    assert host.stock_image.read_bytes() == b"stock kernel image"
    assert host.initrd.read_bytes() == b"stock initrd"
    assert host.paths.system(helper.IMAGE).read_bytes() == host.image.read_bytes()
    assert (host.paths.backup / "extlinux.conf.stock").read_bytes() == before
    assert host.paths.backup.stat().st_mode & 0o777 == 0o700
    for name in ("Image.stock", "initrd.stock", "extlinux.conf.stock", "manifest.json"):
        assert (host.paths.backup / name).stat().st_mode & 0o777 == 0o600
    assert result["rebooted"] is False


def test_stage_is_idempotent_and_activation_is_explicit_and_reversible(host):
    helper.stage(host.validation, host.paths)
    staged = host.paths.extlinux.read_bytes()
    helper.stage(host.validation, host.paths)
    assert host.paths.extlinux.read_bytes() == staged
    activated = helper.select("activate", host.validation, host.paths)
    assert activated == {"action": "activate", "default": helper.LABEL, "rebooted": False}
    assert host.paths.extlinux.read_text() == helper.replace_default(staged.decode(), "primary", helper.LABEL)
    helper.stage(host.validation, host.paths)
    assert helper.settings(host.paths.extlinux.read_text())[0] == helper.LABEL
    helper.select("rollback", None, host.paths)
    assert host.paths.extlinux.read_bytes() == staged
    helper.select("rollback", None, host.paths)
    assert host.paths.extlinux.read_bytes() == staged


@pytest.mark.parametrize("field,value", [
    ("crc_differences", [{"symbol": "builtin_one"}]),
    ("installed_builtin_exports_missing_from_build", [{"symbol": "builtin_two"}]),
    ("crc_differences", None),
    ("installed_exports_missing_from_build", [{"symbol": "builtin_one", "origin": "vmlinux"}]),
    ("release", "5.15.199-tegra"),
    ("upstream_commit", "unreviewed commit"),
    ("image_sha256", "0" * 64),
    ("installed_module_symvers_sha256", "0" * 64),
    ("running_config_sha256", "0" * 64),
])
def test_invalid_validation_refuses_without_writing_boot_or_backups(host, field, value):
    save_metadata(host, **{field: value})
    with pytest.raises(RuntimeError):
        helper.stage(host.validation, host.paths)
    assert host.paths.extlinux.read_text() == STOCK
    assert not host.paths.system(helper.IMAGE).exists()
    assert not host.paths.backup.exists()


@pytest.mark.parametrize("built", [
    b"0x99999999 builtin_one vmlinux EXPORT_SYMBOL\n0x22222222 builtin_two vmlinux EXPORT_SYMBOL\n",
    b"0x11111111 builtin_one vmlinux EXPORT_SYMBOL\n",
])
def test_recomputes_actual_symbol_abi_instead_of_trusting_empty_summary(host, built):
    host.built_symbols.write_bytes(built)
    save_metadata(host, built_vmlinux_symvers_sha256=helper.sha256(host.built_symbols))
    with pytest.raises(RuntimeError, match="ABI mismatch"):
        helper.stage(host.validation, host.paths)
    assert host.paths.extlinux.read_text() == STOCK
    assert not host.paths.backup.exists()


@pytest.mark.parametrize("flag", [b"\x07\x00\x00\x00\x01", b"\x00", b"\x07\x00\x00\x00\x02"])
def test_refuses_enabled_or_unverifiable_secure_boot(host, flag):
    host.secure_boot.write_bytes(flag)
    with pytest.raises(RuntimeError, match="SecureBoot"):
        helper.stage(host.validation, host.paths)
    assert host.paths.extlinux.read_text() == STOCK


@pytest.mark.parametrize("mutation", ["entry", "image", "primary", "backup"])
def test_restage_and_activate_refuse_unexpected_existing_content(host, mutation):
    helper.stage(host.validation, host.paths)
    if mutation == "entry":
        host.paths.extlinux.write_text(host.paths.extlinux.read_text().replace(helper.MARKER, "uav_xhci_short_retry=2"))
    elif mutation == "image":
        host.paths.system(helper.IMAGE).write_bytes(b"unexpected kernel")
    elif mutation == "primary":
        host.paths.extlinux.write_text(host.paths.extlinux.read_text().replace("root=PARTUUID=123", "root=PARTUUID=other", 1))
    else:
        (host.paths.backup / "Image.stock").write_bytes(b"unexpected backup")
    before = host.paths.extlinux.read_bytes()
    with pytest.raises(RuntimeError):
        helper.stage(host.validation, host.paths)
    with pytest.raises(RuntimeError):
        helper.select("activate", host.validation, host.paths)
    assert host.paths.extlinux.read_bytes() == before


def test_refuses_preexisting_unknown_boot_entry_and_image(host):
    host.paths.extlinux.write_text(STOCK + "\nLABEL " + helper.LABEL + "\n LINUX /boot/unrelated\n")
    with pytest.raises(RuntimeError, match="First stage"):
        helper.stage(host.validation, host.paths)
    host.paths.extlinux.write_text(STOCK)
    host.paths.system(helper.IMAGE).write_bytes(host.image.read_bytes())
    with pytest.raises(RuntimeError, match="pre-existing"):
        helper.stage(host.validation, host.paths)
    assert not (host.paths.backup / "manifest.json").exists()


def test_activation_rechecks_live_abi_and_rollback_can_recover_bad_custom_image(host):
    helper.stage(host.validation, host.paths)
    helper.select("activate", host.validation, host.paths)
    host.installed_symbols.write_bytes(b"changed ABI")
    with pytest.raises(RuntimeError, match="Module.symvers changed"):
        helper.select("activate", host.validation, host.paths)
    host.paths.system(helper.IMAGE).write_bytes(b"bad custom image")
    helper.select("rollback", None, host.paths)
    assert helper.settings(host.paths.extlinux.read_text())[0] == "primary"


def test_refuses_different_runtime_kernel_and_nonroot(monkeypatch, host):
    monkeypatch.setattr(helper.os, "uname", lambda: SimpleNamespace(release="unexpected", machine="aarch64"))
    with pytest.raises(RuntimeError, match="matching"):
        helper.stage(host.validation, host.paths)
    monkeypatch.undo()
    monkeypatch.setattr(helper.os, "geteuid", lambda: 1000)
    with pytest.raises(RuntimeError, match="authentication"):
        helper.require_root()


@pytest.fixture
def halt_host(host, monkeypatch):
    # Preserve the already staged e765 kernel, then build a separate synthetic
    # candidate and evidence set. No filesystem path points at the real /boot.
    helper.stage(host.validation, host.paths)
    helper.select("activate", host.validation, host.paths)
    paths = helper.Paths(host.paths.root, host.paths.workspace, halt_cleanup=True)
    build = paths.workspace/"data/maintenance/halt-cleanup"
    image = build/"obj/arch/arm64/boot/Image"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"separate halt-cleanup candidate")
    symbols = build/"obj/vmlinux.symvers"
    symbols.write_bytes(host.built_symbols.read_bytes())
    artefacts = {
        "upstream_patch_sha256": build/"f7812977456c.upstream.patch",
        "backport_patch_sha256": build/"nvidia-5.15.185-f781.patch",
        "patched_xhci_ring_sha256": build/"kernel/kernel-jammy-src/drivers/usb/host/xhci-ring.c",
        "source_archive_sha256": paths.workspace/"data/maintenance/20261006-usb-fix/nvidia-source-36.5.0/kernel_src.tbz2",
        "baseline_xhci_ring_sha256": paths.workspace/"data/maintenance/20261006-repeat-outage-132751/kernel-build/kernel/kernel-jammy-src/drivers/usb/host/xhci-ring.c",
    }
    reviewed = {"image_sha256": helper.sha256(image), "preserved_image_sha256": helper.sha256(paths.system(helper.IMAGE))}
    for key, path in artefacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(("synthetic-reviewed-"+key).encode())
        reviewed[key] = helper.sha256(path)
    monkeypatch.setattr(helper, "HALT_REVIEWED", reviewed)
    config = build/"obj/.config"
    config.write_bytes(host.config+b"# compiler-derived configuration\n")
    compile_info = build/"obj/include/generated/compile.h"
    compile_info.parent.mkdir(parents=True)
    compile_info.write_text('#define UTS_VERSION "'+helper.HALT_VERSION+'"\n')
    obj = build/"obj/drivers/usb/host/xhci-ring.o"
    obj.parent.mkdir(parents=True)
    obj.write_bytes(b"synthetic compiled object")
    branch_source = build/"actual-source-branch-test.c"
    branch_source.write_bytes(b"synthetic branch test")
    (build/"branch-tests.json").write_text(json.dumps(dict(passed=True, cases=65536,
        source_sha256=reviewed["patched_xhci_ring_sha256"], test_c_sha256=helper.sha256(branch_source))))
    (build/"xhci-object.json").write_text(json.dumps(dict(complete=True, upstream_commit=helper.HALT_COMMIT,
        source_sha256=reviewed["patched_xhci_ring_sha256"], object_sha256=helper.sha256(obj))))
    metadata = {**host.metadata, **reviewed, "schema": 1, "base_commit": helper.COMMIT,
                "upstream_commit": helper.HALT_COMMIT, "upstream_commits": [helper.COMMIT, helper.HALT_COMMIT],
                "primary_source_url": "https://github.com/torvalds/linux/commit/"+helper.HALT_COMMIT+".patch",
                "build_directory": str(build), "image": str(image),
                "built_vmlinux_symvers_sha256": helper.sha256(symbols),
                "effective_config_sha256": helper.sha256(config)}
    validation = build/"validation.json"
    validation.write_text(json.dumps(metadata))
    (build/"provenance.json").write_text(json.dumps(metadata))
    return SimpleNamespace(paths=paths, validation=validation, metadata=metadata, image=image,
                           artefacts=artefacts, build=build, config=config, old=host)


def test_halt_candidate_stages_separately_and_preserves_both_rollback_kernels(halt_host):
    host = halt_host
    before = host.paths.extlinux.read_text()
    old_image = host.paths.system(helper.IMAGE).read_bytes()
    stock_image = host.paths.system('/boot/Image').read_bytes()
    result = helper.stage(host.validation, host.paths)
    default, entries = helper.settings(host.paths.extlinux.read_text())
    assert default == helper.LABEL and result['image'] == helper.HALT_IMAGE
    assert host.paths.extlinux.read_text().startswith(before)
    assert set(entries) == {'primary', helper.LABEL, helper.HALT_LABEL}
    assert helper.directive(entries[helper.HALT_LABEL], 'APPEND').endswith(helper.MARKER+' '+helper.HALT_MARKER)
    assert host.paths.system(helper.IMAGE).read_bytes() == old_image
    assert host.paths.system('/boot/Image').read_bytes() == stock_image
    assert host.paths.backup != host.old.paths.backup
    helper.stage(host.validation, host.paths)
    helper.select('activate', host.validation, host.paths)
    assert helper.settings(host.paths.extlinux.read_text())[0] == helper.HALT_LABEL
    helper.select('rollback', None, host.paths, rollback_to=helper.LABEL)
    assert helper.settings(host.paths.extlinux.read_text())[0] == helper.LABEL
    helper.select('activate', host.validation, host.paths)
    helper.select('rollback', None, host.paths)
    assert helper.settings(host.paths.extlinux.read_text())[0] == 'primary'


@pytest.mark.parametrize('mutation', ['image', 'upstream_patch', 'source', 'archive', 'baseline', 'provenance', 'object', 'branch'])
def test_halt_candidate_requires_actual_reviewed_artifacts_before_boot_mutation(halt_host, mutation):
    host = halt_host
    before = host.paths.extlinux.read_bytes()
    if mutation == 'image': host.image.write_bytes(b'unreviewed candidate')
    elif mutation == 'upstream_patch': host.artefacts['upstream_patch_sha256'].write_bytes(b'changed patch')
    elif mutation == 'source': host.artefacts['patched_xhci_ring_sha256'].write_bytes(b'changed source')
    elif mutation == 'archive': host.artefacts['source_archive_sha256'].write_bytes(b'changed archive')
    elif mutation == 'baseline': host.artefacts['baseline_xhci_ring_sha256'].write_bytes(b'changed baseline')
    elif mutation == 'provenance': (host.build/'provenance.json').write_text('{}')
    elif mutation == 'object': (host.build/'obj/drivers/usb/host/xhci-ring.o').write_bytes(b'changed object')
    else: (host.build/'branch-tests.json').write_text('{"passed":false}')
    with pytest.raises(RuntimeError): helper.stage(host.validation, host.paths)
    assert host.paths.extlinux.read_bytes() == before
    assert not host.paths.backup.exists() and not host.paths.system(helper.HALT_IMAGE).exists()


def test_halt_mode_never_accepts_wrong_commit_family_or_unvalidated_abi(halt_host):
    host = halt_host
    metadata = dict(host.metadata)
    metadata['upstream_commit'] = helper.COMMIT
    host.validation.write_text(json.dumps(metadata))
    with pytest.raises(RuntimeError): helper.stage(host.validation, host.paths)
    host.validation.write_text(json.dumps(host.metadata))
    symbols = host.build/'obj/vmlinux.symvers'
    symbols.write_bytes(b'0xffffffff builtin_one vmlinux EXPORT_SYMBOL\n')
    metadata = {**host.metadata, 'built_vmlinux_symvers_sha256': helper.sha256(symbols)}
    host.validation.write_text(json.dumps(metadata))
    with pytest.raises(RuntimeError, match='ABI mismatch'): helper.stage(host.validation, host.paths)


def test_halt_running_candidate_revalidates_exact_effective_config_markers_and_version(halt_host, monkeypatch):
    host = halt_host
    helper.stage(host.validation, host.paths)
    host.paths.system('/proc/config.gz').write_bytes(gzip.compress(host.config.read_bytes()))
    host.paths.system('/proc/cmdline').write_text(helper.MARKER+' '+helper.HALT_MARKER)
    monkeypatch.setattr(helper.os, 'uname', lambda: SimpleNamespace(release=helper.RELEASE, machine='aarch64', version=helper.HALT_VERSION))
    helper.select('activate', host.validation, host.paths)
    host.paths.system('/proc/cmdline').write_text(helper.MARKER)
    with pytest.raises(RuntimeError, match='configuration changed'): helper.select('activate', host.validation, host.paths)


def test_modified_e765_fallback_blocks_halt_activation_and_rollback(halt_host):
    host = halt_host
    helper.stage(host.validation, host.paths)
    host.paths.system(helper.IMAGE).write_bytes(b'changed fallback')
    with pytest.raises(RuntimeError): helper.select('activate', host.validation, host.paths)
    with pytest.raises(RuntimeError, match='rollback entry or Image changed'): helper.select('rollback', None, host.paths)
