"""User accounts and login sessions for a SpecForge server shared on the network.

Local accounts only: passwords are hashed with scrypt (stdlib, no extra dependency). Anyone can *request* an account from the
login page; it stays `pending` until an admin approves it. Every approved user works in the same shared workspace; admins
additionally manage users and change the global settings. Sessions are random tokens kept in an HttpOnly cookie; only
their SHA-256 is stored, so a copy of the database does not hand out live sessions.

Statuses: pending (requested, waiting for an admin), active, disabled (blocked by an admin), rejected (request declined).
"""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from .core.db import row, rows

ROLES = ("admin", "user")
STATUSES = ("pending", "active", "disabled", "rejected")
COOKIE = "sf_session"
SESSION_DAYS = 7               # sliding: each use within the window extends it
MIN_PASSWORD = 8
USERNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}
USER_COLS = "id,username,display_name,role,status,note,created,decided_at,decided_by,last_login"


class AuthError(Exception):
    """A refused auth action; `status` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(d: datetime) -> str:
    return d.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ passwords
def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode(), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${h.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), dklen=len(h) // 2, n=int(n), r=int(r), p=int(p))
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(secrets.token_hex(8))     # verified against for unknown users, so a wrong name costs as long as a wrong password


def check_password(password) -> str:
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        raise AuthError(f"password must be at least {MIN_PASSWORD} characters", 422)
    if len(password) > 256:
        raise AuthError("password is too long (256 characters at most)", 422)
    return password


def check_username(username) -> str:
    if not isinstance(username, str) or not USERNAME_RE.match(username.strip()):
        raise AuthError("username: 3 to 32 characters, letters, digits, '.', '_' or '-', starting with a letter or digit", 422)
    return username.strip()


# ------------------------------------------------------------------ users
def get_user(conn: sqlite3.Connection, user_id: int) -> dict | None:
    return row(conn, f"SELECT {USER_COLS} FROM app_user WHERE id=?", (user_id,))


def find_user(conn: sqlite3.Connection, username: str) -> dict | None:
    return row(conn, f"SELECT {USER_COLS} FROM app_user WHERE username=? COLLATE NOCASE", (username.strip(),))


def list_users(conn: sqlite3.Connection) -> list[dict]:
    return rows(conn, f"SELECT {USER_COLS} FROM app_user ORDER BY status='pending' DESC, username COLLATE NOCASE")


def count_admins(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM app_user WHERE role='admin' AND status='active'").fetchone()[0]


def create_user(conn: sqlite3.Connection, username, password, *, role: str = "user", status: str = "pending",
                display_name=None, note=None, decided_by: str | None = None) -> dict:
    username = check_username(username)
    check_password(password)
    if role not in ROLES:
        raise AuthError(f"role must be one of {', '.join(ROLES)}", 422)
    if status not in STATUSES:
        raise AuthError(f"status must be one of {', '.join(STATUSES)}", 422)
    display_name = (str(display_name).strip()[:80] or None) if display_name else None
    note = (str(note).strip()[:500] or None) if note else None
    if find_user(conn, username):
        raise AuthError("that username is already taken", 409)
    decided = _ts(_now()) if status != "pending" else None
    try:
        cur = conn.execute(
            "INSERT INTO app_user(username,display_name,password_hash,role,status,note,decided_at,decided_by) VALUES(?,?,?,?,?,?,?,?)",
            (username, display_name, hash_password(password), role, status, note, decided, decided_by if decided else None))
    except sqlite3.IntegrityError:          # two requests for the same name at once
        raise AuthError("that username is already taken", 409) from None
    conn.commit()
    return get_user(conn, cur.lastrowid)


def update_user(conn: sqlite3.Connection, user_id: int, *, actor: dict | None = None, role=None, status=None, display_name=None) -> dict:
    """Change role / status / display name. Refuses to leave the server without an active admin, and refuses to let an
    admin lock themselves out (demote or disable their own account)."""
    u = get_user(conn, user_id)
    if not u:
        raise AuthError("no such user", 404)
    if role is not None and role not in ROLES:
        raise AuthError(f"role must be one of {', '.join(ROLES)}", 422)
    if status is not None and status not in STATUSES:
        raise AuthError(f"status must be one of {', '.join(STATUSES)}", 422)
    if status == "pending" and u["status"] != "pending":
        raise AuthError("an account cannot be put back to pending", 422)
    new_role, new_status = role or u["role"], status or u["status"]
    if actor and actor["id"] == u["id"] and (new_role != u["role"] or new_status != u["status"]):
        raise AuthError("you cannot change your own role or status; ask another admin", 409)
    if u["role"] == "admin" and u["status"] == "active" and (new_role != "admin" or new_status != "active") and count_admins(conn) <= 1:
        raise AuthError("this is the last active admin; make another user admin first", 409)
    sets, args = [], []
    if role is not None:
        sets.append("role=?"); args.append(new_role)
    if status is not None and new_status != u["status"]:
        sets += ["status=?", "decided_at=?", "decided_by=?"]
        args += [new_status, _ts(_now()), actor["username"] if actor else None]
    if display_name is not None:
        sets.append("display_name=?"); args.append(str(display_name).strip()[:80] or None)
    if sets:
        conn.execute(f"UPDATE app_user SET {','.join(sets)} WHERE id=?", (*args, user_id))
        if new_status != "active" or new_role != u["role"]:
            conn.execute("DELETE FROM user_session WHERE user_id=?", (user_id,))     # takes effect now, not at the next login
        conn.commit()
    return get_user(conn, user_id)


def set_password(conn: sqlite3.Connection, user_id: int, password, *, keep_session: str | None = None) -> None:
    """New password; every session of the user ends except `keep_session` (the token of the user changing their own)."""
    check_password(password)
    if not get_user(conn, user_id):
        raise AuthError("no such user", 404)
    conn.execute("UPDATE app_user SET password_hash=? WHERE id=?", (hash_password(password), user_id))
    keep = _token_hash(keep_session) if keep_session else ""
    conn.execute("DELETE FROM user_session WHERE user_id=? AND token_hash<>?", (user_id, keep))
    conn.commit()


def change_own_password(conn: sqlite3.Connection, user_id: int, current, new, token: str | None) -> None:
    r = row(conn, "SELECT password_hash FROM app_user WHERE id=?", (user_id,))
    if not r or not isinstance(current, str) or not verify_password(current, r["password_hash"]):
        raise AuthError("current password is wrong", 403)
    set_password(conn, user_id, new, keep_session=token)


def delete_user(conn: sqlite3.Connection, user_id: int, *, actor: dict | None = None) -> dict:
    u = get_user(conn, user_id)
    if not u:
        raise AuthError("no such user", 404)
    if actor and actor["id"] == u["id"]:
        raise AuthError("you cannot delete your own account", 409)
    if u["role"] == "admin" and u["status"] == "active" and count_admins(conn) <= 1:
        raise AuthError("this is the last active admin and cannot be deleted", 409)
    conn.execute("DELETE FROM app_user WHERE id=?", (user_id,))      # sessions go with it (ON DELETE CASCADE)
    conn.commit()
    return u


# ------------------------------------------------------------------ login and sessions
_STATUS_REFUSAL = {
    "pending": "your access request is waiting for an admin to approve it",
    "rejected": "your access request was declined; contact an admin",
    "disabled": "this account is disabled; contact an admin",
}


def authenticate(conn: sqlite3.Connection, username, password) -> dict:
    """The user for a correct username + password of an active account. The account status is revealed only to someone
    who knows the password; everything else is one message."""
    r = row(conn, "SELECT id,password_hash,status FROM app_user WHERE username=? COLLATE NOCASE", (str(username or "").strip(),))
    ok = verify_password(str(password or ""), r["password_hash"] if r else _DUMMY_HASH)
    if not r or not ok:
        raise AuthError("wrong username or password", 401)
    if r["status"] != "active":
        raise AuthError(_STATUS_REFUSAL.get(r["status"], "account not active"), 403)
    conn.execute("UPDATE app_user SET last_login=? WHERE id=?", (_ts(_now()), r["id"]))
    conn.commit()
    return get_user(conn, r["id"])


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int, ip: str | None = None, agent: str | None = None) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    conn.execute("DELETE FROM user_session WHERE expires < ?", (_ts(now),))
    conn.execute("INSERT INTO user_session(token_hash,user_id,created,last_seen,expires,ip,user_agent) VALUES(?,?,?,?,?,?,?)",
                 (_token_hash(token), user_id, _ts(now), _ts(now), _ts(now + timedelta(days=SESSION_DAYS)), ip, (agent or "")[:200]))
    conn.commit()
    return token


def session_user(conn: sqlite3.Connection, token: str | None) -> dict | None:
    """The active user behind a session token, or None. Extends the session at most once a minute (a write per request
    would contend with long-running jobs for the database lock)."""
    if not token:
        return None
    th = _token_hash(token)
    now = _now()
    s = row(conn, "SELECT user_id,last_seen,expires FROM user_session WHERE token_hash=?", (th,))
    if not s or s["expires"] < _ts(now):
        return None
    u = get_user(conn, s["user_id"])
    if not u or u["status"] != "active":
        return None
    if s["last_seen"] < _ts(now - timedelta(minutes=1)):
        conn.execute("UPDATE user_session SET last_seen=?, expires=? WHERE token_hash=?",
                     (_ts(now), _ts(now + timedelta(days=SESSION_DAYS)), th))
        conn.commit()
    return u


def end_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM user_session WHERE token_hash=?", (_token_hash(token),))
        conn.commit()


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM app_user WHERE status='pending'").fetchone()[0]


# ------------------------------------------------------------------ brute-force throttle
class Throttle:
    """At most `limit` failures per key within `window` seconds (in memory: a restart clears it, which is fine on a LAN)."""

    def __init__(self, limit: int = 10, window: float = 600):
        self.limit, self.window = limit, window
        self._hits: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, key: str, now: float) -> list[float]:
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._recent(key, time.monotonic())) >= self.limit

    def fail(self, key: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._hits[key] = [*self._recent(key, now), now]

    def clear(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


# The signed-in user of the request being served (set by the API's auth middleware; None outside a request).
current_user: contextvars.ContextVar[dict | None] = contextvars.ContextVar("sf_user", default=None)
