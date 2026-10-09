"""Synthetic TEST_ONLY fixtures, not USDA foods or measured nutrition values."""
import copy
import io
import zipfile
from unittest.mock import patch
import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import os
import unittest
from decimal import localcontext, Inexact, Rounded

from nutrition.normalization import convert_value
from nutrition.sources.usda_foundation import parse_snapshot, SnapshotError, MAPPING

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fetch_usda', ROOT/'scripts/fetch_usda_foundation.py')
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


def nutrient(nid, amount=0, unit='g'):
    return {'nutrient': {'id':nid,'name':'TEST_ONLY name not used for mapping','unitName':unit},'amount':amount}


class USDAParserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'test.json'
        self.food = {'fdcId':123,'description':'TEST_ONLY synthetic food','dataType':'Foundation',
                     'foodNutrients':[nutrient(1003)]}

    def tearDown(self):
        self.temp.cleanup()

    def parse(self, foods=None, **kw):
        self.path.write_text(json.dumps({'FoundationFoods':foods if foods is not None else [self.food]}))
        options = dict(source_version='TEST_ONLY_v1',source_url='test://synthetic',
                       nutrition_basis='per_100g',basis_evidence='TEST_ONLY fixture explicit basis')
        options.update(kw)
        return parse_snapshot(self.path, **options)

    def test_identity_version(self):
        r=self.parse()['records'][0]
        self.assertEqual((r['source_food_id'],r['source_data_type'],r['source_version']),(123,'Foundation','TEST_ONLY_v1'))
        self.assertEqual(r['review_status'],'pending')

    def test_all_confirmed_mappings(self):
        self.food['foodNutrients']=[nutrient(i,0,u) for i,(_,u) in MAPPING.items()]
        r=self.parse()['records'][0]
        self.assertEqual(len(r['nutrients']),len(MAPPING))
        self.assertTrue(all(n['status']=='converted' for n in r['nutrients']))

    def test_unknown_id(self):
        self.food['foodNutrients']=[nutrient(999999)]
        self.assertEqual(self.parse()['records'][0]['nutrients'][0]['reason'],'unknown_nutrient_id')

    def test_missing_and_zero(self):
        self.food['foodNutrients']=[nutrient(1003,None),nutrient(1004,0)]
        r=self.parse()['records'][0]['standardized']
        self.assertIsNone(r['protein'])
        self.assertEqual(r['fat'],'0.000000')
        self.assertIsNone(r['iron'])

    def test_energy_no_default_selection(self):
        self.food['foodNutrients']=[nutrient(2047,1,'kcal'),nutrient(2048,2,'kcal'),nutrient(1008,3,'kcal')]
        self.assertIsNone(self.parse()['records'][0]['standardized']['calories'])
        self.assertEqual(self.parse(energy_id=2048)['records'][0]['standardized']['calories'],'2.000000')

    def test_sugars_not_merged(self):
        self.food['foodNutrients']=[nutrient(1063,1),nutrient(2000,2)]
        self.assertIsNone(self.parse()['records'][0]['standardized']['sugar'])
        self.assertEqual(self.parse(sugar_id=2000)['records'][0]['standardized']['sugar'],'2.000000')

    def test_unverified_and_serving_basis(self):
        for kw in ({'nutrition_basis':'per_serving'},{'nutrition_basis':'per_100ml'}, {'basis_evidence':None}):
            with self.subTest(kw=kw):
                self.assertIsNone(self.parse(**kw)['records'][0]['standardized']['protein'])

    def test_unknown_unit(self):
        self.food['foodNutrients']=[nutrient(1003,1,'mystery')]
        self.assertEqual(self.parse()['records'][0]['nutrients'][0]['reason'],'unknown_unit')

    def test_raw_provenance(self):
        self.food['foodNutrients']=[nutrient(1003,1000,'mg')]
        r=self.parse()['records'][0]
        self.assertEqual(r['nutrients'][0]['raw_value'],1000)
        self.assertEqual(r['nutrients'][0]['raw_unit'],'mg')
        self.assertEqual(r['standardized']['protein'],'1.000000')
        self.assertEqual(r['source_locator'],'FoundationFoods[0]')
        self.assertEqual(r['raw_record'],self.food)
        self.assertEqual(len(r['source_file_sha256']),64)

    def test_duplicate_foods_retained(self):
        r=self.parse([self.food,copy.deepcopy(self.food)])['records']
        self.assertEqual(len(r),2)
        self.assertTrue(all('duplicate_food_id' in f['issues'] for f in r))

    def test_duplicate_nutrients_not_overwritten(self):
        self.food['foodNutrients']=[nutrient(1003,1),nutrient(1003,2)]
        self.assertIsNone(self.parse()['records'][0]['standardized']['protein'])

    def test_malformed_format(self):
        for raw in ('{','[]','{"FoundationFoods":{}}','{"FoundationFoods":[],"FoundationFoods":[]}'):
            self.path.write_text(raw)
            with self.assertRaises(SnapshotError):
                parse_snapshot(self.path,source_version='TEST_ONLY',source_url='test://fixture')

    def test_invalid_records(self):
        r=self.parse([None,{'fdcId':True,'dataType':'Branded','description':''}])['records']
        self.assertEqual(r[0]['reason'],'invalid_food_record')
        self.assertIn('unexpected_data_type',r[1]['issues'])
        self.assertIn('invalid_food_id',r[1]['issues'])

    def test_invalid_nutrient_shape(self):
        self.food['foodNutrients']=[None]
        self.assertIn('invalid_nutrient_record',self.parse()['records'][0]['issues'])

    def test_nonfinite_and_invalid(self):
        for value in ('NaN','Infinity','abc',True,-1,1e13):
            with self.subTest(value=value):
                self.food['foodNutrients']=[nutrient(1003,value)]
                self.assertIsNone(self.parse()['records'][0]['standardized']['protein'])

    def test_loq_not_true_zero(self):
        self.food['foodNutrients'][0]['loq']=0.1
        self.assertIsNone(self.parse()['records'][0]['standardized']['protein'])

    def test_repeat_and_source_unchanged(self):
        first=self.parse()
        before=self.path.read_bytes()
        second=parse_snapshot(self.path,source_version='TEST_ONLY_v1',source_url='test://synthetic',nutrition_basis='per_100g',basis_evidence='TEST_ONLY fixture explicit basis')
        self.assertEqual(first,second)
        self.assertEqual(before,self.path.read_bytes())
        json.dumps(first,allow_nan=False)

    def test_kj_kcal(self):
        self.assertEqual(self.convert('4.184','kJ','kcal')['standardized_value'],'1.000000')
        self.assertEqual(self.convert(1,'kcal','kJ')['standardized_value'],'4.184000')

    def convert(self,v,u,t):
        return convert_value(v,u,t,basis='per_100g',basis_evidence='TEST_ONLY')

    def test_g_mg(self):
        self.assertEqual(self.convert(1,'g','mg')['standardized_value'],'1000.000000')
        self.assertEqual(self.convert(1,'mg','g')['standardized_value'],'0.001000')

    def test_decimal_context_independence(self):
        expected=self.convert('1.2345675','g','g')
        with localcontext() as context:
            context.prec=2
            self.assertEqual(expected,self.convert('1.2345675','g','g'))
        self.assertEqual(expected['standardized_value'],'1.234568')

    def test_tiny_value_not_zero(self):
        self.assertEqual(self.convert('0.00000001','g','g')['reason'],'below_output_precision')

    def test_incompatible_unit(self):
        self.assertEqual(self.convert(1,'g','kcal')['reason'],'incompatible_units')

    def test_download_url_restrictions(self):
        for url in ('http://fdc.nal.usda.gov/test.zip','https://example.com/test.zip','https://fdc.nal.usda.gov/test.zip?key=secret'):
            with self.assertRaises(ValueError):
                fetch.validate_url(url)

    def test_download_repository_destination_rejected(self):
        with self.assertRaises(ValueError):
            fetch.fetch_snapshot('https://fdc.nal.usda.gov/test.zip',ROOT/'test.zip')

    def test_download_synthetic_zip_and_no_overwrite(self):
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('TEST_ONLY.json','{"FoundationFoods":[]}')
        content=buffer.getvalue()
        class Response(io.BytesIO):
            headers={}
            def geturl(self): return 'https://fdc.nal.usda.gov/test.zip'
        class Opener:
            def open(self,*args,**kwargs): return Response(content)
        path=Path(self.temp.name)/'download.zip'
        result=fetch.fetch_snapshot('https://fdc.nal.usda.gov/test.zip',path,opener=Opener())
        self.assertEqual(result['size_bytes'],len(content))
        self.assertEqual(len(result['source_file_sha256']),64)
        with self.assertRaises(FileExistsError):
            fetch.fetch_snapshot('https://fdc.nal.usda.gov/test.zip',path,opener=Opener())
        self.assertEqual(path.read_bytes(),content)

    def test_download_failure_cleanup(self):
        class Response(io.BytesIO):
            headers={}
            def geturl(self): return 'https://fdc.nal.usda.gov/test.zip'
        class Opener:
            def open(self,*args,**kwargs): return Response(b'TEST_ONLY not zip')
        path=Path(self.temp.name)/'download.zip'
        with self.assertRaises(ValueError):
            fetch.fetch_snapshot('https://fdc.nal.usda.gov/test.zip',path,opener=Opener())
        self.assertFalse(path.exists())
        with patch.object(fetch,'MAX_BYTES',1),self.assertRaises(ValueError):
            fetch.fetch_snapshot('https://fdc.nal.usda.gov/test.zip',path,opener=Opener())
        self.assertFalse(path.exists())

    def test_cli_reproducible_without_database(self):
        self.parse()
        command=[sys.executable,'-m','nutrition.sources.usda_foundation',str(self.path),
                 '--source-version','TEST_ONLY_v1','--source-url','test://synthetic']
        environment=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
        first=subprocess.check_output(command,cwd=ROOT,env=environment)
        second=subprocess.check_output(command,cwd=ROOT,env=environment)
        self.assertEqual(first,second)
        self.assertEqual(json.loads(first)['records'][0]['review_status'],'pending')
        self.assertEqual(sorted(p.name for p in Path(self.temp.name).iterdir()),['test.json'])

    def test_decimal_traps_independent(self):
        with localcontext() as context:
            context.traps[Inexact]=True
            context.traps[Rounded]=True
            self.assertEqual(self.convert('4.184','kJ','kcal')['standardized_value'],'1.000000')

    def test_nonobject_provenance(self):
        result=self.parse([None])
        record=result['records'][0]
        self.assertEqual(record['source_file_sha256'],result['source_file_sha256'])
        self.assertEqual(record['source_version'],'TEST_ONLY_v1')

    def test_huge_string_id_does_not_crash(self):
        self.food['fdcId']='1'*5000
        self.assertIn('invalid_food_id',self.parse()['records'][0]['issues'])

    def test_snapshot_wrapper_and_portions(self):
        self.food['foodPortions']=[{'id':1,'amount':1,'gramWeight':10,'portionDescription':'TEST_ONLY portion'}]
        self.assertEqual(self.parse()['records'][0]['raw_record']['foodPortions'],self.food['foodPortions'])
        for payload in (self.food,[self.food]):
            self.path.write_text(json.dumps(payload))
            with self.assertRaises(SnapshotError):
                parse_snapshot(self.path,source_version='TEST_ONLY',source_url='test://fixture')
        self.food['foodPortions']={}
        self.assertIn('invalid_portion_array',self.parse()['records'][0]['issues'])

    def test_zip_crc_failure_cleanup(self):
        buffer=io.BytesIO()
        with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_STORED) as archive:
            archive.writestr('TEST_ONLY.json','TEST_ONLY_DATA')
        content=bytearray(buffer.getvalue())
        position=content.index(b'TEST_ONLY_DATA')
        content[position]=ord('X')
        class Response(io.BytesIO):
            headers={}
            def geturl(self): return 'https://fdc.nal.usda.gov/test.zip'
        class Opener:
            def open(self,*args,**kwargs): return Response(content)
        path=Path(self.temp.name)/'crc.zip'
        with self.assertRaises((ValueError,zipfile.BadZipFile)):
            fetch.fetch_snapshot('https://fdc.nal.usda.gov/test.zip',path,opener=Opener())
        self.assertFalse(path.exists())

    def test_manifest_not_real_data(self):
        manifest=json.loads((ROOT/'data/manifests/usda_foundation.json').read_text())
        self.assertIsNone(manifest['source_file_sha256'])
        self.assertFalse(manifest['real_data_verified'])


if __name__=='__main__':
    unittest.main()
