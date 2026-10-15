"""Isolated SQLite administrator authentication and authorized review adapter."""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
import uuid

from werkzeug.security import check_password_hash, generate_password_hash

from nutrition.auth.rbac import ROLES, has_permission
from nutrition.review.policy import DEFAULT_POLICY
from nutrition.review.workflow import canonical, record_decision

APPLICATION_ID = 0x4E415554
USER_VERSION = 1
PASSWORD_METHOD = "pbkdf2:sha256:260000"
IDENTITY_ASSURANCE = "authenticated_admin_db_v1"
MAX_FAILURES = 5
LOCK_MINUTES = 5
SESSION_MINUTES = 10
USERNAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,63}")


class AuthenticationError(ValueError):
    """Generic error which deliberately does not reveal account existence."""


@dataclass(frozen=True)
class Principal:
    account_id: str
    username: str
    role: str
    session_id: str
    session_token: str = field(repr=False)


DDL = (
    "CREATE TABLE admin_accounts ("
    "account_id TEXT PRIMARY KEY NOT NULL,username TEXT NOT NULL UNIQUE COLLATE NOCASE,"
    "password_hash TEXT NOT NULL,password_method TEXT NOT NULL,role TEXT NOT NULL "
    "CHECK(role IN ('system_admin','nutrition_reviewer','audit_viewer')),"
    "status TEXT NOT NULL CHECK(status IN ('active','disabled')),created_at_utc TEXT NOT NULL,"
    "last_login_at_utc TEXT,password_changed_at_utc TEXT NOT NULL,failed_attempts INTEGER NOT NULL DEFAULT 0 "
    "CHECK(failed_attempts>=0),locked_until_utc TEXT,auth_version INTEGER NOT NULL DEFAULT 1 CHECK(auth_version>0))",
    "CREATE TABLE auth_sessions ("
    "session_id TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES admin_accounts(account_id),"
    "token_sha256 TEXT NOT NULL UNIQUE CHECK(length(token_sha256)=64),auth_version INTEGER NOT NULL CHECK(auth_version>0),"
    "issued_at_utc TEXT NOT NULL,expires_at_utc TEXT NOT NULL,revoked_at_utc TEXT)",
    "CREATE TABLE security_events ("
    "sequence INTEGER PRIMARY KEY,event_id TEXT NOT NULL UNIQUE,event_type TEXT NOT NULL,"
    "actor_account_id TEXT,target_account_id TEXT,details_json TEXT NOT NULL,created_at_utc TEXT NOT NULL,"
    "previous_hash TEXT,event_hash TEXT NOT NULL UNIQUE)",
    "CREATE TABLE authorized_review_actions ("
    "authorization_id TEXT PRIMARY KEY,operation_id TEXT NOT NULL UNIQUE,account_id TEXT NOT NULL "
    "REFERENCES admin_accounts(account_id),review_event_id TEXT NOT NULL UNIQUE,review_event_hash TEXT NOT NULL,"
    "release_key TEXT NOT NULL,fdc_id INTEGER NOT NULL,permission TEXT NOT NULL CHECK(permission='review.decide'),"
    "identity_assurance TEXT NOT NULL CHECK(identity_assurance='authenticated_admin_db_v1'),"
    "authorized_at_utc TEXT NOT NULL,security_event_id TEXT NOT NULL UNIQUE REFERENCES security_events(event_id))",
    "CREATE TRIGGER security_events_no_update BEFORE UPDATE ON security_events BEGIN "
    "SELECT RAISE(ABORT,'security events are append-only'); END",
    "CREATE TRIGGER security_events_no_delete BEFORE DELETE ON security_events BEGIN "
    "SELECT RAISE(ABORT,'security events are append-only'); END",
    "CREATE TRIGGER authorized_actions_no_update BEFORE UPDATE ON authorized_review_actions BEGIN "
    "SELECT RAISE(ABORT,'authorized review actions are append-only'); END",
    "CREATE TRIGGER authorized_actions_no_delete BEFORE DELETE ON authorized_review_actions BEGIN "
    "SELECT RAISE(ABORT,'authorized review actions are append-only'); END",
)


