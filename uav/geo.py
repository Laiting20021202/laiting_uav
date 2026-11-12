"""Calibrated pixel ray -> aircraft FRD -> NED ground plane -> WGS84."""
import math
import cv2
import numpy as np
from pyproj import Geod, Transformer

_GEOD = Geod(ellps="WGS84")
_TWD97 = Transformer.from_crs("EPSG:4326", "EPSG:3826", always_xy=True)


def to_twd97(latitude, longitude):
    """Taiwan mainland TM2 zone 121; longitude is always the first input axis."""
    if not (math.isfinite(latitude) and math.isfinite(longitude)
            and -90 <= latitude <= 90 and -180 <= longitude <= 180):
        raise ValueError("Invalid geographic coordinates")
    east, north = _TWD97.transform(longitude, latitude, errcheck=True)
    if not all(math.isfinite(v) for v in (east, north)):
        raise ValueError("Invalid projected coordinates")
    return {"epsg": 3826, "easting_m": east, "northing_m": north}


def rotation(roll, pitch, yaw):
    cr, sr, cp, sp, cy, sy = math.cos(roll), math.sin(roll), math.cos(pitch), math.sin(pitch), math.cos(yaw), math.sin(yaw)
    return np.array([[cy*cp, cy*sp*sr-sy*cr, cy*sp*cr+sy*sr],
                     [sy*cp, sy*sp*sr+cy*cr, sy*sp*cr-cy*sr], [-sp, cp*sr, cp*cr]])


