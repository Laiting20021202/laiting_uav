"""Vehicle detection with optional roof-marker positioning and target tracking."""
import logging
import math
import cv2
import numpy as np
from .marker import detect_markers, white_mask_bgr
from .config import model_spec

log = logging.getLogger(__name__)


def iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    intersection = max(0, x2-x1) * max(0, y2-y1)
    return intersection / max(1, (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - intersection)


def expanded(box, shape, scale):
    x1, y1, x2, y2 = box
    cx, cy, w, h = (x1+x2)/2, (y1+y2)/2, (x2-x1)*scale/2, (y2-y1)*scale/2
    return (max(0, int(cx-w)), max(0, int(cy-h)), min(shape[1], int(cx+w)), min(shape[0], int(cy+h)))


def inference_confidence(config):
    # Keep low-confidence observations available to existing tracks. A GUI
    # threshold below the continuation floor must still allow new detections.
    return min(config["vehicle_conf"], config.get("track_conf", 0.10))


def annotate_detections(frame, targets):
    """Use the same raw-score/retention labels in live preview and video replay."""
    annotated = frame.copy()
    for target in targets:
        x1, y1, x2, y2 = target["bbox"]
        retained = target.get("retained", False)
        color = (0, 180, 255) if retained else (80, 210, 60)
        cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
        suffix = f' HOLD {target["missed_s"]:.2f}s' if retained else ""
        label = f'#{target["track_id"]} {target["class_name"]} raw {target["confidence"]:.2f}{suffix}'
        cv2.putText(annotated, label, (x1, max(20, y1-8)), cv2.FONT_HERSHEY_SIMPLEX, .6, color, 2)
        if not retained:
            cv2.drawMarker(annotated, tuple(map(int, target["pixel"])), (0, 230, 255), cv2.MARKER_CROSS, 24, 2)
    return annotated


def classify_target(frame, box, config):
    x1, y1, x2, y2 = box
    roi = frame[y1:y2, x1:x2]
    if roi.size == 0:
        return {"target": False, "white_ratio": 0, "red_ratio": 0}
    white = white_mask_bgr(roi)
    white_ratio = cv2.countNonZero(white) / white.size
    base = {"target": False, "white_ratio": white_ratio, "red_ratio": 0}
    if white_ratio < config["white_ratio"]:
        return base
    rx1, ry1, rx2, ry2 = expanded(box, frame.shape, 1.2)
    zoom = 4.5
    enlarged = cv2.resize(frame[ry1:ry2, rx1:rx2], None, fx=zoom, fy=zoom, interpolation=cv2.INTER_CUBIC)
    for marker in detect_markers(enlarged, min_area=20):
        mx, my, mw, mh = np.asarray(marker.bbox) / zoom
        px, py = marker.center[0] / zoom + rx1, marker.center[1] / zoom + ry1
        area_ratio = mw * mh / max(1, (x2-x1)*(y2-y1))
        if x1 <= px < x2 and y1 <= py < y2 and 0.025 <= area_ratio <= 0.45:
            body_white = white.copy()
            xa, ya = max(0, int(mx+rx1-x1)), max(0, int(my+ry1-y1))
            xb, yb = min(x2-x1, int(mx+mw+rx1-x1)), min(y2-y1, int(my+mh+ry1-y1))
            body_white[ya:yb, xa:xb] = 0
            body_ratio = cv2.countNonZero(body_white)/body_white.size
            if body_ratio < config["white_ratio"]:
                continue
            return {**base, "target": True, "white_ratio": body_ratio, "method": "red_white_x", "red_ratio": marker.red_ratio,
                    "x_score": marker.x_score, "pixel": [px, py]}
    if not config["roof_red_fallback"]:
        return base
    rx1, ry1, rx2, ry2 = expanded(box, frame.shape, config["roi_scale"])
    hsv = cv2.cvtColor(frame[ry1:ry2, rx1:rx2], cv2.COLOR_BGR2HSV)
    red = cv2.inRange(hsv, np.array([0, 22, 35]), np.array([18, 255, 255]))
    red |= cv2.inRange(hsv, np.array([155, 22, 35]), np.array([179, 255, 255]))
    red = cv2.morphologyEx(red, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    count = cv2.countNonZero(red)
    ratio = count / red.size
    base["red_ratio"] = ratio
    if count < config["red_pixels"] or ratio < config["red_ratio"]:
        return base
    contours, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        x, y, w, h = cv2.boundingRect(contour)
        px, py = rx1 + x + w/2, ry1 + y + h/2
        # An adjacent red object outside the vehicle must not become a roof target.
        if w*h >= config["red_pixels"] and x1 <= px < x2 and y1 <= py < y2:
            return {**base, "target": True, "method": "roof_red_fallback", "pixel": [px, py]}
    return base


class Detector:
    def __init__(self, name, config):
        import torch
        from ultralytics import YOLO
        from .prediction import CandidatePredictor
        self.spec = model_spec(name)
        self.name = name
        self.config = config
        self.device = self.spec["device"]
        if self.device == "auto":
            self.device = "0" if torch.cuda.is_available() else "cpu"
        self.model = YOLO(self.spec["path"], task="detect")
        self.kwargs = dict(imgsz=self.spec["imgsz"], device=self.device,
                           half=self.spec["half"] and self.device != "cpu", verbose=False,
                           conf=inference_confidence(config), iou=0.55, max_det=100)
        # Engine names are initialized by its first prediction; warm before accepting it.
        self.model.predict(np.zeros((self.spec["imgsz"], self.spec["imgsz"], 3), np.uint8),
                           predictor=CandidatePredictor, **self.kwargs)
        names = self.model.names
        configured = {name.lower() for name in self.spec["classes"]}
        available = {name.lower() for name in names.values()}
        if configured - available:
            raise ValueError("Model is missing configured classes: " + ", ".join(sorted(configured - available)))
        self.class_ids = [int(i) for i, name in names.items() if name.lower() in configured]
        self.kwargs["classes"] = self.class_ids
        log.info("Detector ready: model=%s path=%s imgsz=%s device=%s half=%s classes=%s new_conf=%.2f track_conf=%.2f ttl_s=%.2f",
                 self.name, self.spec["path"], self.spec["imgsz"], self.device, self.kwargs["half"],
                 [names[i] for i in self.class_ids], config["vehicle_conf"], inference_confidence(config), config["track_ttl_s"])

    def detect(self, frame):
        # Read the live threshold for every prediction, including after model switches.
        kwargs = {**self.kwargs, "conf": inference_confidence(self.config)}
        result = self.model.predict(frame, **kwargs)[0]
        out = []
        if result.boxes is None:
            return out
        scores = getattr(result, "class_scores", None)
        allowed = getattr(self, "class_ids", list(result.names))
        for index, (x1, y1, x2, y2, conf, cls) in enumerate(result.boxes.data.cpu().tolist()):
            box = [max(0, int(x1)), max(0, int(y1)), min(frame.shape[1], int(x2)), min(frame.shape[0], int(y2))]
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            marker = classify_target(frame, box, self.config)
            candidates = [{"class_id": int(cls), "class_name": result.names[int(cls)], "confidence": conf}]
            if scores is not None:
                candidates = sorted(
                    ({"class_id": class_id, "class_name": result.names[class_id], "confidence": scores[index][class_id]}
                     for class_id in allowed
                     if math.isfinite(scores[index][class_id]) and scores[index][class_id] > 0),
                    key=lambda candidate: candidate["confidence"], reverse=True,
                )[:5]
            out.append({**marker, "bbox": box, "confidence": conf, "class_name": result.names[int(cls)],
                        "class_id": int(cls), "class_candidates": candidates,
                        "marker_detected": marker["target"], "target": True,
                        "pixel": marker.get("pixel", [(box[0]+box[2])/2, (box[1]+box[3])/2])})
        return out


class Tracker:
    """Start strong detections, continue weaker matches and briefly retain misses."""
    def __init__(self, config):
        self.config = config
        self.tracks = {}
        self.next_id = 1

    def update(self, detections, at):
        self.tracks = {i: t for i, t in self.tracks.items() if at-t["at"] <= self.config["track_ttl_s"] + 1e-9}
        unused = set(self.tracks)
        out = []
        for detection in sorted(detections, key=lambda d: d["confidence"], reverse=True):
            if not detection["target"] or detection["confidence"] < inference_confidence(self.config):
                continue
            candidates = [(iou(detection["bbox"], self.tracks[i]["bbox"]), i) for i in unused
                          if self.tracks[i]["class_name"] == detection.get("class_name")]
            score, track_id = max(candidates, default=(0, 0))
            if score >= self.config["track_iou"]:
                unused.remove(track_id)
                track = self.tracks[track_id]
                track["hits"] += 1
            else:
                if detection["confidence"] < self.config["vehicle_conf"]:
                    continue
                track_id = self.next_id
                self.next_id += 1
                track = {"hits": 1, "since": at, "last_record": None, "class_name": detection.get("class_name")}
                self.tracks[track_id] = track
            track.update(bbox=detection["bbox"], at=at)
            observed = {**detection, "track_id": track_id, "hits": track["hits"],
                        "visible_s": max(0, at-track["since"]), "retained": False, "missed_s": 0.0,
                        "confirmed": at-track["since"] >= self.config["record_after_s"]}
            track["detection"] = observed
            out.append(observed)
        for track_id in sorted(unused):
            track = self.tracks[track_id]
            # Do not refresh 'at': expiry is measured from the last real match,
            # not from the last frame that displayed a retained box.
            out.append({**track["detection"], "retained": True, "missed_s": max(0, at-track["at"])})
        return out

    def should_record(self, detection, location, at):
        track = self.tracks[detection["track_id"]]
        return not detection.get("retained", False) and detection["confirmed"] and track["last_record"] is None

    def recorded(self, detection, location, at):
        self.tracks[detection["track_id"]].update(last_record=at)
