"""TEST_ONLY synthetic snapshots; no real USDA food measurements."""
import copy
import subprocess
import sys
import os
import importlib.util
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('validator', ROOT/'scripts/validate_usda_snapshot.py')
validator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validator)


class SnapshotValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.source = self.directory/'TEST_ONLY.json'
        self.food = {'fdcId':1,'description':'TEST_ONLY synthetic food','dataType':'Foundation',
                     'foodNutrients':[{'nutrient':{'id':1003,'name':'TEST_ONLY','unitName':'g'},'amount':0}],
                     'foodPortions':[{'amount':1,'gramWeight':10}]}
        self.write([self.food])

    def tearDown(self):
        self.temp.cleanup()

    def write(self,foods):
        self.source.write_text(json.dumps({'FoundationFoods':foods}),encoding='utf-8')

    def validate(self,source=None,output='report',**kwargs):
        return validator.validate_snapshot(source or self.source,self.directory/output,
                                           source_version='TEST_ONLY_v1',input_kind='TEST_ONLY',**kwargs)

    def zip(self,names=None,content=None):
        path=self.directory/'TEST_ONLY.zip'
        with zipfile.ZipFile(path,'w') as archive:
            for name in names or ['folder/TEST_ONLY.json']:
                archive.writestr(name,content or self.source.read_bytes())
        return path

    def test_normal_and_readonly(self):
        original=self.source.read_bytes()
        r=self.validate()
        self.assertEqual(r['counts']['total_foods'],1)
        self.assertEqual(r['counts']['needs_review'],1)
        self.assertEqual(r['numeric_states']['zero'],1)
        self.assertEqual(r['review_status'],'pending')
        self.assertEqual(original,self.source.read_bytes())
        self.assertNotIn(str(self.directory),(self.directory/'report/validation.json').read_text())

    def test_empty(self):
        self.write([])
        r=self.validate()
        self.assertEqual(sum(r['counts'].values()),0)

    def test_bad_structure(self):
        self.source.write_text('{}')
        with self.assertRaises(ValueError):self.validate()
        self.assertFalse((self.directory/'report').exists())

    def test_zip_readonly_hashes(self):
        path=self.zip()
        original=path.read_bytes()
        r=self.validate(path)
        self.assertEqual(r['zip_sha256'],validator._hash(path))
        self.assertEqual(r['json_sha256'],validator._hash(self.source))
        self.assertEqual(r['zip_member'],'folder/TEST_ONLY.json')
        self.assertEqual(path.read_bytes(),original)

    def test_invalid_zip(self):
        path=self.directory/'bad.zip';path.write_bytes(b'TEST_ONLY invalid')
        with self.assertRaises(ValueError):self.validate(path)

    def test_path_traversal(self):
        for name in ('../escape.json','/absolute.json','C:/escape.json','folder\\escape.json'):
            with self.subTest(name=name),self.assertRaises(ValueError):self.validate(self.zip([name]))
        self.assertFalse((self.directory/'report').exists())

    def test_symlink_member(self):
        path=self.directory/'link.zip'
        with zipfile.ZipFile(path,'w') as archive:
            info=zipfile.ZipInfo('link.json');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16
            archive.writestr(info,'../../target')
        with self.assertRaises(ValueError):self.validate(path)

    def test_ambiguous_json(self):
        with self.assertRaises(ValueError):self.validate(self.zip(['a.json','b.json']))

    def test_duplicates(self):
        self.food['foodNutrients']*=2
        self.write([self.food,copy.deepcopy(self.food)])
        r=self.validate()
        self.assertEqual(r['duplicate_food_ids'],{'1':2})
        self.assertEqual(r['duplicate_nutrient_ids_food_counts'],{'1003':2})

    def test_missing(self):
        self.food.pop('foodNutrients');self.write([self.food])
        r=self.validate()
        self.assertEqual(r['missing_fields']['foodNutrients'],1)
        self.assertEqual(r['missing_fields']['mapped_nutrient.1003'],1)

    def test_energy_sugar_distribution(self):
        self.food['foodNutrients']=[{'nutrient':{'id':i,'name':'TEST_ONLY','unitName':u},'amount':0}
                                   for i,u in ((1008,'kcal'),(2047,'kcal'),(2048,'kJ'),(1063,'g'),(2000,'g'))]
        self.write([self.food]);r=self.validate()
        self.assertEqual(r['energy_food_counts'],{'1008':1,'2047':1,'2048':1})
        self.assertEqual(r['sugar_food_counts'],{'1063':1,'2000':1})
        self.assertEqual(r['foods_with_multiple_energy_definitions'],1)
        self.assertEqual(r['foods_with_multiple_sugar_definitions'],1)
        self.assertEqual(r['unit_occurrences']['kJ'],1)

    def test_invalid_unknown_loq(self):
        self.food['foodNutrients']=[{'nutrient':{'id':1003,'unitName':'g'},'amount':'NaN'},
                                   {'nutrient':{'id':9999,'unitName':'unknown'},'amount':-1},
                                   {'nutrient':{'id':1004,'unitName':'g'},'amount':0,'loq':0.1}]
        self.write([self.food]);r=self.validate()
        self.assertEqual(r['numeric_states']['invalid'],2)
        self.assertEqual(r['numeric_states']['qualified_zero'],1)
        self.assertEqual(r['finding_rule_counts']['unknown_nutrient_id'],1)
        self.assertEqual(r['finding_rule_counts']['unknown_unit'],1)

    def test_failure_counts(self):
        self.write([None,{'fdcId':False,'description':'','dataType':'Branded'}])
        r=self.validate()
        self.assertEqual(r['counts']['parse_failed'],2)
        self.assertEqual(sum(r['counts'][k] for k in ('successfully_parsed','needs_review','parse_failed')),2)

    def test_repeat_consistency(self):
        a=self.validate(output='one');b=self.validate(output='two')
        self.assertEqual(a,b)
        self.assertEqual((self.directory/'one/validation.json').read_bytes(),(self.directory/'two/validation.json').read_bytes())

    def test_no_overwrite(self):
        self.validate()
        original=(self.directory/'report/validation.json').read_bytes()
        with self.assertRaises(FileExistsError):self.validate()
        self.assertEqual(original,(self.directory/'report/validation.json').read_bytes())

    def test_wrong_hash_no_report(self):
        with self.assertRaises(ValueError):self.validate(expected_json_sha256='0'*64)
        self.assertFalse((self.directory/'report').exists())

    def test_size_limit(self):
        with patch.object(validator,'MAX_JSON_BYTES',1),self.assertRaises(ValueError):self.validate()

    def test_unusable_portion(self):
        self.food['foodPortions']=[{'amount':1,'gramWeight':0},None]
        self.write([self.food]);r=self.validate()
        self.assertEqual(r['portions']['unusable'],2)

    def test_success_is_not_approved(self):
        self.food['foodNutrients']=[{'nutrient':{'id':i,'name':'TEST_ONLY','unitName':u},'amount':0}
                                   for i,(_,u) in validator.MAPPING.items()]
        self.write([self.food])
        r=self.validate(nutrition_basis='per_100g',basis_evidence='TEST_ONLY explicit basis')
        self.assertEqual(r['counts']['successfully_parsed'],1)
        self.assertEqual(r['review_status'],'pending')

    def test_corrupt_crc_rejected(self):
        path=self.zip(content=b'TEST_ONLY crc payload')
        content=bytearray(path.read_bytes());offset=content.index(b'TEST_ONLY crc payload');content[offset]=ord('X')
        path.write_bytes(content)
        with self.assertRaises(ValueError):self.validate(path)
        self.assertFalse((self.directory/'report').exists())

    def test_cli_and_bad_format_message(self):
        command=[sys.executable,str(ROOT/'scripts/validate_usda_snapshot.py'),str(self.source),
                 '--output-dir',str(self.directory/'cli'),'--source-version','TEST_ONLY','--input-kind','TEST_ONLY']
        environment=dict(os.environ,PYTHONDONTWRITEBYTECODE='1')
        result=subprocess.run(command,capture_output=True,text=True,env=environment)
        self.assertEqual(result.returncode,0,result.stderr)
        self.source.write_text('{}')
        command[command.index('--output-dir')+1]=str(self.directory/'bad')
        result=subprocess.run(command,capture_output=True,text=True,env=environment)
        self.assertEqual(result.returncode,2)
        self.assertIn('验证失败',result.stderr)

    def test_zip_count_limit_and_input_link(self):
        path=self.zip()
        with patch.object(validator,'MAX_MEMBERS',0),self.assertRaises(ValueError):self.validate(path)
        link=self.directory/'link.json';link.symlink_to(self.source)
        with self.assertRaises(ValueError):self.validate(link)

    def test_nul_archive_name_rejected(self):
        # ZipInfo retains the original name even when filename is NUL-truncated.
        info=zipfile.ZipInfo('snapshot.json\x00hidden')
        with self.assertRaises(ValueError):validator._safe_member(info)

    def test_report_write_failure_cleanup(self):
        with patch.object(validator.json,'dump',side_effect=OSError('TEST_ONLY write failure')),self.assertRaises(OSError):self.validate()
        self.assertFalse((self.directory/'report').exists())


if __name__=='__main__':unittest.main()
