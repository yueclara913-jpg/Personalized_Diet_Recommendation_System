"""TEST_ONLY health profile, nutrition target and gap tests."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import main
from nutrition.auth.admin_auth import APPLICATION_ID as ADMIN_APPLICATION_ID
from nutrition.profile.calculations import (ACTIVITY_FACTORS, ManualReviewRequired,
    calculate_bmi, calculate_bmr, calculate_daily_targets, calculate_nutrition_gaps,
    calculate_tdee)
from nutrition.profile.models import HealthProfile
from nutrition.profile.repository import (APPLICATION_ID, get_profile, initialize_database,
                                            insert_profile, update_profile, validate_database)
from nutrition.profile.validation import ProfileValidationError


class HealthProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "TEST_ONLY-health.sqlite"

    def profile(self, **changes):
        values = dict(profile_id="TEST_ONLY_profile", age=30, height_cm=175, weight_kg=70,
                      sex_for_equation="male_parameter", activity_level="moderately_active",
                      health_goal="maintain_weight", dietary_restrictions=("TEST_ONLY_nuts",),
                      population_status="general_adult", now="2026-01-01T00:00:00.000000Z")
        values.update(changes)
        return HealthProfile.create(**values)

    def test_01_normal_bmi(self):
        result = calculate_bmi(70, 175)
        self.assertEqual(result["bmi_rounded"], 22.86)

    def test_02_bmi_invalid_height(self):
        with self.assertRaises(ProfileValidationError): calculate_bmi(70, 0)

    def test_03_bmi_invalid_weight(self):
        with self.assertRaises(ProfileValidationError): calculate_bmi(-1, 175)

    def test_04_bmi_boundary_18_5(self):
        self.assertEqual(calculate_bmi(74, 200)["category"], "reference_range")

    def test_05_bmi_boundary_25(self):
        self.assertEqual(calculate_bmi(100, 200)["category"], "above_reference_range")

    def test_06_bmi_boundary_30(self):
        self.assertEqual(calculate_bmi(120, 200)["category"], "high_reference_range")

    def test_07_bmi_raw_and_units_and_versions(self):
        result = calculate_bmi(70, 175)
        self.assertIn("bmi_raw", result); self.assertEqual(result["input_units"]["weight"], "kg")
        self.assertTrue(result["formula_version"].endswith("_v1"))

    def test_08_male_bmr_reference(self):
        self.assertEqual(calculate_bmr(self.profile())["estimated_bmr_kcal"], 1648.75)

    def test_09_female_bmr_reference(self):
        result = calculate_bmr(self.profile(sex_for_equation="female_parameter"))
        self.assertEqual(result["estimated_bmr_kcal"], 1482.75)

    def test_10_underage_rejected(self):
        with self.assertRaises(ProfileValidationError): self.profile(age=17)

    def test_11_over_age_range_rejected(self):
        with self.assertRaises(ProfileValidationError): self.profile(age=101)

    def test_12_special_population_rejected_for_bmr(self):
        with self.assertRaises(ProfileValidationError): calculate_bmr(self.profile(population_status="pregnant"))

    def test_13_lactation_rejected_for_targets(self):
        with self.assertRaises(ProfileValidationError): calculate_daily_targets(self.profile(population_status="lactating"))

    def test_14_all_activity_factors(self):
        for level, factor in ACTIVITY_FACTORS.items():
            with self.subTest(level=level):
                result = calculate_tdee(self.profile(activity_level=level))
                self.assertEqual(result["activity_factor"], float(factor))

    def test_15_unknown_activity_rejected(self):
        with self.assertRaises(ProfileValidationError): self.profile(activity_level="unknown")

    def test_16_maintain_goal(self):
        result = calculate_daily_targets(self.profile())
        self.assertEqual(result["goal_adjustment_kcal"], 0)

    def test_17_lose_goal(self):
        result = calculate_daily_targets(self.profile(health_goal="lose_weight"))
        self.assertEqual(result["goal_adjustment_kcal"], -300)

    def test_18_gain_goal(self):
        result = calculate_daily_targets(self.profile(health_goal="gain_weight"))
        self.assertEqual(result["goal_adjustment_kcal"], 250)

    def test_19_adjustment_lower_bound(self):
        changes = {"maintain_weight": 0, "lose_weight": -501, "gain_weight": 250}
        with self.assertRaises(ProfileValidationError): calculate_daily_targets(self.profile(), goal_adjustments=changes)

    def test_20_adjustment_upper_bound(self):
        changes = {"maintain_weight": 0, "lose_weight": -300, "gain_weight": 501}
        with self.assertRaises(ProfileValidationError): calculate_daily_targets(self.profile(), goal_adjustments=changes)

    def test_21_unreasonable_target_requires_review(self):
        low = self.profile(age=100, height_cm=100, weight_kg=30,
                           sex_for_equation="female_parameter", activity_level="sedentary",
                           health_goal="lose_weight")
        with self.assertRaises(ManualReviewRequired): calculate_daily_targets(low)

    def test_22_macro_ratios_must_sum_to_one(self):
        with self.assertRaises(ProfileValidationError):
            calculate_daily_targets(self.profile(), macro_ratios={"protein": .3, "fat": .3, "carbohydrates": .3})

    def test_23_protein_grams(self):
        result = calculate_daily_targets(self.profile())
        self.assertAlmostEqual(result["protein_target_g"] * 4, result["target_energy_kcal"] * .25, places=1)

    def test_24_fat_grams(self):
        result = calculate_daily_targets(self.profile())
        self.assertAlmostEqual(result["fat_target_g"] * 9, result["target_energy_kcal"] * .30, places=1)

    def test_25_carbohydrate_grams(self):
        result = calculate_daily_targets(self.profile())
        self.assertAlmostEqual(result["carbohydrate_target_g"] * 4, result["target_energy_kcal"] * .45, places=1)

    def test_26_macro_449_consistency(self):
        result = calculate_daily_targets(self.profile())
        energy = result["protein_target_g"] * 4 + result["fat_target_g"] * 9 + result["carbohydrate_target_g"] * 4
        self.assertAlmostEqual(energy, result["target_energy_kcal"], delta=.08)

    def test_27_target_units_and_assumptions(self):
        result = calculate_daily_targets(self.profile())
        self.assertEqual(result["units"]["energy"], "kcal/day")
        self.assertTrue(result["assumptions"]); self.assertTrue(result["formula_version"].endswith("_v1"))

    def targets(self):
        return calculate_daily_targets(self.profile())

    def test_28_gap_missing_target_rejected(self):
        values = self.targets(); values.pop("protein_target_g")
        with self.assertRaises(ProfileValidationError): calculate_nutrition_gaps(values, {})

    def test_29_zero_consumption_is_known(self):
        result = calculate_nutrition_gaps(self.targets(), {"energy": 0, "protein": 0, "fat": 0, "carbohydrates": 0})
        self.assertEqual(result["nutrients"]["protein"]["consumed"], 0.0)
        self.assertEqual(result["data_completeness"], "complete")

    def test_30_unknown_consumption_is_not_zero(self):
        result = calculate_nutrition_gaps(self.targets(), {"energy": None})
        self.assertIsNone(result["nutrients"]["energy"]["consumed"])
        self.assertEqual(result["data_completeness"], "partial")

    def test_31_positive_remaining_gap(self):
        result = calculate_nutrition_gaps(self.targets(), {"energy": 100, "protein": 10, "fat": 10, "carbohydrates": 10})
        self.assertGreater(result["nutrients"]["energy"]["remaining"], 0)

    def test_32_exceeded_target_preserves_negative_difference(self):
        targets = self.targets()
        result = calculate_nutrition_gaps(targets, {"energy": 9999, "protein": 9999, "fat": 9999, "carbohydrates": 9999})
        energy = result["nutrients"]["energy"]
        self.assertTrue(energy["exceeded"]); self.assertLess(energy["difference"], 0); self.assertGreater(energy["excess"], 0)

    def test_33_completion_ratio(self):
        targets = self.targets()
        result = calculate_nutrition_gaps(targets, {"energy": targets["target_energy_kcal"] / 2})
        self.assertEqual(result["nutrients"]["energy"]["completion_ratio"], .5)

    def test_34_negative_consumption_rejected(self):
        with self.assertRaises(ProfileValidationError): calculate_nutrition_gaps(self.targets(), {"energy": -1})

    def test_35_nan_and_infinity_rejected(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(ProfileValidationError): calculate_bmi(value, 175)

    def test_36_profile_validation_and_restriction_normalization(self):
        item = self.profile(dietary_restrictions=(" TEST_ONLY_nuts ", "TEST_ONLY_nuts"))
        self.assertEqual(item.dietary_restrictions, ("TEST_ONLY_nuts",))

    def test_37_database_initialization_and_schema(self):
        initialize_database(self.db); validate_database(self.db)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID)
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_38_duplicate_initialization_refuses_overwrite(self):
        initialize_database(self.db); before = self.db.read_bytes()
        with self.assertRaises(FileExistsError): initialize_database(self.db)
        self.assertEqual(self.db.read_bytes(), before)

    def test_39_insert_and_read_profile(self):
        initialize_database(self.db); item = self.profile(); insert_profile(self.db, item)
        self.assertEqual(get_profile(self.db, item.profile_id), item)

    def test_40_duplicate_profile_rejected(self):
        initialize_database(self.db); item = self.profile(); insert_profile(self.db, item)
        with self.assertRaises(sqlite3.IntegrityError): insert_profile(self.db, item)

    def test_41_transaction_failure_rolls_back(self):
        initialize_database(self.db)
        def fail(): raise RuntimeError("TEST_ONLY failure")
        with self.assertRaises(RuntimeError): insert_profile(self.db, self.profile(), failure_hook=fail)
        self.assertIsNone(get_profile(self.db, "TEST_ONLY_profile"))

    def test_42_optimistic_update_and_stale_rejection(self):
        initialize_database(self.db); item = self.profile(); insert_profile(self.db, item)
        changed = replace(item, weight_kg=71)
        updated = update_profile(self.db, changed, expected_updated_at=item.updated_at_utc,
                                 now="2026-01-02T00:00:00.000000Z")
        self.assertEqual(get_profile(self.db, item.profile_id).weight_kg, 71)
        with self.assertRaises(ValueError): update_profile(self.db, changed, expected_updated_at=item.updated_at_utc)
        self.assertEqual(updated.created_at_utc, item.created_at_utc)

    def test_43_schema_tampering_rejected(self):
        initialize_database(self.db)
        with sqlite3.connect(self.db) as db: db.execute("ALTER TABLE health_profiles ADD COLUMN extra TEXT")
        with self.assertRaises(ValueError): validate_database(self.db)

    def test_44_admin_database_is_physically_distinct(self):
        self.assertNotEqual(APPLICATION_ID, ADMIN_APPLICATION_ID)
        initialize_database(self.db)
        with sqlite3.connect(self.db) as db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn("admin_accounts", tables)

    def test_45_does_not_read_usda_staging_or_food_csv(self):
        before = hashlib.sha256((Path(__file__).parents[1] / "food_data.csv").read_bytes()).hexdigest()
        calculate_daily_targets(self.profile())
        after = hashlib.sha256((Path(__file__).parents[1] / "food_data.csv").read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_46_existing_flask_bmi_route_still_works(self):
        response = main.app.test_client().post("/calculate_bmi", json={"weight": 70, "height": 175})
        self.assertEqual(response.status_code, 200); self.assertEqual(response.get_json()["bmi"], 22.86)

    def test_47_no_public_health_profile_api(self):
        rules = {rule.rule for rule in main.app.url_map.iter_rules()}
        self.assertFalse(any("profile" in rule or "health" in rule for rule in rules))

    def test_48_no_medical_diagnosis_or_treatment_claim(self):
        result = json.dumps(calculate_daily_targets(self.profile())).lower()
        self.assertNotIn("diagnosis", result.replace("not diagnosis", ""))
        self.assertNotIn("treatment plan", result)

    def test_49_invalid_restriction_type_rejected(self):
        with self.assertRaises(ProfileValidationError): self.profile(dietary_restrictions="nuts")

    def test_50_invalid_equation_option_rejected(self):
        with self.assertRaises(ProfileValidationError): self.profile(sex_for_equation="other")


if __name__ == "__main__":
    unittest.main()
