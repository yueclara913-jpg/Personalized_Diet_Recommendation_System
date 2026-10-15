"""Lossless pending-only staging in a dedicated SQLite database (not schema v1)."""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import tempfile
from urllib.parse import urlsplit

from nutrition.sources.usda_foundation import parse_snapshot, PARSER_VERSION, MAPPING_VERSION

MAX_BYTES = 64 * 1024 * 1024
APPLICATION_ID = 0x55464453
DDL = (
    "CREATE TABLE staging_releases (release_key TEXT PRIMARY KEY NOT NULL, source_url TEXT NOT NULL, source_version TEXT NOT NULL, source_sha256 TEXT NOT NULL CHECK(length(source_sha256)=64), parser_version TEXT NOT NULL, mapping_version TEXT NOT NULL, UNIQUE(source_url,source_version))",
    "CREATE TABLE staging_records (release_key TEXT NOT NULL REFERENCES staging_releases(release_key), fdc_id INTEGER NOT NULL CHECK(fdc_id>0), source_locator TEXT NOT NULL, record_json TEXT NOT NULL, record_sha256 TEXT NOT NULL, anomaly_count INTEGER NOT NULL CHECK(anomaly_count>=0), review_status TEXT NOT NULL DEFAULT 'pending' CHECK(review_status='pending'), PRIMARY KEY(release_key,fdc_id))",
)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON key')
        result[key] = value
    return result


