"""Read-only food CSV audit; standard-library only. Never correct nutrient values."""
import argparse
import csv
import hashlib
import io
import json
from collections import Counter
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path

FIELDS = ['Food_items', 'Category', 'Calories', 'Fats', 'Protein', 'Iron',
          'Carbohydrates', 'Fibre', 'Sugar', 'Meal_Type', 'Sodium', 'Vegetarian', 'Vegan']
NUMERIC = ['Calories', 'Fats', 'Protein', 'Iron', 'Carbohydrates', 'Fibre', 'Sugar', 'Sodium']
REPORT_FILES = ['summary.json', 'anomalies.csv', 'food_metadata.csv']
VERSION = '1.0'
METADATA_FIELDS = ['record_id', 'csv_line', 'food_name', 'source_sha256', 'data_source',
                   'source_version', 'source_updated_at', 'serving_quantity', 'serving_unit',
                   'nutrition_basis', 'energy_unit', 'nutrient_units',
                   'carbohydrate_definition', 'sugar_definition', 'review_status',
                   'reviewer', 'reviewed_at', 'import_method', 'ocr_document_id', 'ocr_page',
                   'ocr_region', 'ocr_engine', 'ocr_engine_version', 'ocr_confidence',
                   'ocr_raw_text', 'evidence_reference']


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def audit_bytes(raw):
    # Deterministic precision independent of the caller's Decimal context.
    with localcontext() as context:
        context.prec = 512
        return _audit_bytes(raw)


