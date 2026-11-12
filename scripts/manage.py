#!/usr/bin/env python3
"""Local administration: change user passwords, select startup model, run diagnostics."""
import argparse
import getpass
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from uav.auth import password_hash
from uav.config import atomic_json, load_settings, model_spec, read_registry


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    user = commands.add_parser("user", help="Create/reset a web user's password")
    user.add_argument("username")
    model = commands.add_parser("model", help="List models or set next-startup model")
    model.add_argument("name", nargs="?")
    commands.add_parser("doctor", help="Check local software, device and deployment readiness")
    args = p.parse_args()
    if args.command == "user":
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", args.username):
            p.error("Username must be 1..64 letters, digits, underscores or hyphens")
        password = getpass.getpass("新密碼：")
        if not 6 <= len(password) <= 256 or password != getpass.getpass("再次輸入："):
            p.error("Passwords must match and contain 6..256 characters")
        path = ROOT / "config/users.json"
        users = json.loads(path.read_text()) if path.exists() else {}
        users[args.username] = password_hash(password)
        atomic_json(path, users)
        print("已儲存密碼雜湊；此使用者原有登入 session 立即失效。")
    elif args.command == "model":
        if args.name is None:
            for name, spec in read_registry().items():
                print(name, "—", spec["description"])
        else:
            model_spec(args.name)
            path = ROOT / "config/settings.json"
            config = json.loads(path.read_text())
            config["detection"]["model"] = args.name
            atomic_json(path, config, 0o644)
            print("已設定啟動模型：", args.name, "；重新啟動 uav-v2 後生效。")
    elif args.command == "doctor":
        config = load_settings()
        for package in ("fastapi", "uvicorn", "ultralytics", "pymavlink", "torch"):
            try:
                print(package, importlib.metadata.version(package))
            except importlib.metadata.PackageNotFoundError:
                print(package, "MISSING")
        import torch
        print("CUDA:", torch.cuda.is_available())
        print("FFmpeg:", shutil.which("ffmpeg") or "MISSING")
        print("cloudflared:", shutil.which("cloudflared") or str(Path.home()/".local/bin/cloudflared"))
        print("Web credential file:", (ROOT/"config/users.json").is_file())
        print("Cloudflare tunnel config:", (ROOT/"config/cloudflared.yml").is_file())
        print("Camera source:", config["camera"]["source"].split("@")[ -1])
        port = config["telemetry"]["connection"]
        print("Flight controller:", "PRESENT" if not port.startswith("/") or Path(port).exists() else "NOT CONNECTED")
        print("Calibrated:", config["geolocation"]["calibrated"])
        print("Startup model:", model_spec(config["detection"]["model"])["file"])
        print("Disk free GB:", round(shutil.disk_usage(ROOT).free/1024**3, 2))
        subprocess.run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"], check=False)
        subprocess.run(["systemctl", "--user", "is-active", "uav-v2", "uav-cloudflared"], check=False)


if __name__ == "__main__":
    main()