def gimbal_rotation(gimbal, aircraft_yaw):
    """MAVLink vehicle frame is horizon-level, rotated ONLY by aircraft heading."""
    if gimbal.get("failure_flags") or gimbal.get("flags", 0) & 1:
        raise ValueError("gimbal_unhealthy")
    q = gimbal.get("q", [])
    if len(q) != 4 or any(v is None or not math.isfinite(v) for v in q):
        raise ValueError("invalid_gimbal_quaternion")
    norm = float(np.linalg.norm(q))
    if not 0.9 <= norm <= 1.1:
        raise ValueError("invalid_gimbal_quaternion")
    w,x,y,z = np.asarray(q)/norm
    r = np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
                  [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
                  [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
    flags = gimbal["flags"]
    if flags & 32 and flags & 64:
        raise ValueError("ambiguous_gimbal_frame")
    earth_frame = bool(flags & 64) or (not flags & 32 and bool(flags & 16))
    if not earth_frame:
        delta = gimbal.get("delta_yaw") if flags & 32 else None
        heading = delta if delta is not None and math.isfinite(delta) else aircraft_yaw
        r = rotation(0, 0, heading) @ r
    return r


def locate(pixel, size, flight, config, stale_s=2):
    base = {"valid": False, "method": "calibrated_ground_plane", "pixel": list(pixel)}
    def invalid(reason):
        return {**base, "reason": reason}
    if not flight.get("connected"):
        return invalid("telemetry_disconnected")
    for name in ("GLOBAL_POSITION_INT", "GPS_RAW_INT", "ATTITUDE"):
        age = flight.get("ages_s", {}).get(name, math.inf)
        if not 0 <= age <= stale_s:
            return invalid("stale_" + name.lower())
    fix = flight.get("fix_type")
    if not isinstance(fix, int) or fix < config["min_gps_fix"]:
        return invalid("gps_fix_below_gate")
    base.update(gps_fix=fix, gps_quality={3: "GPS 3D", 4: "DGPS", 5: "RTK Float", 6: "RTK Fixed"}.get(fix, "GPS"))
    values = [flight.get(k) for k in ("latitude", "longitude", "roll", "pitch", "yaw")]
    if any(v is None or not math.isfinite(v) for v in values):
        return invalid("missing_position_or_attitude")
    lat, lon, roll, pitch, yaw = values
    if not -89 <= lat <= 89 or not -180 <= lon <= 180:
        return invalid("gps_out_of_range")
    assumptions = []
    estimated_intrinsics = not config["calibrated"]
    if estimated_intrinsics:
        ratio = config.get("estimated_focal_ratio")
        if ratio is None:
            return invalid("camera_not_calibrated")
        fx = fy = config["width"] * ratio
        cx, cy = config["width"] / 2, config["height"] / 2
        distortion = [0.] * 5
        assumptions.append("estimated_focal_length")
    else:
        fx, fy, cx, cy = (config[k] for k in ("fx", "fy", "cx", "cy"))
        distortion = config["distortion"]
    if min(fx, fy) <= 0:
        return invalid("camera_not_calibrated")
    width, height = size
    # Reject aspect-ratio changes, which may indicate a crop rather than resize.
    if abs(width / height - config["width"] / config["height"]) > 0.01:
        return invalid("calibration_aspect_mismatch")
    if not (0 <= pixel[0] < width and 0 <= pixel[1] < height):
        return invalid("pixel_out_of_bounds")
    body_rotation = rotation(roll, pitch, yaw)
    height_agl = None
    height_source = None
    distance = flight.get("rangefinder_m")
    if distance and 0 <= flight.get("ages_s", {}).get("DISTANCE_SENSOR", math.inf) <= stale_s:
        height_agl = distance * math.cos(roll) * math.cos(pitch)
        height_source = "downward_rangefinder"
    elif config["ground_altitude_relative_m"] is not None and flight.get("relative_altitude_m") is not None:
        height_agl = flight["relative_altitude_m"] - config["ground_altitude_relative_m"]
        height_source = "configured_ground_plane"
    elif config.get("assume_ground_at_takeoff") and flight.get("relative_altitude_m") is not None:
        height_agl = flight["relative_altitude_m"]
        height_source = "assumed_takeoff_ground"
        assumptions.append("ground_at_takeoff_altitude")
    if height_agl is None or not math.isfinite(height_agl) or height_agl <= 0:
        return invalid("no_ground_height")
    sx, sy = width / config["width"], height / config["height"]
    matrix = np.array([[fx*sx, 0, cx*sx], [0, fy*sy, cy*sy], [0, 0, 1.]])
    xy = cv2.undistortPoints(np.array([[pixel]], dtype=np.float64), matrix, np.asarray(distortion, dtype=np.float64))[0, 0]
    optical_to_frd = np.array([[0, 0, 1], [1, 0, 0], [0, 1, 0]])
    if config["orientation_source"] == "mavlink_gimbal":
        age = flight.get("ages_s", {}).get("GIMBAL_DEVICE_ATTITUDE_STATUS", math.inf)
        if not 0 <= age <= stale_s or not flight.get("gimbal"):
            return invalid("missing_or_stale_gimbal_attitude")
        try:
            camera_rotation = gimbal_rotation(flight["gimbal"], yaw) @ rotation(*np.radians(config["gimbal_boresight_rpy_deg"]))
        except ValueError as exc:
            return invalid(str(exc))
    elif config["orientation_source"] == "assumed_nadir":
        # User-selected temporary assumption: stabilized down, image top follows heading.
        camera_rotation = rotation(0, -math.pi/2, yaw)
        assumptions.append("nadir_heading_follows_aircraft")
    else:
        camera_rotation = body_rotation @ rotation(*np.radians(config["mount_rpy_deg"]))
    ray = camera_rotation @ optical_to_frd @ np.array([xy[0], xy[1], 1.])
    origin = body_rotation @ np.asarray(config["offset_frd_m"])
    if ray[2] <= 0.05:
        return invalid("ray_above_or_near_horizon")
    scale = (height_agl - origin[2]) / ray[2]
    if scale <= 0:
        return invalid("ground_behind_camera")
    ned = origin + scale * ray
    north, east, down = map(float, ned)
    if np.linalg.norm(ned) > config["max_distance_m"]:
        return invalid("projection_too_far")
    target_lon, target_lat, _ = _GEOD.fwd(lon, lat, math.degrees(math.atan2(east, north)), math.hypot(north, east))
    return {**base, "valid": True, "latitude": target_lat, "longitude": target_lon,
            "method": "estimated_ground_plane" if estimated_intrinsics else "calibrated_ground_plane",
            "intrinsics_source": "estimated_focal_ratio" if estimated_intrinsics else "calibrated",
            "assumptions": assumptions,
            "twd97": to_twd97(target_lat, target_lon),
            "north_m": north, "east_m": east, "height_agl_m": height_agl, "height_source": height_source,
            "quality": "uncalibrated_ground_plane_estimate" if estimated_intrinsics else "assumed_nadir_ground_plane" if config["orientation_source"] == "assumed_nadir" else "ground_plane_estimate",
            "orientation_source": config["orientation_source"]}