def _audit_bytes(raw):
    """Analyze all records and preserve raw strings. Physical line = record start."""
    digest = sha256(raw)
    reader = csv.reader(io.StringIO(raw.decode('utf-8-sig'), newline=''), strict=True)
    header = next(reader, [])
    anomalies, metadata = [], []
    missing = Counter({field: 0 for field in FIELDS})
    seen_rows, seen_names = {}, {}
    count = 0

    def issue(line, name, rule, values, risk='medium', status='needs_review',
              evidence='', action='保留原值，人工核实；不用于自动纠错。', record_id=''):
        anomalies.append(dict(record_id=record_id, csv_line=line, food_name=name,
                              rule_id=rule, original_values=values, risk_level=risk,
                              review_status=status, evidence=evidence,
                              suggested_action=action, source_sha256=digest))

    if header != FIELDS:
        issue(1, '', 'SCHEMA_MISMATCH', {'header': header}, 'high', 'confirmed_structural_issue',
              '表头应严格匹配原有13列，包括顺序和唯一性。')
    while True:
        line = reader.line_num + 1
        try:
            cells = next(reader)
        except StopIteration:
            break
        count += 1
        rid = f'{digest}:{line}'
        # Duplicate headers cannot be interpreted without ambiguity.
        record = dict(zip(header, cells)) if len(set(header)) == len(header) else {}
        name = record.get('Food_items', '')
        if len(cells) != len(header):
            issue(line, name, 'ROW_WIDTH', {'cells': cells}, 'high', 'confirmed_structural_issue',
                  '记录字段数与表头不一致。', record_id=rid)
        key = tuple(cells)
        if key in seen_rows:
            issue(line, name, 'DUPLICATE_RECORD', {'cells': cells, 'first_line': seen_rows[key]},
                  status='confirmed_structural_issue', evidence='原始单元格完全相同。', record_id=rid)
        else:
            seen_rows[key] = line
        normalized = name.strip().casefold()
        if normalized:
            if normalized in seen_names:
                issue(line, name, 'DUPLICATE_NAME', {'Food_items': name, 'first_line': seen_names[normalized]},
                      evidence='去首尾空格、忽略大小写后重名；规格可能不同，禁止自动合并。', record_id=rid)
            else:
                seen_names[normalized] = line
        numbers = {}
        for field in FIELDS:
            value = record.get(field, '')
            if not value.strip():
                missing[field] += 1
                issue(line, name, 'MISSING_VALUE', {field: value}, 'high', 'confirmed_structural_issue',
                      '字段为空或缺失；不能填入生成值。', record_id=rid)
                continue
            if value != value.strip():
                issue(line, name, 'SURROUNDING_WHITESPACE', {field: value}, 'low',
                      'confirmed_structural_issue', '仅记录首尾空格，不修改输入。', record_id=rid)
            if field in NUMERIC:
                try:
                    number = Decimal(value.strip())
                    if not number.is_finite():
                        raise InvalidOperation
                except InvalidOperation:
                    issue(line, name, 'INVALID_NUMBER', {field: value}, 'high',
                          'confirmed_structural_issue', '无法解析为有限十进制数。', record_id=rid)
                    continue
                if len(number.as_tuple().digits) > 100 or (number != 0 and abs(number.adjusted()) > 100):
                    issue(line, name, 'NUMERIC_PRECISION_LIMIT', {field: value}, 'medium',
                          'needs_review', '超过审计计算范围（100位有效数字、数量级±100）；不是营养错误判断。', record_id=rid)
                    continue
                if number < 0:
                    issue(line, name, 'NEGATIVE_NUMBER', {field: value}, 'high', 'needs_review',
                          '营养字段为负；保留并核实，不取绝对值。', record_id=rid)
                else:
                    numbers[field] = number
            if field in ['Vegetarian', 'Vegan'] and value not in ['True', 'False']:
                issue(line, name, 'INVALID_BOOLEAN', {field: value}, 'high',
                      'confirmed_structural_issue', '原接口要求True/False布尔标签。', record_id=rid)
        metadata.append(dict(record_id=rid, csv_line=line, food_name=name,
                             source_sha256=digest, data_source='', source_version='',
                             source_updated_at='', serving_quantity='', serving_unit='',
                             nutrition_basis='', energy_unit='', nutrient_units='',
                             carbohydrate_definition='', sugar_definition='',
                             review_status='needs_review', reviewer='', reviewed_at='',
                             import_method='legacy_csv', ocr_document_id='', ocr_page='',
                             ocr_region='', ocr_engine='', ocr_engine_version='',
                             ocr_confidence='', ocr_raw_text='', evidence_reference=''))
        issue(line, name, 'UNKNOWN_PROVENANCE_AND_BASIS', {}, evidence='CSV未提供来源、单位、份量或字段定义；所有营养关系检查待核实。', record_id=rid)
        def nutrition_issue(rule, fields, evidence, risk='high'):
            issue(line, name, rule, {field: record[field] for field in fields}, risk,
                  'needs_review', evidence, record_id=rid)
        if numbers.get('Calories') == 0:
            nutrition_issue('ZERO_CALORIES', ['Calories'], '零热量不计算相对偏差；需核实是否适用。', 'medium')
        if {'Sugar', 'Carbohydrates'} <= numbers.keys() and numbers['Sugar'] > numbers['Carbohydrates']:
            small = numbers['Sugar'] - numbers['Carbohydrates'] <= Decimal('0.2')
            nutrition_issue('SUGAR_GT_CARBS', ['Sugar', 'Carbohydrates'],
                            '前提：相同计量基准、相同单位且糖属于该碳水定义；小差异需考虑四舍五入。',
                            'medium' if small else 'high')
        if {'Fibre', 'Carbohydrates'} <= numbers.keys() and numbers['Fibre'] > numbers['Carbohydrates']:
            nutrition_issue('FIBRE_GT_CARBS', ['Fibre', 'Carbohydrates'],
                            '仅在碳水定义包含纤维且同基准时构成疑点；定义未知。', 'medium')
        if {'Calories', 'Fats'} <= numbers.keys() and 9 * numbers['Fats'] > numbers['Calories']:
            nutrition_issue('FAT_ENERGY_GT_CALORIES', ['Calories', 'Fats'],
                            '前提：热量为kcal、脂肪为g且同份量；9×脂肪超过标注热量，不能直接判错。')
        if {'Calories', 'Protein', 'Fats', 'Carbohydrates', 'Fibre'} <= numbers.keys():
            cal = numbers['Calories']
            fields = ['Calories', 'Protein', 'Fats', 'Carbohydrates', 'Fibre']
            if cal > 0:
                energy = 4 * numbers['Protein'] + 4 * numbers['Carbohydrates'] + 9 * numbers['Fats']
                if abs(energy - cal) > Decimal('0.2') * cal:
                    nutrition_issue('ENERGY_449_DEVIATION', fields,
                                    f'条件性辅助估算E0={energy}；偏差>20%；不是自动纠错标准。', 'medium')
                low = energy - 4 * numbers['Fibre']
                high = energy + 2 * numbers['Fibre']
                tolerance = max(Decimal('50'), Decimal('0.2') * cal)
                if cal < low - tolerance or cal > high + tolerance:
                    nutrition_issue('ENERGY_FIBRE_SCREEN', fields,
                                    f'条件性宽松范围[{low},{high}]，容差{tolerance}；不能覆盖所有能量体系。')
    rules = Counter(item['rule_id'] for item in anomalies)
    summary = dict(audit_version=VERSION, source_sha256=digest, record_count=count,
                   column_count=len(header), columns=header, rule_counts=dict(sorted(rules.items())),
                   missing_counts=dict(missing),
                   missing_percent={key: value / count * 100 if count else None for key, value in missing.items()},
                   anomaly_count=len(anomalies),
                   records_with_numeric_flags=len({a['csv_line'] for a in anomalies if a['rule_id'] in
                       ['SUGAR_GT_CARBS', 'FAT_ENERGY_GT_CALORIES', 'FIBRE_GT_CARBS', 'ENERGY_449_DEVIATION', 'ENERGY_FIBRE_SCREEN']}),
                   review_status='needs_review', nutrient_values_modified=False,
                   not_evaluated=['saturated_fat: field absent', 'absolute nutrition ranges: basis unknown'],
                   energy_policy={'relative_tolerance': '0.2', 'absolute_tolerance': '50',
                                  'units_assumed_only_for_screening': 'kcal and g; NOT verified'})
    if not count:
        summary['dataset_status'] = 'empty'
    return summary, anomalies, metadata


