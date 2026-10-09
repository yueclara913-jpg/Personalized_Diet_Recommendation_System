"""Offline Foundation snapshot validation. No network or database operations."""
import argparse
from collections import Counter
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import zipfile
import zlib
from urllib.parse import urlsplit

# Support both direct script execution and module execution without new dependencies.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from nutrition.sources.usda_foundation import parse_snapshot, MAPPING, PARSER_VERSION, MAPPING_VERSION
from nutrition.normalization import UNITS

VERSION = '1.0.0'
MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_ZIP_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MEMBERS = 256
OFFICIAL_URL = 'https://fdc.nal.usda.gov/fdc-datasets/FoodData_Central_foundation_food_json_2026-04-30.zip'


class ValidationError(ValueError):
    """Unsafe archive, ambiguous input or report configuration."""


def _hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()


def _check_hash(actual, expected, name):
    if expected is not None and (len(expected) != 64 or any(c not in '0123456789abcdef' for c in expected) or actual != expected):
        raise ValidationError(f'{name} SHA-256 mismatch or invalid expected hash')


def _safe_member(info):
    name = info.orig_filename
    parts = PurePosixPath(name).parts
    mode = info.external_attr >> 16
    if (not name or '\\' in name or ':' in name or '\x00' in name or name.startswith('/')
            or '..' in parts or any(part == '.' for part in name.split('/'))
            or stat.S_ISLNK(mode) or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
            or info.flag_bits & 1):
        raise ValidationError('Unsafe ZIP member path, type or encryption')


def _extract_json(path, temporary):
    """Read every member for CRC, extract only one JSON to a generated safe path."""
    if path.stat().st_size > MAX_ZIP_BYTES:
        raise ValidationError('ZIP exceeds 10 MiB limit')
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if not members or len(members) > MAX_MEMBERS:
                raise ValidationError('ZIP empty or too many members')
            seen = set()
            total = 0
            json_members = []
            for info in members:
                _safe_member(info)
                normalized = str(PurePosixPath(info.filename)).casefold()
                if normalized in seen:
                    raise ValidationError('Duplicate or case-colliding ZIP member')
                seen.add(normalized)
                total += info.file_size
                if total > MAX_TOTAL_BYTES or info.file_size > MAX_JSON_BYTES:
                    raise ValidationError('ZIP uncompressed size exceeds limits')
                if info.file_size and info.file_size / max(info.compress_size, 1) > 2000:
                    raise ValidationError('ZIP compression ratio exceeds limit')
                if not info.is_dir() and info.filename.lower().endswith('.json'):
                    json_members.append(info)
            if len(json_members) != 1:
                raise ValidationError('Expected exactly one JSON member in ZIP')
            selected = json_members[0]
            target = Path(temporary) / 'snapshot.json'
            for info in members:
                if info.is_dir():
                    continue
                with archive.open(info) as stream:
                    count = 0
                    # Never use the archive filename as an extraction path.
                    if info == selected:
                        output = target.open('xb')
                    else:
                        output = None
                    try:
                        for block in iter(lambda: stream.read(65536), b''):
                            count += len(block)
                            if count > MAX_JSON_BYTES:
                                raise ValidationError('ZIP member exceeded streaming size limit')
                            if output is not None:
                                output.write(block)
                    finally:
                        if output is not None:
                            output.close()
            return target, selected.filename, len(members)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, zlib.error) as error:
        raise ValidationError('Invalid or unsupported ZIP: ' + str(error)) from error


def _numeric(value):
    if value is None:
        return 'missing'
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return 'invalid'
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0 or number > Decimal('1e12'):
            return 'invalid'
        return 'zero' if number == 0 else 'positive'
    except InvalidOperation:
        return 'invalid'


