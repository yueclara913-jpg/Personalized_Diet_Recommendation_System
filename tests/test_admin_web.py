"""TEST_ONLY administrator web console security and workflow tests."""
from datetime import datetime, timedelta, timezone
import html
import json
from pathlib import Path
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from admin_web import create_app
from admin_web import sessions
from admin_web import services
from nutrition.auth.admin_auth import (assign_role, authenticate, bootstrap_admin, change_password,
                                        create_admin, disable_admin, initialize_auth_database,
                                        verify_authorized_review_action)
from nutrition.importers.usda_sqlite import import_payload
from nutrition.review.workflow import canonical, evaluate_staging_subject, initialize_review_database, record_decision
from nutrition.sources.usda_foundation import parse_snapshot

PASSWORD = "TEST_ONLY-Strong-Password-1"
NEW_PASSWORD = "TEST_ONLY-New-Password-2"


class AdminWebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.auth = self.root / "auth.sqlite"
        self.web = self.root / "web.sqlite"
        self.staging = self.root / "staging.sqlite"
        self.review = self.root / "review.sqlite"
        initialize_auth_database(self.auth)
        self.admin_id = bootstrap_admin(self.auth, "TEST_ONLY_admin", PASSWORD)
        self.admin = authenticate(self.auth, "TEST_ONLY_admin", PASSWORD)
        self.reviewer_id = create_admin(self.auth, self.admin, "TEST_ONLY_reviewer", PASSWORD, "nutrition_reviewer")
        self.viewer_id = create_admin(self.auth, self.admin, "TEST_ONLY_viewer", PASSWORD, "audit_viewer")
        self.raw = self.root / "TEST_ONLY.json"
        nutrients = [(1003, "g"), (1004, "g"), (1005, "g"), (1008, "kcal"),
                     (2047, "kcal"), (1063, "g"), (2000, "g")]
        self.food = {"fdcId": 91, "description": "TEST_ONLY <script>alert(1)</script>",
                     "dataType": "Foundation",
                     "foodNutrients": [{"nutrient": {"id": nid, "name": "TEST_ONLY", "unitName": unit},
                                        "amount": 0 if nid == 1003 else 1} for nid, unit in nutrients],
                     "foodPortions": []}
        self.raw.write_text(json.dumps({"FoundationFoods": [self.food]}), encoding="utf-8")
        self.payload = parse_snapshot(self.raw, source_version="TEST_ONLY_v1",
            source_url="https://fdc.nal.usda.gov/TEST_ONLY", nutrition_basis="per_100g",
            basis_evidence="TEST_ONLY basis", energy_id=1008)
        import_payload(self.payload, mode="staging", database=self.staging)
        initialize_review_database(self.review)
        with sqlite3.connect(self.staging) as db:
            self.release = db.execute("SELECT release_key FROM staging_releases").fetchone()[0]
            self.record_sha = db.execute("SELECT record_sha256 FROM staging_records").fetchone()[0]
        self.app = create_app({"TESTING": True, "SECRET_KEY": "TEST_ONLY-web-secret",
            "ADMIN_AUTH_DB": str(self.auth), "ADMIN_WEB_SESSION_DB": str(self.web),
            "ADMIN_STAGING_DB": str(self.staging), "ADMIN_REVIEW_DB": str(self.review),
            "ADMIN_COOKIE_SECURE": False})
        self.client = self.app.test_client()

    def csrf_from(self, response):
        match = re.search(r'name="csrf_token" value="([^"]+)"', response.get_data(as_text=True))
        self.assertIsNotNone(match)
        return html.unescape(match.group(1))

    def login(self, username="TEST_ONLY_admin", password=PASSWORD, next_path=""):
        page = self.client.get("/admin/login")
        token = self.csrf_from(page)
        return self.client.post("/admin/login", data={"username": username, "password": password,
                                                       "csrf_token": token, "next": next_path})

    def csrf(self):
        return self.cookie("admin_csrf")

    def cookie(self, name):
        for cookie in self.client.cookie_jar:
            if cookie.name == name and cookie.path == "/admin":
                return cookie.value
        return ""

    def detail(self):
        return self.client.get(f"/admin/foods/{self.release}/91")

    def decision_data(self, decision="request_changes"):
        page = self.detail().get_data(as_text=True)
        operation = html.unescape(re.search(r'name="operation_id" value="([^"]+)"', page).group(1))
        return {"csrf_token": self.csrf(), "release_key": self.release, "fdc_id": "91",
                "record_sha256": self.record_sha, "operation_id": operation, "decision": decision,
                "comment": "TEST_ONLY comment", "reason": "TEST_ONLY reason",
                "current_password": PASSWORD}

    def test_01_login_page(self):
        response = self.client.get("/admin/login")
        self.assertEqual(response.status_code, 200)
        self.assertIn("食品营养数据管理平台", response.get_data(as_text=True))

    def test_02_login_success(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertIn("admin_session", response.headers.get("Set-Cookie", ""))

    def test_03_wrong_password(self):
        response = self.login(password="wrong")
        self.assertEqual(response.status_code, 401)
        self.assertNotIn("TEST_ONLY_admin", response.get_data(as_text=True))

    def test_04_missing_account_has_same_error(self):
        response = self.login(username="TEST_ONLY_missing", password="wrong")
        self.assertEqual(response.status_code, 401)
        self.assertIn("用户名或密码错误", response.get_data(as_text=True))

    def test_05_disabled_account_login_refused(self):
        disable_admin(self.auth, self.admin, self.reviewer_id)
        self.assertEqual(self.login("TEST_ONLY_reviewer").status_code, 401)

    def test_06_unlogged_dashboard_redirects(self):
        response = self.client.get("/admin/dashboard")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login", response.location)

    def test_07_unknown_regular_user_rejected(self):
        self.assertEqual(self.login("TEST_ONLY_regular", PASSWORD).status_code, 401)

    def test_08_dashboard_authenticated(self):
        self.login()
        response = self.client.get("/admin/dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn("暂存食品", response.get_data(as_text=True))

    def test_09_logout_revokes_session(self):
        self.login()
        response = self.client.post("/admin/logout", data={"csrf_token": self.csrf()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 302)

    def test_10_logout_requires_csrf(self):
        self.login()
        self.assertEqual(self.client.post("/admin/logout").status_code, 400)

    def test_11_login_requires_csrf(self):
        self.assertEqual(self.client.post("/admin/login", data={"username": "TEST_ONLY_admin", "password": PASSWORD}).status_code, 400)

    def test_12_login_rejects_wrong_csrf(self):
        self.client.get("/admin/login")
        self.assertEqual(self.client.post("/admin/login", data={"username": "TEST_ONLY_admin", "password": PASSWORD, "csrf_token": "wrong"}).status_code, 400)

    def test_13_session_fixation_resisted(self):
        self.client.set_cookie("admin_session", "attacker-fixed", path="/admin")
        self.login()
        self.assertNotEqual(self.cookie("admin_session"), "attacker-fixed")

    def test_14_absolute_expiry(self):
        self.login()
        with sqlite3.connect(self.web) as db: db.execute("UPDATE web_sessions SET expires_at_utc='2000-01-01T00:00:00.000000Z'")
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 302)

    def test_15_idle_expiry(self):
        self.login()
        with sqlite3.connect(self.web) as db: db.execute("UPDATE web_sessions SET last_seen_at_utc='2000-01-01T00:00:00.000000Z'")
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 302)

    def test_16_password_change_invalidates_web_session(self):
        self.login()
        change_password(self.auth, self.admin, PASSWORD, NEW_PASSWORD)
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 302)

    def test_17_role_change_invalidates_web_session(self):
        self.login("TEST_ONLY_reviewer")
        assign_role(self.auth, self.admin, self.reviewer_id, "audit_viewer")
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 302)

    def test_18_disabled_account_invalidates_web_session(self):
        self.login("TEST_ONLY_reviewer")
        disable_admin(self.auth, self.admin, self.reviewer_id)
        self.assertEqual(self.client.get("/admin/dashboard").status_code, 302)

    def test_19_audit_viewer_can_read(self):
        self.login("TEST_ONLY_viewer")
        self.assertEqual(self.client.get("/admin/foods").status_code, 200)

    def test_20_audit_viewer_cannot_decide(self):
        self.login("TEST_ONLY_viewer")
        self.assertEqual(self.client.post("/admin/reviews/decide", data={"csrf_token": self.csrf()}).status_code, 403)

    def test_21_reviewer_can_decide(self):
        self.login("TEST_ONLY_reviewer")
        response = self.client.post("/admin/reviews/decide", data=self.decision_data())
        self.assertEqual(response.status_code, 302)

    def test_22_system_admin_can_decide(self):
        self.login()
        self.assertEqual(self.client.post("/admin/reviews/decide", data=self.decision_data()).status_code, 302)

    def test_23_forged_reviewer_id_rejected(self):
        self.login()
        data = self.decision_data(); data["reviewer_id"] = self.viewer_id
        self.assertEqual(self.client.post("/admin/reviews/decide", data=data).status_code, 400)

    def test_24_forged_assurance_rejected(self):
        self.login()
        data = self.decision_data(); data["identity_assurance"] = "authenticated_admin_db_v1"
        self.assertEqual(self.client.post("/admin/reviews/decide", data=data).status_code, 400)

    def test_25_review_requires_csrf(self):
        self.login(); data = self.decision_data(); data.pop("csrf_token")
        self.assertEqual(self.client.post("/admin/reviews/decide", data=data).status_code, 400)

    def test_26_review_rejects_wrong_csrf(self):
        self.login(); data = self.decision_data(); data["csrf_token"] = "wrong"
        self.assertEqual(self.client.post("/admin/reviews/decide", data=data).status_code, 400)

    def test_27_record_digest_binding(self):
        self.login(); data = self.decision_data(); data["record_sha256"] = "0" * 64
        self.assertEqual(self.client.post("/admin/reviews/decide", data=data).status_code, 400)

    def test_28_wrong_reauthentication_password(self):
        self.login(); data = self.decision_data(); data["current_password"] = "wrong"
        self.assertEqual(self.client.post("/admin/reviews/decide", data=data).status_code, 403)

    def test_29_end_to_end_authorization_verifies(self):
        self.login("TEST_ONLY_reviewer")
        self.client.post("/admin/reviews/decide", data=self.decision_data())
        with sqlite3.connect(self.review) as db: event_id = db.execute("SELECT event_id FROM review_events").fetchone()[0]
        self.assertTrue(verify_authorized_review_action(self.auth, self.review, event_id)["valid"])

    def test_30_old_self_asserted_event_is_not_authorized(self):
        result = record_decision(self.review, self.staging, release_key=self.release, fdc_id=91,
            operation_id="TEST_ONLY-old", event_type="request_changes", reviewer_id="TEST_ONLY-old",
            review_comment="TEST_ONLY", review_reason="TEST_ONLY")
        self.assertFalse(verify_authorized_review_action(self.auth, self.review, result["event_id"])["valid"])

    def test_31_no_formal_approved_created(self):
        self.login(); self.client.post("/admin/reviews/decide", data=self.decision_data())
        with sqlite3.connect(self.review) as db:
            self.assertNotIn("approved", json.dumps(db.execute("SELECT * FROM current_decisions").fetchall()))

    def test_32_xss_food_name_escaped(self):
        self.login(); body = self.detail().get_data(as_text=True)
        self.assertNotIn("<script>alert(1)</script>", body)
        self.assertIn("&lt;script&gt;", body)

    def test_33_sql_injection_search_is_data(self):
        self.login(); response = self.client.get("/admin/foods?q=%25%27%20OR%201%3D1--")
        self.assertEqual(response.status_code, 200)
        with sqlite3.connect(self.staging) as db: self.assertEqual(db.execute("SELECT count(*) FROM staging_records").fetchone()[0], 1)

    def test_34_search_and_pagination(self):
        self.login(); response = self.client.get("/admin/foods?q=TEST_ONLY&page=1")
        self.assertIn("TEST_ONLY", response.get_data(as_text=True))

    def test_35_food_detail_and_conflicting_definitions(self):
        self.login(); body = self.detail().get_data(as_text=True)
        self.assertIn("1008", body); self.assertIn("2047", body); self.assertIn("1063", body); self.assertIn("2000", body)

    def test_36_review_history_display(self):
        self.login(); self.client.post("/admin/reviews/decide", data=self.decision_data())
        self.assertIn("request_changes", self.detail().get_data(as_text=True))

    def test_37_operation_id_idempotent(self):
        self.login(); data = self.decision_data()
        self.client.post("/admin/reviews/decide", data=data)
        self.client.post("/admin/reviews/decide", data=data)
        with sqlite3.connect(self.review) as db: self.assertEqual(db.execute("SELECT count(*) FROM review_events").fetchone()[0], 1)

    def test_38_unconfigured_data_source(self):
        other = create_app({"TESTING": True, "SECRET_KEY": "TEST_ONLY", "ADMIN_AUTH_DB": str(self.auth),
            "ADMIN_WEB_SESSION_DB": str(self.root / "other-web.sqlite"), "ADMIN_COOKIE_SECURE": False})
        client = other.test_client(); page = client.get("/admin/login"); token = self.csrf_from(page)
        client.post("/admin/login", data={"username": "TEST_ONLY_admin", "password": PASSWORD, "csrf_token": token})
        self.assertIn("未配置", client.get("/admin/dashboard").get_data(as_text=True))

    def test_39_review_database_unavailable_is_safe(self):
        self.login(); self.app.config["ADMIN_REVIEW_DB"] = str(self.root / "missing.sqlite")
        self.assertEqual(self.detail().status_code, 200)

    def test_40_cross_database_authorization_failure_safe(self):
        self.login(); data = self.decision_data()
        with patch("admin_web.routes.authorized_review_decision", side_effect=sqlite3.OperationalError("TEST_ONLY")):
            response = self.client.post("/admin/reviews/decide", data=data)
        self.assertEqual(response.status_code, 400)
        with sqlite3.connect(self.auth) as db: self.assertEqual(db.execute("SELECT count(*) FROM authorized_review_actions").fetchone()[0], 0)

    def test_41_open_redirect_blocked(self):
        response = self.login(next_path="https://evil.example/")
        self.assertNotIn("evil.example", response.location)

    def test_42_cookie_security_attributes(self):
        secure_app = create_app({"TESTING": True, "SECRET_KEY": "TEST_ONLY", "ADMIN_AUTH_DB": str(self.auth),
            "ADMIN_WEB_SESSION_DB": str(self.root / "secure-web.sqlite"), "ADMIN_STAGING_DB": str(self.staging),
            "ADMIN_REVIEW_DB": str(self.review), "ADMIN_COOKIE_SECURE": True})
        client = secure_app.test_client(); page = client.get("/admin/login"); token = self.csrf_from(page)
        response = client.post("/admin/login", data={"username": "TEST_ONLY_admin", "password": PASSWORD, "csrf_token": token})
        cookie = "\n".join(response.headers.getlist("Set-Cookie"))
        self.assertIn("Secure", cookie); self.assertIn("HttpOnly", cookie); self.assertIn("SameSite=Strict", cookie)

    def test_43_production_rejects_insecure_cookie(self):
        with self.assertRaises(RuntimeError): create_app({"SECRET_KEY": "TEST_ONLY", "ADMIN_AUTH_DB": str(self.auth),
            "ADMIN_WEB_SESSION_DB": str(self.root / "bad-web.sqlite"), "ADMIN_COOKIE_SECURE": False})

    def test_44_sensitive_responses_no_store(self):
        response = self.client.get("/admin/login")
        self.assertIn("no-store", response.headers["Cache-Control"])

    def test_45_password_not_in_response(self):
        response = self.login()
        self.assertNotIn(PASSWORD, response.get_data(as_text=True) + str(response.headers))

    def test_46_safe_error_hides_database_path(self):
        self.login(); data = self.decision_data(); self.app.config["ADMIN_REVIEW_DB"] = str(self.root / "secret-path.sqlite")
        response = self.client.post("/admin/reviews/decide", data=data)
        self.assertNotIn("secret-path.sqlite", response.get_data(as_text=True))

    def test_47_existing_user_home_remains_available(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Personalized Diet Recommendation System", response.get_data(as_text=True))

    def test_48_session_token_not_stored_plaintext(self):
        self.login(); token = self.cookie("admin_session")
        self.assertNotIn(token.encode(), self.web.read_bytes())

    def test_49_food_list_is_bounded(self):
        self.login(); response = self.client.get("/admin/foods?page=999999999")
        self.assertEqual(response.status_code, 200)

    def test_50_source_authenticity_warning(self):
        self.login(); self.assertIn("真实性尚未独立验证", self.detail().get_data(as_text=True))

    def test_51_evaluation_filter(self):
        self.login(); response = self.client.get("/admin/foods?evaluation=needs_review")
        self.assertIn("TEST_ONLY", response.get_data(as_text=True))
        response = self.client.get("/admin/foods?evaluation=ready_for_manual_review")
        self.assertIn("没有符合条件", response.get_data(as_text=True))

    def test_52_empty_staging_database(self):
        with sqlite3.connect(self.staging) as db: db.execute("DELETE FROM staging_records")
        self.login(); body = self.client.get("/admin/dashboard").get_data(as_text=True)
        self.assertIn(">0<", body)
        self.assertNotIn("数据量超过统计上限", body)

    def test_53_needs_review_acceptance_requires_all_resolutions(self):
        self.login(); data = self.decision_data("accept_candidate")
        self.assertEqual(self.client.post("/admin/reviews/decide", data=data).status_code, 400)
        subject = evaluate_staging_subject(self.staging, self.release, 91)
        data = self.decision_data("accept_candidate")
        for key in subject["required_issue_resolution_keys"]:
            data["resolution_" + key] = "TEST_ONLY 已逐项核实"
        self.assertEqual(self.client.post("/admin/reviews/decide", data=data).status_code, 302)

    def test_54_policy_rejected_record_cannot_be_accepted(self):
        with sqlite3.connect(self.staging) as db:
            record = json.loads(db.execute("SELECT record_json FROM staging_records").fetchone()[0])
            record["source_data_type"] = "TEST_ONLY_disallowed"
            serialized = canonical(record)
            import hashlib
            digest = hashlib.sha256(serialized.encode()).hexdigest()
            db.execute("UPDATE staging_records SET record_json=?,record_sha256=?", (serialized, digest))
        self.record_sha = digest
        self.login(); subject = evaluate_staging_subject(self.staging, self.release, 91)
        self.assertEqual(subject["evaluation"]["decision"], "rejected_by_policy")
        data = self.decision_data("accept_candidate")
        self.assertEqual(self.client.post("/admin/reviews/decide", data=data).status_code, 400)

    def test_55_web_session_database_schema_is_verified(self):
        invalid = self.root / "invalid-web.sqlite"
        with sqlite3.connect(invalid) as db:
            db.execute("CREATE TABLE web_sessions (token_sha256 TEXT PRIMARY KEY)")
            db.execute(f"PRAGMA application_id={sessions.APPLICATION_ID}")
            db.execute(f"PRAGMA user_version={sessions.USER_VERSION}")
        with self.assertRaises(ValueError):
            create_app({"TESTING": True, "SECRET_KEY": "TEST_ONLY",
                        "ADMIN_AUTH_DB": str(self.auth),
                        "ADMIN_WEB_SESSION_DB": str(invalid),
                        "ADMIN_COOKIE_SECURE": False})

    def test_56_unfiltered_food_list_contains_record(self):
        self.login()
        body = self.client.get("/admin/foods").get_data(as_text=True)
        self.assertIn("TEST_ONLY", body)

    def test_57_oversized_release_key_is_rejected(self):
        self.login()
        self.assertEqual(self.client.get("/admin/foods/" + "x" * 129 + "/91").status_code, 404)

    def _dashboard_count_connection(self, total):
        class CountCursor:
            def fetchone(self):
                return (total,)

        class CountOnlyConnection:
            def __init__(self):
                self.statements = []
                self.closed = False

            def execute(self, statement, parameters=()):
                self.statements.append((statement, parameters))
                if statement.strip().lower() == "select count(*) from staging_records":
                    return CountCursor()
                raise AssertionError("dashboard performed an unbounded staging query")

            def close(self):
                self.closed = True

        return CountOnlyConnection()

    def test_58_dashboard_5000_boundary_computes_exact_counts(self):
        connection = self._dashboard_count_connection(5000)
        report = {"decision_counts": {"ready_for_manual_review": 1200,
                  "needs_review": 3700, "rejected_by_policy": 100}}
        with patch("admin_web.services.open_staging_readonly", return_value=connection), \
                patch("admin_web.services.review_database", return_value=report) as scanner:
            model = services.dashboard(self.staging, None)
        scanner.assert_called_once_with(self.staging)
        self.assertTrue(connection.closed)
        self.assertTrue(model["counts_exact"])
        self.assertEqual(model["statistics_status"], "computed")
        self.assertEqual(model["counts"], report["decision_counts"])

    def test_59_dashboard_5001_boundary_never_enumerates_or_evaluates(self):
        for total in (5001, 1_000_000):
            with self.subTest(total=total):
                connection = self._dashboard_count_connection(total)
                with patch("admin_web.services.open_staging_readonly", return_value=connection), \
                        patch("admin_web.services.review_database") as scanner, \
                        patch("admin_web.services.evaluate_staging_subject") as evaluator:
                    model = services.dashboard(self.staging, None)
                scanner.assert_not_called()
                evaluator.assert_not_called()
                self.assertEqual(connection.statements,
                                 [("SELECT count(*) FROM staging_records", ())])
                self.assertTrue(connection.closed)
                self.assertFalse(model["counts_exact"])
                self.assertEqual(model["statistics_status"], "skipped_limit")
                self.assertTrue(all(value is None for value in model["counts"].values()))

    def test_60_dashboard_large_dataset_message_is_unambiguous(self):
        self.login()
        model = {"configured": True, "total": 5001, "pending": 5001,
                 "counts": {"ready_for_manual_review": None, "needs_review": None,
                            "rejected_by_policy": None},
                 "counts_exact": False, "statistics_status": "skipped_limit",
                 "statistics_limit": 5000, "handled": 0, "recent": []}
        with patch("admin_web.routes.dashboard", return_value=model):
            body = self.client.get("/admin/dashboard").get_data(as_text=True)
        self.assertEqual(body.count("<b>待计算</b>"), 3)
        self.assertIn("数据量超过统计上限（5000 条）", body)
        self.assertIn("不表示数量为零", body)


if __name__ == "__main__":
    unittest.main()
