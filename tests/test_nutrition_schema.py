"""All fixtures are synthetic TEST_ONLY data, never real food measurements."""
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('init_nutrition_db', ROOT / 'scripts/init_nutrition_db.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NutritionSchemaTests(unittest.TestCase):
    def setUp(self):
        self.db = module.initialize_database(':memory:')
        self.db.execute("INSERT INTO data_sources VALUES ('TEST_ONLY','人工测试来源','TEST_ONLY','test://source','TEST_ONLY',NULL,'unknown')")
        self.db.execute("INSERT INTO source_releases VALUES ('r','TEST_ONLY','TEST_ONLY_v1',NULL,'2026-01-01',?,'test://fixture')", ('a' * 64,))
        self.db.execute("INSERT INTO foods(food_id,food_name) VALUES ('f','TEST_ONLY 人工食品')")
        self.profile()
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def profile(self, **changes):
        values = dict(profile_id='p', food_id='f', release_id='r', source_food_id='TEST_ONLY_1',
                      source_data_type='TEST_ONLY', source_locator='test://fixture/1',
                      nutrition_basis='per_100g', basis_quantity=100, basis_unit='g',
                      review_status='pending', value_origin='analysis', mapping_version='TEST_ONLY_v1')
        values.update(changes)
        self.db.execute(f"INSERT INTO food_profiles ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))

    def nutrient(self, value, **changes):
        values = dict(profile_id='p', nutrient_code='protein', unit='g', standardized_value=value,
                      raw_value=None if value is None else str(value), raw_unit='g',
                      value_qualifier='missing' if value is None else 'measured', conversion_rule='identity')
        values.update(changes)
        self.db.execute(f"INSERT INTO food_nutrients ({','.join(values)}) VALUES ({','.join('?' for _ in values)})", tuple(values.values()))

    def test_creation(self):
        tables = self.db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        self.assertEqual(len(tables), 11)
        self.assertEqual(self.db.execute('SELECT count(*) FROM nutrient_definitions').fetchone()[0], 8)
        self.assertEqual(self.db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_repeat_initialization_preserves_records(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'test.sqlite'
            db = module.initialize_database(path)
            db.execute("INSERT INTO foods(food_id,food_name) VALUES ('TEST_ONLY','TEST_ONLY')")
            db.commit()
            db.close()
            db = module.initialize_database(path)
            self.assertEqual(db.execute('SELECT count(*) FROM foods').fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            db.close()

    def test_standalone_sql_repeat(self):
        db = module.connect_database(':memory:')
        try:
            sql = module.SCHEMA.read_text(encoding='utf-8')
            db.executescript(sql)
            db.executescript(sql)
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM nutrient_definitions').fetchone()[0], 8)
            self.assertEqual(db.execute('SELECT count(*) FROM foods').fetchone()[0], 0)
        finally:
            db.close()

    def test_unknown_schema_version_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite'
            db = module.initialize_database(path)
            db.execute('PRAGMA user_version=2')
            db.close()
            with self.assertRaises(ValueError):
                module.initialize_database(path)
            db = module.connect_database(path)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 2)
            db.close()

    def test_volume_basis_and_portion_boundaries(self):
        self.profile(profile_id='volume', source_food_id='volume', nutrition_basis='per_100ml', basis_quantity=100, basis_unit='ml')
        for quantity, weight in [(0, None), (-1, None), (1, 0), (1, 'bad'), (1, float('inf'))]:
            with self.subTest(quantity=quantity, weight=weight), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute("INSERT INTO food_portions VALUES ('portion','volume','TEST_ONLY',?,'ml',?,'test://portion','pending')", (quantity, weight))
        self.db.execute("INSERT INTO food_portions VALUES ('portion','volume','TEST_ONLY',1,'ml',NULL,'test://portion','pending')")
        self.assertIsNone(self.db.execute('SELECT serving_weight_g FROM food_portions').fetchone()[0])

    def test_unrelated_database_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'other.sqlite'
            db = module.connect_database(path)
            db.execute('CREATE TABLE other(value TEXT)')
            db.execute("INSERT INTO other VALUES ('TEST_ONLY')")
            db.commit()
            db.close()
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                module.initialize_database(path)
            self.assertEqual(path.read_bytes(), original)

    def test_conflicting_definition_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite'
            db = module.initialize_database(path)
            db.execute("UPDATE nutrient_definitions SET unit='mg' WHERE nutrient_code='protein'")
            db.commit()
            db.close()
            original = path.read_bytes()
            with self.assertRaises(ValueError):
                module.initialize_database(path)
            self.assertEqual(path.read_bytes(), original)

    def test_blank_provenance_rejected(self):
        for field in ('raw_value','raw_unit','conversion_rule'):
            with self.subTest(field=field), self.assertRaises(sqlite3.IntegrityError):
                self.nutrient(0, **{field: '   '})

    def test_separate_connections_enable_foreign_keys(self):
        for _ in range(2):
            db = module.connect_database(':memory:')
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            db.close()

    def test_profiles_in_different_versions_remain_separate(self):
        self.db.execute("INSERT INTO source_releases VALUES ('r2','TEST_ONLY','TEST_ONLY_v2',NULL,'2026',?,'test://fixture')", ('c'*64,))
        self.profile(profile_id='p2', release_id='r2')
        self.nutrient(0)
        self.nutrient(None, profile_id='p2')
        self.assertEqual(self.db.execute('SELECT profile_id,standardized_value FROM food_nutrients ORDER BY profile_id').fetchall(), [('p',0.0),('p2',None)])

    def test_foreign_key(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.profile(profile_id='p2', food_id='absent', source_food_id='2')

    def test_required_name(self):
        for name in (None, '', '   '):
            with self.subTest(name=name), self.assertRaises(sqlite3.IntegrityError):
                self.db.execute('INSERT INTO foods(food_id,food_name) VALUES (?,?)', ('new', name))

    def test_invalid_basis(self):
        for basis, quantity, unit in [('per_kg', 1, 'kg'), ('per_100g', None, 'g'), ('per_100g', 100, None),
                                     ('per_100g', 1, 'g'), ('per_100ml', 100, 'g'), ('unknown', 100, 'g')]:
            with self.subTest(basis=basis, quantity=quantity, unit=unit), self.assertRaises(sqlite3.IntegrityError):
                self.profile(profile_id='new', source_food_id='new', nutrition_basis=basis, basis_quantity=quantity, basis_unit=unit)

    def test_invalid_review_status(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE food_profiles SET review_status='verified' WHERE profile_id='p'")

    def test_same_name_different_id(self):
        self.db.execute("INSERT INTO foods(food_id,food_name) SELECT 'f2',food_name FROM foods WHERE food_id='f'")
        self.assertEqual(self.db.execute('SELECT count(*) FROM foods').fetchone()[0], 2)

    def test_null_and_zero_distinct(self):
        self.nutrient(None)
        self.nutrient(0, nutrient_code='fat')
        self.assertEqual(self.db.execute('SELECT standardized_value FROM food_nutrients ORDER BY nutrient_code').fetchall(), [(0.0,), (None,)])

    def test_unit_mismatch(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.nutrient(0, unit='mg')

    def test_raw_provenance_required(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.nutrient(0, raw_value=None)

    def test_invalid_numeric_values(self):
        for value in (-1, float('inf'), 'not_numeric', 1e13):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                self.nutrient(value)

    def test_missing_not_zero(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.nutrient(0, value_qualifier='missing')

    def test_transaction_rollback(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db:
                self.db.execute("INSERT INTO foods(food_id,food_name) VALUES ('rollback','TEST_ONLY')")
                self.profile(profile_id='bad', food_id='missing', source_food_id='bad')
        self.assertEqual(self.db.execute("SELECT count(*) FROM foods WHERE food_id='rollback'").fetchone()[0], 0)

    def test_ocr_isolation_and_revocation(self):
        self.db.execute("INSERT INTO ocr_imports VALUES ('ocr','r','test://image',?,'TEST_ONLY text','TEST_ONLY','v1',0.99,'pending','2026-01-01')", ('b'*64,))
        self.profile(profile_id='ocr_p', source_food_id='ocr', value_origin='ocr_label', ocr_import_id='ocr', review_status='approved')
        self.assertEqual(self.db.execute('SELECT count(*) FROM reviewed_food_profiles').fetchone()[0], 0)
        self.db.execute("UPDATE ocr_imports SET review_status='approved'")
        self.assertEqual(self.db.execute('SELECT count(*) FROM reviewed_food_profiles').fetchone()[0], 1)
        self.db.execute("UPDATE ocr_imports SET review_status='rejected'")
        self.assertEqual(self.db.execute('SELECT count(*) FROM reviewed_food_profiles').fetchone()[0], 0)

    def test_ocr_requires_link(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.profile(profile_id='bad', source_food_id='bad', value_origin='ocr_label')

    def test_ocr_confidence_and_source(self):
        for confidence in (-0.1, 1.1):
            with self.assertRaises(sqlite3.IntegrityError):
                self.db.execute("INSERT INTO ocr_imports VALUES ('o','r','test://image',?,'TEST_ONLY','test','v1',?,'pending','2026')", ('b'*64, confidence))
        self.db.execute("INSERT INTO source_releases VALUES ('r2','TEST_ONLY','v2',NULL,'2026',?,'test://fixture')", ('c'*64,))
        self.db.execute("INSERT INTO ocr_imports VALUES ('o','r2','test://image',?,'TEST_ONLY','test','v1',NULL,'pending','2026')", ('b'*64,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.profile(profile_id='bad', source_food_id='bad', value_origin='ocr_label', ocr_import_id='o')

    def test_unknown_basis_excluded(self):
        self.db.execute("UPDATE food_profiles SET nutrition_basis='unknown',basis_quantity=NULL,basis_unit=NULL,review_status='approved'")
        self.assertEqual(self.db.execute('SELECT count(*) FROM reviewed_food_profiles').fetchone()[0], 0)

    def test_per_serving_separate_profile(self):
        self.profile(profile_id='serving', source_food_id='serving', nutrition_basis='per_serving', basis_quantity=1, basis_unit='serving')
        self.assertEqual(self.db.execute('SELECT count(*) FROM food_profiles').fetchone()[0], 2)

    def test_initialization_failure_atomic(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'db.sqlite'
            bad = Path(directory) / 'bad.sql'
            db = module.initialize_database(path)
            db.close()
            bad.write_text('CREATE TABLE test_partial(id INTEGER); INVALID SQL;', encoding='utf-8')
            with self.assertRaises(sqlite3.Error):
                module.initialize_database(path, schema_path=bad)
            db = module.connect_database(path)
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='test_partial'").fetchone()[0], 0)
            db.close()

    def test_protect_non_database_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'food.csv'
            original = b'TEST_ONLY,not_a_database\n'
            path.write_bytes(original)
            with self.assertRaises(ValueError):
                module.initialize_database(path)
            self.assertEqual(path.read_bytes(), original)

    def test_hash_and_source_uniqueness(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("UPDATE source_releases SET source_file_sha256='not_a_hash'")
        with self.assertRaises(sqlite3.IntegrityError):
            self.profile(profile_id='duplicate')

    def test_review_target_and_delete_protection(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("INSERT INTO review_events VALUES ('e',NULL,NULL,'approved','TEST_ONLY','2026','TEST_ONLY')")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute("DELETE FROM source_releases WHERE release_id='r'")


if __name__ == '__main__':
    unittest.main()