def _scan(parsed):
    ids, units, missing, numeric, nutrient_duplicates = Counter(), Counter(), Counter(), Counter(), Counter()
    energy = Counter({str(i): 0 for i in (1008, 2047, 2048)})
    sugar = Counter({str(i): 0 for i in (1063, 2000)})
    totals = dict(total_foods=len(parsed['records']), successfully_parsed=0, needs_review=0, parse_failed=0)
    findings, food_ids = [], Counter()
    multiple_energy = multiple_sugar = valid_weights = invalid_portions = 0
    required_ids = {nid for nid, (code, _) in MAPPING.items() if not code.startswith(('energy_', 'sugar_'))}

    def finding(record, rule, location=None, raw=None):
        findings.append(dict(source_food_id=record.get('source_food_id'), food_name=record.get('food_name'),
                             source_locator=location or record['source_locator'], rule=rule,
                             raw_value=raw, review_status='pending'))

    for record in parsed['records']:
        raw = record['raw_record']
        failed = False
        review = record.get('status') == 'needs_review'
        if not isinstance(raw, dict):
            totals['parse_failed'] += 1
            finding(record, 'invalid_food_record', raw=raw)
            continue
        for field in ('fdcId', 'description', 'dataType', 'foodNutrients', 'foodPortions'):
            if field not in raw or raw[field] is None:
                missing[field] += 1
        issues = record.get('issues', [])
        for issue in issues:
            finding(record, issue)
        failed = any(issue in issues for issue in ('invalid_food_id','unexpected_data_type','missing_description','invalid_nutrient_array'))
        if record.get('source_food_id') is not None:
            food_ids[str(record['source_food_id'])] += 1
        present = Counter()
        for item in record.get('nutrients', []):
            entry = item.get('raw_record')
            if not isinstance(entry, dict) or not isinstance(entry.get('nutrient'), dict):
                review = True
                finding(record, 'invalid_nutrient_record', item['source_locator'], entry)
                continue
            nutrient = entry['nutrient']
            nid = item.get('source_nutrient_id')
            key = str(nid) if nid is not None else 'invalid'
            present[key] += 1
            ids[key] += 1
            unit = nutrient.get('unitName')
            units[str(unit)] += 1
            state = _numeric(entry.get('amount'))
            numeric['qualified_zero' if state == 'zero' and entry.get('loq') is not None else state] += 1
            for field in ('id','name','unitName'):
                if field not in nutrient or nutrient[field] is None:
                    missing['nutrient.' + field] += 1
            if 'amount' not in entry or entry['amount'] is None:
                missing['nutrient.amount'] += 1
            if state in ('missing','invalid'):
                review = True
                finding(record, 'numeric_' + state, item['source_locator'], entry.get('amount'))
            if entry.get('loq') is not None:
                numeric['loq_qualified'] += 1
                review = True
                finding(record, 'loq_qualified', item['source_locator'], entry.get('loq'))
            if not isinstance(unit, str) or unit not in UNITS:
                review = True
                finding(record, 'unknown_unit', item['source_locator'], unit)
            if item.get('reason'):
                finding(record, item['reason'], item['source_locator'], item.get('raw_value'))
        for nid, count in present.items():
            if count > 1:
                nutrient_duplicates[nid] += 1
        for nid in energy:
            energy[nid] += int(present[nid] > 0)
        for nid in sugar:
            sugar[nid] += int(present[nid] > 0)
        multiple_energy += int(sum(present[nid] > 0 for nid in energy) > 1)
        multiple_sugar += int(sum(present[nid] > 0 for nid in sugar) > 1)
        for nid in sorted(required_ids):
            if not present[str(nid)]:
                missing['mapped_nutrient.' + str(nid)] += 1
                review = True
                finding(record, 'missing_mapped_nutrient', raw=nid)
        if not any(present[nid] for nid in energy):
            missing['energy_any_definition'] += 1
            review = True
            finding(record, 'missing_energy_definition')
        if not any(present[nid] for nid in sugar):
            missing['sugar_any_definition'] += 1
            review = True
            finding(record, 'missing_sugar_definition')
        portions = raw.get('foodPortions', [])
        if isinstance(portions, list):
            for index, portion in enumerate(portions):
                if (isinstance(portion, dict) and _numeric(portion.get('gramWeight')) == 'positive'
                        and _numeric(portion.get('amount')) == 'positive'):
                    valid_weights += 1
                else:
                    invalid_portions += 1
                    review = True
                    finding(record, 'unusable_portion', record['source_locator'] + f'.foodPortions[{index}]', portion)
        else:
            review = True
        if record.get('nutrition_basis') != 'per_100g' or not record.get('basis_evidence'):
            review = True
            finding(record, 'basis_unverified')
        totals['parse_failed' if failed else 'needs_review' if review else 'successfully_parsed'] += 1
    return dict(counts=totals, nutrient_id_occurrences=dict(sorted(ids.items())), unit_occurrences=dict(sorted(units.items())),
                missing_fields=dict(sorted(missing.items())), numeric_states=dict(sorted(numeric.items())),
                energy_food_counts=dict(energy), sugar_food_counts=dict(sugar),
                foods_with_multiple_energy_definitions=multiple_energy, foods_with_multiple_sugar_definitions=multiple_sugar,
                duplicate_food_ids={k:v for k,v in sorted(food_ids.items()) if v>1},
                duplicate_nutrient_ids_food_counts=dict(sorted(nutrient_duplicates.items())),
                portions=dict(positive_weight_and_amount=valid_weights, unusable=invalid_portions),
                finding_rule_counts=dict(sorted(Counter(f['rule'] for f in findings).items())), findings=findings)


