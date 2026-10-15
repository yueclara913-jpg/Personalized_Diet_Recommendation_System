"""TEST_ONLY artificial fixtures; no real USDA measurements or business databases."""
import copy
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import os
import tempfile
import unittest
from unittest.mock import patch

from nutrition.importers import usda_sqlite as importer
from nutrition.sources.usda_foundation import parse_snapshot

ROOT=Path(__file__).resolve().parents[1]


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.db=self.root/'TEST_ONLY.sqlite'
        self.source=self.root/'TEST_ONLY.json'
        self.food={'fdcId':1,'description':'TEST_ONLY artificial food','dataType':'Foundation',
                   'foodNutrients':[{'nutrient':{'id':1003,'name':'TEST_ONLY protein','unitName':'g'},'amount':0}]}
        self.payload=self.parse()

    def parse(self,foods=None,version='TEST_ONLY_v1',basis='per_100g'):
        self.source.write_text(json.dumps({'FoundationFoods':foods or [self.food]}),encoding='utf-8')
        return parse_snapshot(self.source,source_version=version,source_url='https://fdc.nal.usda.gov/TEST_ONLY',
                              nutrition_basis=basis,basis_evidence='TEST_ONLY explicit premise' if basis=='per_100g' else None)

    def run_import(self,payload=None,**kwargs):
        return importer.import_payload(payload or self.payload,mode='staging',database=self.db,**kwargs)

    def test_normal_pending_and_trace(self):
        report=self.run_import()
        self.assertEqual(report['inserted'],1)
        with sqlite3.connect(self.db) as db:
            row=db.execute('SELECT record_json,review_status FROM staging_records').fetchone()
            self.assertEqual(json.loads(row[0]),self.payload['records'][0])
            self.assertEqual(row[1],'pending')
            self.assertEqual(db.execute('SELECT source_sha256 FROM staging_releases').fetchone()[0],self.payload['source_file_sha256'])
            self.assertEqual(db.execute("SELECT count(*) FROM sqlite_master WHERE name='foods'").fetchone()[0],0)

    def test_dry_run_no_database(self):
        importer.import_payload(self.payload,database=self.db)
        self.assertFalse(self.db.exists())
        self.run_import();before=self.db.read_bytes()
        importer.import_payload(self.payload,database=self.db)
        self.assertEqual(before,self.db.read_bytes())

    def test_null_and_zero(self):
        food=copy.deepcopy(self.food)
        food['foodNutrients'].append({'nutrient':{'id':1004,'name':'TEST_ONLY fat','unitName':'g'}})
        payload=self.parse([food]);self.run_import(payload)
        record=payload['records'][0]
        self.assertEqual(record['standardized']['protein'],'0.000000')
        self.assertIsNone(record['standardized']['fat'])

    def test_unknown_unit_pending(self):
        self.food['foodNutrients'][0]['nutrient']['unitName']='TEST_ONLY_UNKNOWN'
        self.assertEqual(self.run_import(self.parse())['anomaly_records'],1)

    def test_unknown_basis_pending(self):
        report=self.run_import(self.parse(basis='unknown'))
        self.assertIn('unverified_basis',report['findings'][0]['issues'])

    def test_invalid_id(self):
        for invalid in (False,-1,None,2**64):
            with self.subTest(value=invalid):
                self.food['fdcId']=invalid
                with self.assertRaises(ValueError):self.run_import(self.parse())
        self.assertFalse(self.db.exists())

    def test_duplicate_food(self):
        with self.assertRaises(ValueError):self.run_import(self.parse([self.food,self.food]))

    def test_duplicate_nutrient(self):
        self.food['foodNutrients']*=2
        with self.assertRaises(ValueError):self.run_import(self.parse())

    def test_invalid_nutrient_id(self):
        self.food['foodNutrients'][0]['nutrient']['id']=False
        with self.assertRaises(ValueError):self.run_import(self.parse())

    def test_repeat_batch(self):
        self.run_import()
        report=self.run_import(confirm_existing=True)
        self.assertEqual((report['inserted'],report['skipped']),(0,1))

    def test_existing_confirmation_required(self):
        self.run_import()
        with self.assertRaises(ValueError):self.run_import()

    def test_different_versions(self):
        self.run_import()
        self.run_import(self.parse(version='TEST_ONLY_v2'),confirm_existing=True)
        with sqlite3.connect(self.db) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM staging_records').fetchone()[0],2)

    def test_same_version_hash_conflict(self):
        self.run_import();self.food['description']='TEST_ONLY changed'
        with self.assertRaises(ValueError):self.run_import(self.parse(),confirm_existing=True)
        with sqlite3.connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM staging_records').fetchone()[0],1)

    def test_rollback_after_first_insert(self):
        second=copy.deepcopy(self.food);second['fdcId']=2
        payload=self.parse([self.food,second]);original=importer.digest
        def failure(value):
            if isinstance(value,dict) and value.get('source_food_id')==2:
                raise sqlite3.IntegrityError('TEST_ONLY injected insert-time constraint failure')
            return original(value)
        with patch.object(importer,'digest',side_effect=failure),self.assertRaises(sqlite3.IntegrityError):self.run_import(payload)
        self.assertFalse(self.db.exists())

    def test_existing_transaction_rollback(self):
        self.run_import();payload=copy.deepcopy(self.payload)
        second=copy.deepcopy(self.food);second['fdcId']=2
        # Keep release provenance unchanged to model a parsed subset of the same snapshot.
        full=self.parse([self.food,second]);full['source_file_sha256']=payload['source_file_sha256']
        for record in full['records']:record['source_file_sha256']=payload['source_file_sha256']
        original=importer.digest
        def failure(value):
            if isinstance(value,dict) and value.get('source_food_id')==2:raise sqlite3.IntegrityError('TEST_ONLY')
            return original(value)
        with patch.object(importer,'digest',side_effect=failure),self.assertRaises(sqlite3.IntegrityError):self.run_import(full,confirm_existing=True)
        with sqlite3.connect(self.db) as db:self.assertEqual(db.execute('SELECT count(*) FROM staging_records').fetchone()[0],1)

    def test_energy_and_sugar_preserved(self):
        for nid,unit in ((1008,'kcal'),(2047,'kcal'),(2048,'kcal'),(1063,'g'),(2000,'g')):
            self.food['foodNutrients'].append({'nutrient':{'id':nid,'name':'TEST_ONLY','unitName':unit},'amount':1})
        payload=self.parse();report=self.run_import(payload)
        self.assertIn('multiple_energy_definitions',report['findings'][0]['issues'])
        self.assertIn('multiple_sugar_definitions',report['findings'][0]['issues'])
        self.assertIsNone(payload['records'][0]['standardized']['calories'])
        self.assertIsNone(payload['records'][0]['standardized']['sugar'])
        with sqlite3.connect(self.db) as db:self.assertEqual(json.loads(db.execute('SELECT record_json FROM staging_records').fetchone()[0]),payload['records'][0])

    def test_approved_constraint(self):
        self.run_import()
        with sqlite3.connect(self.db) as db,self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE staging_records SET review_status='approved'")

    def test_input_unchanged(self):
        before=copy.deepcopy(self.payload);raw=self.source.read_bytes()
        self.run_import();self.assertEqual(before,self.payload);self.assertEqual(raw,self.source.read_bytes())

    def test_tampered_derived_value(self):
        self.payload['records'][0]['standardized']['protein']='999'
        with self.assertRaises(ValueError):self.run_import()

    def test_parser_and_mapping_version(self):
        for field in ('parser_version','mapping_version'):
            payload=copy.deepcopy(self.payload);payload[field]='TEST_ONLY_wrong'
            with self.assertRaises(ValueError):self.run_import(payload)

    def test_existing_non_sqlite(self):
        self.db.write_bytes(b'TEST_ONLY not SQLite');before=self.db.read_bytes()
        with self.assertRaises(ValueError):self.run_import(confirm_existing=True)
        self.assertEqual(before,self.db.read_bytes())

    def test_refuse_business_database(self):
        spec=importlib.util.spec_from_file_location('initdb',ROOT/'scripts/init_nutrition_db.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        module.initialize_database(self.db).close();before=self.db.read_bytes()
        with self.assertRaises(ValueError):self.run_import(confirm_existing=True)
        self.assertEqual(before,self.db.read_bytes())

    def test_explicit_target(self):
        with self.assertRaises(ValueError):importer.import_payload(self.payload,mode='staging')

    def test_invalid_value_kept_pending(self):
        self.food['foodNutrients'][0]['amount']=-1
        payload=self.parse();self.run_import(payload)
        self.assertIsNone(payload['records'][0]['standardized']['protein'])

    def test_source_and_record_metadata(self):
        for field,value in [('source_url','https://example.com/x'),('review_status','approved'),('source_locator','wrong')]:
            payload=copy.deepcopy(self.payload);payload['records'][0][field]=value
            with self.assertRaises(ValueError):self.run_import(payload)

    def test_foreign_key_and_pending_constraints(self):
        self.run_import()
        with sqlite3.connect(self.db) as db:
            db.execute('PRAGMA foreign_keys=ON')
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("INSERT INTO staging_records VALUES ('missing',2,'x','{}','x',0,'pending')")

    def test_same_identity_content_conflict(self):
        self.run_import()
        self.food['description']='TEST_ONLY different content'
        payload=self.parse();payload['source_file_sha256']=self.payload['source_file_sha256']
        payload['records'][0]['source_file_sha256']=self.payload['source_file_sha256']
        with self.assertRaises(ValueError):self.run_import(payload,confirm_existing=True)

    def test_cli_dry_run_and_rejection(self):
        self.source.write_text(json.dumps(self.payload))
        command=[sys.executable,str(ROOT/'scripts/import_usda_staging.py'),str(self.source)]
        env=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run(command,capture_output=True,text=True,env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['mode'],'dry-run')
        result=subprocess.run(command+['--mode','staging'],capture_output=True,text=True,env=env)
        self.assertEqual(result.returncode,2)
        self.assertEqual(json.loads(result.stderr)['status'],'rejected')

    def test_read_payload_duplicate_keys(self):
        self.source.write_text('{"x":1,"x":2}')
        with self.assertRaises(ValueError):importer.read_payload(self.source)


if __name__=='__main__':unittest.main()