def _utc(value=None):
    value = value or datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("Timezone-aware UTC time required")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_utc(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def _objects(connection):
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()


def _connect(path, readonly=False):
    if readonly:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
    else:
        connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=ON")
    if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
        connection.close()
        raise RuntimeError("SQLite foreign keys unavailable")
    return connection


def _reference_objects():
    connection = _connect(":memory:")
    try:
        for statement in DDL:
            connection.execute(statement)
        return _objects(connection)
    finally:
        connection.close()


def _validate_schema(connection):
    if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise ValueError("Database is not administrator authentication v1")
    if connection.execute("PRAGMA user_version").fetchone()[0] != USER_VERSION:
        raise ValueError("Unsupported authentication database version")
    if _objects(connection) != _reference_objects():
        raise ValueError("Unexpected authentication database schema")
    if connection.execute("PRAGMA foreign_key_check").fetchone() or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("Invalid authentication database")


def initialize_auth_database(path):
    target = Path(path)
    if str(path) == ":memory:" or target.is_symlink() or not target.parent.is_dir():
        raise ValueError("Explicit safe authentication database path required")
    with target.open("xb"):
        pass
    connection = None
    succeeded = False
    try:
        connection = _connect(target)
        connection.execute("BEGIN IMMEDIATE")
        for statement in DDL:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={USER_VERSION}")
        connection.commit()
        succeeded = True
    except BaseException:
        if connection:
            connection.rollback()
        raise
    finally:
        if connection:
            connection.close()
        if not succeeded and target.exists():
            target.unlink()
    return target


def open_auth_database(path, readonly=False):
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("Authentication database must be a regular file")
    connection = _connect(target, readonly)
    try:
        _validate_schema(connection)
        return connection
    except BaseException:
        connection.close()
        raise


def _password(password):
    if not isinstance(password, str) or len(password) < 12 or len(password) > 1024:
        raise ValueError("Password must contain 12 to 1024 characters")
    return password


def _username(username):
    if not isinstance(username, str) or not USERNAME.fullmatch(username):
        raise ValueError("Username must be 3-64 safe characters")
    return username


def _event_payload(sequence, event_id, event_type, actor, target, details, created, previous):
    return {"sequence": sequence, "event_id": event_id, "event_type": event_type,
            "actor_account_id": actor, "target_account_id": target,
            "details": details, "created_at_utc": created, "previous_hash": previous}


def _append_event(connection, event_type, actor=None, target=None, details=None, now=None):
    details = details or {}
    sequence = connection.execute("SELECT coalesce(max(sequence),0)+1 FROM security_events").fetchone()[0]
    previous = connection.execute("SELECT event_hash FROM security_events ORDER BY sequence DESC LIMIT 1").fetchone()
    previous = previous[0] if previous else None
    event_id = str(uuid.uuid4())
    created = _utc(now)
    payload = _event_payload(sequence, event_id, event_type, actor, target, details, created, previous)
    event_hash = hashlib.sha256(canonical(payload).encode()).hexdigest()
    connection.execute("INSERT INTO security_events VALUES (?,?,?,?,?,?,?,?,?)",
                       (sequence, event_id, event_type, actor, target, canonical(details), created, previous, event_hash))
    return event_id, created, event_hash


def bootstrap_admin(path, username, password, now=None):
    username, password = _username(username), _password(password)
    connection = open_auth_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT count(*) FROM admin_accounts").fetchone()[0]:
            raise PermissionError("Bootstrap is only available for the first administrator")
        account_id, timestamp = str(uuid.uuid4()), _utc(now)
        password_hash = generate_password_hash(password, method=PASSWORD_METHOD)
        connection.execute("INSERT INTO admin_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (account_id, username, password_hash, PASSWORD_METHOD, "system_admin", "active",
                            timestamp, None, timestamp, 0, None, 1))
        _append_event(connection, "admin.bootstrap", account_id, account_id,
                      {"role": "system_admin", "identity_assurance": "bootstrap_local_cli"}, now)
        connection.commit()
        return account_id
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def authenticate(path, username, password, now=None):
    username = _username(username)
    if not isinstance(password, str):
        raise AuthenticationError("Authentication failed")
    current = now or datetime.now(timezone.utc)
    connection = open_auth_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT account_id,username,password_hash,role,status,failed_attempts,locked_until_utc,password_method,auth_version "
            "FROM admin_accounts WHERE username=? COLLATE NOCASE", (username,)).fetchone()
        # A dummy hash keeps missing-account verification on the same code path.
        stored = row[2] if row else generate_password_hash("dummy-not-a-valid-password", method=PASSWORD_METHOD)
        method_valid = bool(row and row[7] == PASSWORD_METHOD and stored.startswith(PASSWORD_METHOD + "$"))
        valid = check_password_hash(stored, password) and method_valid
        locked = bool(row and row[6] and _parse_utc(row[6]) > current)
        if not row or row[4] != "active" or locked or not valid:
            if row:
                failures = row[5] + 1
                until = _utc(current + timedelta(minutes=LOCK_MINUTES)) if failures >= MAX_FAILURES else row[6]
                connection.execute("UPDATE admin_accounts SET failed_attempts=?,locked_until_utc=? WHERE account_id=?",
                                   (failures, until, row[0]))
                _append_event(connection, "auth.failure", row[0], row[0],
                              {"reason": "credentials_or_account_state"}, current)
            else:
                _append_event(connection, "auth.failure", None, None,
                              {"reason": "credentials_or_account_state"}, current)
            connection.commit()
            raise AuthenticationError("Authentication failed")
        connection.execute("UPDATE admin_accounts SET failed_attempts=0,locked_until_utc=NULL,last_login_at_utc=? WHERE account_id=?",
                           (_utc(current), row[0]))
        session_id = str(uuid.uuid4())
        session_token = secrets.token_urlsafe(32)
        token_sha = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        connection.execute("INSERT INTO auth_sessions VALUES (?,?,?,?,?,?,NULL)",
                           (session_id, row[0], token_sha, row[8], _utc(current),
                            _utc(current + timedelta(minutes=SESSION_MINUTES))))
        _append_event(connection, "auth.success", row[0], row[0], {"role": row[3]}, current)
        connection.commit()
        return Principal(row[0], row[1], row[3], session_id, session_token)
    except AuthenticationError:
        raise
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _active_account(connection, principal, now=None):
    if (not isinstance(principal, Principal) or not isinstance(principal.session_token, str)
            or not principal.session_token or not isinstance(principal.session_id, str)):
        raise PermissionError("Authenticated administrator required")
    row = connection.execute(
        "SELECT a.username,a.role,a.status,a.auth_version,s.token_sha256,s.auth_version,s.expires_at_utc,s.revoked_at_utc "
        "FROM admin_accounts a JOIN auth_sessions s ON s.account_id=a.account_id "
        "WHERE a.account_id=? AND s.session_id=?", (principal.account_id, principal.session_id)).fetchone()
    actual = hashlib.sha256(principal.session_token.encode("utf-8")).hexdigest()
    current = now or datetime.now(timezone.utc)
    if (not row or row[2] != "active" or row[0] != principal.username or row[1] != principal.role
            or row[3] != row[5] or row[7] is not None or _parse_utc(row[6]) <= current
            or not secrets.compare_digest(row[4], actual)):
        raise PermissionError("Authentication credential is invalid or no longer active")
    return row