def validate_snapshot(input_path, output_dir, *, source_version, source_url=OFFICIAL_URL,
                      nutrition_basis='unknown', basis_evidence=None, expected_json_sha256=None,
                      expected_zip_sha256=None, input_kind='user_supplied_unverified'):
    """Scan JSON/ZIP read-only and write two files in a new exclusive directory."""
    source = Path(input_path)
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        raise FileExistsError('Report directory must be new')
    if source.is_symlink() or not source.is_file():
        raise ValidationError('Input must be a regular non-symlink file')
    if not output.parent.is_dir():
        raise ValidationError('Report parent directory must exist')
    if input_kind not in ('TEST_ONLY','user_supplied_unverified'):
        raise ValidationError('Input kind must explicitly declare verification boundary')
    parsed_url = urlsplit(source_url)
    if parsed_url.username or parsed_url.password or parsed_url.query or parsed_url.fragment:
        raise ValidationError('Source URL must not contain credentials, query or fragment')
    limit = MAX_ZIP_BYTES if source.suffix.lower() == '.zip' else MAX_JSON_BYTES
    if source.stat().st_size > limit:
        raise ValidationError('Input exceeds size limit')
    temp_root = Path(tempfile.gettempdir()).resolve()
    if temp_root == ROOT or ROOT in temp_root.parents:
        raise ValidationError('Temporary directory must be outside repository')
    input_hash = _hash(source)
    with tempfile.TemporaryDirectory(prefix='usda-validation-') as temporary:
        member, zip_hash, zip_member_count = None, None, None
        if source.suffix.lower() == '.zip':
            zip_hash = input_hash
            _check_hash(zip_hash, expected_zip_sha256, 'ZIP')
            json_path, member, zip_member_count = _extract_json(source, temporary)
        elif source.suffix.lower() == '.json':
            if expected_zip_sha256 is not None:
                raise ValidationError('ZIP hash supplied for JSON input')
            if source.stat().st_size > MAX_JSON_BYTES:
                raise ValidationError('JSON exceeds 32 MiB limit')
            json_path = source
        else:
            raise ValidationError('Expected .json or .zip input')
        json_hash = _hash(json_path)
        _check_hash(json_hash, expected_json_sha256, 'JSON')
        try:
            parsed = parse_snapshot(json_path, source_version=source_version, source_url=source_url,
                                    nutrition_basis=nutrition_basis, basis_evidence=basis_evidence)
        except RecursionError as error:
            raise ValidationError('JSON nesting exceeds parser capacity') from error
        if parsed['source_file_sha256'] != json_hash or _hash(source) != input_hash:
            raise ValidationError('Input changed during validation')
        report = dict(validator_version=VERSION, parser_version=PARSER_VERSION, mapping_version=MAPPING_VERSION,
                      source_version=source_version, source_url=source_url, input_kind=input_kind,
                      source_authenticity='not_independently_verified', input_filename=source.name,
                      zip_member=member, zip_member_count=zip_member_count, zip_sha256=zip_hash, json_sha256=json_hash,
                      input_size_bytes=source.stat().st_size, json_size_bytes=json_path.stat().st_size,
                      nutrition_basis=nutrition_basis, basis_evidence=basis_evidence,
                      review_status='pending', food_state_status='requires_manual_review', **_scan(parsed))
        counts = report['counts']
        summary = (f"食品总数：{counts['total_foods']}；成功解析：{counts['successfully_parsed']}；"
                   f"待审核：{counts['needs_review']}；解析失败：{counts['parse_failed']}。\n"
                   "本地解析不证明官方来源真实性；审核状态保持 pending，不可直接导入正式推荐。\n")
        output.mkdir()  # Exclusive creation; never reuse an existing report directory.
        try:
            with (output / 'validation.json').open('x', encoding='utf-8') as target:
                json.dump(report, target, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
                target.write('\n')
            with (output / 'summary.txt').open('x', encoding='utf-8') as target:
                target.write(summary)
        except BaseException:
            shutil.rmtree(output)
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input')
    parser.add_argument('--output-dir', required=True)
    parser.add_argument('--source-version', required=True)
    parser.add_argument('--source-url', default=OFFICIAL_URL)
    parser.add_argument('--basis', default='unknown', choices=('unknown','per_100g','per_100ml','per_serving'))
    parser.add_argument('--basis-evidence')
    parser.add_argument('--expected-json-sha256')
    parser.add_argument('--expected-zip-sha256')
    parser.add_argument('--input-kind', default='user_supplied_unverified', choices=('TEST_ONLY','user_supplied_unverified'))
    args = parser.parse_args()
    try:
        report = validate_snapshot(args.input,args.output_dir,source_version=args.source_version,
                                   source_url=args.source_url,nutrition_basis=args.basis,basis_evidence=args.basis_evidence,
                                   expected_json_sha256=args.expected_json_sha256,expected_zip_sha256=args.expected_zip_sha256,
                                   input_kind=args.input_kind)
    except (ValueError, OSError) as error:
        parser.exit(2, f'验证失败：{error}\n')
    print(f"验证报告已生成：总数 {report['counts']['total_foods']}；{report['counts']}")


if __name__ == '__main__':
    main()
