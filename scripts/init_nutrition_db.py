"""Initialize independent nutrition schema; never import or rewrite food data."""
import argparse
from pathlib import Path
import sqlite3

SCHEMA = Path(__file__).resolve().parents[1] / 'nutrition' / 'schema.sql'


def connect_database(path):
    """Open a connection with enforced foreign keys (also for later readers)."""
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA foreign_keys = ON')
    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        connection.close()
        raise RuntimeError('SQLite foreign key enforcement is unavailable')
    return connection


def _schema_objects(connection):
    return connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
    ).fetchall()


def _validate_existing_schema(connection):
    """Refuse unrelated/partial/altered databases without changing their data."""
    objects = _schema_objects(connection)
    if not objects:
        return
    reference = connect_database(':memory:')
    try:
        reference.executescript(SCHEMA.read_text(encoding='utf-8'))
        if objects != _schema_objects(reference):
            raise ValueError('Existing database schema does not match nutrition v1')
        expected = reference.execute('SELECT * FROM nutrient_definitions ORDER BY nutrient_code').fetchall()
        for row in expected:
            existing = connection.execute(
                'SELECT * FROM nutrient_definitions WHERE nutrient_code=?', (row[0],)
            ).fetchone()
            if existing is not None and existing != row:
                raise ValueError('Existing nutrient definition conflicts with nutrition v1')
        if connection.execute('PRAGMA foreign_key_check').fetchone() is not None:
            raise ValueError('Existing database contains foreign key violations')
    finally:
        reference.close()


def initialize_database(path, *, schema_path=SCHEMA):
    """Return an open FK-enabled connection. Caller must close it.

    Reject non-SQLite files before connecting. Initialize all DDL atomically.
    Existing v1 tables and records are preserved; this is not a migration tool.
    """
    if str(path) != ':memory:':
        target = Path(path)
        if target.exists():
            with target.open('rb') as source:
                if source.read(16) != b'SQLite format 3\x00':
                    raise ValueError('Target exists and is not a SQLite database')
    sql = Path(schema_path).read_text(encoding='utf-8')
    connection = connect_database(path)
    try:
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1):
            raise ValueError(f'Unsupported schema version: {version}')
        _validate_existing_schema(connection)
        connection.executescript('BEGIN IMMEDIATE;\n' + sql + '\nPRAGMA user_version = 1;\nCOMMIT;')
        return connection
    except BaseException:
        connection.rollback()
        connection.close()
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', required=True, help='Explicit SQLite output path (outside Git recommended)')
    args = parser.parse_args()
    connection = initialize_database(args.database)
    connection.close()
    print('Nutrition schema v1 initialized; no food records imported.')


if __name__ == '__main__':
    main()
