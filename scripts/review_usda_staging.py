"""Read a dedicated USDA staging database and write an exclusive review report."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nutrition.importers.usda_sqlite import APPLICATION_ID, DDL
from nutrition.review.policy import DEFAULT_POLICY, canonical, evaluate_food_record, policy_from_dict
from nutrition.sources.usda_foundation import MAPPING_VERSION, PARSER_VERSION


def _objects(connection):
    return connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()


def _reference_objects():
    connection = sqlite3.connect(":memory:")
    try:
        for statement in DDL:
            connection.execute(statement)
        return _objects(connection)
    finally:
        connection.close()


def open_staging_readonly(path):
    target = Path(path)
    if target.is_symlink() or not target.is_file():
        raise ValueError("Staging database must be a regular non-symlink file")
    connection = sqlite3.connect(target.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 1:
            raise RuntimeError("SQLite read-only enforcement unavailable")
        if connection.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
            raise ValueError("Database is not USDA staging v1")
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1 or _objects(connection) != _reference_objects():
            raise ValueError("Unexpected USDA staging schema")
        if connection.execute("PRAGMA foreign_key_check").fetchone() or connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Invalid USDA staging database")
        return connection
    except BaseException:
        connection.close()
        raise


def review_database(path, policy=DEFAULT_POLICY, provenance_evidence=None):
    evidence = provenance_evidence or {}
    if not isinstance(evidence, dict):
        raise ValueError("Provenance evidence must be an object")
    connection = open_staging_readonly(path)
    try:
        release_rows = connection.execute(
            "SELECT release_key,source_url,source_version,source_sha256,parser_version,mapping_version FROM staging_releases ORDER BY release_key"
        ).fetchall()
        releases = {row[0]: row[1:] for row in release_rows}
        if set(evidence) - set(releases):
            raise ValueError("Provenance evidence references an unknown release")
        duplicate_counts = dict(connection.execute(
            "SELECT fdc_id,count(*) FROM staging_records GROUP BY fdc_id"
        ).fetchall())
        results = []
        for release_key, fdc_id, locator, record_json, record_sha256, anomaly_count, status in connection.execute(
                "SELECT release_key,fdc_id,source_locator,record_json,record_sha256,anomaly_count,review_status "
                "FROM staging_records ORDER BY release_key,fdc_id"):
            release = releases.get(release_key)
            if release is None:
                raise ValueError("Staging record has no release")
            source_url, version, source_sha, parser_version, mapping_version = release
            try:
                record = json.loads(record_json)
            except (TypeError, ValueError) as error:
                raise ValueError("Invalid staging record JSON") from error
            if hashlib.sha256(canonical(record).encode("utf-8")).hexdigest() != record_sha256:
                raise ValueError("Staging record digest mismatch")
            context = {"official_file_verified": False,
                       "verified_source_file_sha256": None, "verified_source_version": None,
                       "verified_source_url": None, "verification_reference": None,
                       "duplicate_fdc_count": duplicate_counts.get(fdc_id, 0)}
            specific = evidence.get(release_key, {})
            if not isinstance(specific, dict):
                raise ValueError("Release provenance evidence must be an object")
            allowed_evidence = {"official_file_verified", "verified_source_file_sha256",
                                "verified_source_version", "verified_source_url", "verification_reference"}
            if set(specific) - allowed_evidence:
                raise ValueError("Unknown or unsafe provenance evidence field")
            context.update({key: specific[key] for key in allowed_evidence if key in specific})
            result = evaluate_food_record(record, context, policy)
            if (result["evaluated_record_sha256"] != record_sha256 or fdc_id != result["source_food_id"] or
                    locator != result["source_locator"] or status != "pending" or version != result["source_version"] or
                    source_sha != result["source_file_sha256"] or parser_version != PARSER_VERSION or
                    mapping_version != MAPPING_VERSION):
                raise ValueError("Staging row metadata or digest mismatch")
            results.append(result)
        counts = Counter(item["decision"] for item in results)
        return {"policy_version": policy.version, "review_status": "pending",
                "database_mode": "read_only", "total": len(results),
                "decision_counts": {name: counts.get(name, 0) for name in
                                    ("ready_for_manual_review", "needs_review", "rejected_by_policy")},
                "results": results}
    finally:
        connection.close()


def write_report(report, output_dir):
    output = Path(output_dir)
    if output.exists() or output.is_symlink() or not output.parent.is_dir():
        raise FileExistsError("Report directory must be new with an existing parent")
    output.mkdir()
    try:
        with (output / "review.json").open("x", encoding="utf-8") as target:
            json.dump(report, target, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            target.write("\n")
        counts = report["decision_counts"]
        summary = (f"审核记录：{report['total']}；可交人工审核：{counts['ready_for_manual_review']}；"
                   f"需要复核：{counts['needs_review']}；策略拒绝：{counts['rejected_by_policy']}。\n"
                   "全部结果仍为 pending；规则建议不等于 approved，也不会进入推荐数据库。\n")
        with (output / "summary.txt").open("x", encoding="utf-8") as target:
            target.write(summary)
    except BaseException:
        shutil.rmtree(output)
        raise


def _load_json(path, label):
    if path is None:
        return None
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > 1024 * 1024:
        raise ValueError(f"Invalid {label} JSON file")
    with source.open(encoding="utf-8") as stream:
        return json.load(stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--policy")
    parser.add_argument("--provenance-evidence")
    args = parser.parse_args()
    try:
        policy_data = _load_json(args.policy, "policy")
        policy = policy_from_dict(policy_data) if policy_data is not None else DEFAULT_POLICY
        evidence = _load_json(args.provenance_evidence, "provenance evidence") or {}
        if not isinstance(evidence, dict):
            raise ValueError("Provenance evidence must be an object keyed by release_key")
        report = review_database(args.database, policy, evidence)
        write_report(report, args.output_dir)
    except (ValueError, OSError, sqlite3.Error) as error:
        parser.exit(2, json.dumps({"status": "rejected", "error_type": type(error).__name__,
                                  "message": str(error)}, ensure_ascii=False) + "\n")
    print(json.dumps({"status": "completed", "total": report["total"],
                      "decision_counts": report["decision_counts"],
                      "review_status": "pending"}, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
