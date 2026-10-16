"""Server-side browser sessions stored separately from administrator credentials."""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import secrets
import sqlite3

APPLICATION_ID = 0x4E575342
USER_VERSION = 1
ABSOLUTE_HOURS = 8
IDLE_MINUTES = 30

DDL = (
    "CREATE TABLE web_sessions (token_sha256 TEXT PRIMARY KEY CHECK(length(token_sha256)=64),"
    "account_id TEXT NOT NULL,auth_version INTEGER NOT NULL CHECK(auth_version>0),"
    "created_at_utc TEXT NOT NULL,last_seen_at_utc TEXT NOT NULL,expires_at_utc TEXT NOT NULL,"
    "csrf_sha256 TEXT NOT NULL CHECK(length(csrf_sha256)=64),revoked_at_utc TEXT)",
)

EXPECTED_COLUMNS = (
    ("token_sha256", "TEXT", 0, 1),
    ("account_id", "TEXT", 1, 0),
    ("auth_version", "INTEGER", 1, 0),
    ("created_at_utc", "TEXT", 1, 0),
    ("last_seen_at_utc", "TEXT", 1, 0),
    ("expires_at_utc", "TEXT", 1, 0),
    ("csrf_sha256", "TEXT", 1, 0),
    ("revoked_at_utc", "TEXT", 0, 0),
)


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _connect(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize(path):
    target = Path(path)
    if target.is_symlink() or not target.parent.is_dir():
        raise ValueError("Invalid web session database path")
    if not target.exists():
        with target.open("xb"):
            pass
        connection = _connect(target)
        try:
            for statement in DDL:
                connection.execute(statement)
            connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
            connection.execute(f"PRAGMA user_version={USER_VERSION}")
            connection.commit()
        finally:
            connection.close()
    validate(target)


def validate(path):
    connection = _connect(path)
    try:
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Database is not admin web sessions v1")
        if connection.execute("PRAGMA user_version").fetchone()[0] != USER_VERSION:
            raise ValueError("Unsupported web session database version")
        names = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
        columns = tuple((row[1], row[2].upper(), row[3], row[5])
                        for row in connection.execute("PRAGMA table_info(web_sessions)"))
        if (names != {"web_sessions"} or columns != EXPECTED_COLUMNS or
                connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
            raise ValueError("Invalid web session database")
    finally:
        connection.close()


def create(path, account_id, auth_version, now=None):
    current = now or datetime.now(timezone.utc)
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO web_sessions VALUES (?,?,?,?,?,?,?,NULL)", (
            hashlib.sha256(token.encode()).hexdigest(), account_id, auth_version,
            _utc(current), _utc(current), _utc(current + timedelta(hours=ABSOLUTE_HOURS)),
            hashlib.sha256(csrf.encode()).hexdigest()))
        connection.commit()
    finally:
        connection.close()
    return token, csrf


def resolve(path, token, account_state, now=None, touch=True):
    if not isinstance(token, str) or not token:
        return None
    current = now or datetime.now(timezone.utc)
    digest = hashlib.sha256(token.encode()).hexdigest()
    connection = _connect(path)
    try:
        row = connection.execute(
            "SELECT account_id,auth_version,last_seen_at_utc,expires_at_utc,csrf_sha256,revoked_at_utc "
            "FROM web_sessions WHERE token_sha256=?", (digest,)).fetchone()
        if (not row or row[5] is not None or _parse(row[3]) <= current or
                _parse(row[2]) + timedelta(minutes=IDLE_MINUTES) <= current):
            return None
        state = account_state(row[0])
        if not state or state["status"] != "active" or state["auth_version"] != row[1]:
            return None
        if touch:
            connection.execute("UPDATE web_sessions SET last_seen_at_utc=? WHERE token_sha256=?",
                               (_utc(current), digest))
            connection.commit()
        return {"account_id": row[0], "username": state["username"], "role": state["role"],
                "csrf_sha256": row[4], "auth_version": row[1]}
    finally:
        connection.close()


def verify_csrf(session, token):
    if not session or not isinstance(token, str):
        return False
    return secrets.compare_digest(session["csrf_sha256"], hashlib.sha256(token.encode()).hexdigest())


def revoke(path, token, now=None):
    if not token:
        return
    connection = _connect(path)
    try:
        connection.execute("UPDATE web_sessions SET revoked_at_utc=? WHERE token_sha256=?",
                           (_utc(now), hashlib.sha256(token.encode()).hexdigest()))
        connection.commit()
    finally:
        connection.close()
