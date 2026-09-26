"""
Dashboard authentication: one owner password (PBKDF2 hash on disk), in-memory
sessions, and login throttling. The password itself is never stored or logged.
"""

import hashlib
import hmac
import json
import secrets
import threading
import time
from pathlib import Path

ITERATIONS = 240_000
MIN_PASSWORD_LENGTH = 10


class AuthStore:
    def __init__(self, path: str | Path = "data/dashboard_auth.json"):
        self.path = Path(path)

    def is_configured(self) -> bool:
        return self.path.exists()

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"algo": "pbkdf2_sha256", "iterations": ITERATIONS,
                                         "salt": salt.hex(), "hash": digest.hex()}),
                             encoding="utf-8")

    def check(self, password: str) -> bool:
        if not self.is_configured():
            return False
        record = json.loads(self.path.read_text(encoding="utf-8"))
        digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(record["salt"]),
                                     record["iterations"])
        return hmac.compare_digest(digest.hex(), record["hash"])


class Sessions:
    def __init__(self, hours: float = 12):
        self.ttl = hours * 3600
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[token] = time.time() + self.ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            expiry = self._tokens.get(token)
            if expiry is None or expiry < time.time():
                self._tokens.pop(token, None)
                return False
            return True

    def revoke(self, token: str | None) -> None:
        with self._lock:
            self._tokens.pop(token or "", None)


class LoginThrottle:
    """After MAX_FAILURES wrong passwords from one address, lock it for LOCK_SECONDS."""
    MAX_FAILURES = 5
    LOCK_SECONDS = 60

    def __init__(self):
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def wait_seconds(self, ip: str) -> int:
        with self._lock:
            _, locked_until = self._state.get(ip, (0, 0))
            return max(0, int(locked_until - time.time()))

    def failed(self, ip: str) -> None:
        with self._lock:
            count, _ = self._state.get(ip, (0, 0))
            count += 1
            locked = time.time() + self.LOCK_SECONDS if count >= self.MAX_FAILURES else 0
            self._state[ip] = (0 if locked else count, locked)

    def succeeded(self, ip: str) -> None:
        with self._lock:
            self._state.pop(ip, None)
