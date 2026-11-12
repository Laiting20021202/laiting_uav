#!/usr/bin/env python3
"""Stage the ABI-verified xHCI kernel beside stock; never reboot automatically."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

RELEASE = "5.15.185-tegra"
COMMIT = "e765ab012f73717238c95ab9c34bfc3c767fa48c"
LABEL = "uav-xhci-retry"
IMAGE = "/boot/Image.uav-xhci-retry"
MARKER = "uav_xhci_short_retry=1"
BEGIN = "# BEGIN UAV xHCI short retry"
END = "# END UAV xHCI short retry"
HALT_COMMIT = "f7812977456c3c1efc69ecf79ea88302bf472f4b"
HALT_LABEL = "uav-xhci-halt-cleanup"
HALT_IMAGE = "/boot/Image.uav-xhci-halt-cleanup"
HALT_MARKER = "uav_xhci_halt_cleanup=1"
HALT_BEGIN = "# BEGIN UAV xHCI halt cleanup"
HALT_END = "# END UAV xHCI halt cleanup"
HALT_VERSION = "#2 SMP PREEMPT Wed Oct 7 14:34:16 CST 2026"
HALT_REVIEWED = {
    "image_sha256": "4bf3a99e78cb4dc528a11813d2dad334dfe3dee0dd4be81cf992876352756e5e",
    "upstream_patch_sha256": "1fc10c6a1bd62c6a960bebd1002084c6942c2e5eb7c12ea45746d651f23ca6e5",
    "backport_patch_sha256": "38fb16b9902c7c8ec9525b5c40273906c60939b65473370c0050cfd44b23a471",
    "source_archive_sha256": "422bad33e1bf5cfea0b9f7ada5dc4ec732621b2d15b68c8f2e0da51b3b7dcf69",
    "baseline_xhci_ring_sha256": "511fe032635c0413ecabb68d8b43a074301d241b427708703d9c4d4fbf245e09",
    "patched_xhci_ring_sha256": "89f8c48d257b252a4f9c1a1257c77986964999174e6dc7c94a700a11b44f2a77",
    "preserved_image_sha256": "898fb51a3ee68920f4b04d841663a9a56567c5d4f0941f4402ebbb5e00f24160",
}
REPO = Path(__file__).resolve().parents[1]


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def require_root() -> None:
    if os.geteuid() != 0:
        raise RuntimeError("Local administrator authentication is required")


def atomic_write(path: Path, data: bytes, mode: int) -> None:
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink destination: {path}")
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def settings(text: str) -> tuple[str, dict[str, list[str]]]:
    """Parse active directives, preserving their values and rejecting duplicates."""
    defaults = []
    entries: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        words = stripped.split(None, 1)
        key = words[0]
        value = words[1] if len(words) == 2 else ""
        key = key.upper()
        value = value.strip()
        if key == "DEFAULT":
            defaults.append(value)
        elif key == "LABEL":
            if not value or value in entries:
                raise RuntimeError("Duplicate or empty extlinux LABEL")
            current = value
            entries[current] = []
        elif current is not None:
            entries[current].append(stripped)
    if len(defaults) != 1:
        raise RuntimeError("Expected exactly one extlinux DEFAULT")
    return defaults[0], entries


def directive(lines: list[str], name: str, required: bool = True) -> str | None:
    matches = [line.split(None, 1)[1] for line in lines
               if line.split(None, 1)[0].upper() == name and len(line.split(None, 1)) == 2]
    if len(matches) > 1 or (required and len(matches) != 1):
        raise RuntimeError(f"Unexpected primary {name} directive count")
    return matches[0] if matches else None


def primary_settings(text: str) -> tuple[str, list[str]]:
    default, entries = settings(text)
    if "primary" not in entries:
        raise RuntimeError("Stock primary boot entry is absent")
    lines = entries["primary"]
    if directive(lines, "LINUX") != "/boot/Image":
        raise RuntimeError("Stock primary kernel is not /boot/Image")
    if directive(lines, "INITRD") != "/boot/initrd":
        raise RuntimeError("Stock primary initrd is not /boot/initrd")
    append = directive(lines, "APPEND")
    if not append or any(marker in append for marker in ("uav_xhci_short_retry=", "uav_xhci_halt_cleanup=")):
        raise RuntimeError("Stock primary APPEND is empty or already contains our marker")
    directive(lines, "FDT", required=False)
    return default, lines


def entry_block(primary: list[str], halt_cleanup=False) -> str:
    lines = [HALT_BEGIN if halt_cleanup else BEGIN, "LABEL " + (HALT_LABEL if halt_cleanup else LABEL)]
    for line in primary:
        key, value = line.split(None, 1)
        if key.upper() == "LINUX":
            value = HALT_IMAGE if halt_cleanup else IMAGE
        elif key.upper() == "APPEND":
            value += " " + MARKER + (" " + HALT_MARKER if halt_cleanup else "")
        elif key.upper() == "MENU" and value.upper().startswith("LABEL "):
            value = ("LABEL UAV xHCI halt cleanup (stock and short retry remain available)" if halt_cleanup else
                     "LABEL UAV xHCI short-packet retry fix (stock remains available)")
        lines.append("      " + key + " " + value)
    return "\n".join(lines + [HALT_END if halt_cleanup else END, ""])


def primary_hash(lines: list[str]) -> str:
    return digest("\n".join(lines).encode())


def replace_default(text: str, old: str, new: str) -> str:
    pattern = re.compile(r"^([ \t]*DEFAULT[ \t]+)" + re.escape(old) + r"([ \t]*)$", re.M | re.I)
    updated, count = pattern.subn(lambda match: match[1] + new + match[2], text)
    if count != 1:
        raise RuntimeError("Could not uniquely replace extlinux DEFAULT")
    return updated


def symbol_crcs(path: Path) -> dict[str, tuple[str, str]]:
    rows = {}
    for line in path.read_text().splitlines():
        parts = line.split()
        if len(parts) >= 4:
            if parts[1] in rows:
                raise RuntimeError("Duplicate symbol in Module.symvers")
            rows[parts[1]] = (parts[0], parts[2])
    if not rows:
        raise RuntimeError("Empty symbol-version file")
    return rows


@dataclass(frozen=True)
class Paths:
    root: Path = Path("/")
    workspace: Path = REPO
    halt_cleanup: bool = False

    @property
    def commit(self) -> str:
        return HALT_COMMIT if self.halt_cleanup else COMMIT

    @property
    def label(self) -> str:
        return HALT_LABEL if self.halt_cleanup else LABEL

    @property
    def image(self) -> str:
        return HALT_IMAGE if self.halt_cleanup else IMAGE

    def system(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    @property
    def backup(self) -> Path:
        name = "xhci-halt-cleanup-20261007" if self.halt_cleanup else "xhci-short-retry-20261006"
        return self.system("/var/backups/laiting-uav/"+name)

    @property
    def extlinux(self) -> Path:
        return self.system("/boot/extlinux/extlinux.conf")


def validate_halt_provenance(result: dict, validation_path: Path, image: Path, paths: Paths) -> str:
    build = validation_path.parent
    if (result.get("schema") != 1 or result.get("base_commit") != COMMIT or
            result.get("upstream_commits") != [COMMIT, HALT_COMMIT] or
            result.get("primary_source_url") != "https://github.com/torvalds/linux/commit/"+HALT_COMMIT+".patch" or
            Path(result.get("build_directory", "")).resolve() != build or
            image != build/"obj/arch/arm64/boot/Image"):
        raise RuntimeError("Unexpected halt-cleanup build provenance")
    provenance = json.loads((build/"provenance.json").read_text())
    for key, expected in HALT_REVIEWED.items():
        if result.get(key) != expected or (key != "image_sha256" and provenance.get(key) != expected):
            raise RuntimeError("Reviewed halt-cleanup provenance hash mismatch: "+key)
    if any(provenance.get(key) != result.get(key) for key in
           ("schema", "upstream_commit", "base_commit", "primary_source_url", "build_directory")):
        raise RuntimeError("Halt-cleanup provenance does not match validation")
    artefacts = {
        "upstream_patch_sha256": build/"f7812977456c.upstream.patch",
        "backport_patch_sha256": build/"nvidia-5.15.185-f781.patch",
        "patched_xhci_ring_sha256": build/"kernel/kernel-jammy-src/drivers/usb/host/xhci-ring.c",
        "source_archive_sha256": paths.workspace/"data/maintenance/20261006-usb-fix/nvidia-source-36.5.0/kernel_src.tbz2",
        "baseline_xhci_ring_sha256": paths.workspace/"data/maintenance/20261006-repeat-outage-132751/kernel-build/kernel/kernel-jammy-src/drivers/usb/host/xhci-ring.c",
        "preserved_image_sha256": paths.system(IMAGE),
    }
    if any(not path.is_file() or path.is_symlink() or sha256(path) != HALT_REVIEWED[key]
           for key, path in artefacts.items()):
        raise RuntimeError("Actual halt-cleanup source, patch, archive or preserved Image hash mismatch")
    branches = json.loads((build/"branch-tests.json").read_text())
    obj = json.loads((build/"xhci-object.json").read_text())
    if (branches.get("passed") is not True or branches.get("cases") != 65536 or
            branches.get("source_sha256") != HALT_REVIEWED["patched_xhci_ring_sha256"] or
            branches.get("test_c_sha256") != sha256(build/"actual-source-branch-test.c") or
            obj.get("complete") is not True or obj.get("upstream_commit") != HALT_COMMIT or
            obj.get("source_sha256") != HALT_REVIEWED["patched_xhci_ring_sha256"] or
            obj.get("object_sha256") != sha256(build/"obj/drivers/usb/host/xhci-ring.o")):
        raise RuntimeError("Halt-cleanup source branch or compiled object validation failed")
    effective_config = sha256(build/"obj/.config")
    if effective_config != result.get("effective_config_sha256"):
        raise RuntimeError("Actual built configuration hash mismatch")
    compile_info = (build/"obj/include/generated/compile.h").read_text()
    if '#define UTS_VERSION "'+HALT_VERSION+'"' not in compile_info:
        raise RuntimeError("Unexpected built kernel version")
    return effective_config


def validate(validation_path: Path, paths: Paths) -> tuple[dict, Path]:
    require_root()
    if os.uname().release != RELEASE or os.uname().machine != "aarch64":
        raise RuntimeError("Restricted to the matching aarch64 Jetson kernel")
    validation_path = validation_path.resolve(strict=True)
    maintenance = (paths.workspace / "data/maintenance").resolve()
    if not validation_path.is_relative_to(maintenance):
        raise RuntimeError("Validation must be within workspace data/maintenance")
    result = json.loads(validation_path.read_text())
    if result.get("release") != RELEASE or result.get("upstream_commit") != paths.commit:
        raise RuntimeError("Unexpected kernel release or upstream commit")
    if result.get("crc_differences") != [] or result.get("installed_builtin_exports_missing_from_build") != []:
        raise RuntimeError("Built-in module ABI validation failed or is incomplete")
    if any(row.get("origin") == "vmlinux" for row in result.get("installed_exports_missing_from_build", [])):
        raise RuntimeError("Validation lists missing built-in exports")
    image = Path(result["image"]).resolve(strict=True)
    if not image.is_relative_to(maintenance) or not image.is_file():
        raise RuntimeError("Built image must be within workspace data/maintenance")
    if sha256(image) != result.get("image_sha256"):
        raise RuntimeError("Built Image hash mismatch")
    effective_config = validate_halt_provenance(result, validation_path, image, paths) if paths.halt_cleanup else None
    installed_path = paths.system(f"/lib/modules/{RELEASE}/build/Module.symvers")
    if sha256(installed_path) != result.get("installed_module_symvers_sha256"):
        raise RuntimeError("Installed Module.symvers changed since build")
    # Recompute against the actual built symbol file; an empty summary list
    # alone does not establish compatibility with the retained installed .ko.
    built_path = image.parents[3] / "vmlinux.symvers"
    if sha256(built_path) != result.get("built_vmlinux_symvers_sha256"):
        raise RuntimeError("Built vmlinux.symvers hash mismatch")
    stock = symbol_crcs(installed_path)
    built = symbol_crcs(built_path)
    builtin = {name: row for name, row in stock.items() if row[1] == "vmlinux"}
    if not builtin:
        raise RuntimeError("No installed built-in exports to validate")
    if any(name not in built or built[name][0] != row[0] for name, row in builtin.items()):
        raise RuntimeError("Recomputed built-in ABI mismatch or missing export")
    if any(stock[name][0] != built[name][0] for name in stock.keys() & built.keys()):
        raise RuntimeError("Recomputed exported CRC mismatch")
    config = gzip.decompress(paths.system("/proc/config.gz").read_bytes())
    if digest(config) != result.get("running_config_sha256"):
        cmdline = paths.system("/proc/cmdline").read_text().split() if paths.halt_cleanup else []
        if (not paths.halt_cleanup or digest(config) != effective_config or
                os.uname().version != HALT_VERSION or not all(marker in cmdline for marker in (MARKER, HALT_MARKER))):
            raise RuntimeError("Running kernel configuration changed since build")
    secure_boot = list(paths.system("/sys/firmware/efi/efivars").glob("SecureBoot-*"))
    if len(secure_boot) != 1:
        raise RuntimeError("Cannot establish EFI SecureBoot state")
    # efivarfs includes four attribute bytes before the one-byte value.
    flag = secure_boot[0].read_bytes()
    if len(flag) != 5 or flag[4] != 0:
        raise RuntimeError("SecureBoot is enabled or its flag cannot be verified")
    return result, image


@contextmanager
def backup_lock(paths: Paths):
    directory = paths.backup
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if directory.is_symlink() or directory.stat().st_uid != os.geteuid() or directory.stat().st_mode & 0o077:
        raise RuntimeError("Backup directory must be private and owned by the administrator")
    lock_path = directory / ".lock"
    if lock_path.is_symlink():
        raise RuntimeError("Refusing symlink lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def load_state(paths: Paths) -> dict:
    path = paths.backup / "manifest.json"
    if path.is_symlink():
        raise RuntimeError("Refusing symlink manifest")
    state = json.loads(path.read_text())
    if state.get("schema") != 1 or state.get("release") != RELEASE or state.get("upstream_commit") != paths.commit:
        raise RuntimeError("Unexpected staging manifest")
    for name, expected in state["backups"].items():
        if name not in ("Image.stock", "initrd.stock", "extlinux.conf.stock"):
            raise RuntimeError("Unexpected backup filename")
        backup = paths.backup / name
        if backup.is_symlink() or sha256(backup) != expected:
            raise RuntimeError("Stock backup hash mismatch")
    if set(state["backups"]) != {"Image.stock", "initrd.stock", "extlinux.conf.stock"}:
        raise RuntimeError("Stock backup set is incomplete")
    return state


def inspect_boot(paths: Paths, state: dict, require_entry: bool) -> tuple[str, str]:
    if paths.extlinux.is_symlink():
        raise RuntimeError("Refusing symlink extlinux configuration")
    text = paths.extlinux.read_text()
    default, primary = primary_settings(text)
    if primary_hash(primary) != state["primary_sha256"]:
        raise RuntimeError("Stock primary settings changed since staging")
    if sha256(paths.system("/boot/Image")) != state["backups"]["Image.stock"] or sha256(paths.system("/boot/initrd")) != state["backups"]["initrd.stock"]:
        raise RuntimeError("Stock Image or initrd changed since staging")
    _, entries = settings(text)
    block = state["entry"]
    begin, end = (HALT_BEGIN, HALT_END) if paths.halt_cleanup else (BEGIN, END)
    present = paths.label in entries or begin in text or end in text
    if present and (text.count(block) != 1 or text.count(begin) != 1 or text.count(end) != 1):
        raise RuntimeError("Unexpected existing xHCI boot entry")
    if require_entry and not present:
        raise RuntimeError("xHCI boot entry has not been staged")
    if paths.halt_cleanup:
        expected = state.get("short_retry_entry")
        old_image = paths.system(IMAGE)
        if (expected != entry_block(primary) or text.count(expected) != 1 or
                text.count(BEGIN) != 1 or text.count(END) != 1 or
                old_image.is_symlink() or sha256(old_image) != state.get("short_retry_sha256") or
                state.get("short_retry_sha256") != HALT_REVIEWED["preserved_image_sha256"]):
            raise RuntimeError("Preserved short-retry rollback entry or Image changed")
    if default not in (("primary", LABEL, HALT_LABEL) if paths.halt_cleanup else ("primary", LABEL)):
        raise RuntimeError("Unexpected DEFAULT; refusing to overwrite another selection")
    return text, default


def stage(validation_path: Path, paths: Paths = Paths()) -> dict:
    result, source = validate(validation_path, paths)
    with backup_lock(paths):
        manifest_path = paths.backup / "manifest.json"
        target = paths.system(paths.image)
        if manifest_path.exists():
            state = load_state(paths)
            if state["image_sha256"] != result["image_sha256"] or state["validation_sha256"] != sha256(validation_path):
                raise RuntimeError("A different image or validation has already been staged")
        else:
            if paths.extlinux.is_symlink() or target.exists() or target.is_symlink():
                raise RuntimeError("Unexpected pre-existing staged image or symlink configuration")
            text = paths.extlinux.read_text()
            default, primary = primary_settings(text)
            _, entries = settings(text)
            begin, end = (HALT_BEGIN, HALT_END) if paths.halt_cleanup else (BEGIN, END)
            allowed = ("primary", LABEL) if paths.halt_cleanup else ("primary",)
            if default not in allowed or paths.label in entries or begin in text or end in text:
                raise RuntimeError("First stage requires the unchanged stock DEFAULT primary")
            if paths.halt_cleanup and (text.count(entry_block(primary)) != 1 or
                    sha256(paths.system(IMAGE)) != HALT_REVIEWED["preserved_image_sha256"]):
                raise RuntimeError("Verified short-retry rollback entry and Image are required")
            stock = {"Image.stock": paths.system("/boot/Image"),
                     "initrd.stock": paths.system("/boot/initrd"),
                     "extlinux.conf.stock": paths.extlinux}
            backups = {}
            for name, source_path in stock.items():
                if not source_path.is_file() or source_path.is_symlink():
                    raise RuntimeError("Expected regular stock boot file")
                saved = paths.backup / name
                data = source_path.read_bytes()
                if saved.exists() or saved.is_symlink():
                    if saved.is_symlink() or sha256(saved) != digest(data):
                        raise RuntimeError("Unexpected existing stock backup")
                else:
                    atomic_write(saved, data, 0o600)
                backups[name] = digest(data)
            state = {"schema": 1, "release": RELEASE, "upstream_commit": paths.commit,
                     "image_sha256": result["image_sha256"],
                     "validation_sha256": sha256(validation_path),
                     "validation_path": str(validation_path.resolve()),
                     "backups": backups, "primary_sha256": primary_hash(primary),
                     "entry": entry_block(primary, paths.halt_cleanup)}
            if paths.halt_cleanup:
                state.update(short_retry_entry=entry_block(primary),
                             short_retry_sha256=HALT_REVIEWED["preserved_image_sha256"])
            atomic_write(manifest_path, (json.dumps(state, indent=2) + "\n").encode(), 0o600)
        text, default = inspect_boot(paths, state, require_entry=False)
        if target.exists() or target.is_symlink():
            if target.is_symlink() or sha256(target) != state["image_sha256"]:
                raise RuntimeError("Unexpected existing staged Image hash")
        else:
            payload = source.read_bytes()
            if digest(payload) != state["image_sha256"]:
                raise RuntimeError("Built Image changed before staging")
            atomic_write(target, payload, 0o644)
        if sha256(target) != state["image_sha256"]:
            raise RuntimeError("Staged Image verification failed")
        if state["entry"] not in text:
            text = text.rstrip("\n") + "\n\n" + state["entry"]
            atomic_write(paths.extlinux, text.encode(), paths.extlinux.stat().st_mode & 0o777)
        inspect_boot(paths, state, require_entry=True)
        if sha256(target) != state["image_sha256"]:
            raise RuntimeError("Staged Image verification failed")
        return {"action": "stage", "image": paths.image, "default": default,
                "rebooted": False, "backup": str(paths.backup)}


def select(action: str, validation_path: Path | None, paths: Paths = Paths(), *, rollback_to="primary") -> dict:
    require_root()
    if action == "activate":
        if validation_path is None:
            raise RuntimeError("Activation requires the build validation")
        result, _ = validate(validation_path, paths)
    elif action != "rollback":
        raise RuntimeError("Unknown selection action")
    if rollback_to != "primary" and not (paths.halt_cleanup and rollback_to == LABEL):
        raise RuntimeError("Unsupported rollback selection")
    with backup_lock(paths):
        state = load_state(paths)
        if action == "activate":
            if state["validation_sha256"] != sha256(validation_path) or state["image_sha256"] != result["image_sha256"]:
                raise RuntimeError("Activation validation differs from staged image")
            target = paths.system(paths.image)
            if target.is_symlink() or sha256(target) != state["image_sha256"]:
                raise RuntimeError("Staged Image hash mismatch")
        text, current = inspect_boot(paths, state, require_entry=True)
        desired = paths.label if action == "activate" else rollback_to
        if current != desired:
            atomic_write(paths.extlinux, replace_default(text, current, desired).encode(), paths.extlinux.stat().st_mode & 0o777)
        return {"action": action, "default": desired, "rebooted": False}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("stage", "activate", "rollback"))
    parser.add_argument("--validation", type=Path)
    parser.add_argument("--halt-cleanup", action="store_true", help="Explicit reviewed f781 + e765 candidate mode")
    parser.add_argument("--rollback-to", choices=("primary", LABEL), default="primary")
    args = parser.parse_args()
    if args.action in ("stage", "activate") and args.validation is None:
        parser.error("--validation is required for stage and activate")
    if args.rollback_to != "primary" and (args.action != "rollback" or not args.halt_cleanup):
        parser.error("--rollback-to uav-xhci-retry requires rollback --halt-cleanup")
    paths = Paths(halt_cleanup=args.halt_cleanup)
    try:
        result = stage(args.validation, paths) if args.action == "stage" else select(
            args.action, args.validation, paths, rollback_to=args.rollback_to)
    except (RuntimeError, OSError, ValueError, KeyError, IndexError) as error:
        parser.exit(1, f"Refused: {error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
