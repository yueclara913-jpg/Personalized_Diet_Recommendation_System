"""Read FoundationFoods JSON snapshots without writing food data or databases."""
import argparse
from collections import Counter
from decimal import Decimal
import hashlib
import json
from pathlib import Path

from nutrition.normalization import convert_value

PARSER_VERSION = '1.0.0'
MAPPING_VERSION = 'usda-id-v1'
# FDC nutrient IDs, NOT legacy nutrient numbers. See docs/usda_foundation_parser.md.
MAPPING = {
    1003: ('protein', 'g'), 1004: ('fat', 'g'),
    1005: ('carbohydrates_by_difference', 'g'), 1079: ('fiber', 'g'),
    1089: ('iron', 'mg'), 1093: ('sodium', 'mg'),
    2000: ('sugar_total_nlea', 'g'), 1063: ('sugar_total', 'g'),
    1008: ('energy_legacy', 'kcal'), 2047: ('energy_atwater_general', 'kcal'),
    2048: ('energy_atwater_specific', 'kcal'),
}
STANDARD_CODES = ('calories','protein','fat','carbohydrates','fiber','sugar','sodium','iron')


class SnapshotError(ValueError):
    """Invalid snapshot format or provenance configuration."""


def _text(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {k: _text(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_text(v) for v in value]
    return value


def _id(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and len(value) <= 20 and value.isascii() and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise SnapshotError('Duplicate JSON object key: ' + key)
        result[key] = value
    return result


def parse_snapshot(path, *, source_version, source_url, nutrition_basis='unknown',
                   basis_evidence=None, energy_id=None, sugar_id=None):
    """Return deterministic, JSON-serializable staging records.

    The caller supplies verified release metadata and basis evidence. No energy
    method is selected unless its exact nutrient ID is explicitly requested.
    Duplicate food/nutrient identities are retained and marked for review.
    """
    if not isinstance(source_version, str) or not source_version.strip() or not isinstance(source_url, str) or not source_url.strip():
        raise SnapshotError('source_version and source_url are required')
    if nutrition_basis not in ('unknown','per_100g','per_100ml','per_serving'):
        raise SnapshotError('Invalid nutrition_basis')
    if energy_id not in (None,1008,2047,2048) or sugar_id not in (None,1063,2000):
        raise SnapshotError('Unsupported explicit nutrient selection')
    if Path(path).stat().st_size > 32 * 1024 * 1024:
        raise SnapshotError('JSON exceeds 32 MiB parser limit')
    raw = Path(path).read_bytes()
    try:
        document = json.loads(raw.decode('utf-8-sig'), parse_float=Decimal,
                              parse_constant=lambda value: Decimal(value),
                              object_pairs_hook=_unique_object)
    except (UnicodeError, ValueError) as error:
        raise SnapshotError('Invalid UTF-8 Foundation JSON: ' + str(error)) from error
    if not isinstance(document, dict) or not isinstance(document.get('FoundationFoods'), list):
        raise SnapshotError('Expected object with FoundationFoods array')
    foods = document['FoundationFoods']
    counts = Counter(_id(f.get('fdcId')) for f in foods if isinstance(f, dict))
    digest = hashlib.sha256(raw).hexdigest()
    records = []
    for index, food in enumerate(foods):
        locator = f'FoundationFoods[{index}]'
        if not isinstance(food, dict):
            records.append(dict(source_locator=locator, source_version=source_version, source_url=source_url,
                                source_file_sha256=digest, review_status='pending',
                                status='needs_review', reason='invalid_food_record', raw_record=_text(food)))
            continue
        identity = _id(food.get('fdcId'))
        issues = []
        if identity is None:
            issues.append('invalid_food_id')
        elif counts[identity] > 1:
            issues.append('duplicate_food_id')
        if food.get('dataType') != 'Foundation':
            issues.append('unexpected_data_type')
        if not isinstance(food.get('description'), str) or not food['description'].strip():
            issues.append('missing_description')
        if 'foodPortions' in food and not isinstance(food['foodPortions'], list):
            issues.append('invalid_portion_array')
        entries = food.get('foodNutrients', [])
        if not isinstance(entries, list):
            entries = []
            issues.append('invalid_nutrient_array')
        nutrients = []
        ids = []
        for nindex, entry in enumerate(entries):
            location = f'{locator}.foodNutrients[{nindex}]'
            if not isinstance(entry, dict) or not isinstance(entry.get('nutrient'), dict):
                nutrients.append(dict(source_locator=location, status='needs_review', reason='invalid_nutrient_record', raw_record=_text(entry)))
                issues.append('invalid_nutrient_record')
                continue
            nutrient = entry['nutrient']
            nid = _id(nutrient.get('id'))
            ids.append(nid)
            mapping = MAPPING.get(nid)
            raw_value = _text(entry.get('amount'))
            item = dict(source_nutrient_id=nid, source_name=nutrient.get('name'),
                        source_locator=location, raw_record=_text(entry), mapped_code=None)
            if mapping:
                item.update(convert_value(raw_value, nutrient.get('unitName'), mapping[1],
                                          basis=nutrition_basis, basis_evidence=basis_evidence))
                item['mapped_code'] = mapping[0]
                # LOQ-marked zero is not an exact zero measurement.
                if entry.get('loq') is not None:
                    item.update(standardized_value=None, status='needs_review', reason='below_loq_or_qualified_value')
            else:
                item.update(raw_value=raw_value, raw_unit=nutrient.get('unitName'), standardized_value=None,
                            status='needs_review', reason='unknown_nutrient_id')
            if item.get('status') == 'needs_review':
                issues.append('nutrient_needs_review')
            nutrients.append(item)
        repeated = {nid for nid,count in Counter(ids).items() if nid is not None and count > 1}
        for item in nutrients:
            if item.get('source_nutrient_id') in repeated:
                item.update(standardized_value=None, status='needs_review', reason='duplicate_nutrient_id')
        if repeated:
            issues.append('duplicate_nutrient_id')
        standard = {code: None for code in STANDARD_CODES}
        for item in nutrients:
            nid = item.get('source_nutrient_id')
            code = {1003:'protein',1004:'fat',1005:'carbohydrates',1079:'fiber',1089:'iron',1093:'sodium'}.get(nid)
            if nid == energy_id and energy_id is not None:
                code = 'calories'
            if nid == sugar_id and sugar_id is not None:
                code = 'sugar'
            if code:
                standard[code] = item.get('standardized_value')
        records.append(dict(source_food_id=identity, food_name=food.get('description'),
                            source_data_type=food.get('dataType'), source_version=source_version,
                            source_url=source_url, source_file_sha256=digest, source_locator=locator,
                            nutrition_basis=nutrition_basis, basis_evidence=basis_evidence,
                            energy_id=energy_id, sugar_id=sugar_id,
                            parser_version=PARSER_VERSION, mapping_version=MAPPING_VERSION,
                            review_status='pending', status='needs_review' if issues else 'parsed',
                            issues=sorted(set(issues)), nutrients=nutrients, standardized=standard,
                            raw_record=_text(food)))
    return dict(source_file_sha256=digest, source_version=source_version,
                parser_version=PARSER_VERSION, mapping_version=MAPPING_VERSION, records=records)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path')
    parser.add_argument('--source-version', required=True)
    parser.add_argument('--source-url', required=True)
    parser.add_argument('--basis', default='unknown')
    parser.add_argument('--basis-evidence')
    parser.add_argument('--energy-id', type=int)
    parser.add_argument('--sugar-id', type=int)
    args = parser.parse_args()
    result = parse_snapshot(args.path, source_version=args.source_version, source_url=args.source_url,
                            nutrition_basis=args.basis, basis_evidence=args.basis_evidence,
                            energy_id=args.energy_id, sugar_id=args.sugar_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
