"""SQLite target events; WAL transactions and bounded retention, no flight commands."""
import json
import logging
import shutil
import sqlite3
import threading
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class EventStore:
    def __init__(self, path, max_events=10000, min_free_mb=512):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.limit = int(max_events)
        self.min_free = min_free_mb * 1024 * 1024
        self.lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        path.chmod(0o600)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        schema = "CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, payload TEXT NOT NULL)"
        existing = self.conn.execute("SELECT sql FROM sqlite_master WHERE name='events'").fetchone()
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            if existing is None:
                self.conn.execute(schema)
            elif "AUTOINCREMENT" not in existing[0].upper():
                # Keep historical records and prevent old photo URLs referring to
                # a different record after a clear or retention cleanup.
                self.conn.execute("ALTER TABLE events RENAME TO events_legacy")
                self.conn.execute(schema)
                self.conn.execute("INSERT INTO events SELECT * FROM events_legacy")
                self.conn.execute("DROP TABLE events_legacy")
            self.conn.execute("CREATE TABLE IF NOT EXISTS event_images (event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE, jpeg BLOB NOT NULL, thumbnail BLOB NOT NULL)")
        self.conn.commit()

    def add(self, event, jpeg=None, thumbnail=None):
        return self.add_many([(event, jpeg, thumbnail)])[0]

    def add_many(self, records):
        """Save all candidate records from one observation in one transaction."""
        records = list(records)
        prepared = []
        for event, jpeg, thumbnail in records:
            if jpeg is not None and thumbnail is None:
                raise ValueError("A record photo requires a thumbnail")
            event = {"timestamp": utc_now(), **event}
            payload = json.dumps(event, ensure_ascii=False, allow_nan=False)
            prepared.append((event, payload, jpeg, thumbnail))
        size = sum(len(jpeg or b'') + len(thumbnail or b'') for _, _, jpeg, thumbnail in prepared)
        if shutil.disk_usage(self.path.parent).free < self.min_free + size:
            raise OSError("Insufficient free disk space for target records")
        saved = []
        with self.lock, self.conn:
            for event, payload, jpeg, thumbnail in prepared:
                cursor = self.conn.execute("INSERT INTO events(timestamp,payload) VALUES (?,?)", (event["timestamp"], payload))
                event_id = cursor.lastrowid
                if jpeg is not None:
                    self.conn.execute("INSERT INTO event_images VALUES (?,?,?)", (event_id, jpeg, thumbnail))
                saved.append({**event, "id": event_id, "has_image": jpeg is not None})
            if saved:
                self.conn.execute("DELETE FROM events WHERE id <= ?", (saved[-1]["id"] - self.limit,))
        return saved

    def list(self, limit=100, before=None):
        with self.lock:
            rows = self.conn.execute("SELECT e.id,e.payload,i.event_id IS NOT NULL FROM events e LEFT JOIN event_images i ON i.event_id=e.id WHERE e.id < ? ORDER BY e.id DESC LIMIT ?",
                                     (before or 9223372036854775807, int(limit))).fetchall()
        return [{**json.loads(row[1]), "id": row[0], "has_image": bool(row[2])} for row in rows]

    def example(self):
        with self.lock:
            row = self.conn.execute("SELECT e.id,e.payload,i.event_id IS NOT NULL FROM events e LEFT JOIN event_images i ON i.event_id=e.id WHERE json_extract(e.payload,'$.example')=1 ORDER BY e.id DESC LIMIT 1").fetchone()
        return {**json.loads(row[1]), "id": row[0], "has_image": bool(row[2])} if row else None

    def image(self, event_id, thumbnail=False):
        column = "thumbnail" if thumbnail else "jpeg"
        with self.lock:
            row = self.conn.execute(f"SELECT {column} FROM event_images WHERE event_id=?", (event_id,)).fetchone()
        return bytes(row[0]) if row else None

    def clear(self):
        with self.lock:
            with self.conn:
                count = self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
                self.conn.execute("DELETE FROM events")
            # Both full JPEGs and thumbnails are removed by the same transaction.
            # Return the released pages to disk, including the WAL.
            try:
                self.conn.execute("VACUUM")
                self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.OperationalError:
                log.warning("Records deleted; database compaction deferred", exc_info=True)
        return count

    def count(self):
        with self.lock:
            return self.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    def oldest_id(self):
        with self.lock:
            return self.conn.execute("SELECT MIN(id) FROM events").fetchone()[0]

    def close(self):
        with self.lock:
            self.conn.close()
