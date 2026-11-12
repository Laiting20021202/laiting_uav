#!/usr/bin/env python3
"""Stage the official xHCI fix; never install, restart a service, or reboot."""
from __future__ import annotations

import argparse
import difflib
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time

RELEASE = "5.15.185-tegra"
COMMIT = "e765ab012f73717238c95ab9c34bfc3c767fa48c"
ARCHIVE_SHA = "422bad33e1bf5cfea0b9f7ada5dc4ec732621b2d15b68c8f2e0da51b3b7dcf69"
BASE_RING_SHA = "b02f6bae18281c6ef0df7e1aba659cd0dabd0c9b40a207b7cb7b1354ae9e89c4"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def run(argv: list[str], log: Path, cwd: Path | None = None) -> None:
    with log.open("a") as out:
        out.write("\n$ " + " ".join(argv) + "\n")
        out.flush()
        subprocess.run(argv, cwd=cwd, stdout=out, stderr=subprocess.STDOUT, check=True)


def symbols(path: Path) -> dict[str, dict[str, str]]:
    result = {}
    for line in path.read_text().splitlines():
        words = line.split()
        if len(words) >= 4:
            result[words[1]] = {"crc": words[0], "origin": words[2], "export": words[3]}
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-dir", required=True, type=Path)
    parser.add_argument("--jobs", default=2, type=int, choices=(1, 2))
    args = parser.parse_args()
    if os.uname().release != RELEASE or os.uname().machine != "aarch64":
        raise SystemExit("Run only on the matching aarch64 Jetson kernel")
    repo = Path(__file__).resolve().parents[2]
    build = args.build_dir.resolve()
    if not build.is_relative_to(repo / "data/maintenance"):
        raise SystemExit("The build directory must be within data/maintenance")
    build.mkdir(mode=0o700, parents=True, exist_ok=True)
    archive = repo / "data/maintenance/20261006-usb-fix/nvidia-source-36.5.0/kernel_src.tbz2"
    if sha256(archive) != ARCHIVE_SHA:
        raise SystemExit("NVIDIA source archive does not match the checked source")
    source = build / "kernel/kernel-jammy-src"
    log = build / "build.log"
    if not (build / "source-extraction-complete").exists():
        run(["tar", "-xjf", str(archive), "-C", str(build)], log)
        (build / "source-extraction-complete").write_text(ARCHIVE_SHA + "\n")
    ring = source / "drivers/usb/host/xhci-ring.c"
    base = (repo / "data/maintenance/20261006-usb-fix/nvidia-source-36.5.0/xhci-ring.c").read_text()
    if hashlib.sha256(base.encode()).hexdigest() != BASE_RING_SHA:
        raise SystemExit("Checked xhCI base source has changed")
    function_start = base.index("static int process_bulk_intr_td(")
    short_case = base.index("\tcase COMP_SHORT_PACKET:\n", function_start)
    insertion = short_case + len("\tcase COMP_SHORT_PACKET:\n")
    expected = base[:insertion] + "\t\tep->err_count = 0;\n" + base[insertion:]
    if expected.count("\t\tep->err_count = 0;") != base.count("\t\tep->err_count = 0;") + 1:
        raise SystemExit("The upstream one-line change does not uniquely match this source")
    if ring.read_text() == base:
        run(["patch", "--batch", "--forward", "--fuzz=0", "-p1", "-i", str(Path(__file__).with_name("nvidia-5.15.185.patch"))], log, source)
    if ring.read_text() != expected:
        raise SystemExit("The patched xHCI source differs from the expected one-line fix")
    (build / "applied.diff").write_text("".join(difflib.unified_diff(base.splitlines(True), expected.splitlines(True), fromfile="stock/xhci-ring.c", tofile="patched/xhci-ring.c")))
    running = gzip.decompress(Path("/proc/config.gz").read_bytes())
    (build / "running.config").write_bytes(running)
    obj = build / "obj"
    obj.mkdir(exist_ok=True)
    (obj / ".config").write_bytes(running)
    command = ["make", "-C", str(source), "O=" + str(obj), "ARCH=arm64", "LOCALVERSION=-tegra"]
    run(command + ["olddefconfig"], log)
    effective = (obj / ".config").read_bytes()
    (build / "effective.config").write_bytes(effective)
    (build / "config.diff").write_text("".join(difflib.unified_diff(running.decode().splitlines(True), effective.decode().splitlines(True), fromfile="running.config", tofile="effective.config")))
    def semantic_config(data: bytes) -> list[str]:
        return [line for line in data.decode().splitlines() if line.startswith("CONFIG_") and not line.startswith(("CONFIG_CC_VERSION_TEXT=", "CONFIG_GCC_VERSION="))]
    if semantic_config(running) != semantic_config(effective):
        raise SystemExit("Kernel feature configuration changed; resolve config.diff before building")
    release = subprocess.check_output(command + ["-s", "kernelrelease"], text=True).strip()
    if release != RELEASE:
        raise SystemExit("Unexpected kernel release: " + release)
    started = time.time()
    run(command + ["-j" + str(args.jobs), "drivers/usb/host/xhci-ring.o"], log)
    (build / "xhci-object.json").write_text(json.dumps({
        "upstream_commit": COMMIT, "release": release,
        "source_sha256": sha256(ring),
        "object_sha256": sha256(obj / "drivers/usb/host/xhci-ring.o"),
        "complete": True, "installed": False,
    }, indent=2) + "\n")
    run(command + ["-j" + str(args.jobs), "Image"], log)
    image = obj / "arch/arm64/boot/Image"
    stock_path = Path("/lib/modules") / RELEASE / "build/Module.symvers"
    stock = symbols(stock_path)
    built = symbols(obj / "vmlinux.symvers")
    common = sorted(stock.keys() & built.keys())
    differences = [dict(symbol=name, installed=stock[name], built=built[name]) for name in common if stock[name]["crc"] != built[name]["crc"]]
    missing = [dict(symbol=name, **stock[name]) for name in sorted(stock.keys() - built.keys())]
    additional = [dict(symbol=name, **built[name]) for name in sorted(built.keys() - stock.keys())]
    missing_builtin = [entry for entry in missing if entry["origin"] == "vmlinux"]
    result = {
        "release": release, "upstream_commit": COMMIT,
        "source_archive_sha256": ARCHIVE_SHA, "base_xhci_ring_sha256": BASE_RING_SHA,
        "patched_xhci_ring_sha256": sha256(ring), "image": str(image), "image_sha256": sha256(image),
        "build_elapsed_seconds": time.time() - started,
        "running_config_sha256": hashlib.sha256(running).hexdigest(),
        "effective_config_sha256": hashlib.sha256(effective).hexdigest(),
        "installed_export_count": len(stock), "built_export_count": len(built),
        "matched_export_count": len(common), "crc_differences": differences,
        "installed_exports_missing_from_build": missing, "build_exports_absent_from_installed": additional,
        "installed_builtin_export_count": sum(entry["origin"] == "vmlinux" for entry in stock.values()),
        "installed_builtin_exports_missing_from_build": missing_builtin,
        "validation_scope": "All built-in exported CRCs; installed .ko modules are retained and have not been changed by this build",
        "installed_module_symvers_sha256": sha256(stock_path), "built_vmlinux_symvers_sha256": sha256(obj / "vmlinux.symvers"),
        "installed": False, "rebooted": False,
    }
    (build / "validation.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k not in ("crc_differences", "installed_exports_missing_from_build", "build_exports_absent_from_installed")}, indent=2))
    if differences or missing_builtin:
        raise SystemExit("ABI validation failed; do not install this image. See validation.json")
    if missing:
        print("Installed module-origin exports were not rebuilt; retain existing .ko modules. See validation.json for the accounted list.")


if __name__ == "__main__":
    main()
