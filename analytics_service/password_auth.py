"""Local PoC password authentication. Sessions are opaque, expiring and revocable."""
import hashlib
import hmac
import secrets
import threading
import time
from collections import deque

from pydantic import BaseModel, ConfigDict, Field

from .errors import AnalyticsError

COOKIE = "aml_session"
SESSION_SECONDS = 3600


class Login(BaseModel):
    model_config = ConfigDict(extra="forbid")
    username: str = Field(min_length=1, max_length=40)
    password: str = Field(min_length=1, max_length=128)


def hash_password(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


class PasswordAuth:
    def __init__(self, identities):
        self.users = {i.username: i for i in identities if i.username}
        self.sessions = {}
        self.attempts = deque()
        self.lock = threading.Lock()
        self.dummy_hash = hash_password(secrets.token_urlsafe(32))

    def _prune(self, now):
        self.sessions = {key: value for key, value in self.sessions.items() if value[1] > now}

    def login(self, username, password, old_session=None):
        now = time.monotonic()
        # A global bound cannot be bypassed by rotating names or spoofing proxy headers.
        with self.lock:
            while self.attempts and self.attempts[0] < now - 60:
                self.attempts.popleft()
            if len(self.attempts) >= 10:
                raise AnalyticsError("rate_limit", "Too many sign-in attempts. Try again in a minute.", 429)
            self.attempts.append(now)
        identity = self.users.get(username)
        stored = identity.password_hash if identity else self.dummy_hash
        candidate = hash_password(password, bytes.fromhex(stored.split("$")[1]))
        if not hmac.compare_digest(stored, candidate) or identity is None:
            raise AnalyticsError("invalid_login", "Username or password is incorrect.", 401)
        token = secrets.token_urlsafe(40)
        with self.lock:
            self._prune(now)
            if len(self.sessions) >= 100:
                raise AnalyticsError("busy", "Too many active sessions.", 429)
            if old_session:
                self.sessions.pop(hashlib.sha256(old_session.encode()).hexdigest(), None)
            self.sessions[hashlib.sha256(token.encode()).hexdigest()] = (identity, now + SESSION_SECONDS)
        return token, identity

    def authenticate(self, token):
        if not token or len(token) > 128:
            return None
        with self.lock:
            self._prune(time.monotonic())
            value = self.sessions.get(hashlib.sha256(token.encode()).hexdigest())
            return value[0] if value else None

    def logout(self, token):
        if token:
            with self.lock:
                self.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
