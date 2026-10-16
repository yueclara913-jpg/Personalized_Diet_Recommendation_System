"""Versioned SQLite repository physically isolated from administrator data."""
import json
from pathlib import Path
import sqlite3

from .models import HealthProfile, utc_now
from .validation import validate_profile

APPLICATION_ID = 0x48505246
USER_VERSION = 1
DDL = ("""CREATE TABLE health_profiles (
 profile_id TEXT PRIMARY KEY CHECK(length(profile_id) BETWEEN 1 AND 64),
 age INTEGER NOT NULL CHECK(age BETWEEN 18 AND 100),
 height_cm REAL NOT NULL CHECK(height_cm BETWEEN 100 AND 250),
 weight_kg REAL NOT NULL CHECK(weight_kg BETWEEN 30 AND 350),
 sex_for_equation TEXT NOT NULL CHECK(sex_for_equation IN ('male_parameter','female_parameter')),
 activity_level TEXT NOT NULL CHECK(activity_level IN ('sedentary','lightly_active','moderately_active','very_active','extra_active')),
 health_goal TEXT NOT NULL CHECK(health_goal IN ('maintain_weight','lose_weight','gain_weight')),
 dietary_restrictions_json TEXT NOT NULL,
 population_status TEXT NOT NULL CHECK(population_status IN ('general_adult','pregnant','lactating','special_assessment')),
 created_at_utc TEXT NOT NULL,
 updated_at_utc TEXT NOT NULL
) STRICT""",)


def _objects(connection):
    return connection.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()


def _reference_objects():
    connection = sqlite3.connect(":memory:")
    try:
        for statement in DDL:
            connection.execute(statement)
        return _objects(connection)
    finally:
        connection.close()


def _connect(path, readonly=False):
    target = Path(path)
    uri = target.resolve().as_uri() + ("?mode=ro" if readonly else "")
    connection = sqlite3.connect(uri, uri=True)
    connection.execute("PRAGMA foreign_keys=ON")
    if readonly:
        connection.execute("PRAGMA query_only=ON")
    return connection


def initialize_database(path):
    target = Path(path)
    if target.exists() or target.is_symlink() or not target.parent.is_dir():
        raise FileExistsError("Health profile database path must be new")
    connection = None
    try:
        connection = _connect(target)
        connection.execute("BEGIN IMMEDIATE")
        for statement in DDL:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={USER_VERSION}")
        connection.commit()
        validate_database(target)
    except BaseException:
        if connection is not None:
            connection.rollback()
        if target.exists():
            target.unlink()
        raise
    finally:
        if connection is not None:
            connection.close()
    return target


def validate_database(path):
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("Health profile database must be a regular file")
    connection = _connect(target, readonly=True)
    try:
        if (connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID or
                connection.execute("PRAGMA user_version").fetchone()[0] != USER_VERSION or
                _objects(connection) != _reference_objects() or
                connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok"):
            raise ValueError("Invalid health profile database")
    finally:
        connection.close()


def _row(profile):
    return (profile.profile_id, profile.age, profile.height_cm, profile.weight_kg,
            profile.sex_for_equation, profile.activity_level, profile.health_goal,
            json.dumps(profile.dietary_restrictions, ensure_ascii=False, separators=(",", ":")),
            profile.population_status, profile.created_at_utc, profile.updated_at_utc)


def insert_profile(path, profile, *, failure_hook=None):
    item = validate_profile(profile)
    validate_database(path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("INSERT INTO health_profiles VALUES (?,?,?,?,?,?,?,?,?,?,?)", _row(item))
        if failure_hook:
            failure_hook()
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return item


def get_profile(path, profile_id):
    validate_database(path)
    connection = _connect(path, readonly=True)
    try:
        row = connection.execute("SELECT * FROM health_profiles WHERE profile_id=?", (profile_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    try:
        restrictions = json.loads(row[7])
    except (TypeError, json.JSONDecodeError) as error:
        raise ValueError("Invalid stored health profile JSON") from error
    return validate_profile(HealthProfile(row[0], row[1], row[2], row[3], row[4], row[5], row[6],
                                          restrictions, row[8], row[9], row[10]))


def update_profile(path, profile, *, expected_updated_at, now=None, failure_hook=None):
    current = validate_profile(profile)
    timestamp = now or utc_now()
    updated = HealthProfile(current.profile_id, current.age, current.height_cm, current.weight_kg,
                            current.sex_for_equation, current.activity_level, current.health_goal,
                            current.dietary_restrictions, current.population_status,
                            current.created_at_utc, timestamp)
    updated = validate_profile(updated)
    validate_database(path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        cursor = connection.execute(
            "UPDATE health_profiles SET age=?,height_cm=?,weight_kg=?,sex_for_equation=?,activity_level=?,"
            "health_goal=?,dietary_restrictions_json=?,population_status=?,updated_at_utc=? "
            "WHERE profile_id=? AND updated_at_utc=?", _row(updated)[1:9] + (updated.updated_at_utc,
                                                                            updated.profile_id,
                                                                            expected_updated_at))
        if cursor.rowcount != 1:
            raise ValueError("Profile changed or does not exist")
        if failure_hook:
            failure_hook()
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return updated
