"""TEST_ONLY review fixtures; never real USDA foods or official verification."""
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

from nutrition.importers.usda_sqlite import APPLICATION_ID, DDL, import_payload
from nutrition.review.policy import ReviewPolicy, evaluate_food_record, policy_from_dict
from nutrition.sources.usda_foundation import parse_snapshot

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("review_cli", ROOT / "scripts/review_usda_staging.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


class ReviewPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = self.root / "TEST_ONLY_raw.json"
        self.db = self.root / "TEST_ONLY_staging.sqlite"
        nutrient_ids = ((1003, "g"), (1004, "g"), (1005, "g"), (1008, "kcal"),
                        (1063, "g"), (1079, "g"), (1089, "mg"), (1093, "mg"))
        self.food = {"fdcId": 101, "description": "TEST_ONLY artificial food", "dataType": "Foundation",
                     "foodNutrients": [{"nutrient": {"id": nid, "name": "TEST_ONLY", "unitName": unit},
                                        "amount": 0 if nid == 1003 else 1}
                                       for nid, unit in nutrient_ids], "foodPortions": []}
        self.payload = self.parse()
        self.record = self.payload["records"][0]
        self.context = {"official_file_verified": True,
                        "verified_source_file_sha256": self.record["source_file_sha256"],
                        "verified_source_version": self.record["source_version"],
                        "verified_source_url": self.record["source_url"],
                        "verification_reference": "TEST_ONLY independent evidence",
                        "duplicate_fdc_count": 1}

    def parse(self, food=None, basis="per_100g", evidence="TEST_ONLY basis evidence",
              version="TEST_ONLY_v1"):
        self.raw.write_text(json.dumps({"FoundationFoods": [food or self.food]}), encoding="utf-8")
        return parse_snapshot(self.raw, source_version=version,
                              source_url="https://fdc.nal.usda.gov/TEST_ONLY",
                              nutrition_basis=basis, basis_evidence=evidence,
                              energy_id=1008, sugar_id=1063)

    def evaluate(self, record=None, context=None, policy=None):
        return evaluate_food_record(self.record if record is None else record,
                                    self.context if context is None else context,
                                    policy or ReviewPolicy())

    def import_db(self, payload=None):
        return import_payload(payload or self.payload, mode="staging", database=self.db)

    def test_complete_record_ready_but_pending(self):
        result = self.evaluate()
        self.assertEqual(result["decision"], "ready_for_manual_review")
        self.assertEqual(result["review_status"], "pending")
        self.assertNotIn("approved", json.dumps(result))

    def test_missing_official_evidence(self):
        result = self.evaluate(context={})
        self.assertEqual(result["decision"], "needs_review")
        self.assertIn("SRC007", [x["rule_id"] for x in result["issues"]])

    def test_hash_alone_is_not_authenticity(self):
        result = self.evaluate(context={"verified_source_file_sha256": self.record["source_file_sha256"]})
        self.assertEqual(result["decision"], "needs_review")

    def test_missing_value_and_real_zero_differ(self):
        record = copy.deepcopy(self.record)
        record["nutrients"][1]["raw_value"] = None
        record["nutrients"][1]["status"] = "missing"
        record["nutrients"][1]["reason"] = "missing_value"
        result = self.evaluate(record)
        self.assertIn("missing_value", [x["issue_type"] for x in result["issues"]])
        self.assertFalse(any(x["source_location"] == "nutrients[0].raw_value" for x in result["issues"]))

    def test_invalid_numeric_rejected(self):
        record = copy.deepcopy(self.record)
        record["nutrients"][0]["raw_value"] = "NaN"
        self.assertEqual(self.evaluate(record)["decision"], "rejected_by_policy")

    def test_unknown_unit_configurable(self):
        record = copy.deepcopy(self.record)
        record["nutrients"][0]["raw_unit"] = "TEST_ONLY_UNIT"
        self.assertEqual(self.evaluate(record)["decision"], "needs_review")
        strict = ReviewPolicy(unknown_unit_level="rejected_by_policy")
        self.assertEqual(self.evaluate(record, policy=strict)["decision"], "rejected_by_policy")

    def test_unknown_basis(self):
        record = self.parse(basis="unknown", evidence=None)["records"][0]
        context = dict(self.context, verified_source_file_sha256=record["source_file_sha256"])
        result = self.evaluate(record, context)
        self.assertIn("BAS001", [x["rule_id"] for x in result["issues"]])
        self.assertIsNone(record["standardized"]["protein"])

    def test_multiple_energy_definitions(self):
        food = copy.deepcopy(self.food)
        food["foodNutrients"].append({"nutrient": {"id": 2047, "name": "TEST_ONLY", "unitName": "kcal"}, "amount": 2})
        record = self.parse(food)["records"][0]
        context = dict(self.context, verified_source_file_sha256=record["source_file_sha256"])
        issue = next(x for x in self.evaluate(record, context)["issues"] if x["rule_id"] == "DEF001")
        self.assertEqual(issue["affected_nutrient_ids"], [1008, 2047])
        self.assertEqual(record["standardized"]["calories"], "1.000000")

    def test_multiple_sugar_definitions(self):
        food = copy.deepcopy(self.food)
        food["foodNutrients"].append({"nutrient": {"id": 2000, "name": "TEST_ONLY", "unitName": "g"}, "amount": 2})
        record = self.parse(food)["records"][0]
        context = dict(self.context, verified_source_file_sha256=record["source_file_sha256"])
        issue = next(x for x in self.evaluate(record, context)["issues"] if x["rule_id"] == "DEF002")
        self.assertEqual(issue["affected_nutrient_ids"], [1063, 2000])

    def test_loq_value(self):
        food = copy.deepcopy(self.food)
        food["foodNutrients"][0]["loq"] = 0.1
        record = self.parse(food)["records"][0]
        context = dict(self.context, verified_source_file_sha256=record["source_file_sha256"])
        self.assertIn("NUT007", [x["rule_id"] for x in self.evaluate(record, context)["issues"]])

    def test_duplicate_nutrient_rejected(self):
        record = copy.deepcopy(self.record)
        record["nutrients"].append(copy.deepcopy(record["nutrients"][0]))
        self.assertEqual(self.evaluate(record)["decision"], "rejected_by_policy")

    def test_raw_nutrient_trace_mismatch(self):
        record = copy.deepcopy(self.record)
        record["nutrients"][0]["source_nutrient_id"] = 9999
        result = self.evaluate(record)
        self.assertEqual(result["decision"], "rejected_by_policy")
        self.assertIn("STR005", [x["rule_id"] for x in result["issues"]])

    def test_standardized_value_tampering_rejected(self):
        record = copy.deepcopy(self.record)
        record["standardized"]["protein"] = "999.000000"
        result = self.evaluate(record)
        self.assertEqual(result["decision"], "rejected_by_policy")
        self.assertIn("NUT011", [x["rule_id"] for x in result["issues"]])

    def test_required_nutrients_configurable(self):
        food = copy.deepcopy(self.food)
        food["foodNutrients"] = [x for x in food["foodNutrients"] if x["nutrient"]["id"] != 1089]
        record = self.parse(food)["records"][0]
        context = dict(self.context, verified_source_file_sha256=record["source_file_sha256"])
        self.assertEqual(self.evaluate(record, context)["decision"], "ready_for_manual_review")
        policy = ReviewPolicy(required_nutrients=("iron",))
        self.assertEqual(self.evaluate(record, context, policy)["decision"], "needs_review")

    def test_missing_level_can_reject(self):
        record = copy.deepcopy(self.record)
        record["nutrients"] = [x for x in record["nutrients"] if x["source_nutrient_id"] != 1003]
        policy = ReviewPolicy(missing_level="rejected_by_policy")
        self.assertEqual(self.evaluate(record, policy=policy)["decision"], "rejected_by_policy")

    def test_policy_json_and_version(self):
        policy = policy_from_dict({"version": "TEST_ONLY_policy", "required_nutrients": ["protein"],
                                   "multiple_energy_level": "ignore"})
        self.assertEqual(self.evaluate(policy=policy)["policy_version"], "TEST_ONLY_policy")
        with self.assertRaises(ValueError):
            policy_from_dict({"unknown": True})

    def test_deterministic_and_input_unchanged(self):
        record, context = copy.deepcopy(self.record), copy.deepcopy(self.context)
        first = self.evaluate(record, context)
        second = self.evaluate(record, context)
        self.assertEqual(first, second)
        self.assertEqual(record, self.record)
        self.assertEqual(context, self.context)

    def test_bad_identity_name_and_duplicate_batch(self):
        record = copy.deepcopy(self.record)
        record["source_food_id"] = False
        record["food_name"] = ""
        result = self.evaluate(record, dict(self.context, duplicate_fdc_count=2))
        self.assertEqual(result["decision"], "rejected_by_policy")
        self.assertTrue({"SRC001", "STR001", "INT001"} <= {x["rule_id"] for x in result["issues"]})

    def test_bad_source_fields(self):
        for field, value in (("source_url", "https://example.com"), ("source_version", ""),
                             ("source_file_sha256", "bad"), ("source_locator", "wrong")):
            with self.subTest(field=field):
                record = copy.deepcopy(self.record)
                record[field] = value
                self.assertEqual(self.evaluate(record)["decision"], "rejected_by_policy")

    def test_dry_matter_and_portion_review(self):
        record = copy.deepcopy(self.record)
        record["food_name"] = "TEST_ONLY dry matter"
        record["raw_record"]["foodPortions"] = [{"amount": 1, "gramWeight": 10}]
        rules = {x["rule_id"] for x in self.evaluate(record)["issues"]}
        self.assertTrue({"BAS003", "BAS004"} <= rules)

    def test_readonly_database_review(self):
        self.import_db()
        before = self.db.read_bytes()
        report = CLI.review_database(self.db)
        self.assertEqual(report["database_mode"], "read_only")
        self.assertEqual(report["decision_counts"]["needs_review"], 1)
        self.assertEqual(before, self.db.read_bytes())

    def test_verified_release_evidence_ready(self):
        self.import_db()
        with sqlite3.connect(self.db) as connection:
            key = connection.execute("SELECT release_key FROM staging_releases").fetchone()[0]
        evidence = {name: value for name, value in self.context.items() if name != "duplicate_fdc_count"}
        report = CLI.review_database(self.db, provenance_evidence={key: evidence})
        self.assertEqual(report["decision_counts"]["ready_for_manual_review"], 1)

    def test_same_fdc_in_different_releases_is_not_duplicate(self):
        self.import_db()
        second = self.parse(version="TEST_ONLY_v2")
        import_payload(second, mode="staging", database=self.db, confirm_existing=True)
        report = CLI.review_database(self.db)
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["decision_counts"], {
            "ready_for_manual_review": 0, "needs_review": 2, "rejected_by_policy": 0})
        self.assertFalse(any(issue["rule_id"] == "INT001"
                             for result in report["results"] for issue in result["issues"]))

    def test_same_release_duplicate_blocked_by_composite_primary_key(self):
        self.import_db()
        with sqlite3.connect(self.db) as connection:
            row = connection.execute("SELECT * FROM staging_records").fetchone()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("INSERT INTO staging_records VALUES (?,?,?,?,?,?,?)", row)
            self.assertEqual(connection.execute("SELECT count(*) FROM staging_records").fetchone()[0], 1)

    def test_business_database_refused(self):
        spec = importlib.util.spec_from_file_location("initdb", ROOT / "scripts/init_nutrition_db.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.initialize_database(self.db).close()
        with self.assertRaises(ValueError):
            CLI.review_database(self.db)

    def test_empty_staging_database(self):
        connection = sqlite3.connect(self.db)
        for statement in DDL:
            connection.execute(statement)
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute("PRAGMA user_version=1")
        connection.commit()
        connection.close()
        report = CLI.review_database(self.db)
        self.assertEqual(report["total"], 0)
        self.assertEqual(sum(report["decision_counts"].values()), 0)

    def test_altered_schema_refused(self):
        self.import_db()
        with sqlite3.connect(self.db) as connection:
            connection.execute("CREATE TABLE TEST_ONLY_extra(x)")
        with self.assertRaises(ValueError):
            CLI.review_database(self.db)

    def test_provenance_cannot_override_batch_integrity(self):
        self.import_db()
        with sqlite3.connect(self.db) as connection:
            key = connection.execute("SELECT release_key FROM staging_releases").fetchone()[0]
        with self.assertRaises(ValueError):
            CLI.review_database(self.db, provenance_evidence={key: {"duplicate_fdc_count": 1}})

    def test_tampered_record_digest_refused(self):
        self.import_db()
        with sqlite3.connect(self.db) as connection:
            connection.execute("UPDATE staging_records SET record_json='{}'")
        with self.assertRaises(ValueError):
            CLI.review_database(self.db)

    def test_report_exclusive_and_repeatable(self):
        self.import_db()
        report = CLI.review_database(self.db)
        output = self.root / "report"
        CLI.write_report(report, output)
        first = (output / "review.json").read_bytes()
        with self.assertRaises(FileExistsError):
            CLI.write_report(report, output)
        self.assertEqual(first, (output / "review.json").read_bytes())

    def test_cli_success_and_failure(self):
        self.import_db()
        command = [sys.executable, str(ROOT / "scripts/review_usda_staging.py"),
                   "--database", str(self.db), "--output-dir", str(self.root / "cli-report")]
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        result = subprocess.run(command, capture_output=True, text=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["review_status"], "pending")
        result = subprocess.run(command, capture_output=True, text=True, env=environment)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(json.loads(result.stderr)["status"], "rejected")

    def test_readonly_connection_rejects_write(self):
        self.import_db()
        connection = CLI.open_staging_readonly(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("DELETE FROM staging_records")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
