"""TEST_ONLY daily diet record, repository and nutrition analysis tests."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sqlite3
import tempfile
import unittest

import main
from nutrition.auth.admin_auth import APPLICATION_ID as ADMIN_APP_ID
from nutrition.diet.calculations import calculate_daily_intake, calculate_meal_intake, scale_record_nutrients
from nutrition.diet.models import DietRecord
from nutrition.diet.repository import (APPLICATION_ID, get_record, initialize_database,
    insert_record, list_records, revoke_record, update_record, validate_database)
from nutrition.diet.services import analyze_daily_nutrition
from nutrition.diet.validation import DietValidationError
from nutrition.profile.calculations import ManualReviewRequired
from nutrition.profile.models import HealthProfile
from nutrition.profile.repository import initialize_database as initialize_profiles, insert_profile

NOW = "2026-01-01T08:00:00.000000Z"
LATER = "2026-01-01T09:00:00.000000Z"


class DailyDietLoggingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); self.profiles = self.root / "TEST_ONLY-profiles.sqlite"
        self.diet = self.root / "TEST_ONLY-diet.sqlite"
        initialize_profiles(self.profiles)
        self.profile = HealthProfile.create(profile_id="TEST_ONLY_profile", age=30, height_cm=175,
            weight_kg=70, sex_for_equation="male_parameter", activity_level="moderately_active",
            health_goal="maintain_weight", now=NOW)
        insert_profile(self.profiles, self.profile); initialize_database(self.diet)

    def record(self, **changes):
        values = dict(record_id="TEST_ONLY_record", profile_id=self.profile.profile_id,
            meal_date="2026-01-02", meal_type="breakfast", food_name="TEST_ONLY oats", amount_g=100,
            food_source_type="manual_unverified", source_reference="TEST_ONLY label",
            source_version="TEST_ONLY_v1", energy_definition="TEST_ONLY label kcal",
            energy_kcal_per_100g=100, protein_g_per_100g=10,
            fat_g_per_100g=5, carbohydrates_g_per_100g=20, notes="TEST_ONLY", now=NOW)
        values.update(changes); return DietRecord.create(**values)

    def test_01_breakfast_record(self): self.assertEqual(self.record().meal_type, "breakfast")
    def test_02_lunch_record(self): self.assertEqual(self.record(meal_type="lunch").meal_type, "lunch")
    def test_03_dinner_record(self): self.assertEqual(self.record(meal_type="dinner").meal_type, "dinner")
    def test_04_snack_record(self): self.assertEqual(self.record(meal_type="snack").meal_type, "snack")

    def test_05_unsupported_meal_rejected(self):
        with self.assertRaises(DietValidationError): self.record(meal_type="midnight")

    def test_06_invalid_date_rejected(self):
        with self.assertRaises(DietValidationError): self.record(meal_date="2026-02-30")

    def test_07_noncanonical_date_rejected(self):
        with self.assertRaises(DietValidationError): self.record(meal_date="2026-1-2")

    def test_08_invalid_timestamp_rejected(self):
        with self.assertRaises(DietValidationError): self.record(now="not-time")

    def test_09_duplicate_record_id_rejected(self):
        item=self.record(); insert_record(self.diet,self.profiles,item)
        with self.assertRaises(sqlite3.IntegrityError): insert_record(self.diet,self.profiles,item)

    def test_10_zero_amount_rejected(self):
        with self.assertRaises(DietValidationError): self.record(amount_g=0)

    def test_11_negative_amount_rejected(self):
        with self.assertRaises(DietValidationError): self.record(amount_g=-1)

    def test_12_negative_nutrient_rejected(self):
        with self.assertRaises(DietValidationError): self.record(protein_g_per_100g=-1)

    def test_13_nonfinite_numbers_rejected(self):
        for value in (float("nan"),float("inf"),float("-inf")):
            with self.subTest(value=value),self.assertRaises(DietValidationError): self.record(amount_g=value)

    def test_14_scale_100g(self): self.assertEqual(scale_record_nutrients(self.record())["nutrients"]["energy_kcal"],100)
    def test_15_scale_50g(self): self.assertEqual(scale_record_nutrients(self.record(amount_g=50))["nutrients"]["protein_g"],5)
    def test_16_scale_150g(self): self.assertEqual(scale_record_nutrients(self.record(amount_g=150))["nutrients"]["fat_g"],7.5)

    def test_17_decimal_rounding(self):
        result=scale_record_nutrients(self.record(amount_g=Decimal("33.333"),protein_g_per_100g=Decimal("3.333")))
        self.assertEqual(result["nutrients"]["protein_g"],1.11)

    def test_18_missing_energy_stays_unknown(self):
        self.assertIsNone(scale_record_nutrients(self.record(energy_kcal_per_100g=None,energy_definition=None))["nutrients"]["energy_kcal"])
    def test_19_missing_protein_stays_unknown(self):
        self.assertIsNone(scale_record_nutrients(self.record(protein_g_per_100g=None))["nutrients"]["protein_g"])
    def test_20_missing_fat_stays_unknown(self):
        self.assertIsNone(scale_record_nutrients(self.record(fat_g_per_100g=None))["nutrients"]["fat_g"])
    def test_21_missing_carbohydrates_stays_unknown(self):
        self.assertIsNone(scale_record_nutrients(self.record(carbohydrates_g_per_100g=None))["nutrients"]["carbohydrates_g"])

    def test_22_known_zero_is_not_unknown(self):
        result=scale_record_nutrients(self.record(protein_g_per_100g=0))
        self.assertEqual(result["nutrients"]["protein_g"],0); self.assertEqual(result["nutrient_data_status"],"complete_unverified")

    def test_23_no_records_is_not_zero_intake(self):
        result=calculate_daily_intake([],"2026-01-02",profile_id=self.profile.profile_id)
        self.assertEqual(result["energy_kcal"]["data_status"],"no_records"); self.assertIsNone(result["energy_kcal"]["total"])

    def test_24_single_meal_statistics(self):
        result=calculate_meal_intake([self.record()]); self.assertEqual(result["record_count"],1); self.assertEqual(result["energy_kcal"]["total"],100)

    def test_25_multiple_meal_records(self):
        rows=[self.record(record_id="TEST_ONLY_1"),self.record(record_id="TEST_ONLY_2",amount_g=50)]
        self.assertEqual(calculate_meal_intake(rows)["energy_kcal"]["total"],150)

    def test_26_daily_multi_meal_statistics(self):
        rows=[self.record(record_id="TEST_ONLY_1"),self.record(record_id="TEST_ONLY_2",meal_type="lunch")]
        result=calculate_daily_intake(rows,"2026-01-02",profile_id=self.profile.profile_id)
        self.assertEqual(result["meal_count"],2); self.assertEqual(result["record_count"],2)

    def test_27_different_date_isolated(self):
        rows=[self.record(),self.record(record_id="TEST_ONLY_other",meal_date="2026-01-03")]
        self.assertEqual(calculate_daily_intake(rows,"2026-01-02",profile_id=self.profile.profile_id)["record_count"],1)

    def test_28_different_profile_requires_explicit_scope(self):
        other=self.record(record_id="TEST_ONLY_other",profile_id="TEST_ONLY_otherprofile")
        with self.assertRaises(DietValidationError): calculate_daily_intake([self.record(),other],"2026-01-02")

    def test_29_invalid_source_rejected(self):
        with self.assertRaises(DietValidationError): self.record(food_source_type="unknown")

    def test_30_manual_source_is_unverified(self): self.assertEqual(self.record().nutrient_data_status,"complete_unverified")

    def test_31_unverified_cannot_claim_approved(self):
        with self.assertRaises(DietValidationError): self.record(food_source_type="approved_catalog")

    def test_32_usda_staging_cannot_claim_approved(self):
        with self.assertRaises(DietValidationError): self.record(food_source_type="usda_staging")

    def test_33_database_initialization(self): validate_database(self.diet)

    def test_34_application_id(self):
        with sqlite3.connect(self.diet) as db:self.assertEqual(db.execute("PRAGMA application_id").fetchone()[0],APPLICATION_ID)

    def test_35_user_version(self):
        with sqlite3.connect(self.diet) as db:self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0],1)

    def test_36_schema_damage_rejected(self):
        with sqlite3.connect(self.diet) as db:db.execute("ALTER TABLE diet_records ADD COLUMN extra TEXT")
        with self.assertRaises(ValueError):validate_database(self.diet)

    def test_37_insert_failure_rolls_back(self):
        def fail():raise RuntimeError("TEST_ONLY")
        with self.assertRaises(RuntimeError):insert_record(self.diet,self.profiles,self.record(),failure_hook=fail)
        self.assertIsNone(get_record(self.diet,self.profiles,"TEST_ONLY_record",profile_id=self.profile.profile_id))

    def test_38_update_failure_rolls_back(self):
        item=self.record();insert_record(self.diet,self.profiles,item)
        def fail():raise RuntimeError("TEST_ONLY")
        with self.assertRaises(RuntimeError):update_record(self.diet,self.profiles,replace(item,amount_g=50),expected_updated_at=NOW,now=LATER,failure_hook=fail)
        self.assertEqual(get_record(self.diet,self.profiles,item.record_id,profile_id=item.profile_id).amount_g,100)

    def test_39_optimistic_conflict(self):
        item=self.record();insert_record(self.diet,self.profiles,item)
        update_record(self.diet,self.profiles,replace(item,amount_g=50),expected_updated_at=NOW,now=LATER)
        with self.assertRaises(ValueError):update_record(self.diet,self.profiles,item,expected_updated_at=NOW,now="2026-01-01T10:00:00.000000Z")

    def test_40_revoke_not_delete(self):
        item=self.record();insert_record(self.diet,self.profiles,item);revoke_record(self.diet,self.profiles,item.record_id,profile_id=item.profile_id,expected_updated_at=NOW,now=LATER)
        stored=get_record(self.diet,self.profiles,item.record_id,profile_id=item.profile_id)
        self.assertEqual(stored.record_status,"revoked")

    def test_41_corrupt_stored_record_rejected(self):
        item=self.record();insert_record(self.diet,self.profiles,item)
        with sqlite3.connect(self.diet) as db:db.execute("UPDATE diet_records SET source_snapshot_sha256=?",("0"*64,))
        with self.assertRaises(DietValidationError):get_record(self.diet,self.profiles,item.record_id,profile_id=item.profile_id)

    def test_42_nonexistent_profile_rejected(self):
        with self.assertRaises(DietValidationError):insert_record(self.diet,self.profiles,self.record(profile_id="TEST_ONLY_missing"))

    def test_43_target_integration(self):
        result=analyze_daily_nutrition(self.profile,[self.record()],"2026-01-02")
        self.assertEqual(result["targets"]["target_status"],"estimated_general_adult_reference")

    def test_44_manual_review_blocks_analysis(self):
        extreme=HealthProfile.create(profile_id="TEST_ONLY_extreme",age=18,height_cm=250,weight_kg=350,
            sex_for_equation="male_parameter",activity_level="extra_active",health_goal="maintain_weight",now=NOW)
        with self.assertRaises(ManualReviewRequired):analyze_daily_nutrition(extreme,[],"2026-01-02")

    def test_45_complete_gap_calculation(self):
        result=analyze_daily_nutrition(self.profile,[self.record()],"2026-01-02")
        self.assertEqual(result["gaps"]["data_completeness"],"complete")

    def test_46_partial_unknown_cannot_create_precise_gap(self):
        result=analyze_daily_nutrition(self.profile,[self.record(protein_g_per_100g=None)],"2026-01-02")
        self.assertIsNone(result["intake"]["protein_g"]["total"]);self.assertIsNone(result["gaps"]["nutrients"]["protein"]["difference"])

    def test_47_all_unknown_intake(self):
        row=self.record(energy_kcal_per_100g=None,energy_definition=None,protein_g_per_100g=None,fat_g_per_100g=None,carbohydrates_g_per_100g=None)
        result=calculate_daily_intake([row],"2026-01-02",profile_id=self.profile.profile_id)
        self.assertTrue(all(result[name]["data_status"]=="unknown" for name in ("energy_kcal","protein_g","fat_g","carbohydrates_g")))

    def test_48_complete_intake(self):
        result=calculate_daily_intake([self.record()],"2026-01-02",profile_id=self.profile.profile_id)
        self.assertTrue(all(result[name]["data_status"]=="complete" for name in ("energy_kcal","protein_g","fat_g","carbohydrates_g")))

    def test_49_snapshot_not_changed_by_source_object(self):
        source={"energy":100};item=self.record(energy_kcal_per_100g=source["energy"]);source["energy"]=999
        self.assertEqual(scale_record_nutrients(item)["nutrients"]["energy_kcal"],100)

    def test_50_no_public_diet_http_crud(self):
        self.assertFalse(any("diet" in rule.rule or "profile" in rule.rule for rule in main.app.url_map.iter_rules()))

    def test_51_database_is_not_admin_database(self): self.assertNotEqual(APPLICATION_ID,ADMIN_APP_ID)

    def test_52_profile_mismatch_analysis_rejected(self):
        with self.assertRaises(DietValidationError):analyze_daily_nutrition(self.profile,[self.record(profile_id="TEST_ONLY_other")],"2026-01-02")

    def test_53_list_records_scoped_by_date(self):
        insert_record(self.diet,self.profiles,self.record());insert_record(self.diet,self.profiles,self.record(record_id="TEST_ONLY_other",meal_date="2026-01-03"))
        self.assertEqual(len(list_records(self.diet,self.profiles,profile_id=self.profile.profile_id,meal_date="2026-01-02")),1)

    def test_54_update_cannot_change_snapshot(self):
        item=self.record();insert_record(self.diet,self.profiles,item)
        changed=self.record(energy_kcal_per_100g=999)
        with self.assertRaises(ValueError):update_record(self.diet,self.profiles,changed,expected_updated_at=NOW,now=LATER)

    def test_55_partial_total_keeps_known_subtotal(self):
        rows=[self.record(record_id="TEST_ONLY_1"),self.record(record_id="TEST_ONLY_2",energy_kcal_per_100g=None,energy_definition=None)]
        result=calculate_daily_intake(rows,"2026-01-02",profile_id=self.profile.profile_id)["energy_kcal"]
        self.assertEqual(result["data_status"],"partial");self.assertIsNone(result["total"]);self.assertEqual(result["known_subtotal"],100)

    def test_56_revoked_record_excluded_from_aggregation(self):
        item=replace(self.record(),record_status="revoked")
        result=calculate_daily_intake([item],"2026-01-02",profile_id=self.profile.profile_id)
        self.assertEqual(result["record_count"],0)

    def test_57_manual_source_counted(self):
        result=calculate_daily_intake([self.record()],"2026-01-02",profile_id=self.profile.profile_id)
        self.assertEqual(result["unverified_source_record_count"],1);self.assertEqual(result["source_trust_status"],"unverified_sources_present")

    def test_58_snapshot_digest_stable(self):
        first=self.record();second=self.record(record_id="TEST_ONLY_second",amount_g=50)
        self.assertEqual(first.source_snapshot_sha256,second.source_snapshot_sha256)

    def test_59_symlink_database_rejected(self):
        link=self.root/"link.sqlite";link.symlink_to(self.diet)
        with self.assertRaises(FileExistsError):initialize_database(link)

    def test_60_existing_database_refuses_overwrite(self):
        before=self.diet.read_bytes()
        with self.assertRaises(FileExistsError):initialize_database(self.diet)
        self.assertEqual(self.diet.read_bytes(),before)

    def test_61_corrupt_stored_timestamp_rejected(self):
        item=self.record();insert_record(self.diet,self.profiles,item)
        with sqlite3.connect(self.diet) as db:db.execute("UPDATE diet_records SET updated_at_utc='bad-time'")
        with self.assertRaises(DietValidationError):get_record(self.diet,self.profiles,item.record_id,profile_id=item.profile_id)

    def test_62_corrupt_stored_meal_type_rejected(self):
        item=self.record();insert_record(self.diet,self.profiles,item)
        with sqlite3.connect(self.diet) as db:
            db.execute("PRAGMA ignore_check_constraints=ON");db.execute("UPDATE diet_records SET meal_type='invalid'")
        with self.assertRaises(DietValidationError):get_record(self.diet,self.profiles,item.record_id,profile_id=item.profile_id)

    def test_63_invalid_revoke_timestamp_rejected_without_write(self):
        item=self.record();insert_record(self.diet,self.profiles,item)
        with self.assertRaises(DietValidationError):revoke_record(self.diet,self.profiles,item.record_id,profile_id=item.profile_id,expected_updated_at=NOW,now="bad")
        self.assertEqual(get_record(self.diet,self.profiles,item.record_id,profile_id=item.profile_id).record_status,"active")

    def test_64_energy_definition_required_and_snapshotted(self):
        with self.assertRaises(DietValidationError):self.record(energy_definition=None)
        item=self.record();self.assertIn("TEST_ONLY label kcal",item.energy_definition)


if __name__=="__main__":unittest.main()
