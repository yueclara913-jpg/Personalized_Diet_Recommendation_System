"""Submit a manual nutrition decision after administrator authentication and RBAC."""
import argparse
import getpass
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nutrition.auth.admin_auth import authenticate, authorized_review_decision
from nutrition.review.policy import DEFAULT_POLICY, policy_from_dict

MAX_JSON_BYTES = 1024 * 1024


def load_json(path, label, default):
    if path is None:
        return default
    source = Path(path)
    if source.is_symlink() or not source.is_file() or source.stat().st_size > MAX_JSON_BYTES:
        raise ValueError(f"Invalid {label} JSON file")
    with source.open(encoding="utf-8") as stream:
        return json.load(stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--auth-db", required=True)
    parser.add_argument("--review-db", required=True)
    parser.add_argument("--staging-db", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--release-key", required=True)
    parser.add_argument("--fdc-id", required=True, type=int)
    parser.add_argument("--operation-id", required=True)
    parser.add_argument("--decision", required=True,
                        choices=("accept_candidate", "request_changes", "reject_candidate", "supersede_decision"))
    parser.add_argument("--replacement-decision",
                        choices=("accept_candidate", "request_changes", "reject_candidate"))
    parser.add_argument("--comment", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument("--policy")
    parser.add_argument("--provenance-evidence")
    parser.add_argument("--issue-resolutions")
    parser.add_argument("--evidence-references")
    args = parser.parse_args()
    try:
        principal = authenticate(args.auth_db, args.username, getpass.getpass("Administrator password: "))
        policy_data = load_json(args.policy, "policy", None)
        policy = policy_from_dict(policy_data) if policy_data is not None else DEFAULT_POLICY
        result = authorized_review_decision(
            args.auth_db, principal, args.review_db, args.staging_db,
            release_key=args.release_key, fdc_id=args.fdc_id, operation_id=args.operation_id,
            event_type=args.decision, replacement_decision=args.replacement_decision,
            review_comment=args.comment, review_reason=args.reason,
            policy=policy,
            provenance_evidence=load_json(args.provenance_evidence, "provenance evidence", {}),
            issue_resolutions=load_json(args.issue_resolutions, "issue resolutions", {}),
            evidence_references=load_json(args.evidence_references, "evidence references", []))
    except (ValueError, TypeError, PermissionError, OSError, sqlite3.Error) as error:
        parser.exit(2, json.dumps({"status": "rejected", "error_type": type(error).__name__,
                                  "message": str(error)}, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
