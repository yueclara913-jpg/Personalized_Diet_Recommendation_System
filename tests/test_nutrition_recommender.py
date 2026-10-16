"""TEST_ONLY explainable recommender tests; no item is real or production-approved."""
import copy
from dataclasses import replace
from decimal import Decimal
import hashlib
from pathlib import Path
import unittest

from nutrition.diet.models import DietRecord
from nutrition.diet.services import analyze_daily_nutrition
from nutrition.profile.calculations import ManualReviewRequired
from nutrition.profile.models import HealthProfile
from nutrition.recommendation import (FoodCandidate, RecommendationPolicy,
                                      RecommendationValidationError, recommend_foods)
from nutrition.recommendation.portions import enumerate_portions, scale_nutrients
from nutrition.recommendation.validation import validate_candidate, validate_policy

NOW = "2026-01-01T00:00:00.000000Z"
SHA = "a" * 64


class NutritionRecommenderTests(unittest.TestCase):
    def setUp(self):
        self.profile = HealthProfile.create(profile_id="TEST_ONLY_profile", age=30,
            height_cm=175, weight_kg=70, sex_for_equation="male_parameter",
            activity_level="moderately_active", health_goal="maintain_weight", now=NOW)
        self.record = DietRecord.create(record_id="TEST_ONLY_record", profile_id=self.profile.profile_id,
            meal_date="2026-01-02", meal_type="breakfast", food_name="TEST_ONLY consumed",
            amount_g=100, food_source_type="manual_unverified", source_reference="TEST_ONLY label",
            source_version="TEST_ONLY_v1", energy_definition="TEST_ONLY kcal",
            energy_kcal_per_100g=1200, protein_g_per_100g=50, fat_g_per_100g=40,
            carbohydrates_g_per_100g=100, now=NOW)
        self.analysis = analyze_daily_nutrition(self.profile, [self.record], "2026-01-02")

    def candidate(self, **changes):
        values = dict(food_id="TEST_ONLY_food", food_name="TEST_ONLY artificial food",
            source_type="TEST_ONLY", source_reference="test://fixture/food", source_version="TEST_ONLY_v1",
            nutrition_basis="per_100g", energy_definition="TEST_ONLY measured kcal",
            energy_kcal_per_100g=200, protein_g_per_100g=20, fat_g_per_100g=5,
            carbohydrates_g_per_100g=25, allergen_tags=(), dietary_tags=(),
            minimum_portion_g=50, maximum_portion_g=150, portion_step_g=50,
            candidate_status="TEST_ONLY_EVALUATION", source_snapshot_sha256=SHA)
        values.update(changes)
        return FoodCandidate(**values)

    def recommend(self, candidates=None, **kwargs):
        return recommend_foods(self.profile, self.analysis,
                               [self.candidate()] if candidates is None else candidates, **kwargs)

    def test_01_legal_candidate(self):
        self.assertEqual(self.recommend()["status"], "ok")

    def test_02_dictionary_candidate_rejected(self):
        result = self.recommend([self.candidate().__dict__])
        self.assertEqual(result["excluded_candidates"][0]["reason_codes"], ["INVALID_CANDIDATE"])

    def test_03_missing_nutrient_rejected(self):
        result = self.recommend([self.candidate(protein_g_per_100g=None)])
        self.assertEqual(result["status"], "no_eligible_candidates")

    def test_04_negative_nutrient_rejected(self):
        self.assertIn("INVALID", self.recommend([self.candidate(fat_g_per_100g=-1)])["excluded_candidates"][0]["reason_codes"][0])

    def test_05_nan_rejected(self):
        self.assertEqual(self.recommend([self.candidate(energy_kcal_per_100g=float("nan"))])["status"], "no_eligible_candidates")

    def test_06_infinity_rejected(self):
        self.assertEqual(self.recommend([self.candidate(protein_g_per_100g=float("inf"))])["status"], "no_eligible_candidates")

    def test_07_unapproved_source_rejected(self):
        result = self.recommend([self.candidate(source_type="USDA_STAGING")])
        self.assertIn("source", result["excluded_candidates"][0]["detail"])

    def test_08_test_only_mode_marked(self):
        self.assertTrue(self.recommend()["recommendations"][0]["test_only"])

    def test_09_production_rejects_test_data(self):
        self.assertEqual(self.recommend(mode="production")["status"], "no_eligible_candidates")

    def test_10_client_approved_string_not_trusted(self):
        result = self.recommend([self.candidate(candidate_status="approved")])
        self.assertEqual(result["status"], "no_eligible_candidates")

    def test_11_accept_candidate_not_production_approval(self):
        item = self.candidate(candidate_status="accept_candidate")
        self.assertEqual(self.recommend([item])["status"], "no_eligible_candidates")

    def test_12_allergen_conflict_filtered(self):
        profile = replace(self.profile, dietary_restrictions=("allergen:peanut",))
        analysis = analyze_daily_nutrition(profile, [self.record], "2026-01-02")
        result = recommend_foods(profile, analysis, [self.candidate(allergen_tags=("peanut",))])
        self.assertIn("ALLERGEN_CONFLICT", result["excluded_candidates"][0]["reason_codes"])

    def test_13_unknown_allergen_information_fail_closed(self):
        profile = replace(self.profile, dietary_restrictions=("allergen:milk",))
        analysis = analyze_daily_nutrition(profile, [self.record], "2026-01-02")
        result = recommend_foods(profile, analysis, [self.candidate(allergen_tags=None)])
        self.assertIn("ALLERGEN_INFORMATION_UNKNOWN", result["excluded_candidates"][0]["reason_codes"])

    def test_14_dietary_tag_conflict(self):
        profile = replace(self.profile, dietary_restrictions=("exclude:contains_pork",))
        analysis = analyze_daily_nutrition(profile, [self.record], "2026-01-02")
        result = recommend_foods(profile, analysis, [self.candidate(dietary_tags=("contains_pork",))])
        self.assertIn("DIETARY_RESTRICTION_CONFLICT", result["excluded_candidates"][0]["reason_codes"])

    def test_15_unknown_dietary_tags_fail_closed(self):
        profile = replace(self.profile, dietary_restrictions=("exclude:contains_alcohol",))
        analysis = analyze_daily_nutrition(profile, [self.record], "2026-01-02")
        result = recommend_foods(profile, analysis, [self.candidate(dietary_tags=None)])
        self.assertIn("DIETARY_TAGS_UNKNOWN", result["excluded_candidates"][0]["reason_codes"])

    def test_16_free_text_restriction_rejected(self):
        profile = replace(self.profile, dietary_restrictions=("no peanuts please",))
        analysis = analyze_daily_nutrition(profile, [self.record], "2026-01-02")
        with self.assertRaises(RecommendationValidationError):
            recommend_foods(profile, analysis, [self.candidate()])

    def test_17_zero_portion_rejected(self):
        self.assertEqual(self.recommend([self.candidate(minimum_portion_g=0)])["status"], "no_eligible_candidates")

    def test_18_negative_portion_rejected(self):
        self.assertEqual(self.recommend([self.candidate(portion_step_g=-1)])["status"], "no_eligible_candidates")

    def test_19_reversed_portion_range_rejected(self):
        self.assertEqual(self.recommend([self.candidate(minimum_portion_g=200, maximum_portion_g=100)])["status"], "no_eligible_candidates")

    def test_20_portion_enumeration_limit(self):
        item = self.candidate(minimum_portion_g=1, maximum_portion_g=1000, portion_step_g=1)
        self.assertEqual(self.recommend([item])["status"], "no_eligible_candidates")

    def test_21_portions_include_50_100_150(self):
        values = enumerate_portions(Decimal("50"), Decimal("150"), Decimal("50"), 3)
        self.assertEqual(values, (Decimal("50"), Decimal("100"), Decimal("150")))

    def test_22_scale_50g(self):
        self.assertEqual(scale_nutrients({"energy": Decimal("200")}, Decimal("50"))["energy"], Decimal("100.00"))

    def test_23_scale_100g(self):
        self.assertEqual(scale_nutrients({"protein": Decimal("12.34")}, Decimal("100"))["protein"], Decimal("12.34"))

    def test_24_scale_150g(self):
        self.assertEqual(scale_nutrients({"fat": Decimal("5")}, Decimal("150"))["fat"], Decimal("7.50"))

    def test_25_decimal_half_up(self):
        self.assertEqual(scale_nutrients({"protein": Decimal("1.005")}, Decimal("100"))["protein"], Decimal("1.01"))

    def test_26_energy_gap_can_improve(self):
        row = self.recommend()["recommendations"][0]
        self.assertGreater(row["score_components"]["energy"]["normalized_gap_improvement"], 0)

    def test_27_protein_gap_can_improve(self):
        row = self.recommend()["recommendations"][0]
        self.assertGreater(row["score_components"]["protein"]["normalized_gap_improvement"], 0)

    def test_28_fat_overshoot_penalized(self):
        row = self.recommend([self.candidate(fat_g_per_100g=500, minimum_portion_g=100, maximum_portion_g=100)])["recommendations"][0]
        self.assertGreater(row["score_components"]["fat"]["incremental_overshoot"], 0)

    def test_29_carbohydrate_overshoot_penalized(self):
        row = self.recommend([self.candidate(carbohydrates_g_per_100g=1000, minimum_portion_g=100, maximum_portion_g=100)])["recommendations"][0]
        self.assertGreater(row["score_components"]["carbohydrates"]["weighted_overshoot_penalty"], 0)

    def test_30_composite_score_is_component_based(self):
        row = self.recommend()["recommendations"][0]
        improvement = sum(Decimal(str(v["weighted_improvement"])) for v in row["score_components"].values())
        penalty = sum(Decimal(str(v["weighted_overshoot_penalty"])) for v in row["score_components"].values())
        expected = improvement - penalty - Decimal(str(row["portion_penalty"]))
        self.assertAlmostEqual(row["score"], float(expected), places=5)

    def test_31_negative_weight_rejected(self):
        policy = replace(RecommendationPolicy(), improvement_weights=(("energy", Decimal("-1")),("protein",Decimal("1")),("fat",Decimal("0.5")),("carbohydrates",Decimal("0.5"))))
        with self.assertRaises(RecommendationValidationError): validate_policy(policy)

    def test_32_weights_must_sum_to_one(self):
        policy = replace(RecommendationPolicy(), overshoot_weights=tuple((key, Decimal("1")) for key in ("energy","protein","fat","carbohydrates")))
        with self.assertRaises(RecommendationValidationError): validate_policy(policy)

    def test_33_zero_targets_cannot_enter(self):
        bad = copy.deepcopy(self.analysis); bad["targets"]["target_energy_kcal"] = 0
        with self.assertRaises(RecommendationValidationError): recommend_foods(self.profile, bad, [self.candidate()])

    def test_34_scoring_stable(self):
        self.assertEqual(self.recommend(), self.recommend())

    def test_35_tie_breaks_by_food_id(self):
        rows = self.recommend([self.candidate(food_id="TEST_ONLY_b"), self.candidate(food_id="TEST_ONLY_a")], top_n=2)["recommendations"]
        self.assertEqual([row["food_id"] for row in rows], ["TEST_ONLY_a", "TEST_ONLY_b"])

    def test_36_tie_breaks_by_portion(self):
        row = self.recommend()["recommendations"][0]
        self.assertEqual(row["portion_g"], 150.0)

    def test_37_top_n_truncation(self):
        items = [self.candidate(food_id=f"TEST_ONLY_{letter}") for letter in "abc"]
        self.assertEqual(len(self.recommend(items, top_n=2)["recommendations"]), 2)

    def test_38_empty_candidates(self):
        self.assertEqual(self.recommend([])["status"], "no_eligible_candidates")

    def test_39_all_candidates_filtered(self):
        self.assertEqual(self.recommend([self.candidate(nutrition_basis="per_serving")])["status"], "no_eligible_candidates")

    def test_40_partial_intake_blocked(self):
        record = replace(self.record, protein_g_per_100g=None, nutrient_data_status="partial_unverified")
        # Recreate so the immutable source digest remains valid.
        record = DietRecord.create(**{k:v for k,v in dict(record.__dict__).items() if k not in {"nutrient_data_status","source_snapshot_sha256","record_status","created_at_utc","updated_at_utc"}}, now=NOW)
        analysis = analyze_daily_nutrition(self.profile, [record], "2026-01-02")
        self.assertEqual(recommend_foods(self.profile, analysis, [self.candidate()])["status"], "insufficient_intake_data")

    def test_41_unknown_intake_blocked(self):
        record = DietRecord.create(record_id="TEST_ONLY_unknown", profile_id=self.profile.profile_id,
            meal_date="2026-01-02", meal_type="snack", food_name="TEST_ONLY unknown", amount_g=10,
            food_source_type="manual_unverified", source_reference="TEST_ONLY", now=NOW)
        analysis = analyze_daily_nutrition(self.profile, [record], "2026-01-02")
        self.assertEqual(recommend_foods(self.profile, analysis, [self.candidate()])["status"], "insufficient_intake_data")

    def test_42_no_records_blocked(self):
        analysis = analyze_daily_nutrition(self.profile, [], "2026-01-02")
        self.assertEqual(recommend_foods(self.profile, analysis, [self.candidate()])["status"], "insufficient_intake_data")

    def test_43_manual_review_propagates(self):
        extreme = HealthProfile.create(profile_id="TEST_ONLY_extreme", age=18, height_cm=250, weight_kg=350,
            sex_for_equation="male_parameter", activity_level="extra_active", health_goal="maintain_weight", now=NOW)
        fake = copy.deepcopy(self.analysis); fake["profile_id"] = extreme.profile_id; fake["intake"]["profile_id"] = extreme.profile_id
        with self.assertRaises(ManualReviewRequired): recommend_foods(extreme, fake, [self.candidate()])

    def test_44_profile_id_mismatch(self):
        other = replace(self.profile, profile_id="TEST_ONLY_other")
        with self.assertRaises(RecommendationValidationError): recommend_foods(other, self.analysis, [self.candidate()])

    def test_45_bad_target_status(self):
        bad = copy.deepcopy(self.analysis); bad["targets"]["target_status"] = "manual_review_required"
        with self.assertRaises(RecommendationValidationError): recommend_foods(self.profile, bad, [self.candidate()])

    def test_46_fake_complete_flag_rejected(self):
        bad = copy.deepcopy(self.analysis); bad["intake"]["protein_g"]["data_status"] = "complete"; bad["intake"]["protein_g"]["total"] = 999
        with self.assertRaises(RecommendationValidationError): recommend_foods(self.profile, bad, [self.candidate()])

    def test_47_inconsistent_gap_rejected(self):
        bad = copy.deepcopy(self.analysis); bad["gaps"]["nutrients"]["protein"]["remaining"] += 1
        with self.assertRaises(RecommendationValidationError): recommend_foods(self.profile, bad, [self.candidate()])

    def test_48_profile_target_compatibility(self):
        self.assertEqual(self.recommend()["policy_version"], "nutrition_gap_recommender_v1")

    def test_49_diet_analysis_compatibility(self):
        self.assertEqual(self.analysis["gaps"]["data_completeness"], "complete")

    def test_50_missing_energy_definition(self):
        self.assertEqual(self.recommend([self.candidate(energy_definition="")])["status"], "no_eligible_candidates")

    def test_51_invalid_snapshot_hash(self):
        self.assertEqual(self.recommend([self.candidate(source_snapshot_sha256="bad")])["status"], "no_eligible_candidates")

    def test_52_explanation_is_traceable(self):
        row = self.recommend()["recommendations"][0]
        self.assertIn("150 g", row["explanation"])
        self.assertEqual(row["source"]["snapshot_sha256"], SHA)

    def test_53_no_medical_claims(self):
        text = self.recommend()["recommendations"][0]["explanation"]
        self.assertNotIn("治疗", text); self.assertNotIn("降低血压", text)

    def test_54_no_usda_staging_trust(self):
        result = self.recommend([self.candidate(source_type="USDA_STAGING")])
        self.assertEqual(result["status"], "no_eligible_candidates")

    def test_55_no_database_write_side_effect(self):
        before = sorted(str(path) for path in Path(".").rglob("*.sqlite"))
        self.recommend()
        self.assertEqual(before, sorted(str(path) for path in Path(".").rglob("*.sqlite")))

    def test_56_no_http_recommendation_route(self):
        source = Path("main.py").read_text(encoding="utf-8")
        self.assertNotIn("nutrition/recommend", source)

    def test_57_main_legacy_functions_unchanged_by_module(self):
        self.assertTrue(Path("main.py").is_file())

    def test_58_baseline_and_proposed_share_filter(self):
        profile = replace(self.profile, dietary_restrictions=("allergen:peanut",))
        analysis = analyze_daily_nutrition(profile, [self.record], "2026-01-02")
        item = self.candidate(allergen_tags=("peanut",))
        proposed = recommend_foods(profile, analysis, [item])
        baseline = recommend_foods(profile, analysis, [item], algorithm="baseline")
        self.assertEqual(proposed["excluded_candidates"], baseline["excluded_candidates"])

    def test_59_baseline_reproducible(self):
        self.assertEqual(self.recommend(algorithm="baseline"), self.recommend(algorithm="baseline"))

    def test_60_baseline_only_weights_energy(self):
        row = self.recommend(algorithm="baseline")["recommendations"][0]
        self.assertEqual(row["score_components"]["protein"]["weighted_improvement"], 0.0)

    def test_61_conflict_never_reaches_top_n(self):
        profile = replace(self.profile, dietary_restrictions=("allergen:egg",))
        analysis = analyze_daily_nutrition(profile, [self.record], "2026-01-02")
        bad = self.candidate(food_id="TEST_ONLY_bad", allergen_tags=("egg",))
        good = self.candidate(food_id="TEST_ONLY_good")
        result = recommend_foods(profile, analysis, [bad, good])
        self.assertNotIn("TEST_ONLY_bad", [row["food_id"] for row in result["recommendations"]])

    def test_62_large_portion_not_rewarded_without_limit(self):
        rows = self.recommend()["recommendations"]
        self.assertLessEqual(rows[0]["portion_g"], 150)
        self.assertTrue(all(row["portion_penalty"] >= 0 for row in rows))

    def test_63_illegal_top_n_values(self):
        for value in (0, -1, 101, True, 1.5):
            with self.subTest(value=value), self.assertRaises(RecommendationValidationError): self.recommend(top_n=value)

    def test_64_duplicate_food_id_rejected(self):
        result = self.recommend([self.candidate(), self.candidate()])
        self.assertEqual(result["excluded_candidates"][0]["detail"], "duplicate food_id")

    def test_65_unknown_mode_rejected(self):
        with self.assertRaises(RecommendationValidationError): self.recommend(mode="clinical")

    def test_66_unknown_algorithm_rejected(self):
        with self.assertRaises(RecommendationValidationError): self.recommend(algorithm="random")

    def test_67_candidate_input_unchanged(self):
        item = self.candidate(); before = copy.deepcopy(item)
        self.recommend([item]); self.assertEqual(item, before)

    def test_68_analysis_input_unchanged(self):
        before = copy.deepcopy(self.analysis); self.recommend(); self.assertEqual(self.analysis, before)

    def test_69_profile_input_unchanged(self):
        before = copy.deepcopy(self.profile); self.recommend(); self.assertEqual(self.profile, before)

    def test_70_units_are_explicit(self):
        units = self.recommend()["recommendations"][0]["nutrient_units"]
        self.assertEqual(units, {"energy":"kcal","protein":"g","fat":"g","carbohydrates":"g"})

    def test_71_required_vegan_tag_is_hard_constraint(self):
        profile = replace(self.profile, dietary_restrictions=("require:vegan",))
        analysis = analyze_daily_nutrition(profile, [self.record], "2026-01-02")
        result = recommend_foods(profile, analysis, [self.candidate(dietary_tags=("vegetarian",))])
        self.assertIn("REQUIRED_DIETARY_TAG_MISSING", result["excluded_candidates"][0]["reason_codes"])


if __name__ == "__main__":
    unittest.main()
