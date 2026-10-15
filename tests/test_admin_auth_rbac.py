"""TEST_ONLY administrator authentication, RBAC and review integration tests."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from werkzeug.security import check_password_hash

from nutrition.auth.admin_auth import (APPLICATION_ID, IDENTITY_ASSURANCE, AuthenticationError,
    Principal, assign_role, authenticate, authorized_review_decision, bootstrap_admin,
    change_password, create_admin, disable_admin, initialize_auth_database, list_admins,
    open_auth_database, reset_password, verify_auth_database, verify_authorized_review_action)
from nutrition.auth.rbac import has_permission
from nutrition.importers.usda_sqlite import import_payload
from nutrition.review.workflow import history, initialize_review_database, record_decision
from nutrition.sources.usda_foundation import parse_snapshot

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "TEST_ONLY-Strong-Password-1"
NEW_PASSWORD = "TEST_ONLY-New-Password-2"


class AdminAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = self.root / "TEST_ONLY-auth.sqlite"
        initialize_auth_database(self.auth)
        self.admin_id = bootstrap_admin(self.auth, "TEST_ONLY_admin", PASSWORD,
                                        datetime(2026, 1, 1, tzinfo=timezone.utc))
        self.admin = authenticate(self.auth, "TEST_ONLY_admin", PASSWORD)

    def account(self, username="TEST_ONLY_reviewer", role="nutrition_reviewer"):
        account_id = create_admin(self.auth, self.admin, username, PASSWORD, role)
        return account_id, authenticate(self.auth, username, PASSWORD)

    def integration(self):
        raw = self.root / "TEST_ONLY-food.json"
        food = {"fdcId": 91, "description": "TEST_ONLY artificial food", "dataType": "Foundation",
                "foodNutrients": [{"nutrient": {"id": nutrient, "name": "TEST_ONLY", "unitName": unit},
                                   "amount": 0 if nutrient == 1003 else 1}
                                  for nutrient, unit in ((1003, "g"), (1004, "g"), (1005, "g"), (1008, "kcal"))],
                "foodPortions": []}
        raw.write_text(json.dumps({"FoundationFoods": [food]}), encoding="utf-8")
        payload = parse_snapshot(raw, source_version="TEST_ONLY_v1",
                                 source_url="https://fdc.nal.usda.gov/TEST_ONLY",
                                 nutrition_basis="per_100g", basis_evidence="TEST_ONLY", energy_id=1008)
        staging, review = self.root / "TEST_ONLY-staging.sqlite", self.root / "TEST_ONLY-review.sqlite"
        import_payload(payload, mode="staging", database=staging)
        initialize_review_database(review)
        with sqlite3.connect(staging) as connection:
            release = connection.execute("SELECT release_key FROM staging_releases").fetchone()[0]
        record = payload["records"][0]
        provenance = {release: {"official_file_verified": True,
            "verified_source_file_sha256": record["source_file_sha256"],
            "verified_source_version": record["source_version"],
            "verified_source_url": record["source_url"], "verification_reference": "TEST_ONLY"}}
        return staging, review, release, provenance

    def decision(self, principal, **changes):
        staging, review, release, provenance = self.integration()
        values = dict(release_key=release, fdc_id=91, operation_id="TEST_ONLY-op",
                      event_type="accept_candidate", review_comment="TEST_ONLY comment",
                      review_reason="TEST_ONLY reason", evidence_references=["TEST_ONLY evidence"],
                      provenance_evidence=provenance)
        values.update(changes)
        return authorized_review_decision(self.auth, principal, review, staging, **values), review

    def test_01_initialize_database(self):
        with open_auth_database(self.auth, readonly=True) as db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)

    def test_02_duplicate_initialize_refuses_overwrite(self):
        before = self.auth.read_bytes()
        with self.assertRaises(FileExistsError): initialize_auth_database(self.auth)
        self.assertEqual(before, self.auth.read_bytes())

    def test_03_bootstrap_creates_system_admin(self):
        self.assertEqual(self.admin.role, "system_admin")

    def test_04_second_bootstrap_rejected(self):
        with self.assertRaises(PermissionError): bootstrap_admin(self.auth, "TEST_ONLY_other", PASSWORD)

    def test_05_duplicate_username_rejected_case_insensitive(self):
        with self.assertRaises(sqlite3.IntegrityError):
            create_admin(self.auth, self.admin, "test_only_ADMIN", PASSWORD, "audit_viewer")

    def test_06_password_is_hashed_and_plaintext_absent(self):
        with sqlite3.connect(self.auth) as db:
            stored = db.execute("SELECT password_hash FROM admin_accounts WHERE account_id=?", (self.admin_id,)).fetchone()[0]
        self.assertNotEqual(stored, PASSWORD)
        self.assertTrue(check_password_hash(stored, PASSWORD))
        self.assertNotIn(PASSWORD.encode(), self.auth.read_bytes())

    def test_07_correct_password_authenticates(self):
        self.assertEqual(authenticate(self.auth, "TEST_ONLY_admin", PASSWORD).account_id, self.admin_id)

    def test_08_wrong_password_is_generic(self):
        with self.assertRaisesRegex(AuthenticationError, "Authentication failed"):
            authenticate(self.auth, "TEST_ONLY_admin", "wrong")

    def test_09_missing_account_is_generic(self):
        with self.assertRaisesRegex(AuthenticationError, "Authentication failed"):
            authenticate(self.auth, "TEST_ONLY_missing", "wrong")

    def test_10_disabled_account_rejected(self):
        account_id, _ = self.account()
        disable_admin(self.auth, self.admin, account_id)
        with self.assertRaises(AuthenticationError): authenticate(self.auth, "TEST_ONLY_reviewer", PASSWORD)

    def test_11_login_failure_is_audited_without_password(self):
        with self.assertRaises(AuthenticationError): authenticate(self.auth, "TEST_ONLY_admin", "wrong-secret")
        with sqlite3.connect(self.auth) as db:
            details = db.execute("SELECT details_json FROM security_events ORDER BY sequence DESC LIMIT 1").fetchone()[0]
        self.assertNotIn("wrong-secret", details)
        self.assertIn("credentials_or_account_state", details)

    def test_12_login_success_is_audited(self):
        authenticate(self.auth, "TEST_ONLY_admin", PASSWORD)
        with sqlite3.connect(self.auth) as db:
            self.assertEqual(db.execute("SELECT event_type FROM security_events ORDER BY sequence DESC LIMIT 1").fetchone()[0], "auth.success")

    def test_13_rate_limit_locks_after_five_failures(self):
        base = datetime(2026, 2, 1, tzinfo=timezone.utc)
        for index in range(5):
            with self.assertRaises(AuthenticationError): authenticate(self.auth, "TEST_ONLY_admin", "bad", base + timedelta(seconds=index))
        with self.assertRaises(AuthenticationError): authenticate(self.auth, "TEST_ONLY_admin", PASSWORD, base + timedelta(seconds=10))

    def test_14_unknown_role_denied(self):
        self.assertFalse(has_permission("invented", "review.decide"))

    def test_15_unknown_permission_denied(self):
        self.assertFalse(has_permission("system_admin", "invented.permission"))

    def test_16_unauthenticated_denied(self):
        with self.assertRaises(PermissionError):
            create_admin(self.auth, None, "TEST_ONLY_none", PASSWORD, "audit_viewer")

    def test_17_audit_viewer_is_read_only(self):
        _, viewer = self.account("TEST_ONLY_viewer", "audit_viewer")
        self.assertTrue(has_permission(viewer.role, "audit.read"))
        self.assertFalse(has_permission(viewer.role, "review.decide"))

    def test_18_reviewer_can_decide_but_not_manage(self):
        _, reviewer = self.account()
        self.assertTrue(has_permission(reviewer.role, "review.decide"))
        self.assertFalse(has_permission(reviewer.role, "admin.create"))

    def test_19_system_admin_permissions(self):
        self.assertTrue(has_permission(self.admin.role, "admin.create"))
        self.assertTrue(has_permission(self.admin.role, "review.decide"))

    def test_20_invalid_role_assignment_rejected(self):
        account_id, _ = self.account()
        with self.assertRaises(ValueError): assign_role(self.auth, self.admin, account_id, "owner")

    def test_21_client_role_forgery_rejected(self):
        _, reviewer = self.account()
        forged = Principal(reviewer.account_id, reviewer.username, "system_admin", reviewer.session_id, reviewer.session_token)
        with self.assertRaises(PermissionError): create_admin(self.auth, forged, "TEST_ONLY_x", PASSWORD, "audit_viewer")

    def test_22_last_system_admin_cannot_be_disabled(self):
        with self.assertRaises(ValueError): disable_admin(self.auth, self.admin, self.admin_id)

    def test_23_last_system_admin_cannot_lose_role(self):
        with self.assertRaises(ValueError): assign_role(self.auth, self.admin, self.admin_id, "audit_viewer")

    def test_24_password_change_invalidates_old_password(self):
        change_password(self.auth, self.admin, PASSWORD, NEW_PASSWORD)
        with self.assertRaises(AuthenticationError): authenticate(self.auth, "TEST_ONLY_admin", PASSWORD)
        self.assertEqual(authenticate(self.auth, "TEST_ONLY_admin", NEW_PASSWORD).account_id, self.admin_id)

    def test_25_authorized_password_reset(self):
        account_id, _ = self.account()
        reset_password(self.auth, self.admin, account_id, NEW_PASSWORD)
        self.assertEqual(authenticate(self.auth, "TEST_ONLY_reviewer", NEW_PASSWORD).account_id, account_id)

    def test_26_unauthorized_password_reset(self):
        account_id, reviewer = self.account()
        with self.assertRaises(PermissionError): reset_password(self.auth, reviewer, account_id, NEW_PASSWORD)

    def test_27_list_admins_is_redacted(self):
        listing = json.dumps(list_admins(self.auth, self.admin))
        self.assertNotIn("password", listing.lower())
        self.assertNotIn(PASSWORD, listing)

    def test_28_authenticated_review_decision_is_linked(self):
        _, reviewer = self.account()
        result, review = self.decision(reviewer)
        self.assertEqual(result["identity_assurance"], IDENTITY_ASSURANCE)
        self.assertEqual(result["legacy_event_identity_assurance"], "self_asserted_cli")
        self.assertTrue(verify_authorized_review_action(self.auth, review, result["event_id"])["valid"])

    def test_29_audit_viewer_cannot_decide(self):
        _, viewer = self.account("TEST_ONLY_viewer", "audit_viewer")
        with self.assertRaises(PermissionError): self.decision(viewer)

    def test_30_disabled_account_cannot_decide(self):
        account_id, reviewer = self.account()
        disable_admin(self.auth, self.admin, account_id)
        with self.assertRaises(PermissionError): self.decision(reviewer)

    def test_31_revoked_role_cannot_decide(self):
        account_id, reviewer = self.account()
        assign_role(self.auth, self.admin, account_id, "audit_viewer")
        with self.assertRaises(PermissionError): self.decision(reviewer)

    def test_32_direct_legacy_call_is_not_authorized(self):
        staging, review, release, provenance = self.integration()
        result = record_decision(review, staging, release_key=release, fdc_id=91,
            operation_id="TEST_ONLY-forged", event_type="accept_candidate", reviewer_id=self.admin_id,
            review_comment="TEST_ONLY", review_reason="TEST_ONLY", provenance_evidence=provenance)
        self.assertEqual(result["reviewer_identity_assurance"], "self_asserted_cli")
        self.assertFalse(verify_authorized_review_action(self.auth, review, result["event_id"])["valid"])

    def test_33_authorized_event_retains_legacy_hash_chain(self):
        _, reviewer = self.account()
        result, review = self.decision(reviewer)
        with sqlite3.connect(review) as connection:
            release = connection.execute("SELECT release_key FROM review_events").fetchone()[0]
        event = history(review, release, 91)[0]
        self.assertEqual(event["event_hash"], result["event_hash"])
        self.assertEqual(event["reviewer_identity_assurance"], "self_asserted_cli")

    def test_34_authorized_operation_is_idempotent(self):
        _, reviewer = self.account()
        staging, review, release, provenance = self.integration()
        kwargs = dict(release_key=release, fdc_id=91, operation_id="TEST_ONLY-op",
                      event_type="accept_candidate", review_comment="TEST_ONLY",
                      review_reason="TEST_ONLY", provenance_evidence=provenance)
        first = authorized_review_decision(self.auth, reviewer, review, staging, **kwargs)
        second = authorized_review_decision(self.auth, reviewer, review, staging, **kwargs)
        self.assertEqual(first["event_id"], second["event_id"])

    def test_35_security_audit_verifies(self):
        self.assertTrue(verify_auth_database(self.auth)["valid"])

    def test_36_security_audit_tampering_detected(self):
        with sqlite3.connect(self.auth) as db:
            db.execute("DROP TRIGGER security_events_no_update")
            db.execute("UPDATE security_events SET event_hash=? WHERE sequence=1", ("0" * 64,))
            db.execute("CREATE TRIGGER security_events_no_update BEFORE UPDATE ON security_events BEGIN SELECT RAISE(ABORT,'security events are append-only'); END")
        self.assertFalse(verify_auth_database(self.auth)["valid"])

    def test_37_transaction_rolls_back_duplicate_create(self):
        before = verify_auth_database(self.auth)["security_event_count"]
        with self.assertRaises(sqlite3.IntegrityError):
            create_admin(self.auth, self.admin, "TEST_ONLY_admin", PASSWORD, "audit_viewer")
        self.assertEqual(verify_auth_database(self.auth)["security_event_count"], before)

    def test_38_business_database_rejected(self):
        wrong = self.root / "wrong.sqlite"
        sqlite3.connect(wrong).close()
        with self.assertRaises(ValueError): open_auth_database(wrong)

    def test_39_schema_version_rejected(self):
        with sqlite3.connect(self.auth) as db: db.execute("PRAGMA user_version=2")
        with self.assertRaises(ValueError): open_auth_database(self.auth)

    def test_40_cli_success_and_failure_do_not_echo_password(self):
        cli, target = ROOT / "scripts/manage_admin_auth.py", self.root / "cli.sqlite"
        success = subprocess.run([sys.executable, str(cli), "init", "--auth-db", str(target)],
                                 capture_output=True, text=True)
        self.assertEqual(success.returncode, 0, success.stderr)
        failure = subprocess.run([sys.executable, str(cli), "authenticate", "--auth-db", str(target),
                                  "--username", "TEST_ONLY_none"], input="TEST_ONLY-secret\n",
                                 capture_output=True, text=True)
        self.assertEqual(failure.returncode, 2)
        self.assertNotIn("TEST_ONLY-secret", failure.stdout + failure.stderr)

    def test_41_password_method_and_parameters_are_recorded(self):
        with sqlite3.connect(self.auth) as db:
            method, stored = db.execute(
                "SELECT password_method,password_hash FROM admin_accounts WHERE account_id=?", (self.admin_id,)).fetchone()
        self.assertEqual(method, "pbkdf2:sha256:260000")
        self.assertTrue(stored.startswith(method + "$"))

    def test_42_parameterized_username_lookup_resists_sql_injection(self):
        with self.assertRaises(ValueError): authenticate(self.auth, "x' OR 1=1 --", PASSWORD)

    def test_43_authorized_review_cli_success(self):
        _, reviewer = self.account()
        staging, review, release, provenance = self.integration()
        evidence = self.root / "evidence.json"
        evidence.write_text(json.dumps(provenance), encoding="utf-8")
        cli = ROOT / "scripts/authorized_nutrition_review.py"
        command = [sys.executable, str(cli), "--auth-db", str(self.auth), "--review-db", str(review),
                   "--staging-db", str(staging), "--username", reviewer.username,
                   "--release-key", release, "--fdc-id", "91", "--operation-id", "TEST_ONLY-cli-op",
                   "--decision", "accept_candidate", "--comment", "TEST_ONLY", "--reason", "TEST_ONLY",
                   "--provenance-evidence", str(evidence)]
        result = subprocess.run(command, input=PASSWORD + "\n", capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(output["identity_assurance"], IDENTITY_ASSURANCE)
        self.assertNotIn(PASSWORD, result.stdout + result.stderr)

    def test_44_forged_matching_admin_cannot_create_admin(self):
        forged = Principal(self.admin.account_id, self.admin.username, self.admin.role,
                           "forged-session", "forged-token")
        with self.assertRaises(PermissionError):
            create_admin(self.auth, forged, "TEST_ONLY_created", PASSWORD, "audit_viewer")

    def test_45_forged_matching_admin_cannot_reset_password(self):
        target_id, _ = self.account()
        forged = Principal(self.admin.account_id, self.admin.username, self.admin.role,
                           "forged-session", "forged-token")
        with self.assertRaises(PermissionError):
            reset_password(self.auth, forged, target_id, NEW_PASSWORD)

    def test_46_forged_matching_admin_cannot_authorize_review(self):
        forged = Principal(self.admin.account_id, self.admin.username, self.admin.role,
                           "forged-session", "forged-token")
        with self.assertRaises(PermissionError): self.decision(forged)
        with sqlite3.connect(self.auth) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM authorized_review_actions").fetchone()[0], 0)

    def test_47_forged_matching_reviewer_cannot_authorize_review(self):
        _, reviewer = self.account()
        forged = Principal(reviewer.account_id, reviewer.username, reviewer.role,
                           "forged-session", "forged-token")
        with self.assertRaises(PermissionError): self.decision(forged)
        with sqlite3.connect(self.auth) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM authorized_review_actions").fetchone()[0], 0)

    def test_48_password_change_revokes_existing_session(self):
        old = self.admin
        change_password(self.auth, old, PASSWORD, NEW_PASSWORD)
        with self.assertRaises(PermissionError):
            create_admin(self.auth, old, "TEST_ONLY_after_change", PASSWORD, "audit_viewer")

    def test_49_cross_database_failure_never_creates_authorization(self):
        _, reviewer = self.account()
        staging, review, release, provenance = self.integration()
        with self.assertRaises(ValueError):
            authorized_review_decision(self.auth, reviewer, review, staging,
                release_key=release, fdc_id=91, operation_id="TEST_ONLY-failure",
                event_type="accept_candidate", review_comment="TEST_ONLY", review_reason="TEST_ONLY",
                provenance_evidence=provenance,
                authorization_failure_hook=lambda: (_ for _ in ()).throw(ValueError("TEST_ONLY")))
        with sqlite3.connect(self.auth) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM authorized_review_actions").fetchone()[0], 0)
        with sqlite3.connect(review) as db:
            event_id = db.execute("SELECT event_id FROM review_events").fetchone()[0]
        self.assertFalse(verify_authorized_review_action(self.auth, review, event_id)["valid"])

    def test_50_session_token_is_not_stored_in_plaintext(self):
        self.assertNotIn(self.admin.session_token.encode(), self.auth.read_bytes())
        self.assertNotIn(self.admin.session_token, repr(self.admin))

    def test_51_expired_session_cannot_authorize(self):
        with sqlite3.connect(self.auth) as db:
            db.execute("UPDATE auth_sessions SET expires_at_utc='2000-01-01T00:00:00.000000Z' WHERE session_id=?",
                       (self.admin.session_id,))
        with self.assertRaises(PermissionError):
            create_admin(self.auth, self.admin, "TEST_ONLY_expired", PASSWORD, "audit_viewer")


if __name__ == "__main__":
    unittest.main()
