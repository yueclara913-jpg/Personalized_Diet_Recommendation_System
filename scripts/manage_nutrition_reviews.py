"""Manage an isolated manual-review audit database; no authentication or approval."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nutrition.review.policy import DEFAULT_POLICY, policy_from_dict
from nutrition.review.workflow import (evaluate_staging_subject, history,
                                        initialize_review_database, record_decision,
                                        summary, verify_review_database)

MAX_JSON_BYTES = 1024 * 1024


def load_json(path, label, default=None):
    if path is None:
        return default
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"Invalid {label} JSON file")
    with source.open(encoding="utf-8") as stream:
        return json.load(stream)


def add_subject(parser, *, review_db=False):
    if review_db:
        parser.add_argument("--review-db", required=True)
    parser.add_argument("--staging-db", required=True)
    parser.add_argument("--release-key", required=True)
    parser.add_argument("--fdc-id", required=True, type=int)
    parser.add_argument("--policy")
    parser.add_argument("--provenance-evidence")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--review-db", required=True)
    evaluate = commands.add_parser("evaluate")
    add_subject(evaluate)
    decide = commands.add_parser("decide")
    add_subject(decide, review_db=True)
    decide.add_argument("--operation-id", required=True)
    decide.add_argument("--decision", required=True,
                        choices=("accept_candidate", "request_changes", "reject_candidate", "supersede_decision"))
    decide.add_argument("--replacement-decision",
                        choices=("accept_candidate", "request_changes", "reject_candidate"))
    decide.add_argument("--reviewer-id", required=True)
    decide.add_argument("--comment", required=True)
    decide.add_argument("--reason", required=True)
    decide.add_argument("--issue-resolutions")
    decide.add_argument("--evidence-references")
    hist = commands.add_parser("history")
    hist.add_argument("--review-db", required=True)
    hist.add_argument("--release-key", required=True)
    hist.add_argument("--fdc-id", required=True, type=int)
    verify = commands.add_parser("verify")
    verify.add_argument("--review-db", required=True)
    stats = commands.add_parser("summary")
    stats.add_argument("--review-db", required=True)
    args = parser.parse_args()
    try:
        if args.command == "init":
            initialize_review_database(args.review_db)
            result = {"status": "initialized", "review_database": "manual-review-v1"}
        elif args.command in ("evaluate", "decide"):
            policy_data = load_json(args.policy, "policy")
            policy = policy_from_dict(policy_data) if policy_data is not None else DEFAULT_POLICY
            provenance = load_json(args.provenance_evidence, "provenance evidence", {})
            if args.command == "evaluate":
                result = evaluate_staging_subject(args.staging_db, args.release_key, args.fdc_id,
                                                  policy=policy, provenance_evidence=provenance)
            else:
                resolutions = load_json(args.issue_resolutions, "issue resolutions", {})
                references = load_json(args.evidence_references, "evidence references", [])
                result = record_decision(
                    args.review_db, args.staging_db, release_key=args.release_key, fdc_id=args.fdc_id,
                    operation_id=args.operation_id, event_type=args.decision,
                    replacement_decision=args.replacement_decision, reviewer_id=args.reviewer_id,
                    review_comment=args.comment, review_reason=args.reason,
                    issue_resolutions=resolutions, evidence_references=references,
                    policy=policy, provenance_evidence=provenance)
        elif args.command == "history":
            result = {"history": history(args.review_db, args.release_key, args.fdc_id)}
        elif args.command == "verify":
            result = verify_review_database(args.review_db)
            if not result["valid"]:
                raise ValueError("Audit verification failed: " + "; ".join(result["errors"]))
        else:
            result = summary(args.review_db)
    except (ValueError, TypeError, OSError, sqlite3.Error) as error:
        parser.exit(2, json.dumps({"status": "rejected", "error_type": type(error).__name__,
                                  "message": str(error)}, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
