#!/usr/bin/env python3
"""Render a video using the live model, thresholds, tracker and box annotations.

Replay processes every sampled frame using its video timestamp. It measures
processing throughput separately; output FPS is not a real-time speed claim.
No camera, telemetry, event database or flight-control commands are involved.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from uav.config import load_settings
from uav.detection import Detector, Tracker, annotate_detections, inference_confidence


def read_frame(pipe, size):
    data = bytearray()
    while len(data) < size:
        block = pipe.read(size-len(data))
        if not block:
            if data:
                raise RuntimeError("Decoder returned an incomplete frame")
            return None
        data.extend(block)
    return data


def replay(source, output, config, model=None):
    source, output = Path(source).resolve(), Path(output).resolve()
    if not source.is_file():
        raise ValueError(f"Source video not found: {source}")
    if output == source:
        raise ValueError("Output must differ from the original video")
    if output.exists():
        raise ValueError(f"Output already exists; choose another path: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    probe = json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_streams", "-show_format", "-of", "json", str(source)]))
    stream = probe["streams"][0]
    width, height, fps = (int(config["camera"][key]) for key in ("width", "height", "fps"))
    cv2.setNumThreads(2)
    detector = Detector(model or config["detection"]["model"], config["detection"])
    tracker = Tracker(config["detection"])
    # Warm the actual rectangular shape before measuring frame inference times.
    detector.detect(np.zeros((height, width, 3), np.uint8))
    temp = output.with_name(output.stem + ".partial.mp4")
    summary_path = output.with_suffix(".summary.json")
    frames_path = output.with_suffix(".detections.jsonl")
    decode_log = output.with_suffix(".decode.log")
    encode_log = output.with_suffix(".encode.log")
    decode_cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-threads", "2", "-filter_threads", "1",
                  "-i", str(source), "-map", "0:v:0", "-an", "-sn", "-dn", "-vf",
                  f"scale={width}:{height}:flags=lanczos,fps={fps}", "-f", "rawvideo", "-pix_fmt", "bgr24", "pipe:1"]
    encode_cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-y", "-f", "rawvideo", "-pix_fmt", "bgr24",
                  "-video_size", f"{width}x{height}", "-framerate", str(fps), "-i", "pipe:0", "-an", "-c:v", "libx264",
                  "-preset", "fast", "-crf", "18", "-threads", "2", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temp)]
    frames = detected_frames = retained_frames = observed_boxes = retained_boxes = 0
    inference_ms = []
    counts = Counter()
    track_classes = {}
    minimum_confidence = {}
    decoder = encoder = None
    started = time.monotonic()
    try:
        with decode_log.open("wb") as dec_err, encode_log.open("wb") as enc_err, frames_path.open("w") as records:
            decoder = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE, stderr=dec_err)
            encoder = subprocess.Popen(encode_cmd, stdin=subprocess.PIPE, stderr=enc_err)
            while True:
                data = read_frame(decoder.stdout, width*height*3)
                if data is None:
                    break
                frame = np.frombuffer(data, np.uint8).reshape(height, width, 3)
                at = frames/fps
                before = time.monotonic()
                detections = detector.detect(frame)
                elapsed_ms = (time.monotonic()-before)*1000
                inference_ms.append(elapsed_ms)
                targets = tracker.update(detections, at)
                current = [target for target in targets if not target["retained"]]
                held = [target for target in targets if target["retained"]]
                detected_frames += bool(current)
                retained_frames += bool(held)
                observed_boxes += len(current)
                retained_boxes += len(held)
                for target in current:
                    name = target["class_name"]
                    counts[name] += 1
                    track_classes[target["track_id"]] = name
                    minimum_confidence[name] = min(minimum_confidence.get(name, 1.0), target["confidence"])
                annotated = annotate_detections(frame, targets)
                cv2.rectangle(annotated, (0, 0), (width, 72), (18, 25, 30), -1)
                settings = config["detection"]
                header = (f"VIDEO REPLAY | {detector.name} | {width}x{height} / {fps} FPS | imgsz {detector.spec['imgsz']} | "
                          f"new {settings['vehicle_conf']:.2f} / track {inference_confidence(settings):.2f} / hold {settings['track_ttl_s']:.2f}s")
                status = (f"t={at:.2f}s | frame {frames+1} | detection {elapsed_ms:.0f} ms | observed {len(current)} / HOLD {len(held)} | "
                          "raw model confidence; orange = retained previous box")
                for y, label in ((27, header), (57, status)):
                    cv2.putText(annotated, label, (12, y), cv2.FONT_HERSHEY_SIMPLEX, .65, (235, 240, 245), 1, cv2.LINE_AA)
                encoder.stdin.write(annotated.tobytes())
                records.write(json.dumps({"frame": frames+1, "video_time_s": at, "inference_ms": elapsed_ms,
                                          "raw_detections": detections, "tracks": targets}, ensure_ascii=False)+"\n")
                frames += 1
                if frames % 25 == 0:
                    print(f"frames={frames} video={frames/fps:.2f}s processing={time.monotonic()-started:.1f}s observed={observed_boxes} held={retained_boxes}", flush=True)
            decoder.stdout.close()
            if decoder.wait(timeout=60):
                raise RuntimeError(f"Video decoding failed; see {decode_log}")
            encoder.stdin.close()
            if encoder.wait(timeout=60):
                raise RuntimeError(f"Video encoding failed; see {encode_log}")
        if frames == 0:
            raise RuntimeError("No video frames were decoded")
        wall_s = time.monotonic()-started
        summary = {
            "created_at": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(), "source": str(source), "output": str(output),
            "source_video": {key: stream.get(key) for key in ("width", "height", "avg_frame_rate", "nb_frames", "duration")},
            "settings": {"model": detector.name, "model_path": detector.spec["path"],
                         "model_sha256": hashlib.sha256(Path(detector.spec["path"]).read_bytes()).hexdigest(),
                         "width": width, "height": height, "input_fps": fps, "imgsz": detector.spec["imgsz"],
                         "device": detector.device, "half": detector.kwargs["half"], "nms_iou": detector.kwargs["iou"],
                         "max_det": detector.kwargs["max_det"], "classes": list(detector.spec["classes"]),
                         "new_target_confidence": config["detection"]["vehicle_conf"],
                         "continuation_confidence": inference_confidence(config["detection"]),
                         "track_iou": config["detection"]["track_iou"], "hold_s": config["detection"]["track_ttl_s"]},
            "frames": frames, "output_duration_s": frames/fps, "frames_with_observation": detected_frames,
            "frames_with_retained_boxes": retained_frames, "observed_boxes": observed_boxes, "retained_boxes": retained_boxes,
            "track_segments": len(track_classes), "track_segments_by_class": dict(Counter(track_classes.values())),
            "observed_boxes_by_class": dict(counts), "minimum_observed_confidence_by_class": minimum_confidence,
            "processing_seconds": wall_s, "processing_fps": frames/wall_s,
            "mean_inference_ms": float(np.mean(inference_ms)), "p95_inference_ms": float(np.percentile(inference_ms, 95)),
            "frame_log": str(frames_path), "accuracy_measured": False,
            "notes": ["Offline video replay: every 20 FPS sample is processed; slow inference does not drop samples.",
                      "Processing FPS and encoded playback FPS are different; live latest-frame capture can skip frames.",
                      "Original main video is scaled to configured input dimensions before inference.",
                      "Raw confidence is not accuracy; HOLD reuses the last real observation, not a new detection.",
                      "No camera, telemetry, live event records, audio or DJI metadata are copied into this output."]}
        temp.replace(output)
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2)+"\n")
        print(json.dumps({"output": str(output), "summary": str(summary_path), "frames": frames,
                          "processing_fps": summary["processing_fps"], "retained_frames": retained_frames}), flush=True)
        return summary
    finally:
        for process in (decoder, encoder):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--config", type=Path, default=ROOT/"config/settings.json")
    parser.add_argument("--model", help="Override the configured model using a registry name")
    args = parser.parse_args()
    config = load_settings(args.config)
    output = args.output or args.source.with_name(args.source.stem+"_v5_realtime.mp4")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    replay(args.source, output, config, args.model)


if __name__ == "__main__":
    main()
