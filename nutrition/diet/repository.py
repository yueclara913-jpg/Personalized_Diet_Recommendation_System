"""Isolated, versioned SQLite diet log repository with explicit profile checks."""
from dataclasses import replace
from pathlib import Path
import sqlite3

from nutrition.profile.repository import get_profile
from .models import DietRecord, utc_now, validate_record
from .validation import DietValidationError, identifier, iso_date, utc_timestamp

APPLICATION_ID = 0x44494554
USER_VERSION = 1
DDL = ("""CREATE TABLE diet_records (
 record_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, meal_date TEXT NOT NULL,
 meal_type TEXT NOT NULL CHECK(meal_type IN ('breakfast','lunch','dinner','snack')),
 food_name TEXT NOT NULL, amount_g REAL NOT NULL CHECK(amount_g>0),
 food_source_type TEXT NOT NULL CHECK(food_source_type IN ('manual_unverified','external_unverified')),
 source_reference TEXT NOT NULL, source_version TEXT, energy_definition TEXT,
 energy_kcal_per_100g REAL CHECK(energy_kcal_per_100g IS NULL OR energy_kcal_per_100g>=0),
 protein_g_per_100g REAL CHECK(protein_g_per_100g IS NULL OR protein_g_per_100g>=0),
 fat_g_per_100g REAL CHECK(fat_g_per_100g IS NULL OR fat_g_per_100g>=0),
 carbohydrates_g_per_100g REAL CHECK(carbohydrates_g_per_100g IS NULL OR carbohydrates_g_per_100g>=0),
 nutrient_data_status TEXT NOT NULL CHECK(nutrient_data_status IN ('complete_unverified','partial_unverified','unknown_unverified')),
 source_snapshot_sha256 TEXT NOT NULL CHECK(length(source_snapshot_sha256)=64),
 notes TEXT, record_status TEXT NOT NULL CHECK(record_status IN ('active','revoked')),
 created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL
) STRICT""",
       "CREATE INDEX diet_records_profile_date ON diet_records(profile_id,meal_date,record_status)")


def _objects(connection):
    return connection.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()


def _reference_objects():
    connection = sqlite3.connect(":memory:")
    try:
        for statement in DDL: connection.execute(statement)
        return _objects(connection)
    finally: connection.close()


def _connect(path, readonly=False):
    target = Path(path)
    connection = sqlite3.connect(target.resolve().as_uri() + ("?mode=ro" if readonly else ""), uri=True)
    connection.execute("PRAGMA foreign_keys=ON")
    if readonly: connection.execute("PRAGMA query_only=ON")
    return connection


def initialize_database(path):
    target = Path(path)
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise FileExistsError("Diet database path must be new")
    connection = None
    try:
        connection = _connect(target); connection.execute("BEGIN IMMEDIATE")
        for statement in DDL: connection.execute(statement)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={USER_VERSION}"); connection.commit()
        validate_database(target)
    except BaseException:
        if connection: connection.rollback()
        if target.exists(): target.unlink()
        raise
    finally:
        if connection: connection.close()
    return target


def validate_database(path):
    target = Path(path)
    if target.is_symlink() or not target.is_file(): raise ValueError("Diet database must be a regular file")
    connection = _connect(target, True)
    try:
        if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or
                connection.execute("PRAGMA user_version").fetchone()[0] != USER_VERSION or
                _objects(connection) != _reference_objects() or
                connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
            raise ValueError("Invalid diet database")
    finally: connection.close()


def _ensure_profile(profile_db, profile_id):
    identifier(profile_id, "profile_id")
    if get_profile(profile_db, profile_id) is None: raise DietValidationError("Health profile does not exist")


def _row(item):
    return tuple(getattr(item, name) for name in DietRecord.__dataclass_fields__)


def insert_record(path, profile_db, record, *, failure_hook=None):
    item = validate_record(record); _ensure_profile(profile_db, item.profile_id); validate_database(path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO diet_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", _row(item))
        if failure_hook: failure_hook()
        connection.commit()
    except BaseException: connection.rollback(); raise
    finally: connection.close()
    return item


def _from_row(row):
    return validate_record(DietRecord(*row))


def get_record(path, profile_db, record_id, *, profile_id):
    identifier(record_id, "record_id"); _ensure_profile(profile_db, profile_id); validate_database(path)
    connection = _connect(path, True)
    try: row = connection.execute("SELECT * FROM diet_records WHERE record_id=? AND profile_id=?", (record_id, profile_id)).fetchone()
    finally: connection.close()
    return None if row is None else _from_row(row)


def list_records(path, profile_db, *, profile_id, meal_date):
    _ensure_profile(profile_db, profile_id); selected = iso_date(meal_date); validate_database(path)
    connection = _connect(path, True)
    try: rows = connection.execute("SELECT * FROM diet_records WHERE profile_id=? AND meal_date=? ORDER BY created_at_utc,record_id", (profile_id, selected)).fetchall()
    finally: connection.close()
    return [_from_row(row) for row in rows]


def update_record(path, profile_db, record, *, expected_updated_at, now=None, failure_hook=None):
    item = validate_record(record); _ensure_profile(profile_db, item.profile_id); validate_database(path)
    timestamp = now or utc_now(); updated = validate_record(replace(item, updated_at_utc=timestamp))
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute("SELECT source_snapshot_sha256 FROM diet_records WHERE record_id=? AND profile_id=?", (item.record_id,item.profile_id)).fetchone()
        if existing is None or existing[0] != item.source_snapshot_sha256: raise ValueError("Record missing or immutable snapshot changed")
        cursor = connection.execute("UPDATE diet_records SET meal_date=?,meal_type=?,amount_g=?,notes=?,updated_at_utc=? WHERE record_id=? AND profile_id=? AND updated_at_utc=? AND record_status='active'",
                                    (updated.meal_date,updated.meal_type,updated.amount_g,updated.notes,updated.updated_at_utc,updated.record_id,updated.profile_id,expected_updated_at))
        if cursor.rowcount != 1: raise ValueError("Diet record changed, revoked or does not exist")
        if failure_hook: failure_hook()
        connection.commit()
    except BaseException: connection.rollback(); raise
    finally: connection.close()
    return updated


def revoke_record(path, profile_db, record_id, *, profile_id, expected_updated_at, now=None):
    identifier(record_id, "record_id"); _ensure_profile(profile_db, profile_id); validate_database(path)
    timestamp = utc_timestamp(now or utc_now(), "updated_at_utc")
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute("UPDATE diet_records SET record_status='revoked',updated_at_utc=? WHERE record_id=? AND profile_id=? AND updated_at_utc=? AND record_status='active'", (timestamp,record_id,profile_id,expected_updated_at))
        if cursor.rowcount != 1: raise ValueError("Diet record changed, revoked or does not exist")
        connection.commit()
    except BaseException: connection.rollback(); raise
    finally: connection.close()
