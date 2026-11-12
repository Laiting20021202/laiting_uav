#!/usr/bin/env bash
# Create the isolated web environment while preserving NVIDIA's Jetson packages.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.."
python3 -m venv --without-pip --system-site-packages .venv
# Ubuntu's system pip is inherited even when python3-venv/ensurepip is absent.
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -c 'import cv2, torch, pymavlink, ultralytics, fastapi; print("CUDA:", torch.cuda.is_available())'
command -v ffmpeg >/dev/null || { echo 'Install FFmpeg: sudo apt install ffmpeg'; exit 1; }
echo 'Environment ready. Credentials: .venv/bin/python scripts/manage.py user USERNAME'
