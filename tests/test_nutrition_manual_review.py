"""TEST_ONLY manual-review workflow tests; no real USDA or authenticated admins."""
from datetime import datetime, timezone
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from nutrition.importers.usda_sqlite import digest, import_payload
from nutrition.review.policy import ReviewPolicy
from nutrition.review.workflow import (APPLICATION_ID, DDL, evaluate_staging_subject, history,
                                        initialize_review_database, issue_key,
                                        open_review_database, record_decision, summary,
                                        verify_review_database)
from nutrition.sources.usda_foundation import parse_snapshot

ROOT = Path(__file__).resolve().parents[1]


class ManualReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "TEST_ONLY.json"
        self.staging = self.root / "TEST_ONLY-staging.sqlite"
        self.review = self.root / "TEST_ONLY-review.sqlite"
        nutrients = ((1003, "g"), (1004, "g"), (1005, "g"), (1008, "kcal"))
        self.food = {"fdcId": 41, "description": "TEST_ONLY artificial food", "dataType": "Foundation",
                     "foodNutrients": [{"nutrient": {"id": nid, "name": "TEST_ONLY", "unitName": unit},
                                        "amount": 0 if nid == 1003 else 1} for nid, unit in nutrients],
                     "foodPortions": []}
        self.payload = self.parse()
        import_payload(self.payload, mode="staging", database=self.staging)
        initialize_review_database(self.review)
        with sqlite3.connect(self.staging) as connection:
            self.release_key = connection.execute("SELECT release_key FROM staging_releases").fetchone()[0]
        record = self.payload["records"][0]
        self.provenance = {self.release_key: {
            "official_file_verified": True,
            "verified_source_file_sha256": record["source_file_sha256"],
            "verified_source_version": record["source_version"],
            "verified_source_url": record["source_url"],
            "verification_reference": "TEST_ONLY external evidence declaration"}}

    def parse(self, food=None, version="TEST_ONLY_v1"):
        self.raw.write_text(json.dumps({"FoundationFoods": [food or self.food]}), encoding="utf-8")
        return parse_snapshot(self.raw, source_version=version,
                              source_url="https://fdc.nal.usda.gov/TEST_ONLY",
                              nutrition_basis="per_100g", basis_evidence="TEST_ONLY basis",
                              energy_id=1008)

    def decide(self, **changes):
        values = dict(review_path=self.review, staging_path=self.staging,
                      release_key=self.release_key, fdc_id=41,
                      operation_id="TEST_ONLY-operation-1", event_type="accept_candidate",
                      reviewer_id="TEST_ONLY-reviewer", review_comment="TEST_ONLY comment",
                      review_reason="TEST_ONLY reason", evidence_references=["TEST_ONLY evidence"],
                      provenance_evidence=self.provenance,
                      decided_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
        values.update(changes)
        return record_decision(**values)

    def test_initialize_database(self):
        with open_review_database(self.review, readonly=True) as connection:
            self.assertEqual(connection.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM review_events").fetchone()[0], 0)

    def test_existing_target_refused_without_overwrite(self):
        before = self.review.read_bytes()
        with self.assertRaises(FileExistsError):
            initialize_review_database(self.review)
        self.assertEqual(before, self.review.read_bytes())

    def test_staging_lookup_and_policy_reuse(self):
        result = evaluate_staging_subject(self.staging, self.release_key, 41,
                                          provenance_evidence=self.provenance)
        self.assertEqual(result["evaluation"]["decision"], "ready_for_manual_review")
        self.assertEqual(result["evaluation"]["policy_version"], "nutrition-review-v1")
        self.assertEqual(result["official_source_authentication"], "not_performed_by_workflow")

    def test_normal_decision_is_candidate_only(self):
        result = self.decide()
        self.assertEqual(result["effective_decision"], "accept_candidate")
        self.assertFalse(result["formal_approval_created"])
        self.assertEqual(result["review_status"], "pending")
        self.assertEqual(summary(self.review)["formal_approved_foods"], 0)

    def test_invalid_decision_type(self):
        with self.assertRaises(ValueError):
            self.decide(event_type="approved")

    def test_supersede_requires_previous_decision(self):
        with self.assertRaises(ValueError):
            self.decide(event_type="supersede_decision", replacement_decision="reject_candidate")

    def test_existing_decision_requires_supersede(self):
        self.decide()
        with self.assertRaises(ValueError):
            self.decide(operation_id="TEST_ONLY-operation-2", event_type="reject_candidate")

    def test_supersede_appends_and_links_history(self):
        first = self.decide()
        second = self.decide(operation_id="TEST_ONLY-operation-2", event_type="supersede_decision",
                             replacement_decision="request_changes")
        events = history(self.review, self.release_key, 41)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[1]["previous_event_id"], first["event_id"])
        self.assertEqual(events[1]["supersedes_event_id"], first["event_id"])
        self.assertEqual(second["effective_decision"], "request_changes")

    def test_missing_reviewer(self):
        with self.assertRaises(ValueError):
            self.decide(reviewer_id="")

    def test_reviewer_is_self_asserted_not_authenticated(self):
        result = self.decide(reviewer_id="admin")
        self.assertEqual(result["reviewer_identity_assurance"], "self_asserted_cli")
        self.assertNotIn("authenticated", json.dumps(result))

    def test_missing_comment_and_reason(self):
        for field in ("review_comment", "review_reason"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.decide(**{field: ""})

    def test_record_hash_mismatch_refused(self):
        with sqlite3.connect(self.staging) as connection:
            connection.execute("UPDATE staging_records SET record_json='{}'")
        with self.assertRaises(ValueError):
            self.decide()

    def test_wrong_release_or_food_refused(self):
        for changes in ({"release_key": "missing"}, {"fdc_id": 999}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.decide(**changes)

    def test_policy_version_change_requires_explicit_supersede(self):
        self.decide()
        changed = ReviewPolicy(version="TEST_ONLY-policy-v2")
        with self.assertRaises(ValueError):
            self.decide(operation_id="TEST_ONLY-operation-2", policy=changed,
                        event_type="reject_candidate")
        self.decide(operation_id="TEST_ONLY-operation-3", policy=changed,
                    event_type="supersede_decision", replacement_decision="reject_candidate")
        self.assertEqual(summary(self.review)["current_decisions"]["reject_candidate"], 1)

    def test_changed_record_requires_explicit_supersede(self):
        first = self.decide()
        changed_food = copy.deepcopy(self.food)
        changed_food["description"] = "TEST_ONLY changed artificial food"
        changed = self.parse(changed_food)
        changed_record = changed["records"][0]
        with sqlite3.connect(self.staging) as connection:
            connection.execute("UPDATE staging_releases SET source_sha256=? WHERE release_key=?",
                               (changed["source_file_sha256"], self.release_key))
            connection.execute(
                "UPDATE staging_records SET record_json=?,record_sha256=? WHERE release_key=? AND fdc_id=?",
                (json.dumps(changed_record, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                 digest(changed_record), self.release_key, 41))
        with self.assertRaises(ValueError):
            self.decide(operation_id="TEST_ONLY-operation-2", event_type="request_changes",
                        provenance_evidence=None)
        second = self.decide(operation_id="TEST_ONLY-operation-3", event_type="supersede_decision",
                             replacement_decision="request_changes", provenance_evidence=None)
        events = history(self.review, self.release_key, 41)
        self.assertNotEqual(events[0]["record_sha256"], events[1]["record_sha256"])
        self.assertEqual(events[1]["supersedes_event_id"], first["event_id"])
        self.assertEqual(second["effective_decision"], "request_changes")

    def test_policy_rejected_cannot_accept(self):
        food = copy.deepcopy(self.food)
        food["foodNutrients"][0]["nutrient"]["unitName"] = "TEST_ONLY_UNKNOWN"
        payload = self.parse(food)
        other = self.root / "rejected.sqlite"
        import_payload(payload, mode="staging", database=other)
        with sqlite3.connect(other) as connection:
            key = connection.execute("SELECT release_key FROM staging_releases").fetchone()[0]
        strict = ReviewPolicy(unknown_unit_level="rejected_by_policy")
        with self.assertRaises(ValueError):
            self.decide(staging_path=other, release_key=key, policy=strict,
                        provenance_evidence=None)

    def test_policy_rejected_can_be_recorded_as_rejection(self):
        food = copy.deepcopy(self.food)
        food["foodNutrients"][0]["nutrient"]["unitName"] = "TEST_ONLY_UNKNOWN"
        payload = self.parse(food)
        other = self.root / "rejected.sqlite"
        import_payload(payload, mode="staging", database=other)
        with sqlite3.connect(other) as connection:
            key = connection.execute("SELECT release_key FROM staging_releases").fetchone()[0]
        strict = ReviewPolicy(unknown_unit_level="rejected_by_policy")
        result = self.decide(staging_path=other, release_key=key, policy=strict,
                             provenance_evidence=None, event_type="reject_candidate")
        self.assertEqual(result["status"], "recorded")

    def test_needs_review_acceptance_requires_all_resolutions(self):
        subject = evaluate_staging_subject(self.staging, self.release_key, 41)
        self.assertEqual(subject["evaluation"]["decision"], "needs_review")
        with self.assertRaises(ValueError):
            self.decide(provenance_evidence=None)
        resolutions = {key: "TEST_ONLY manually investigated" for key in subject["required_issue_resolution_keys"]}
        result = self.decide(provenance_evidence=None, issue_resolutions=resolutions)
        self.assertEqual(result["effective_decision"], "accept_candidate")

    def test_unverified_source_never_claims_authentication(self):
        subject = evaluate_staging_subject(self.staging, self.release_key, 41)
        self.assertEqual(subject["official_source_authentication"], "not_performed_by_workflow")
        self.assertIn("SRC007", [x["rule_id"] for x in subject["evaluation"]["issues"]])

    def test_idempotent_replay(self):
        first = self.decide()
        second = self.decide()
        self.assertEqual(second["status"], "idempotent_replay")
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(history(self.review, self.release_key, 41)), 1)

    def test_operation_id_conflict(self):
        self.decide()
        with self.assertRaises(ValueError):
            self.decide(review_comment="TEST_ONLY changed")

    def test_hash_chain_and_history_verification(self):
        self.decide()
        self.decide(operation_id="TEST_ONLY-operation-2", event_type="supersede_decision",
                    replacement_decision="reject_candidate")
        verification = verify_review_database(self.review)
        self.assertTrue(verification["valid"])
        self.assertEqual(verification["event_count"], 2)
        self.assertEqual(verification["tamper_resistance_limit"], "no_external_signature_or_trusted_anchor")

    def test_event_updates_and_deletes_are_blocked(self):
        self.decide()
        with sqlite3.connect(self.review) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("UPDATE review_events SET review_comment='changed'")
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM review_events")

    def test_event_digest_tampering_is_detected(self):
        self.decide()
        with sqlite3.connect(self.review) as connection:
            connection.execute("DROP TRIGGER review_events_no_update")
            connection.execute("UPDATE review_events SET event_hash=?", ("0" * 64,))
            connection.execute(next(statement for statement in DDL
                                    if statement.startswith("CREATE TRIGGER review_events_no_update")))
        verification = verify_review_database(self.review)
        self.assertFalse(verification["valid"])
        self.assertTrue(any("event hash mismatch" in error for error in verification["errors"]))

    def test_projection_tamper_detected(self):
        self.decide()
        with sqlite3.connect(self.review) as connection:
            connection.execute("UPDATE current_decisions SET effective_decision='reject_candidate'")
        self.assertFalse(verify_review_database(self.review)["valid"])
        with self.assertRaises(ValueError):
            summary(self.review)
        with self.assertRaises(ValueError):
            self.decide(operation_id="TEST_ONLY-operation-2", event_type="supersede_decision",
                        replacement_decision="reject_candidate")

    def test_write_failure_rolls_back_event_and_projection(self):
        def fail():
            raise sqlite3.IntegrityError("TEST_ONLY injected failure")
        with self.assertRaises(sqlite3.IntegrityError):
            self.decide(failure_hook=fail)
        self.assertEqual(summary(self.review)["event_count"], 0)
        self.assertEqual(summary(self.review)["current_candidates"], 0)

    def test_staging_database_remains_readonly(self):
        before = self.staging.read_bytes()
        self.decide()
        self.assertEqual(before, self.staging.read_bytes())

    def test_formal_business_database_refused_as_staging(self):
        spec = importlib.util.spec_from_file_location("initdb", ROOT / "scripts/init_nutrition_db.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        business = self.root / "business.sqlite"
        module.initialize_database(business).close()
        with self.assertRaises(ValueError):
            self.decide(staging_path=business)

    def test_different_releases_are_isolated(self):
        second = self.parse(version="TEST_ONLY_v2")
        import_payload(second, mode="staging", database=self.staging, confirm_existing=True)
        with sqlite3.connect(self.staging) as connection:
            keys = [row[0] for row in connection.execute("SELECT release_key FROM staging_releases ORDER BY source_version")]
        self.decide()
        second_record = second["records"][0]
        evidence = {keys[1]: {"official_file_verified": True,
                              "verified_source_file_sha256": second_record["source_file_sha256"],
                              "verified_source_version": second_record["source_version"],
                              "verified_source_url": second_record["source_url"],
                              "verification_reference": "TEST_ONLY"}}
        self.decide(release_key=keys[1], operation_id="TEST_ONLY-operation-2",
                    provenance_evidence=evidence)
        self.assertEqual(summary(self.review)["current_candidates"], 2)

    def test_review_database_has_no_production_food_tables(self):
        with sqlite3.connect(self.review) as connection:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(names, {"review_events", "current_decisions"})

    def test_schema_version_and_shape_refused(self):
        with sqlite3.connect(self.review) as connection:
            connection.execute("PRAGMA user_version=2")
        with self.assertRaises(ValueError):
            summary(self.review)

    def test_input_objects_and_raw_file_unchanged(self):
        provenance = copy.deepcopy(self.provenance)
        raw = self.raw.read_bytes()
        self.decide(provenance_evidence=provenance)
        self.assertEqual(provenance, self.provenance)
        self.assertEqual(raw, self.raw.read_bytes())

    def test_utc_timestamp_and_evidence_are_recorded(self):
        self.decide()
        event = history(self.review, self.release_key, 41)[0]
        self.assertEqual(event["decided_at_utc"], "2026-01-01T00:00:00.000000Z")
        self.assertEqual(json.loads(event["evidence_references_json"]), ["TEST_ONLY evidence"])
        self.assertEqual(event["reviewer_identity_assurance"], "self_asserted_cli")

    def test_cli_init_evaluate_decide_history_verify_summary_and_failure(self):
        cli = ROOT / "scripts/manage_nutrition_reviews.py"
        cli_db = self.root / "cli-review.sqlite"
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        def run(*args):
            return subprocess.run([sys.executable, str(cli), *map(str, args)],
                                  capture_output=True, text=True, env=environment)
        result = run("init", "--review-db", cli_db)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = run("evaluate", "--staging-db", self.staging, "--release-key", self.release_key,
                     "--fdc-id", 41)
        self.assertEqual(result.returncode, 0, result.stderr)
        subject = json.loads(result.stdout)
        resolutions = {key: "TEST_ONLY resolution" for key in subject["required_issue_resolution_keys"]}
        resolution_file = self.root / "resolutions.json"
        resolution_file.write_text(json.dumps(resolutions), encoding="utf-8")
        result = run("decide", "--review-db", cli_db, "--staging-db", self.staging,
                     "--release-key", self.release_key, "--fdc-id", 41,
                     "--operation-id", "TEST_ONLY-cli", "--decision", "accept_candidate",
                     "--reviewer-id", "admin", "--comment", "TEST_ONLY", "--reason", "TEST_ONLY",
                     "--issue-resolutions", resolution_file)
        self.assertEqual(result.returncode, 0, result.stderr)
        for command, extra in (("history", ["--release-key", self.release_key, "--fdc-id", "41"]),
                               ("verify", []), ("summary", [])):
            result = run(command, "--review-db", cli_db, *extra)
            self.assertEqual(result.returncode, 0, result.stderr)
        result = run("summary", "--review-db", self.staging)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
