"""Append-only manual review workflow isolated from staging and production data."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

from nutrition.review.policy import DEFAULT_POLICY, ReviewPolicy, canonical, evaluate_food_record
from nutrition.sources.usda_foundation import MAPPING_VERSION, PARSER_VERSION
from scripts.review_usda_staging import open_staging_readonly

APPLICATION_ID = 0x4E525657
USER_VERSION = 1
DECISIONS = ("accept_candidate", "request_changes", "reject_candidate")
EVENT_TYPES = DECISIONS + ("supersede_decision",)
HASH_ALGORITHM = "sha256-canonical-json-v1"
MAX_AUDIT_JSON_BYTES = 1024 * 1024
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}")

DDL = (
    "CREATE TABLE review_events ("
    "event_sequence INTEGER PRIMARY KEY NOT NULL CHECK(event_sequence>0),"
    "event_id TEXT NOT NULL UNIQUE CHECK(length(trim(event_id))>0),"
    "operation_id TEXT NOT NULL UNIQUE CHECK(length(trim(operation_id))>0),"
    "request_sha256 TEXT NOT NULL CHECK(length(request_sha256)=64),"
    "release_key TEXT NOT NULL CHECK(length(trim(release_key))>0),"
    "fdc_id INTEGER NOT NULL CHECK(typeof(fdc_id)='integer' AND fdc_id>0),"
    "record_sha256 TEXT NOT NULL CHECK(length(record_sha256)=64),"
    "policy_version TEXT NOT NULL CHECK(length(trim(policy_version))>0),"
    "evaluation_decision TEXT NOT NULL CHECK(evaluation_decision IN ('ready_for_manual_review','needs_review','rejected_by_policy')) ,"
    "evaluation_sha256 TEXT NOT NULL CHECK(length(evaluation_sha256)=64),"
    "evaluation_json TEXT NOT NULL,"
    "event_type TEXT NOT NULL CHECK(event_type IN ('accept_candidate','request_changes','reject_candidate','supersede_decision')) ,"
    "effective_decision TEXT NOT NULL CHECK(effective_decision IN ('accept_candidate','request_changes','reject_candidate')) ,"
    "review_comment TEXT NOT NULL CHECK(length(trim(review_comment))>0),"
    "review_reason TEXT NOT NULL CHECK(length(trim(review_reason))>0),"
    "issue_resolutions_json TEXT NOT NULL,"
    "evidence_references_json TEXT NOT NULL,"
    "reviewer_id TEXT NOT NULL CHECK(length(trim(reviewer_id))>0),"
    "reviewer_identity_assurance TEXT NOT NULL CHECK(reviewer_identity_assurance='self_asserted_cli'),"
    "decided_at_utc TEXT NOT NULL CHECK(decided_at_utc GLOB '????-??-??T??:??:??.??????Z'),"
    "previous_event_id TEXT REFERENCES review_events(event_id),"
    "supersedes_event_id TEXT REFERENCES review_events(event_id),"
    "previous_event_hash TEXT,"
    "hash_algorithm TEXT NOT NULL CHECK(hash_algorithm='sha256-canonical-json-v1'),"
    "event_hash TEXT NOT NULL UNIQUE CHECK(length(event_hash)=64),"
    "CHECK((event_type='supersede_decision' AND previous_event_id IS NOT NULL AND supersedes_event_id=previous_event_id) OR "
    "(event_type<>'supersede_decision' AND previous_event_id IS NULL AND supersedes_event_id IS NULL))"
    ")",
    "CREATE TABLE current_decisions ("
    "release_key TEXT NOT NULL,fdc_id INTEGER NOT NULL,current_event_id TEXT NOT NULL UNIQUE REFERENCES review_events(event_id),"
    "record_sha256 TEXT NOT NULL,policy_version TEXT NOT NULL,effective_decision TEXT NOT NULL "
    "CHECK(effective_decision IN ('accept_candidate','request_changes','reject_candidate')) ,"
    "PRIMARY KEY(release_key,fdc_id))",
    "CREATE TRIGGER review_events_no_update BEFORE UPDATE ON review_events BEGIN SELECT RAISE(ABORT,'review events are append-only'); END",
    "CREATE TRIGGER review_events_no_delete BEFORE DELETE ON review_events BEGIN SELECT RAISE(ABORT,'review events are append-only'); END",
)


def _objects(connection):
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()


def _connect(path, *, readonly=False):
    if readonly:
        connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            connection.close()
            raise RuntimeError("SQLite read-only enforcement unavailable")
    else:
        connection = sqlite3.connect(path)
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise RuntimeError("SQLite foreign key enforcement unavailable")
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
        raise ValueError("Database is not manual nutrition review v1")
    if connection.execute("PRAGMA user_version").fetchone()[0] != USER_VERSION:
        raise ValueError("Unsupported manual review database version")
    if _objects(connection) != _reference_objects():
        raise ValueError("Unexpected manual review database schema")
    if connection.execute("PRAGMA foreign_key_check").fetchone() or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise ValueError("Invalid manual review database")


def initialize_review_database(path):
    """Exclusively create a dedicated review database; never reuse a file."""
    if path is None or str(path) == ":memory:":
        raise ValueError("Explicit file path required")
    target = Path(path)
    if target.is_symlink() or not target.parent.is_dir():
        raise ValueError("Review database parent must exist and target must not be a link")
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
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()
        if not succeeded and target.exists():
            target.unlink()
    return target


def open_review_database(path, *, readonly=False):
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("Review database must be a regular non-symlink file")
    connection = _connect(target, readonly=readonly)
    try:
        _validate_schema(connection)
        return connection
    except BaseException:
        connection.close()
        raise


def _release_evidence(provenance_evidence, release_key):
    evidence = provenance_evidence or {}
    if not isinstance(evidence, dict) or set(evidence) - {release_key}:
        raise ValueError("Provenance evidence must contain only the selected release")
    selected = evidence.get(release_key, {})
    allowed = {"official_file_verified", "verified_source_file_sha256", "verified_source_version",
               "verified_source_url", "verification_reference"}
    if not isinstance(selected, dict) or set(selected) - allowed:
        raise ValueError("Invalid provenance evidence fields")
    return selected


def evaluate_staging_subject(staging_path, release_key, fdc_id, *, policy=DEFAULT_POLICY,
                             provenance_evidence=None):
    """Read one immutable staging subject and reuse PR #7's policy evaluator."""
    if not isinstance(policy, ReviewPolicy):
        raise TypeError("policy must be ReviewPolicy")
    if not isinstance(release_key, str) or not release_key.strip() or type(fdc_id) is not int or fdc_id <= 0:
        raise ValueError("Valid release_key and FDC ID required")
    connection = open_staging_readonly(staging_path)
    try:
        release = connection.execute(
            "SELECT source_url,source_version,source_sha256,parser_version,mapping_version "
            "FROM staging_releases WHERE release_key=?", (release_key,)
        ).fetchone()
        row = connection.execute(
            "SELECT source_locator,record_json,record_sha256,review_status FROM staging_records "
            "WHERE release_key=? AND fdc_id=?", (release_key, fdc_id)
        ).fetchone()
        if release is None or row is None:
            raise ValueError("Staging release or food does not exist")
        source_url, source_version, source_sha, parser_version, mapping_version = release
        locator, record_json, record_sha, staging_status = row
        if parser_version != PARSER_VERSION or mapping_version != MAPPING_VERSION or staging_status != "pending":
            raise ValueError("Unsupported staging metadata")
        try:
            record = json.loads(record_json)
        except (TypeError, ValueError) as error:
            raise ValueError("Invalid staging record JSON") from error
        actual_sha = hashlib.sha256(canonical(record).encode("utf-8")).hexdigest()
        if (actual_sha != record_sha or record.get("source_food_id") != fdc_id or
                record.get("source_version") != source_version or record.get("source_url") != source_url or
                record.get("source_file_sha256") != source_sha or record.get("source_locator") != locator):
            raise ValueError("Staging record digest or provenance mismatch")
        context = {"official_file_verified": False, "verified_source_file_sha256": None,
                   "verified_source_version": None, "verified_source_url": None,
                   "verification_reference": None,
                   "duplicate_fdc_count": connection.execute(
                       "SELECT count(*) FROM staging_records WHERE release_key=? AND fdc_id=?",
                       (release_key, fdc_id)).fetchone()[0]}
        context.update(_release_evidence(provenance_evidence, release_key))
        evaluation = evaluate_food_record(record, context, policy)
        return {"release_key": release_key, "fdc_id": fdc_id, "record_sha256": record_sha,
                "source_version": source_version, "source_file_sha256": source_sha,
                "reviewer_identity_assurance": "self_asserted_cli",
                "official_source_authentication": "not_performed_by_workflow",
                "required_issue_resolution_keys": [issue_key(issue) for issue in evaluation["issues"]],
                "evaluation": evaluation}
    finally:
        connection.close()


