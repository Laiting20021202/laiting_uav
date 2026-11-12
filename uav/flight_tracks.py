"""Opt-in, persistent GPS tracks; sampling is independent of camera and browsers."""
import logging
import math
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def valid_position(flight, stale_s):
    def finite(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)

    ages = flight.get("ages_s", {})
    lat, lon = flight.get("latitude"), flight.get("longitude")
    fix = flight.get("fix_type")
    return bool(flight.get("connected") and finite(fix) and fix >= 3
                and finite(lat) and -90 <= lat <= 90 and finite(lon) and -180 <= lon <= 180
                and all(finite(ages.get(k)) and 0 <= ages[k] <= stale_s
                        for k in ("GLOBAL_POSITION_INT", "GPS_RAW_INT")))


def distance_m(a, b):
    lat1, lat2 = math.radians(a["latitude"]), math.radians(b["latitude"])
    dlat = lat2 - lat1
    dlon = math.radians(b["longitude"] - a["longitude"])
    h = math.sin(dlat / 2)**2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2)**2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1, max(0, h))))


class FlightTracks:
    def __init__(self, path, flight, stale_s=3, demo=False, min_free_mb=512):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.flight, self.stale_s, self.demo = flight, stale_s, demo
        self.min_free = min_free_mb * 1024 * 1024
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.path.chmod(0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        with self.conn:
            self.conn.execute("""CREATE TABLE IF NOT EXISTS tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
                stopped_at TEXT, end_reason TEXT, demo INTEGER NOT NULL,
                point_count INTEGER NOT NULL DEFAULT 0, distance_m REAL NOT NULL DEFAULT 0)""")
            self.conn.execute("""CREATE TABLE IF NOT EXISTS track_points (
                track_id INTEGER NOT NULL REFERENCES tracks(id), seq INTEGER NOT NULL,
                timestamp TEXT NOT NULL, latitude REAL NOT NULL, longitude REAL NOT NULL,
                segment INTEGER NOT NULL, PRIMARY KEY(track_id, seq))""")
            # A restarted service never silently resumes a previous flight.
            self.conn.execute("""UPDATE tracks SET stopped_at=COALESCE(
                (SELECT MAX(timestamp) FROM track_points WHERE track_id=tracks.id), started_at),
                end_reason='service_restart' WHERE stopped_at IS NULL""")
        self.active_id = None
        self.previous = None
        self.segment = 0
        self.gap = False
        self.error = None
        self.stop_event = threading.Event()
        self.thread = None

    def start_worker(self):
        self.thread = threading.Thread(target=self._run, name="flight-track-recorder", daemon=True)
        self.thread.start()

    def _check_space(self):
        if shutil.disk_usage(self.path.parent).free < self.min_free:
            raise OSError("磁碟可用空間不足，無法寫入飛行軌跡")

    def start_recording(self):
        with self.lock:
            if self.active_id is None:
                self._check_space()
                with self.conn:
                    row = self.conn.execute("INSERT INTO tracks(started_at,demo) VALUES (?,?)", (utc_now(), self.demo))
                self.active_id = row.lastrowid
                self.previous, self.segment, self.gap, self.error = None, 0, False, None
                self.sample()
            return self.snapshot()

    def stop_recording(self, track_id):
        with self.lock:
            # Repeating stop is safe; an old tab cannot stop a newer recording.
            track = self._track(track_id)
            if track is None:
                raise KeyError(track_id)
            if track["stopped_at"] is None:
                self.sample()
                self._finish("stopped")
            return self.snapshot()

    def _finish(self, reason):
        if self.active_id is not None:
            with self.conn:
                self.conn.execute("UPDATE tracks SET stopped_at=?,end_reason=? WHERE id=?",
                                  (utc_now(), reason, self.active_id))
            self.active_id = None

    def sample(self):
        with self.lock:
            if self.active_id is None:
                return
            try:
                flight = self.flight()
                if not valid_position(flight, self.stale_s):
                    self.gap = True
                    return
                self._check_space()
                now = time.monotonic()
                point = {"latitude": flight["latitude"], "longitude": flight["longitude"]}
                broken = self.previous and (self.gap or now - self.previous["at"] > self.stale_s
                                            or self.previous["connection"] != flight.get("connections"))
                segment = self.segment + int(bool(broken))
                distance = distance_m(self.previous, point) if self.previous and not broken else 0
                with self.conn:
                    count = self._track(self.active_id)["point_count"]
                    self.conn.execute("INSERT INTO track_points VALUES (?,?,?,?,?,?)",
                                      (self.active_id, count + 1, utc_now(), point["latitude"], point["longitude"], segment))
                    self.conn.execute("UPDATE tracks SET point_count=point_count+1,distance_m=distance_m+? WHERE id=?",
                                      (distance, self.active_id))
                self.previous = {**point, "at": now, "connection": flight.get("connections")}
                self.segment, self.gap, self.error = segment, False, None
            except (OSError, sqlite3.Error):
                log.exception("Flight track sample could not be saved")
                self.gap = True
                self.error = "軌跡寫入失敗，請檢查磁碟空間；系統會繼續重試。"

    def _track(self, track_id):
        row = self.conn.execute("SELECT * FROM tracks WHERE id=?", (track_id,)).fetchone()
        return dict(row) if row else None

    def _points(self, track_id, after, limit):
        return [dict(row) for row in self.conn.execute(
            "SELECT seq,timestamp,latitude,longitude,segment FROM track_points WHERE track_id=? AND seq>? ORDER BY seq LIMIT ?",
            (track_id, after, limit))]

    def snapshot(self, track_id=0, after=0, limit=2000):
        with self.lock:
            active = self._track(self.active_id) if self.active_id else None
            row = self.conn.execute("SELECT * FROM tracks WHERE stopped_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
            latest = dict(row) if row else None
            selected = active or latest
            cursor = max(0, after) if selected and selected["id"] == track_id else 0
            points = self._points(selected["id"], cursor, limit) if selected else []
            last = points[-1]["seq"] if points else cursor
            return {"active": active, "latest": latest, "track": selected, "points": points,
                    "has_more": bool(selected and last < selected["point_count"]), "error": self.error}

    def completed(self, track_id, after=0, limit=2000):
        with self.lock:
            track = self._track(track_id)
            if track is None:
                raise KeyError(track_id)
            if track["stopped_at"] is None:
                raise ValueError("請先停止紀錄，再下載軌跡")
            points = self._points(track_id, max(0, after), limit)
            last = points[-1]["seq"] if points else after
            return {"track": track, "points": points, "has_more": last < track["point_count"]}

    def _run(self):
        while not self.stop_event.wait(1):
            self.sample()

    def close(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join()
        with self.lock:
            try:
                self._finish("service_restart")
            finally:
                self.conn.close()