def _authorize(connection, principal, permission):
    row = _active_account(connection, principal)
    if not has_permission(row[1], permission):
        raise PermissionError("Permission denied")
    return row


def create_admin(path, principal, username, password, role, now=None):
    username, password = _username(username), _password(password)
    if role not in ROLES:
        raise ValueError("Invalid role")
    connection = open_auth_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _authorize(connection, principal, "admin.create")
        account_id, timestamp = str(uuid.uuid4()), _utc(now)
        connection.execute("INSERT INTO admin_accounts VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                           (account_id, username, generate_password_hash(password, method=PASSWORD_METHOD),
                            PASSWORD_METHOD, role, "active", timestamp, None, timestamp, 0, None, 1))
        _append_event(connection, "admin.create", principal.account_id, account_id, {"role": role}, now)
        connection.commit()
        return account_id
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def _protect_last_admin(connection, account_id, new_role=None, disabling=False):
    row = connection.execute("SELECT role,status FROM admin_accounts WHERE account_id=?", (account_id,)).fetchone()
    if not row:
        raise ValueError("Administrator not found")
    removes = row == ("system_admin", "active") and (disabling or new_role != "system_admin")
    if removes and connection.execute(
            "SELECT count(*) FROM admin_accounts WHERE role='system_admin' AND status='active'").fetchone()[0] <= 1:
        raise ValueError("Cannot remove the last active system administrator")


