"""Salted scrypt credentials, expiring revocable sessions and login throttling."""
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path
from .config import atomic_json

log = logging.getLogger(__name__)


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def password_matches(password, encoded):
    try:
        kind, salt, expected = encoded.split("$")
        if kind != "scrypt":
            return False
        value = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(value.hex(), expected)
    except (ValueError, TypeError):
        return False


class Auth:
    def __init__(self, users_path, hours=8, sessions_path=None):
        self.users_path = users_path
        self.lifetime = hours * 3600
        self.sessions = {}
        self.attempts = OrderedDict()
        self.lock = threading.RLock()
        self.dummy = password_hash(secrets.token_urlsafe(32))
        self.sessions_path = Path(sessions_path) if sessions_path else None
        self.saved_at = time.monotonic()
        if self.sessions_path and self.sessions_path.exists():
            try:
                saved = json.loads(self.sessions_path.read_text())
                users = json.loads(self.users_path.read_text())
                now, wall = time.monotonic(), time.time()
                for key, entry in saved.items():
                    remaining = min(self.lifetime, entry['expires_unix']-wall)
                    if (remaining > 0 and len(key) == 64 and entry.get('csrf')
                            and users.get(entry['username']) == entry['credential']):
                        self.sessions[key] = {k: entry[k] for k in ('username','csrf','credential')}
                        self.sessions[key]['expires'] = now+remaining
                log.info('Restored %d unexpired login sessions', len(self.sessions))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                self.sessions = {}
                log.exception('Could not restore login sessions; a new login is required')

    def _save(self):
        # One application worker owns this private file; tokens are stored as hashes.
        if not self.sessions_path:
            return
        now, wall = time.monotonic(), time.time()
        saved = {key:dict(username=value['username'],csrf=value['csrf'],
                          credential=value['credential'],expires_unix=wall+value['expires']-now)
                 for key,value in self.sessions.items() if value['expires'] > now}
        atomic_json(self.sessions_path, saved)
        self.saved_at = now

    def close(self):
        with self.lock:
            self._save()

    def login(self, username, password, ip):
        now = time.monotonic()
        with self.lock:
            # Per-IP and global windows bound brute force and password-hash CPU use.
            keys = ("ip:" + ip, "global")
            for key, limit in zip(keys, (10, 100)):
                history = [t for t in self.attempts.get(key, []) if now - t < 300]
                self.attempts[key] = history
                self.attempts.move_to_end(key)
                if len(history) >= limit:
                    return None, "limited"
            for key in keys:
                self.attempts[key].append(now)
            while len(self.attempts) > 4096:
                self.attempts.popitem(last=False)
        users = json.loads(self.users_path.read_text())
        encoded = users.get(username, self.dummy)
        if not password_matches(password, encoded) or username not in users:
            return None, "invalid"
        token = secrets.token_urlsafe(32)
        session = {"username": username, "csrf": secrets.token_urlsafe(24),
                   "expires": now + self.lifetime, "credential": encoded}
        with self.lock:
            self.sessions = {k: v for k, v in self.sessions.items() if v["expires"] > now}
            if len(self.sessions) >= 256:
                self.sessions.pop(next(iter(self.sessions)))
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = session
            self._save()
        return token, None

    def get(self, token, refresh=False):
        if not token:
            return None
        key = hashlib.sha256(token.encode()).hexdigest()
        with self.lock:
            session = self.sessions.get(key)
            if not session:
                return None
            users = json.loads(self.users_path.read_text())
            now = time.monotonic()
            if session["expires"] <= now or users.get(session["username"]) != session["credential"]:
                self.sessions.pop(key, None)
                self._save()
                log.info("Session ended for %s: %s", session["username"],
                         "idle timeout" if session["expires"] <= now else "credentials changed")
                return None
            if refresh:
                session["expires"] = now + self.lifetime
                # Bound disk writes while checkpointing active logins for a crash.
                if now-self.saved_at >= min(60, self.lifetime/4):
                    self._save()
            return dict(session)

    def logout(self, token):
        with self.lock:
            self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
            self._save()