def issue_key(issue):
    return canonical([issue["rule_id"], issue["source_location"], issue["affected_nutrient_ids"]])


def _text(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 4000:
        raise ValueError(f"{name} is required and must be at most 4000 characters")
    return value.strip()


def _identifier(value, name):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError(f"Invalid {name}")
    return value


def _utc_now(value=None):
    if value is None:
        value = datetime.now(timezone.utc)
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() != timezone.utc.utcoffset(value):
        raise ValueError("Decision time must be timezone-aware UTC")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _json_object(value, name):
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be an object")
    if not all(isinstance(key, str) and isinstance(item, str) and item.strip() and len(item) <= 4000
               for key, item in value.items()):
        raise ValueError(f"{name} must map string keys to nonempty strings of at most 4000 characters")
    if len(canonical(value).encode("utf-8")) > MAX_AUDIT_JSON_BYTES:
        raise ValueError(f"{name} exceeds size limit")
    return value


def _json_list(value, name):
    if value is None:
        value = []
    if (not isinstance(value, list) or len(value) > 100 or
            not all(isinstance(x, str) and x.strip() and len(x) <= 4000 for x in value) or
            len(canonical(value).encode("utf-8")) > MAX_AUDIT_JSON_BYTES):
        raise ValueError(f"{name} must be a list of nonempty strings")
    return value


def _event_payload(row):
    keys = ("event_sequence", "event_id", "operation_id", "request_sha256", "release_key", "fdc_id",
            "record_sha256", "policy_version", "evaluation_decision", "evaluation_sha256",
            "evaluation_json", "event_type", "effective_decision", "review_comment", "review_reason",
            "issue_resolutions_json", "evidence_references_json", "reviewer_id",
            "reviewer_identity_assurance", "decided_at_utc", "previous_event_id",
            "supersedes_event_id", "previous_event_hash", "hash_algorithm")
    return dict(zip(keys, row[:len(keys)]))


def _request_payload(subject, event_type, effective_decision, reviewer_id, comment, reason,
                     resolutions, references, policy_version):
    return {"release_key": subject["release_key"], "fdc_id": subject["fdc_id"],
            "record_sha256": subject["record_sha256"], "policy_version": policy_version,
            "evaluation_sha256": hashlib.sha256(canonical(subject["evaluation"]).encode("utf-8")).hexdigest(),
            "event_type": event_type, "effective_decision": effective_decision,
            "review_comment": comment, "review_reason": reason,
            "issue_resolutions": resolutions, "evidence_references": references,
            "reviewer_id": reviewer_id}


def record_decision(review_path, staging_path, *, release_key, fdc_id, operation_id,
                    event_type, reviewer_id, review_comment, review_reason,
                    replacement_decision=None, issue_resolutions=None, evidence_references=None,
                    policy=DEFAULT_POLICY, provenance_evidence=None, decided_at=None,
                    failure_hook=None):
    """Atomically append one event and update its current-decision projection."""
    operation_id = _identifier(operation_id, "operation_id")
    reviewer_id = _identifier(reviewer_id, "reviewer_id")
    review_comment = _text(review_comment, "review_comment")
    review_reason = _text(review_reason, "review_reason")
    if event_type not in EVENT_TYPES:
        raise ValueError("Invalid review event type")
    if event_type == "supersede_decision":
        if replacement_decision not in DECISIONS:
            raise ValueError("Supersede requires a valid replacement decision")
        effective_decision = replacement_decision
    else:
        if replacement_decision is not None:
            raise ValueError("Replacement decision is only valid for supersede")
        effective_decision = event_type
    resolutions = _json_object(issue_resolutions, "issue_resolutions")
    references = _json_list(evidence_references, "evidence_references")
    subject = evaluate_staging_subject(staging_path, release_key, fdc_id, policy=policy,
                                       provenance_evidence=provenance_evidence)
    evaluation = subject["evaluation"]
    if effective_decision == "accept_candidate":
        if evaluation["decision"] == "rejected_by_policy":
            raise ValueError("Policy-rejected records cannot be accepted")
        if evaluation["decision"] == "needs_review":
            required = {issue_key(issue) for issue in evaluation["issues"]}
            supplied = set(resolutions)
            if supplied != required or not all(isinstance(resolutions[key], str) and resolutions[key].strip()
                                               for key in required):
                raise ValueError("Conditional acceptance requires a nonempty resolution for every issue")
    timestamp = _utc_now(decided_at)
    request = _request_payload(subject, event_type, effective_decision, reviewer_id,
                               review_comment, review_reason, resolutions, references, policy.version)
    request_sha = hashlib.sha256(canonical(request).encode("utf-8")).hexdigest()
    connection = open_review_database(review_path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        # The write reservation prevents a normal concurrent writer from
        # changing the audit between verification and this transaction.
        verification = verify_review_database(review_path)
        if not verification["valid"]:
            raise ValueError("Existing review audit is invalid: " + "; ".join(verification["errors"]))
        existing = connection.execute(
            "SELECT event_id,request_sha256,event_hash FROM review_events WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if existing is not None:
            if existing[1] != request_sha:
                raise ValueError("Operation ID was already used for a different request")
            connection.rollback()
            return {"status": "idempotent_replay", "event_id": existing[0],
                    "event_hash": existing[2], "effective_decision": effective_decision,
                    "reviewer_identity_assurance": "self_asserted_cli"}
        current = connection.execute(
            "SELECT current_event_id,record_sha256,policy_version,effective_decision FROM current_decisions "
            "WHERE release_key=? AND fdc_id=?", (release_key, fdc_id)
        ).fetchone()
        if current is None and event_type == "supersede_decision":
            raise ValueError("Cannot supersede without a previous decision")
        if current is not None and event_type != "supersede_decision":
            raise ValueError("An existing decision can only be changed by supersede_decision")
        previous_event_id = current[0] if current else None
        previous_event_hash = connection.execute(
            "SELECT event_hash FROM review_events ORDER BY event_sequence DESC LIMIT 1"
        ).fetchone()
        previous_event_hash = previous_event_hash[0] if previous_event_hash else None
        sequence = connection.execute("SELECT coalesce(max(event_sequence),0)+1 FROM review_events").fetchone()[0]
        event_id = str(uuid.uuid4())
        evaluation_json = canonical(evaluation)
        evaluation_sha = hashlib.sha256(evaluation_json.encode("utf-8")).hexdigest()
        row_without_hash = (sequence, event_id, operation_id, request_sha, release_key, fdc_id,
                            subject["record_sha256"], policy.version, evaluation["decision"], evaluation_sha,
                            evaluation_json, event_type, effective_decision, review_comment, review_reason,
                            canonical(resolutions), canonical(references), reviewer_id, "self_asserted_cli",
                            timestamp, previous_event_id, previous_event_id if event_type == "supersede_decision" else None,
                            previous_event_hash, HASH_ALGORITHM)
        event_hash = hashlib.sha256(canonical(_event_payload(row_without_hash)).encode("utf-8")).hexdigest()
        connection.execute("INSERT INTO review_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                           row_without_hash + (event_hash,))
        if failure_hook is not None:
            failure_hook()
        if current is None:
            connection.execute("INSERT INTO current_decisions VALUES (?,?,?,?,?,?)",
                               (release_key, fdc_id, event_id, subject["record_sha256"], policy.version,
                                effective_decision))
        else:
            connection.execute(
                "UPDATE current_decisions SET current_event_id=?,record_sha256=?,policy_version=?,effective_decision=? "
                "WHERE release_key=? AND fdc_id=?",
                (event_id, subject["record_sha256"], policy.version, effective_decision, release_key, fdc_id))
        connection.commit()
        return {"status": "recorded", "event_id": event_id, "event_hash": event_hash,
                "effective_decision": effective_decision, "review_status": "pending",
                "reviewer_identity_assurance": "self_asserted_cli",
                "formal_approval_created": False}
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def history(review_path, release_key, fdc_id):
    connection = open_review_database(review_path, readonly=True)
    try:
        columns = [item[1] for item in connection.execute("PRAGMA table_info(review_events)")]
        rows = connection.execute(
            "SELECT * FROM review_events WHERE release_key=? AND fdc_id=? ORDER BY event_sequence",
            (release_key, fdc_id)).fetchall()
        return [dict(zip(columns, row)) for row in rows]
    finally:
        connection.close()


def verify_review_database(review_path):
    connection = open_review_database(review_path, readonly=True)
    errors = []
    try:
        rows = connection.execute("SELECT * FROM review_events ORDER BY event_sequence").fetchall()
        previous_hash = None
        by_id = {}
        subject_current = {}
        for expected_sequence, row in enumerate(rows, 1):
            payload = _event_payload(row)
            calculated = hashlib.sha256(canonical(payload).encode("utf-8")).hexdigest()
            if row[0] != expected_sequence:
                errors.append(f"event sequence gap at {row[0]}")
            if row[-2] != HASH_ALGORITHM or row[-1] != calculated:
                errors.append(f"event hash mismatch at {row[0]}")
            if row[-3] != previous_hash:
                errors.append(f"event chain mismatch at {row[0]}")
            evaluation = json.loads(row[10])
            resolutions = json.loads(row[15])
            references = json.loads(row[16])
            if not isinstance(evaluation, dict) or not isinstance(resolutions, dict) or not isinstance(references, list):
                errors.append(f"invalid audit JSON types at {row[0]}")
                evaluation = evaluation if isinstance(evaluation, dict) else {}
                resolutions = resolutions if isinstance(resolutions, dict) else {}
                references = references if isinstance(references, list) else []
            if row[10] != canonical(evaluation) or hashlib.sha256(row[10].encode("utf-8")).hexdigest() != row[9]:
                errors.append(f"evaluation snapshot mismatch at {row[0]}")
            if (evaluation.get("source_food_id"), evaluation.get("evaluated_record_sha256"),
                    evaluation.get("policy_version"), evaluation.get("decision"),
                    evaluation.get("review_status")) != (row[5], row[6], row[7], row[8], "pending"):
                errors.append(f"evaluation metadata mismatch at {row[0]}")
            request = {"release_key": row[4], "fdc_id": row[5], "record_sha256": row[6],
                       "policy_version": row[7], "evaluation_sha256": row[9],
                       "event_type": row[11], "effective_decision": row[12],
                       "review_comment": row[13], "review_reason": row[14],
                       "issue_resolutions": resolutions, "evidence_references": references,
                       "reviewer_id": row[17]}
            if hashlib.sha256(canonical(request).encode("utf-8")).hexdigest() != row[3]:
                errors.append(f"request digest mismatch at {row[0]}")
            subject = (row[4], row[5])
            prior = subject_current.get(subject)
            if row[11] == "supersede_decision":
                if prior is None or row[20] != prior or row[21] != prior:
                    errors.append(f"invalid supersede transition at {row[0]}")
            elif prior is not None or row[20] is not None or row[21] is not None:
                errors.append(f"invalid initial transition at {row[0]}")
            subject_current[subject] = row[1]
            by_id[row[1]] = row
            previous_hash = row[-1]
        projections = connection.execute("SELECT * FROM current_decisions").fetchall()
        if {(row[0], row[1]): row[2] for row in projections} != subject_current:
            errors.append("current projection subject set or latest event mismatch")
        for release_key, fdc_id, event_id, record_sha, policy_version, effective in projections:
            row = by_id.get(event_id)
            if row is None or (row[4], row[5], row[6], row[7], row[12]) != (
                    release_key, fdc_id, record_sha, policy_version, effective):
                errors.append(f"current projection mismatch for {release_key}/{fdc_id}")
        return {"valid": not errors, "event_count": len(rows), "current_count": connection.execute(
            "SELECT count(*) FROM current_decisions").fetchone()[0], "errors": errors,
            "last_event_hash": previous_hash, "hash_algorithm": HASH_ALGORITHM,
            "tamper_resistance_limit": "no_external_signature_or_trusted_anchor"}
    except (ValueError, TypeError, json.JSONDecodeError) as error:
        return {"valid": False, "event_count": 0, "current_count": 0,
                "errors": [str(error)], "last_event_hash": None,
                "hash_algorithm": HASH_ALGORITHM,
                "tamper_resistance_limit": "no_external_signature_or_trusted_anchor"}
    finally:
        connection.close()


def summary(review_path):
    verification = verify_review_database(review_path)
    if not verification["valid"]:
        raise ValueError("Review audit verification failed: " + "; ".join(verification["errors"]))
    connection = open_review_database(review_path, readonly=True)
    try:
        decisions = {name: 0 for name in DECISIONS}
        decisions.update(dict(connection.execute(
            "SELECT effective_decision,count(*) FROM current_decisions GROUP BY effective_decision"
        ).fetchall()))
        return {"current_candidates": sum(decisions.values()), "current_decisions": decisions,
                "event_count": connection.execute("SELECT count(*) FROM review_events").fetchone()[0],
                "reviewer_identity_assurance": "self_asserted_cli",
                "formal_approved_foods": 0}
    finally:
        connection.close()