def assign_role(path, principal, account_id, role, now=None):
    if role not in ROLES:
        raise ValueError("Invalid role")
    connection = open_auth_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _authorize(connection, principal, "admin.assign_role")
        _protect_last_admin(connection, account_id, new_role=role)
        connection.execute("UPDATE admin_accounts SET role=?,auth_version=auth_version+1 WHERE account_id=?", (role, account_id))
        connection.execute("UPDATE auth_sessions SET revoked_at_utc=? WHERE account_id=? AND revoked_at_utc IS NULL",
                           (_utc(now), account_id))
        _append_event(connection, "admin.assign_role", principal.account_id, account_id, {"role": role}, now)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def disable_admin(path, principal, account_id, now=None):
    connection = open_auth_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _authorize(connection, principal, "admin.disable")
        _protect_last_admin(connection, account_id, disabling=True)
        if connection.execute("UPDATE admin_accounts SET status='disabled',auth_version=auth_version+1 WHERE account_id=?", (account_id,)).rowcount != 1:
            raise ValueError("Administrator not found")
        connection.execute("UPDATE auth_sessions SET revoked_at_utc=? WHERE account_id=? AND revoked_at_utc IS NULL",
                           (_utc(now), account_id))
        _append_event(connection, "admin.disable", principal.account_id, account_id, {}, now)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def change_password(path, principal, current_password, new_password, now=None):
    verified = authenticate(path, principal.username, current_password, now)
    if verified.account_id != principal.account_id:
        raise PermissionError("Account mismatch")
    _set_password(path, verified, verified.account_id, new_password, "admin.change_password", now)


def reset_password(path, principal, account_id, new_password, now=None):
    _set_password(path, principal, account_id, new_password, "admin.reset_password", now)


