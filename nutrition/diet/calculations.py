"""Decimal per-100 g scaling and completeness-aware daily aggregation."""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP

from .models import validate_record
from .validation import MEAL_TYPES, NUTRIENT_NAMES, DietValidationError, iso_date

ROUNDING = Decimal("0.01")


def _round(value):
    return float(value.quantize(ROUNDING, rounding=ROUND_HALF_UP))


def scale_record_nutrients(record):
    item = validate_record(record)
    factor = Decimal(str(item.amount_g)) / Decimal("100")
    values = {}
    for nutrient in NUTRIENT_NAMES:
        source = getattr(item, nutrient + "_per_100g")
        values[nutrient] = None if source is None else _round(Decimal(str(source)) * factor)
    return {"record_id": item.record_id, "profile_id": item.profile_id, "meal_date": item.meal_date,
            "meal_type": item.meal_type, "amount_g": item.amount_g,
            "basis": "per_100g_snapshot", "rounding": "ROUND_HALF_UP to 0.01",
            "nutrients": values, "nutrient_data_status": item.nutrient_data_status,
            "food_source_type": item.food_source_type,
            "source_snapshot_sha256": item.source_snapshot_sha256}


def _aggregate(records):
    scaled = [scale_record_nutrients(record) for record in records]
    result = {"record_count": len(scaled)}
    for nutrient in NUTRIENT_NAMES:
        known = [Decimal(str(row["nutrients"][nutrient])) for row in scaled
                 if row["nutrients"][nutrient] is not None]
        unknown_count = len(scaled) - len(known)
        subtotal = sum(known, Decimal("0"))
        if not scaled:
            status, total = "no_records", None
        elif not known:
            status, total = "unknown", None
        elif unknown_count:
            status, total = "partial", None
        else:
            status, total = "complete", _round(subtotal)
        result[nutrient] = {"total": total, "known_subtotal": None if not known else _round(subtotal),
                            "unknown_record_count": unknown_count, "data_status": status,
                            "unit": "kcal" if nutrient == "energy_kcal" else "g"}
    return result


def calculate_meal_intake(records):
    checked = [validate_record(record) for record in records]
    items = [record for record in checked if record.record_status == "active"]
    if items and (len({item.profile_id for item in items}) != 1 or len({item.meal_date for item in items}) != 1 or
                  len({item.meal_type for item in items}) != 1):
        raise DietValidationError("Meal aggregation requires one profile, date and meal type")
    result = _aggregate(items)
    result.update({"profile_id": items[0].profile_id if items else None,
                   "meal_date": items[0].meal_date if items else None,
                   "meal_type": items[0].meal_type if items else None})
    return result


def calculate_daily_intake(records, meal_date, *, profile_id=None):
    selected_date = iso_date(meal_date)
    checked = [validate_record(record) for record in records]
    active = [record for record in checked
              if record.record_status == "active" and record.meal_date == selected_date]
    if profile_id is not None:
        active = [record for record in active if record.profile_id == profile_id]
    elif len({record.profile_id for record in active}) > 1:
        raise DietValidationError("Daily aggregation requires an explicit profile_id")
    aggregate = _aggregate(active)
    by_meal = {}
    for meal_type in sorted(MEAL_TYPES):
        rows = [record for record in active if record.meal_type == meal_type]
        by_meal[meal_type] = _aggregate(rows)
    unverified = sum(record.food_source_type in {"manual_unverified", "external_unverified"} for record in active)
    aggregate.update({"profile_id": profile_id or (active[0].profile_id if active else None),
                      "meal_date": selected_date,
                      "meal_count": len({record.meal_type for record in active}),
                      "meals": by_meal, "unverified_source_record_count": unverified,
                      "source_trust_status": "unverified_sources_present" if unverified else
                                             ("no_records" if not active else "verified_sources_only"),
                      "warning": "Known subtotals are not complete totals when any nutrient value is unknown."})
    return aggregate
