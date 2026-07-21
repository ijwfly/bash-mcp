"""User registry: a JSON file mapping linux users to external identities.

The registry is mutated by both the server process and the CLI
(generate_token.py, possibly via docker exec), so mutations are guarded by an
fcntl file lock and writes are atomic (tmp file + os.replace).

File format:
    {"users": {"user_alice": {"user_id": "alice", "created_at": "...", "revoked": false}}}
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import time

import jwt

DEFAULT_PATH = os.environ.get("REGISTRY_PATH", "/workspace/.users.json")

LINUX_USER_RE = re.compile(r"^user_[a-z0-9_]{1,27}$")


class RegistryError(Exception):
    pass


class UserCollision(RegistryError):
    pass


class UserRevoked(RegistryError):
    pass


def sanitize_username(user_id: str) -> str:
    """Convert arbitrary user_id to a valid Linux username.

    Truncated to 27 chars so the full name fits the 32-char Linux limit.
    """
    cleaned = re.sub(r"[^a-z0-9]", "_", user_id.lower())[:27]
    return f"user_{cleaned}"


def parse_ttl(ttl_str: str) -> int:
    """Parse a TTL string like '30d', '24h', '1h' into seconds."""
    match = re.fullmatch(r"(\d+)([dhms])", ttl_str)
    if not match:
        raise ValueError(f"Invalid TTL format: {ttl_str!r}. Use e.g. '30d', '24h', '3600s'.")
    value, unit = int(match.group(1)), match.group(2)
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return value * multipliers[unit]


def make_token(user_id: str, linux_user: str, secret: str, ttl_seconds: int | None) -> str:
    now = int(time.time())
    payload = {"user_id": user_id, "sub": linux_user, "iat": now}
    if ttl_seconds is not None:
        payload["exp"] = now + ttl_seconds
    return jwt.encode(payload, secret, algorithm="HS256")


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _read(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {"users": {}}
    except (json.JSONDecodeError, OSError) as e:
        raise RegistryError(f"Cannot read registry {path}: {e}") from e
    if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
        raise RegistryError(f"Corrupted registry {path}: unexpected structure")
    return data


def _write(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _mutate(path: str, func):
    """Run func(data) under an exclusive cross-process lock and persist data."""
    lock_path = path + ".lock"
    lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        data = _read(path)
        result = func(data)
        _write(path, data)
        return result
    finally:
        os.close(lock_fd)


# Read-side cache keyed by file mtime, so per-request is_active() checks
# don't re-parse the file when nothing changed.
_cache = None


def load(path: str | None = None) -> dict:
    global _cache
    path = path or DEFAULT_PATH
    try:
        mtime = os.stat(path).st_mtime
    except FileNotFoundError:
        return {"users": {}}
    if _cache and _cache[0] == path and _cache[1] == mtime:
        return _cache[2]
    data = _read(path)
    _cache = (path, mtime, data)
    return data


# ---------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def add_user(user_id: str, path: str | None = None, reactivate: bool = False) -> str:
    """Register user_id (idempotent). Returns the linux username.

    Raises UserCollision if a different user_id already maps to the same
    linux user, UserRevoked if the user is revoked and reactivate is False.
    """
    path = path or DEFAULT_PATH
    linux_user = sanitize_username(user_id)

    def op(data: dict) -> str:
        users = data.setdefault("users", {})
        entry = users.get(linux_user)
        if entry is None:
            users[linux_user] = {
                "user_id": user_id,
                "created_at": _now_iso(),
                "revoked": False,
            }
            return linux_user
        if entry.get("user_id") != user_id:
            raise UserCollision(
                f"user_id {user_id!r} maps to {linux_user!r}, already taken by "
                f"user_id {entry.get('user_id')!r}"
            )
        if entry.get("revoked"):
            if not reactivate:
                raise UserRevoked(f"user {user_id!r} is revoked")
            entry["revoked"] = False
        return linux_user

    return _mutate(path, op)


def find(user_id_or_linux: str, path: str | None = None) -> dict | None:
    """Look up a user by external user_id or linux username."""
    users = load(path)["users"]
    if user_id_or_linux in users:
        return {"linux_user": user_id_or_linux, **users[user_id_or_linux]}
    for linux_user, entry in users.items():
        if entry.get("user_id") == user_id_or_linux:
            return {"linux_user": linux_user, **entry}
    return None


def revoke_user(user_id_or_linux: str, path: str | None = None) -> str:
    """Mark a user revoked. Returns the linux username. Raises KeyError if unknown."""
    path = path or DEFAULT_PATH

    def op(data: dict) -> str:
        users = data.setdefault("users", {})
        if user_id_or_linux in users:
            linux_user = user_id_or_linux
        else:
            for candidate, entry in users.items():
                if entry.get("user_id") == user_id_or_linux:
                    linux_user = candidate
                    break
            else:
                raise KeyError(user_id_or_linux)
        users[linux_user]["revoked"] = True
        return linux_user

    return _mutate(path, op)


def list_users(path: str | None = None) -> list[dict]:
    users = load(path)["users"]
    return [{"linux_user": lu, **entry} for lu, entry in sorted(users.items())]


def is_active(linux_user: str, path: str | None = None) -> bool:
    entry = load(path)["users"].get(linux_user)
    return entry is not None and not entry.get("revoked")
