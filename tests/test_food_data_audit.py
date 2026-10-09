import csv
import io
import json
import tempfile
import unittest
from decimal import localcontext
from unittest.mock import patch
from pathlib import Path
from scripts.audit_food_data import FIELDS, METADATA_FIELDS, REPORT_FILES, audit_bytes, audit_file, sha256


def fixture(**changes):
    row = dict(zip(FIELDS, ['Test Food', 'Protein', '100', '4', '10', '1', '6', '1', '2', 'Lunch', '10', 'True', 'False']))
    row.update(changes)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=FIELDS)
    writer.writeheader(); writer.writerow(row)
    return stream.getvalue().encode()


def rules(raw):
    return {a['rule_id'] for a in audit_bytes(raw)[1]}


class AuditTests(unittest.TestCase):
    def test_extreme_precision_and_context_independence(self):
        for value in ['1e999999', '1e-999999', '9' * 101]:
            self.assertIn('NUMERIC_PRECISION_LIMIT', rules(fixture(Protein=value)))
        raw = fixture(Protein='15.000000000000000000000000000001', Fats='0', Carbohydrates='15', Fibre='0')
        with localcontext() as context:
            context.prec = 3
            self.assertIn('ENERGY_449_DEVIATION', rules(raw))

    def test_zero_calories_with_other_invalid_fields(self):
        result = rules(fixture(Calories='0', Protein='bad'))
        self.assertIn('ZERO_CALORIES', result)
        self.assertNotIn('ENERGY_449_DEVIATION', result)
        self.assertNotIn('ENERGY_FIBRE_SCREEN', result)

    def test_energy_wide_tolerance_exact_boundary(self):
        self.assertNotIn('ENERGY_FIBRE_SCREEN', rules(fixture(Protein='37.5', Fats='0', Carbohydrates='0', Fibre='0')))
        self.assertIn('ENERGY_FIBRE_SCREEN', rules(fixture(Protein='37.5001', Fats='0', Carbohydrates='0', Fibre='0')))

    def test_invalid_csv_or_encoding_never_changes_source(self):
        for raw in [b'\xff', b'Food_items\n"unfinished']:
            with tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / 'food_data.csv'; source.write_bytes(raw)
                with self.assertRaises((UnicodeDecodeError, csv.Error)):
                    audit_file(source, Path(folder) / 'output')
                self.assertEqual(source.read_bytes(), raw)

    def test_exclusive_summary_creation_blocks_race(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'food_data.csv'; raw = fixture(); source.write_bytes(raw)
            output = Path(folder) / 'output'; output.mkdir()
            original_open = Path.open
            def racing_open(target, *args, **kwargs):
                if target == output / 'summary.json' and args and args[0] == 'x':
                    target.symlink_to(source)
                return original_open(target, *args, **kwargs)
            with patch.object(Path, 'open', racing_open):
                with self.assertRaises(FileExistsError): audit_file(source, output)
            self.assertEqual(source.read_bytes(), raw)

    def test_empty_metadata_schema_is_stable(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'empty.csv'; source.write_bytes(b'')
            output = Path(folder) / 'output'; audit_file(source, output)
            with (output / 'food_metadata.csv').open(encoding='utf-8') as stream:
                self.assertEqual(next(csv.reader(stream)), METADATA_FIELDS)

    def test_committed_candidate_reports_match_all_source_records(self):
        root = Path(__file__).resolve().parents[1]
        raw = (root / 'food_data.csv').read_bytes()
        source_rows = list(csv.DictReader(io.StringIO(raw.decode())))
        directory = root / 'reports/food_quality/v1'
        expected, anomalies, metadata = audit_bytes(raw)
        saved = json.loads((directory / 'summary.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['anomaly_count'], 311)
        self.assertEqual(saved['rule_counts'], expected['rule_counts'])
        self.assertNotIn('source_path', saved)
        with (directory / 'anomalies.csv').open(encoding='utf-8') as stream:
            actual = list(csv.DictReader(stream))
        self.assertEqual(len(actual), len(anomalies))
        for row, expected_row in zip(actual, anomalies):
            for field, value in expected_row.items():
                self.assertEqual(json.loads(row[field]) if field == 'original_values' else row[field],
                                 value if field == 'original_values' else str(value))
            original = source_rows[int(row['csv_line']) - 2]
            self.assertEqual(row['food_name'], original['Food_items'])
            for field, value in json.loads(row['original_values']).items():
                self.assertEqual(value, original[field])
        with (directory / 'food_metadata.csv').open(encoding='utf-8') as stream:
            actual_metadata = list(csv.DictReader(stream))
        self.assertEqual(len(actual_metadata), 99)
        for row, expected_row in zip(actual_metadata, metadata):
            self.assertEqual(row, {field: str(value) for field, value in expected_row.items()})
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / 'first', Path(folder) / 'second'
            audit_file(root / 'food_data.csv', first); audit_file(root / 'food_data.csv', second)
            for filename in REPORT_FILES:
                self.assertNotIn(b'\r\n', (first / filename).read_bytes())
                self.assertEqual((first / filename).read_bytes(), (second / filename).read_bytes())
                self.assertEqual((first / filename).read_bytes(), (directory / filename).read_bytes())

    def test_unknown_units_never_claim_nutrient_error(self):
        summary, anomalies, metadata = audit_bytes(fixture(Sugar='20', Fats='30'))
        self.assertIn('SUGAR_GT_CARBS', rules(fixture(Sugar='20')))
        self.assertTrue(all(a['review_status'] == 'needs_review' for a in anomalies))
        self.assertEqual(metadata[0]['nutrition_basis'], '')
        self.assertEqual(metadata[0]['data_source'], '')
        self.assertIn('ocr_confidence', metadata[0])
        self.assertFalse(summary['nutrient_values_modified'])

    def test_sugar_boundary_and_rounding(self):
        self.assertNotIn('SUGAR_GT_CARBS', rules(fixture(Sugar='6')))
        for value, risk in [('6.1', 'medium'), ('6.2', 'medium'), ('6.21', 'high')]:
            _, anomalies, _ = audit_bytes(fixture(Sugar=value))
            found = next(a for a in anomalies if a['rule_id'] == 'SUGAR_GT_CARBS')
            self.assertEqual(found['risk_level'], risk)

    def test_fat_energy_boundary(self):
        self.assertNotIn('FAT_ENERGY_GT_CALORIES', rules(fixture(Calories='90', Fats='10')))
        self.assertIn('FAT_ENERGY_GT_CALORIES', rules(fixture(Calories='89.9', Fats='10')))

    def test_energy_exact_threshold_and_fibre(self):
        # E0=120 exactly, 20% above 100: not flagged (strict >).
        self.assertNotIn('ENERGY_449_DEVIATION', rules(fixture(Protein='15', Fats='0', Carbohydrates='15', Fibre='0')))
        self.assertIn('ENERGY_449_DEVIATION', rules(fixture(Protein='15.1', Fats='0', Carbohydrates='15', Fibre='0')))
        # E0=160, fibre-adjusted lower bound=100: no strong flag.
        self.assertNotIn('ENERGY_FIBRE_SCREEN', rules(fixture(Protein='20', Fats='0', Carbohydrates='20', Fibre='15')))
        self.assertIn('ENERGY_FIBRE_SCREEN', rules(fixture(Protein='50', Fats='0', Carbohydrates='20', Fibre='0')))

    def test_invalid_missing_negative_and_zero(self):
        for value in ['abc', 'NaN', 'Infinity', '-Infinity']:
            self.assertIn('INVALID_NUMBER', rules(fixture(Protein=value)))
        self.assertIn('MISSING_VALUE', rules(fixture(Protein='')))
        self.assertIn('NEGATIVE_NUMBER', rules(fixture(Protein='-1')))
        self.assertIn('ZERO_CALORIES', rules(fixture(Calories='0')))
        self.assertIn('INVALID_BOOLEAN', rules(fixture(Vegan='yes')))

    def test_duplicates_and_multiline_physical_lines(self):
        raw = fixture(Food_items='Two\nLines')
        header, row = list(csv.reader(io.StringIO(raw.decode())))
        out = io.StringIO(newline=''); writer = csv.writer(out)
        writer.writerow(header); writer.writerow(row); writer.writerow(row)
        _, anomalies, metadata = audit_bytes(out.getvalue().encode())
        duplicate = next(a for a in anomalies if a['rule_id'] == 'DUPLICATE_RECORD')
        self.assertEqual(duplicate['csv_line'], 4)
        self.assertEqual(duplicate['original_values']['first_line'], 2)
        self.assertEqual(metadata[1]['csv_line'], 4)
        self.assertIn('DUPLICATE_NAME', {a['rule_id'] for a in anomalies})

    def test_report_traceability_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.csv'; raw = fixture(Sugar='20')
            source.write_bytes(raw)
            output = Path(folder) / 'reports'
            summary = audit_file(source, output)
            self.assertEqual(source.read_bytes(), raw)
            self.assertEqual(summary['source_sha256'], sha256(raw))
            self.assertEqual(summary['source_sha256_after'], sha256(raw))
            with (output / 'anomalies.csv').open(encoding='utf-8') as stream:
                records = list(csv.DictReader(stream))
            for record in records:
                self.assertEqual(record['source_sha256'], sha256(raw))
                self.assertEqual(record['csv_line'], '2')
                self.assertEqual(record['record_id'], sha256(raw) + ':2')
                self.assertIsInstance(json.loads(record['original_values']), dict)
            with self.assertRaises(FileExistsError):
                audit_file(source, output)

    def test_source_overwrite_rejected_including_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'anomalies.csv'; source.write_bytes(fixture())
            with self.assertRaises(ValueError): audit_file(source, Path(folder))
            target = Path(folder) / 'other'; target.mkdir()
            (target / 'summary.json').symlink_to(source)
            with self.assertRaises(ValueError): audit_file(source, target)

    def test_schema_and_empty_file(self):
        self.assertIn('SCHEMA_MISMATCH', rules(b'A,A\n1,2\n'))
        self.assertIn('ROW_WIDTH', rules((','.join(FIELDS) + '\nx,y\n').encode()))
        self.assertEqual(audit_bytes(b'')[0]['dataset_status'], 'empty')
        self.assertEqual(audit_bytes(b'')[0]['missing_percent']['Calories'], None)

    def test_complete_repository_dataset(self):
        raw = (Path(__file__).resolve().parents[1] / 'food_data.csv').read_bytes()
        summary, _, metadata = audit_bytes(raw)
        self.assertEqual(summary['record_count'], 99)
        self.assertEqual(summary['column_count'], 13)
        for rule, count in [('SUGAR_GT_CARBS', 36), ('FAT_ENERGY_GT_CALORIES', 24),
                            ('ENERGY_449_DEVIATION', 74), ('ENERGY_FIBRE_SCREEN', 72),
                            ('FIBRE_GT_CARBS', 6), ('UNKNOWN_PROVENANCE_AND_BASIS', 99)]:
            self.assertEqual(summary['rule_counts'][rule], count)
        self.assertEqual(summary['records_with_numeric_flags'], 86)
        self.assertEqual(len(metadata), 99)
        self.assertEqual(sum(summary['missing_counts'].values()), 0)


if __name__ == '__main__':
    unittest.main()