def _set_password(path, principal, account_id, new_password, event_type, now):
    new_password = _password(new_password)
    connection = open_auth_database(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if event_type == "admin.reset_password":
            _authorize(connection, principal, "admin.reset_password")
        else:
            _active_account(connection, principal)
        if connection.execute(
            "UPDATE admin_accounts SET password_hash=?,password_method=?,password_changed_at_utc=?,"
            "failed_attempts=0,locked_until_utc=NULL,auth_version=auth_version+1 WHERE account_id=?",
            (generate_password_hash(new_password, method=PASSWORD_METHOD), PASSWORD_METHOD, _utc(now), account_id)
        ).rowcount != 1:
            raise ValueError("Administrator not found")
        connection.execute("UPDATE auth_sessions SET revoked_at_utc=? WHERE account_id=? AND revoked_at_utc IS NULL",
                           (_utc(now), account_id))
        _append_event(connection, event_type, principal.account_id, account_id, {}, now)
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_admins(path, principal):
    connection = open_auth_database(path, readonly=True)
    try:
        _authorize(connection, principal, "audit.read")
        rows = connection.execute(
            "SELECT account_id,username,role,status,created_at_utc,last_login_at_utc,password_changed_at_utc "
            "FROM admin_accounts ORDER BY username COLLATE NOCASE").fetchall()
        keys = ("account_id", "username", "role", "status", "created_at_utc", "last_login_at_utc", "credential_changed_at_utc")
        return [dict(zip(keys, row)) for row in rows]
    finally:
        connection.close()


def authorized_review_decision(auth_path, principal, review_path, staging_path,
                               authorization_failure_hook=None, **decision):
    """Create a legacy-compatible review event plus separately verified authorization evidence."""
    connection = open_auth_database(auth_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _authorize(connection, principal, "review.decide")
        operation_id = decision.get("operation_id")
        # The legacy row intentionally remains self_asserted_cli. Only this independently
        # verified link upgrades the composite action's assurance.
        result = record_decision(review_path, staging_path, reviewer_id=principal.account_id, **decision)
        event_id, event_hash = result["event_id"], result["event_hash"]
        existing = connection.execute(
            "SELECT authorization_id,account_id,review_event_id,review_event_hash,release_key,fdc_id "
            "FROM authorized_review_actions WHERE operation_id=?", (operation_id,)).fetchone()
        if existing:
            if existing[1:] != (principal.account_id, event_id, event_hash,
                                decision["release_key"], decision["fdc_id"]):
                raise ValueError("Authorized operation ID conflicts with an existing action")
            connection.rollback()
            return {"status": "authorized_idempotent_replay", "authorization_id": existing[0],
                    "event_id": event_id, "event_hash": event_hash,
                    "identity_assurance": IDENTITY_ASSURANCE, "formal_approval_created": False}
        if authorization_failure_hook is not None:
            authorization_failure_hook()
        details = {"operation_id": operation_id, "review_event_id": event_id,
                   "review_event_hash": event_hash, "permission": "review.decide",
                   "identity_assurance": IDENTITY_ASSURANCE}
        security_event_id, timestamp, _ = _append_event(
            connection, "review.authorize", principal.account_id, principal.account_id, details)
        authorization_id = str(uuid.uuid4())
        connection.execute("INSERT INTO authorized_review_actions VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (authorization_id, operation_id, principal.account_id, event_id, event_hash,
                            decision["release_key"], decision["fdc_id"], "review.decide",
                            IDENTITY_ASSURANCE, timestamp, security_event_id))
        connection.commit()
        return {**result, "authorization_id": authorization_id,
                "identity_assurance": IDENTITY_ASSURANCE, "authenticated_account_id": principal.account_id,
                "legacy_event_identity_assurance": result["reviewer_identity_assurance"]}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def verify_auth_database(path):
    connection = open_auth_database(path, readonly=True)
    errors = []
    try:
        invalid_credentials = connection.execute(
            "SELECT count(*) FROM admin_accounts WHERE password_method<>? OR password_hash NOT LIKE ?",
            (PASSWORD_METHOD, PASSWORD_METHOD + "$%"),).fetchone()[0]
        if invalid_credentials:
            errors.append("invalid stored password hash metadata")
        previous = None
        for expected, row in enumerate(connection.execute("SELECT * FROM security_events ORDER BY sequence"), 1):
            sequence, event_id, event_type, actor, target, details_json, created, old_hash, event_hash = row
            try:
                details = json.loads(details_json)
            except ValueError:
                details = None
            payload = _event_payload(sequence, event_id, event_type, actor, target, details, created, old_hash)
            calculated = hashlib.sha256(canonical(payload).encode()).hexdigest()
            if sequence != expected or old_hash != previous or details is None or details_json != canonical(details) or calculated != event_hash:
                errors.append(f"security event integrity failure at sequence {sequence}")
            previous = event_hash
        actions = connection.execute(
            "SELECT a.authorization_id,a.operation_id,a.account_id,a.review_event_id,a.review_event_hash,"
            "a.permission,a.identity_assurance,a.security_event_id,e.event_type,e.actor_account_id,e.target_account_id,e.details_json "
            "FROM authorized_review_actions a LEFT JOIN security_events e ON e.event_id=a.security_event_id"
        ).fetchall()
        for action in actions:
            expected_details = {"operation_id": action[1], "review_event_id": action[3],
                                "review_event_hash": action[4], "permission": action[5],
                                "identity_assurance": action[6]}
            if (action[8:11] != ("review.authorize", action[2], action[2]) or
                    action[11] != canonical(expected_details)):
                errors.append(f"invalid authorized review link {action[0]}")
        return {"valid": not errors, "errors": errors,
                "security_event_count": connection.execute("SELECT count(*) FROM security_events").fetchone()[0],
                "authorized_review_action_count": connection.execute("SELECT count(*) FROM authorized_review_actions").fetchone()[0],
                "identity_assurance": IDENTITY_ASSURANCE,
                "hash_chain_limit": "no_external_signature_or_trusted_anchor"}
    finally:
        connection.close()


def verify_authorized_review_action(auth_path, review_path, review_event_id):
    """Verify that an unchanged PR #8 event has a valid authenticated authorization link."""
    auth_verification = verify_auth_database(auth_path)
    if not auth_verification["valid"]:
        return {"valid": False, "errors": auth_verification["errors"]}
    connection = open_auth_database(auth_path, readonly=True)
    try:
        row = connection.execute(
            "SELECT account_id,review_event_hash,identity_assurance FROM authorized_review_actions "
            "WHERE review_event_id=?", (review_event_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return {"valid": False, "errors": ["review event has no authenticated authorization link"]}
    from nutrition.review.workflow import open_review_database, verify_review_database
    review_verification = verify_review_database(review_path)
    if not review_verification["valid"]:
        return {"valid": False, "errors": review_verification["errors"]}
    review = open_review_database(review_path, readonly=True)
    try:
        event = review.execute(
            "SELECT event_hash,reviewer_id,reviewer_identity_assurance FROM review_events WHERE event_id=?",
            (review_event_id,)).fetchone()
    finally:
        review.close()
    if event is None or event[0] != row[1] or event[1] != row[0] or event[2] != "self_asserted_cli":
        return {"valid": False, "errors": ["authorized link does not match immutable review event"]}
    return {"valid": True, "errors": [], "identity_assurance": row[2],
            "legacy_event_identity_assurance": event[2], "formal_approval_created": False}
