"""Full camera frames with permanent, readable target annotations in the JPEG."""
import io
import math
from datetime import datetime
from zoneinfo import ZoneInfo
import cv2
from PIL import Image, ImageDraw, ImageFont

REASONS = {
    "camera_not_calibrated": "Camera not calibrated",
    "telemetry_disconnected": "Flight controller offline",
    "gps_fix_below_gate": "GPS quality below threshold",
    "no_ground_height": "Ground height unavailable",
    "simulation": "Simulation - no measured position",
}


def confidence_label(value):
    return "<0.1%" if 0 < value < 0.001 else f"{value:.1%}"


def record_lines(event):
    d, g = event["detection"], event["target_gps"]
    time = datetime.fromisoformat(event["timestamp"]).astimezone(ZoneInfo("Asia/Taipei"))
    twd = g.get("twd97", {})
    lines = ["EXAMPLE - FORMAT PREVIEW / NOT A REAL DETECTION" if event.get("example") else "LAITING UAV / TARGET RECORD",
             f"Time: {time:%Y-%m-%d %H:%M:%S.%f}"[:-3] + "  UTC+08:00 (Taipei)",
             f"Type: {d['class_name']}   |   Confidence: {confidence_label(d['confidence'])}   |   Track: {d['track_id']}"]
    if d.get("candidate_count", 1) > 1:
        lines.append(f"Candidate: {d['candidate_rank']}/{d['candidate_count']}   |   Most likely: {d['primary_class_name']} "
                     f"({confidence_label(d['primary_confidence'])})")
    if event.get("reprocessed_at"):
        recalculated = datetime.fromisoformat(event["reprocessed_at"]).astimezone(ZoneInfo("Asia/Taipei"))
        lines.append(f"Recalculated: {recalculated:%Y-%m-%d %H:%M:%S} UTC+08:00 / original telemetry / record {event['source_event_id']}")
    lat, lon = g.get("latitude"), g.get("longitude")
    if (g.get("valid") and isinstance(lat, (int, float)) and isinstance(lon, (int, float))
            and math.isfinite(lat) and math.isfinite(lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
        lines.append(f"Target WGS84 (EPSG:4326):  Latitude {lat:.7f} deg   Longitude {lon:.7f} deg")
    else:
        lines.append("Target WGS84 (EPSG:4326): Latitude / Longitude UNAVAILABLE")
    if g.get("valid") and twd and all(math.isfinite(twd[k]) for k in ("easting_m", "northing_m")):
        lines.append(f"Target TWD97 / TM2 121 (EPSG:3826):  E {twd['easting_m']:.2f} m   N {twd['northing_m']:.2f} m")
        lines.append("EXAMPLE coordinates - fictional, for layout review only" if event.get("example") else
                     "Position: ESTIMATE / uncalibrated lens" if g.get("intrinsics_source")=="estimated_focal_ratio" else
                     "Position: nadir assumption / ground-plane estimate" if g.get("orientation_source")=="assumed_nadir" else
                     "Position: calibrated ground-plane estimate")
        if not event.get("example"):
            if g.get("gps_quality"):
                lines.append("GPS: " + g["gps_quality"] + " / target accuracy not measured")
            assumptions = g.get("assumptions", [])
            if "nadir_heading_follows_aircraft" in assumptions:
                lines.append("Assumption: camera points down / image top follows aircraft heading")
            if "ground_at_takeoff_altitude" in assumptions:
                lines.append("Assumption: target ground at takeoff elevation")
    else:
        reason = g.get("reason", "position_unavailable")
        lines.extend(["Target TWD97 / TM2 121 (EPSG:3826): UNAVAILABLE",
                      "Reason: " + REASONS.get(reason, reason.replace("_", " "))])
    return lines


def render_record(frame, event):
    """Keep camera resolution; add metadata below the full field of view."""
    height, width = frame.shape[:2]
    size = max(12, round(width / 60))
    font = ImageFont.truetype("DejaVuSans.ttf", size)
    lines = record_lines(event)
    # Fit even long status messages without clipping on small camera frames.
    while size > 8 and max(font.getlength(line) for line in lines) > width-24:
        size -= 1
        font = ImageFont.truetype("DejaVuSans.ttf", size)
    step = size + max(8, size//3)
    footer_height = step * len(lines) + 32
    image = Image.new("RGB", (width, height+footer_height), (17, 25, 34))
    image.paste(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)), (0, 0))
    draw = ImageDraw.Draw(image)
    color = (255, 187, 71) if event.get("example") else (67, 239, 153)
    x1, y1, x2, y2 = event["detection"]["bbox"]
    box = (max(0,min(width-2,int(x1))), max(0,min(height-2,int(y1))),
           max(1,min(width-1,int(x2))), max(1,min(height-1,int(y2))))
    if box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError("Invalid target bounding box")
    draw.rectangle(box, outline=color, width=max(2, width//480))
    label = ("EXAMPLE | " if event.get("example") else "") + event['detection']['class_name']
    label_width = min(width, int(font.getlength(label))+16)
    left = min(box[0], max(0, width-label_width))
    top = max(0, box[1]-step)
    draw.rectangle((left,top,left+label_width,min(height-1,top+step)), fill=color)
    draw.text((left+6,top+2), label, fill=(12,20,28), font=font)
    for i, line in enumerate(lines):
        draw.text((12,height+12+i*step), line, fill=color if i==0 else (237,243,249), font=font)
    exif = Image.Exif()
    exif[270] = "\n".join(lines)
    output = io.BytesIO()
    # Avoid another chroma downsample of small colored target details. This
    # archival JPEG is separate from both raw inference and the GUI thumbnail.
    image.save(output, format="JPEG", quality=95, subsampling=0, exif=exif)
    image.thumbnail((480,480))
    thumb = io.BytesIO()
    image.save(thumb, format="JPEG", quality=78)
    return output.getvalue(), thumb.getvalue()
