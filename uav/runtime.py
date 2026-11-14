"""Own capture/inference workers and publish bounded preview/state for the web app."""
import copy
import json
import logging
import math
import os
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
import cv2
import numpy as np
from .camera import Camera
from .config import ROOT, atomic_json, model_spec
from .control import ServiceControl, notify_watchdog
from .detection import Detector, Tracker, annotate_detections
from .geo import locate
from .records import render_record
from .telemetry import Telemetry

log = logging.getLogger(__name__)


class Runtime:
    def __init__(self, config, store):
        self.config, self.store = config, store
        self.demo = config["demo"]
        self.lock = threading.RLock()
        self.records_lock = threading.RLock()
        self.record_cutoff = -float("inf")
        self.stop_event = threading.Event()
        self.telemetry = Telemetry(config["telemetry"])
        self.camera = Camera(config["camera"], self.telemetry)
        self.tracker = Tracker(config["detection"])
        self.detector = None
        self.pending_model = config["detection"]["model"] if not self.demo else None
        self.state = {"demo": self.demo, "model": None, "model_loading": bool(self.pending_model),
                      "model_error": None, "storage_error": None, "detections": [], "inference_ms": None}
        self.jpeg = None
        self.preview_seq = 0
        self.preview_at = 0
        self.last_capture = 0
        self.boot_id = str(uuid.uuid4())
        self.control = ServiceControl(self.demo)
        self.last_reconnect = -float("inf")
        self.progress_at = time.monotonic()
        self.last_encode_at = 0
        self.thread = None

    def start(self):
        self.ros_bridge = None
        bridge_path = os.environ.get('UAV_ROS_BRIDGE')
        if bridge_path and not self.demo:
            from .ros_bridge import SourceBridge
            # Only interval requests; no flight commands or second serial reader.
            self.telemetry.message_rates.update(self.config.get('ros_message_rates', {
                'SCALED_IMU': 20, 'SCALED_PRESSURE': 5}))
            try:
                self.ros_bridge = SourceBridge(self, bridge_path)
                self.ros_bridge.start()
            except Exception:
                log.exception('ROS source tap unavailable; existing service continues')
        if not self.demo:
            self.telemetry.start()
            self.camera.start()
        self.thread = threading.Thread(target=self._run, name="inference", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if getattr(self, 'ros_bridge', None):
            self.ros_bridge.stop()
        self.camera.stop()
        self.telemetry.stop()
        if self.thread:
            self.thread.join(timeout=60)

    def reconnect(self, device):
        if self.demo:
            raise ValueError("Demo mode has no hardware connection")
        if device not in ("camera", "flight", "all"):
            raise ValueError("Unknown device")
        with self.lock:
            if time.monotonic()-self.last_reconnect < 3:
                raise RuntimeError("Reconnect already requested; please wait")
            self.last_reconnect = time.monotonic()
            self.state["detections"] = []
        if device in ("camera", "all"):
            self.camera.request_reconnect()
        if device in ("flight", "all"):
            self.telemetry.request_reconnect()

    def watchdog(self):
        now = time.monotonic()
        # A disconnected device is healthy when its reconnect loop is progressing.
        progressing = self.thread and self.thread.is_alive() and now-self.progress_at < 60
        if not self.demo:
            progressing = progressing and all(worker.thread and worker.thread.is_alive() and now-worker.progress_at < 30
                                               for worker in (self.camera, self.telemetry))
            if self.camera.control:
                worker = self.camera.control
                # G3P control is deliberately on demand so its session cannot
                # own or expire the camera's long-running RTSP viewfinder.
                if worker.thread and worker.thread.is_alive():
                    progressing = progressing and now-worker.progress_at < 30
        if progressing:
            notify_watchdog()

    def switch_model(self, name):
        if self.demo:
            raise ValueError("Demo mode uses synthetic detections")
        model_spec(name)
        with self.lock:
            if self.state["model_loading"]:
                raise RuntimeError("A model is already loading")
            self.pending_model = name
            self.state.update(model_loading=True, model_error=None)

    def set_confidence(self, confidence, *, track_confidence=None, track_ttl_s=None):
        if self.demo:
            raise ValueError("示範模式無法修改實機辨識設定")
        if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence) or not 0.01 <= confidence <= 1):
            raise ValueError("信心值門檻必須介於 1% 與 100%")
        updates = {"vehicle_conf": confidence}
        for key, value, lo, hi in (("track_conf", track_confidence, 0.01, 1),
                                   ("track_ttl_s", track_ttl_s, 0.1, 10)):
            if value is None:
                continue
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not lo <= value <= hi):
                raise ValueError(f"{key} 必須介於 {lo} 與 {hi}")
            updates[key] = value
        with self.lock:
            path = Path(self.config.get("_settings_path", ROOT / "config/settings.json"))
            saved = json.loads(path.read_text())
            saved["detection"].update(updates)
            # Commit to disk first: failed saves must leave the live setting intact.
            atomic_json(path, saved, mode=path.stat().st_mode & 0o777)
            self.config["detection"].update(updates)
        return confidence

    def snapshot(self):
        with self.lock:
            state = copy.deepcopy(self.state)
            state.update(preview_age_s=round(time.monotonic()-self.preview_at, 2) if self.preview_at else None,
                         camera_age_s=round(time.monotonic()-self.last_capture, 2) if self.last_capture else None,
                         detection_confidence=self.config["detection"]["vehicle_conf"],
                         tracking_confidence=self.config["detection"]["track_conf"],
                         track_ttl_s=self.config["detection"]["track_ttl_s"],
                         record_after_s=self.config["detection"]["record_after_s"])
        state.update(timestamp=datetime.now(timezone.utc).isoformat(), flight=self.flight_snapshot(),
                     camera=self.camera.snapshot(), boot_id=self.boot_id,
                     service_restart_available=self.control.enabled,
                     camera_error=self.camera.error, event_count=self.store.count(),
                     free_disk_mb=round(shutil.disk_usage(self.store.path.parent).free/1048576),
                     geolocation_calibrated=self.config["geolocation"]["calibrated"],
                     geolocation_estimated_intrinsics=self.config["geolocation"].get("estimated_focal_ratio") is not None,
                     geolocation_assume_ground_at_takeoff=self.config["geolocation"].get("assume_ground_at_takeoff", False),
                     min_gps_fix=self.config["geolocation"]["min_gps_fix"],
                     orientation_source=self.config["geolocation"]["orientation_source"],
                     telemetry_stale_s=self.config["telemetry"]["stale_s"])
        state["camera_connected"] = state["camera_age_s"] is not None and state["camera_age_s"] < 3 if self.demo else state["camera"]["connected"]
        return state

    def flight_snapshot(self):
        return self._demo_flight() if self.demo else self.telemetry.snapshot()

    def frame(self):
        with self.lock:
            if not self.demo and not self.camera.snapshot()["connected"]:
                return None, self.preview_seq, self.preview_at
            return self.jpeg, self.preview_seq, self.preview_at

    @staticmethod
    def _demo_flight():
        return {"connected": True, "flight_mode": "SIMULATION", "armed": False, "latitude": 25.033964,
                "longitude": 121.564468, "relative_altitude_m": 60, "voltage_v": 24.2, "battery_pct": 88,
                "groundspeed": 0, "roll": 0, "pitch": 0, "yaw": 0, "fix_type": 6, "satellites": 20,
                "hdop": 0.7, "ages_s": {k: 0 for k in ("HEARTBEAT", "GLOBAL_POSITION_INT", "GPS_RAW_INT", "ATTITUDE", "SYS_STATUS", "VFR_HUD")}}

    def _load_pending(self):
        with self.lock:
            name = self.pending_model
            self.pending_model = None
        if name is None:
            return
        try:
            candidate = Detector(name, self.config["detection"])
            self.detector = candidate
            self.tracker = Tracker(self.config["detection"])
            with self.lock:
                self.state.update(model=name, model_error=None)
        except Exception as exc:
            log.exception("Model load failed: %s", name)
            with self.lock:
                self.state["model_error"] = f"{name}: {type(exc).__name__}; check service log"
        finally:
            with self.lock:
                self.state["model_loading"] = False

    def clear_events(self):
        with self.records_lock:
            count = self.store.clear()
            self.record_cutoff = time.monotonic()
            self.tracker = Tracker(self.config["detection"])
            with self.lock:
                self.state["detections"] = []
            return count

    def _record_targets(self, frame, flight, at, timestamp, detections):
        if at <= self.record_cutoff:
            return []
        targets = self.tracker.update(detections, at)
        for target in targets:
            if target["retained"]:
                # A previous box is display continuity, not a fresh observation
                # from which to calculate GPS or save another event.
                target["location"] = {"valid": False, "reason": "tracking_retained"}
                continue
            location = locate(target["pixel"], (frame.shape[1], frame.shape[0]), flight,
                              self.config["geolocation"], self.config["telemetry"]["stale_s"])
            if self.demo:
                location = {"valid": False, "reason": "simulation", "method": "simulation"}
            target["location"] = location
            if self.tracker.should_record(target, location, at):
                try:
                    candidates = target.get("class_candidates") or [
                        {"class_name": target["class_name"], "confidence": target["confidence"]}]
                    candidates = candidates[:5]
                    records = []
                    for rank, candidate in enumerate(candidates, 1):
                        detection = {**target, **candidate}
                        if len(candidates) > 1:
                            detection.update(candidate_rank=rank, candidate_count=len(candidates),
                                             primary_class_name=target["class_name"],
                                             primary_confidence=target["confidence"])
                        event = {"timestamp": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
                                 "boot_id": self.boot_id, "demo": self.demo, "model": self.state["model"],
                                 "detection": detection, "target_gps": location, "aircraft": flight,
                                 "calibration": self.config["geolocation"]}
                        jpeg, thumbnail = render_record(frame, event)
                        records.append((event, jpeg, thumbnail))
                    self.store.add_many(records)
                    self.tracker.recorded(target, location, at)
                    with self.lock:
                        self.state["storage_error"] = None
                except Exception as exc:
                    with self.lock:
                        self.state["storage_error"] = type(exc).__name__ + ": target could not be saved"
        return targets

    def _process(self, frame, flight, at, timestamp, detections):
        with self.records_lock:
            targets = self._record_targets(frame, flight, at, timestamp, detections)
            with self.lock:
                self.state["detections"] = targets
        now = time.monotonic()
        if now-self.last_encode_at < 1/self.config["preview"]["fps"]:
            return
        annotated = annotate_detections(frame, targets)
        width = min(self.config["preview"]["width"], annotated.shape[1])
        annotated = cv2.resize(annotated, (width, round(annotated.shape[0]*width/annotated.shape[1])))
        ok, jpeg = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, self.config["preview"]["jpeg_quality"]])
        if ok:
            with self.lock:
                self.jpeg = jpeg.tobytes()
                self.preview_seq += 1
                self.preview_at = at
                self.last_encode_at = now

    def _run(self):
        last_sequence = 0
        last_epoch = None
        while not self.stop_event.is_set():
            try:
                self.progress_at = time.monotonic()
                self._load_pending()
                iteration_started = time.monotonic()
                if self.demo:
                    frame = np.full((540, 960, 3), (33, 43, 49), dtype=np.uint8)
                    for x in range(0, 960, 60):
                        cv2.line(frame, (x, 0), (x, 540), (45, 58, 64), 1)
                    cv2.rectangle(frame, (390, 180), (570, 360), (235, 235, 235), -1)
                    cv2.rectangle(frame, (455, 245), (505, 295), (30, 30, 210), -1)
                    cv2.line(frame, (460, 250), (500, 290), (255, 255, 255), 7)
                    cv2.line(frame, (500, 250), (460, 290), (255, 255, 255), 7)
                    cv2.putText(frame, "SIMULATION / NO LIVE FLIGHT DATA", (30, 45), cv2.FONT_HERSHEY_SIMPLEX, .8, (100, 200, 255), 2)
                    sample = dict(frame=frame, flight=self._demo_flight(), at=time.monotonic(), timestamp=time.time())
                    detections = [dict(bbox=[390, 180, 570, 360], confidence=.95, class_name="car", target=True,
                                       pixel=[480, 270], method="simulation", white_ratio=.8, red_ratio=.07)]
                else:
                    sample = self.camera.get()
                    if sample is None or sample["sequence"] == last_sequence or time.monotonic()-sample["at"] > self.config["telemetry"]["stale_s"]:
                        self.stop_event.wait(.05)
                        continue
                    last_sequence = sample["sequence"]
                    if sample.get("epoch") != last_epoch:
                        self.tracker = Tracker(self.config["detection"])
                        last_epoch = sample.get("epoch")
                    start = time.monotonic()
                    detections = self.detector.detect(sample["frame"]) if self.detector else []
                    if not self.camera.is_current(sample):
                        self.tracker = Tracker(self.config["detection"])
                        continue
                    with self.lock:
                        self.state["inference_ms"] = round((time.monotonic()-start)*1000, 1) if self.detector else None
                with self.lock:
                    self.last_capture = sample["at"]
                self._process(sample["frame"], sample["flight"], sample["at"], sample["timestamp"], detections)
                # Include inference, recording and JPEG work in the frame budget.
                # Slow iterations still yield for 50 ms, then read the newest
                # camera sample; missed frames never trigger a catch-up burst.
                fps = self.config["preview"]["fps"] if self.demo else self.config["camera"]["fps"]
                elapsed = time.monotonic()-iteration_started
                self.stop_event.wait(max(.05, 1/fps-elapsed))
            except Exception as exc:
                log.exception("Inference iteration failed")
                with self.lock:
                    self.state["model_error"] = type(exc).__name__ + ": inference failed; check service log"
                self.stop_event.wait(2)
