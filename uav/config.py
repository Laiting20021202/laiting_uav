"""Read and validate the single settings file and local model registry."""
import copy
import json
import math
import os
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def load_settings(path=None, demo=False):
    settings_path = Path(path or ROOT / "config/settings.json").resolve()
    c = json.loads(settings_path.read_text())
    c["_settings_path"] = str(settings_path)
    c["detection"].setdefault("record_after_s", 0.0)
    c["detection"].setdefault("track_conf", 0.10)
    c["telemetry"].setdefault("retry_s", 1)
    c["telemetry"].setdefault("rate_retry_s", 5)
    c.setdefault("flight_tracks", {}).setdefault("enabled", False)
    if not isinstance(c["flight_tracks"]["enabled"], bool):
        raise ValueError("Invalid flight_tracks.enabled")
    ranges = {"web.port": (1, 65535), "web.session_hours": (0.01, 72),
              "camera.width": (32, 4096), "camera.height": (32, 2160),
              "camera.fps": (1, 60), "camera.timeout_s": (1, 60),
              "camera.refresh_s": (30, 1800),
              "preview.width": (160, 1920), "preview.fps": (1, 20),
              "preview.jpeg_quality": (10, 90), "telemetry.stale_s": (0.1, 10),
              "telemetry.heartbeat_timeout_s": (2, 30), "telemetry.system_id": (0, 255),
              "telemetry.retry_s": (0.1, 30), "telemetry.rate_retry_s": (2, 60),
              "detection.vehicle_conf": (0.01, 1), "detection.track_conf": (0.01, 1),
              "detection.white_ratio": (0, 1),
              "detection.red_ratio": (0, 1), "detection.red_pixels": (1, 10000),
              "detection.roi_scale": (1, 3), "detection.confirm_hits": (1, 100),
              "detection.track_iou": (0.01, 1), "detection.track_ttl_s": (0.1, 10),
              "detection.record_interval_s": (1, 3600), "storage.max_events": (1, 1000000),
              "detection.record_after_s": (0, 60),
              "storage.min_free_mb": (32, 1000000), "geolocation.min_gps_fix": (3, 6),
              "geolocation.max_distance_m": (1, 5000)}
    for key, (lo, hi) in ranges.items():
        section, name = key.split(".")
        value = c[section][name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise ValueError(f"Invalid {key}: expected {lo}..{hi}")
    geo = c["geolocation"]
    geo.setdefault("estimated_focal_ratio", None)
    geo.setdefault("assume_ground_at_takeoff", False)
    ratio = geo["estimated_focal_ratio"]
    if ratio is not None and (isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0.1 <= ratio <= 10):
        raise ValueError("Invalid geolocation.estimated_focal_ratio")
    if not isinstance(geo["assume_ground_at_takeoff"], bool):
        raise ValueError("Invalid geolocation.assume_ground_at_takeoff")
    if geo["orientation_source"] not in ("fixed", "assumed_nadir", "mavlink_gimbal"):
        raise ValueError("Invalid geolocation.orientation_source")
    if not 0 <= c["telemetry"]["gimbal_component_id"] <= 255:
        raise ValueError("Invalid gimbal_component_id")
    if not 0 <= c["telemetry"].get("component_id", 1) <= 255:
        raise ValueError("Invalid telemetry.component_id")
    if c["telemetry"]["gimbal_device_id"] is not None and not 0 <= c["telemetry"]["gimbal_device_id"] <= 6:
        raise ValueError("Invalid gimbal_device_id")
    if c["telemetry"].get("usb_serial") is not None and not isinstance(c["telemetry"]["usb_serial"], str):
        raise ValueError("Invalid telemetry.usb_serial")
    for field in ("usb_vid", "usb_pid"):
        value = c["telemetry"].get(field)
        if value is not None and (not isinstance(value, int) or not 0 <= value <= 65535):
            raise ValueError(f"Invalid telemetry.{field}")
    for key, size in (("mount_rpy_deg", 3), ("gimbal_boresight_rpy_deg", 3), ("offset_frd_m", 3), ("distortion", 5)):
        if len(geo[key]) != size or not all(math.isfinite(float(v)) for v in geo[key]):
            raise ValueError(f"Invalid geolocation.{key}")
    for key in ("fx", "fy", "cx", "cy", "width", "height", "max_distance_m"):
        if not math.isfinite(float(geo[key])):
            raise ValueError(f"Invalid geolocation.{key}")
    if geo["width"] <= 0 or geo["height"] <= 0:
        raise ValueError("Calibration image dimensions must be positive")
    if geo["calibrated"] and min(geo["fx"], geo["fy"]) <= 0:
        raise ValueError("Calibrated camera requires positive fx/fy")
    if geo["ground_altitude_relative_m"] is not None and not math.isfinite(float(geo["ground_altitude_relative_m"])):
        raise ValueError("Invalid ground altitude")
    origin = urlparse(c["web"]["origin"])
    local_http = origin.scheme == "http" and origin.hostname in ("localhost", "127.0.0.1")
    if origin.scheme != "https" and not local_http:
        raise ValueError("Public origin must use HTTPS")
    if c["web"]["host"] not in ("127.0.0.1", "::1"):
        raise ValueError("Bind to loopback; publish through Cloudflare Tunnel")
    camera_settings = c["camera"].get("g3p_settings", {})
    if not isinstance(camera_settings, dict) or not camera_settings or not all(
            isinstance(k, str) and k and isinstance(v, str) and v for k, v in camera_settings.items()):
        raise ValueError("Invalid camera.g3p_settings")
    c["demo"] = demo
    if demo:
        c = copy.deepcopy(c)
        c["web"].update(origin="http://127.0.0.1:8081", port=8081)
    return c


def read_registry():
    base = (ROOT / "models").resolve()
    primary = base / "registry.json"
    manifests = [primary] if primary.is_file() else sorted(base.glob("*/registry.json"))
    if not manifests:
        raise ValueError("No model registry found under models/")
    registry = {}
    for manifest in manifests:
        if not manifest.resolve().is_relative_to(base):
            raise ValueError("Model registry must remain under models/")
        for name, value in json.loads(manifest.read_text()).items():
            if name in registry:
                raise ValueError(f"Duplicate model name: {name}")
            spec = dict(value)
            spec["file"] = str(manifest.parent.relative_to(base) / spec["file"])
            registry[name] = spec
    for name in registry:
        model_spec(name, registry)
    return registry


def model_spec(name, registry=None):
    registry = registry if registry is not None else read_registry()
    spec = dict(registry[name])
    base = (ROOT / "models").resolve()
    path = (base / spec["file"]).resolve()
    if not path.is_relative_to(base) or path.suffix not in (".pt", ".engine", ".onnx") or not path.is_file():
        raise ValueError(f"Invalid/missing model file for {name}")
    if not isinstance(spec["imgsz"], int) or not 32 <= spec["imgsz"] <= 4096 or not spec["classes"]:
        raise ValueError(f"Invalid model settings for {name}")
    spec["path"] = str(path)
    return spec


def atomic_json(path, value, mode=0o600):
    path = Path(path)
    temp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)
    path.chmod(mode)