def audit_file(source, output):
    source, output = Path(source).resolve(), Path(output).resolve()
    # Guard every output target before creating anything (including symlinks).
    for filename in REPORT_FILES:
        target = output / filename
        if target.resolve() == source or (target.exists() and source.exists() and target.samefile(source)):
            raise ValueError('Report output must not overwrite the source CSV')
        if target.exists() or target.is_symlink():
            raise FileExistsError(f'Report already exists: {target}')
    raw = source.read_bytes()
    summary, anomalies, metadata = audit_bytes(raw)
    output.mkdir(parents=True, exist_ok=True)
    summary['source_name'] = source.name
    summary['source_sha256_after'] = sha256(source.read_bytes())
    if summary['source_sha256_after'] != summary['source_sha256']:
        raise RuntimeError('Source changed during audit')
    # Exclusive creation also rejects files/symlinks introduced after preflight.
    with (output / 'summary.json').open('x', encoding='utf-8') as stream:
        stream.write(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    anomaly_fields = ['record_id', 'csv_line', 'food_name', 'rule_id', 'original_values',
                      'risk_level', 'review_status', 'evidence', 'suggested_action', 'source_sha256']
    metadata_fields = METADATA_FIELDS
    for filename, records, fields in [('anomalies.csv', anomalies, anomaly_fields), ('food_metadata.csv', metadata, metadata_fields)]:
        with (output / filename).open('x', encoding='utf-8', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, lineterminator='\n')
            writer.writeheader()
            for record in records:
                row = dict(record)
                if filename == 'anomalies.csv':
                    row['original_values'] = json.dumps(row['original_values'], ensure_ascii=False, sort_keys=True)
                writer.writerow(row)
    if sha256(source.read_bytes()) != summary['source_sha256']:
        raise RuntimeError('Source changed during report generation')
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path(__file__).resolve().parents[1] / 'food_data.csv')
    parser.add_argument('--output-dir', type=Path, required=True, help='New directory for audit reports')
    args = parser.parse_args()
    summary = audit_file(args.input, args.output_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