def read_payload(path):
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_BYTES:
        raise ValueError('Expected bounded regular parsed JSON file')
    return json.loads(source.read_text(encoding='utf-8'), object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Non-finite JSON value')))


def validate_payload(payload):
    """Recompute each parsed record from preserved raw data, distrusting derived fields.

    Original-file hash is a declared provenance value, not independently verified
    against a real snapshot. The staging batch has a separate computed hash.
    """
    if len(canonical(payload).encode('utf-8')) > MAX_BYTES:
        raise ValueError('Parsed payload exceeds size limit')
    if not isinstance(payload, dict) or set(payload) != {'source_file_sha256','source_version','parser_version','mapping_version','records'}:
        raise ValueError('Expected exact parser staging envelope')
    if payload['parser_version'] != PARSER_VERSION or payload['mapping_version'] != MAPPING_VERSION:
        raise ValueError('Unsupported parser or mapping version')
    if not isinstance(payload['source_version'], str) or not payload['source_version'].strip():
        raise ValueError('Missing source version')
    sha = payload['source_file_sha256']
    if not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{64}', sha):
        raise ValueError('Invalid declared source SHA-256')
    records = payload['records']
    if not isinstance(records, list) or not records:
        raise ValueError('Nonempty parsed records required')
    seen, urls, anomalies = set(), set(), []
    with tempfile.TemporaryDirectory(prefix='usda-stage-check-') as directory:
        path = Path(directory) / 'TEST_REPLAY.json'
        for index, record in enumerate(records):
            if not isinstance(record, dict):
                raise ValueError('Invalid parsed food record')
            fdc = record.get('source_food_id')
            if type(fdc) is not int or not 0 < fdc <= 9223372036854775807 or fdc in seen:
                raise ValueError('Invalid or duplicate FDC ID')
            seen.add(fdc)
            url = record.get('source_url')
            if not isinstance(url, str):
                raise ValueError('Missing source URL')
            parts = urlsplit(url)
            if parts.scheme != 'https' or parts.hostname != 'fdc.nal.usda.gov' or parts.username or parts.password or parts.query or parts.fragment or parts.port not in (None,443):
                raise ValueError('Expected official credential-free USDA HTTPS source URL')
            urls.add(url)
            if record.get('source_data_type') != 'Foundation' or record.get('review_status') != 'pending':
                raise ValueError('Only pending Foundation records accepted')
            if record.get('source_locator') != f'FoundationFoods[{index}]':
                raise ValueError('Invalid source record position')
            if record.get('source_version') != payload['source_version'] or record.get('source_file_sha256') != sha:
                raise ValueError('Record release mismatch')
            # Replay at original index so every nested locator and all derived values are checked.
            raw = record.get('raw_record')
            if not isinstance(raw, dict) or not isinstance(raw.get('foodNutrients'), list):
                raise ValueError('Raw food and nutrient array required')
            nutrient_ids = []
            for entry in raw['foodNutrients']:
                nutrient = entry.get('nutrient') if isinstance(entry, dict) else None
                nid = nutrient.get('id') if isinstance(nutrient, dict) else None
                if isinstance(nid, str) and nid.isascii() and nid.isdigit() and len(nid)<=20:
                    nid = int(nid)
                if type(nid) is not int or nid <= 0 or nid in nutrient_ids:
                    raise ValueError('Invalid or duplicate nutrient ID')
                nutrient_ids.append(nid)
            path.write_text(canonical({'FoundationFoods':[raw]}), encoding='utf-8')
            expected = parse_snapshot(path, source_version=payload['source_version'], source_url=url,
                                      nutrition_basis=record.get('nutrition_basis'), basis_evidence=record.get('basis_evidence'),
                                      energy_id=record.get('energy_id'), sugar_id=record.get('sugar_id'))['records'][0]
            expected['source_file_sha256'] = sha
            # The replay only changes the original array position, never original values.
            expected['source_locator'] = f'FoundationFoods[{index}]'
            for nutrient in expected['nutrients']:
                nutrient['source_locator'] = nutrient['source_locator'].replace('FoundationFoods[0]',f'FoundationFoods[{index}]',1)
            if expected != record:
                raise ValueError('Parsed record differs from verified parser replay')
            issues = set(record['issues'])
            for nutrient in record['nutrients']:
                if nutrient.get('status') != 'converted':
                    issues.add(nutrient.get('reason') or 'nutrient_not_converted')
            if record['nutrition_basis'] != 'per_100g' or not record['basis_evidence']:
                issues.add('unverified_basis')
            for name, ids in [('energy',(1008,2047,2048)),('sugar',(1063,2000))]:
                if sum(n in nutrient_ids for n in ids)>1:
                    issues.add('multiple_'+name+'_definitions')
            anomalies.append(sorted(issues))
    if len(urls) != 1:
        raise ValueError('Mixed sources in batch')
    url = next(iter(urls))
    return url, anomalies


def _connect(path):
    connection = sqlite3.connect(path)
    connection.execute('PRAGMA foreign_keys=ON')
    if connection.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        connection.close()
        raise RuntimeError('Foreign keys unavailable')
    return connection


def _objects(connection):
    return connection.execute("SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()


def _validate_database(connection):
    reference = _connect(':memory:')
    try:
        for statement in DDL:
            reference.execute(statement)
        if connection.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID or _objects(connection) != _objects(reference):
            raise ValueError('Target is not dedicated staging v1; business databases refused')
        if connection.execute('PRAGMA user_version').fetchone()[0] != 1:
            raise ValueError('Unsupported staging version')
        if connection.execute('PRAGMA foreign_key_check').fetchone() or connection.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise ValueError('Invalid staging database')
    finally:
        reference.close()


def import_payload(payload, *, mode='dry-run', database=None, confirm_existing=False):
    if mode not in ('dry-run','staging'):
        raise ValueError('Invalid mode')
    url, anomalies = validate_payload(payload)
    report = dict(mode=mode, total=len(payload['records']), inserted=0, skipped=0,
                  anomaly_records=sum(bool(x) for x in anomalies), anomaly_count=sum(map(len,anomalies)),
                  findings=[dict(source_locator=r['source_locator'],fdc_id=r['source_food_id'],issues=a)
                            for r,a in zip(payload['records'],anomalies)],
                  review_status='pending', source_authenticity='not_independently_verified',
                  source_sha256=payload['source_file_sha256'], batch_sha256=digest(payload),
                  source_version=payload['source_version'], source_url=url,
                  parser_version=PARSER_VERSION, mapping_version=MAPPING_VERSION)
    if mode=='dry-run':
        return report  # Never opens/creates a database, even when a path was supplied.
    if database is None or str(database)==':memory:':
        raise ValueError('Explicit dedicated staging database path required')
    target = Path(database)
    if target.suffix.lower() not in ('.sqlite','.sqlite3','.db') or target.is_symlink():
        raise ValueError('Expected dedicated non-symlink SQLite path')
    existed = target.exists()
    if existed and not confirm_existing:
        raise ValueError('Existing database requires confirm_existing')
    if existed:
        with target.open('rb') as stream:
            if stream.read(16)!=b'SQLite format 3\x00':
                raise ValueError('Existing non-SQLite target refused')
    else:
        # Exclusive creation avoids silently opening another file created in between.
        with target.open('xb'):
            pass
    connection = None
    try:
        connection = _connect(target)
        connection.execute('BEGIN IMMEDIATE')
        if existed:
            _validate_database(connection)
        else:
            for statement in DDL:
                connection.execute(statement)
            connection.execute(f'PRAGMA application_id={APPLICATION_ID}')
            connection.execute('PRAGMA user_version=1')
        release_key = digest([url,payload['source_version']])
        release = (release_key,url,payload['source_version'],payload['source_file_sha256'],PARSER_VERSION,MAPPING_VERSION)
        old = connection.execute('SELECT * FROM staging_releases WHERE release_key=?',(release_key,)).fetchone()
        if old is not None and old != release:
            raise ValueError('Same source/version has conflicting hash or parser metadata')
        if old is None:
            connection.execute('INSERT INTO staging_releases VALUES (?,?,?,?,?,?)',release)
        for record, issues in zip(payload['records'],anomalies):
            text = canonical(record)
            row = (release_key,record['source_food_id'],record['source_locator'],text,digest(record),len(issues),'pending')
            old = connection.execute('SELECT * FROM staging_records WHERE release_key=? AND fdc_id=?',row[:2]).fetchone()
            if old is not None:
                if old != row:
                    raise ValueError('Existing FDC record conflicts; no overwrite permitted')
                report['skipped']+=1
            else:
                connection.execute('INSERT INTO staging_records VALUES (?,?,?,?,?,?,?)',row)
                report['inserted']+=1
        connection.commit()
        return report
    except BaseException:
        if connection is not None:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()
        if not existed and target.exists():
            # Delete only a failed, rolled-back newly created file (no schema persisted).
            check = _connect(target)
            try:
                empty = not _objects(check)
            finally:
                check.close()
            if empty:
                target.unlink()
