"""Immutable per-100 g nutrition snapshots attached to local diet records."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
import uuid

from .validation import (DietValidationError, MEAL_TYPES, NUTRIENT_NAMES, RECORD_STATUSES,
                         SOURCE_TYPES, decimal_value, identifier, iso_date, text, utc_timestamp)


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def snapshot_digest(values):
    payload = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DietRecord:
    record_id: str
    profile_id: str
    meal_date: str
    meal_type: str
    food_name: str
    amount_g: float
    food_source_type: str
    source_reference: str
    source_version: str | None
    energy_definition: str | None
    energy_kcal_per_100g: float | None
    protein_g_per_100g: float | None
    fat_g_per_100g: float | None
    carbohydrates_g_per_100g: float | None
    nutrient_data_status: str
    source_snapshot_sha256: str
    notes: str | None
    record_status: str
    created_at_utc: str
    updated_at_utc: str

    @classmethod
    def create(cls, *, profile_id, meal_date, meal_type, food_name, amount_g,
               food_source_type, source_reference, source_version=None,
               energy_definition=None,
               energy_kcal_per_100g=None, protein_g_per_100g=None,
               fat_g_per_100g=None, carbohydrates_g_per_100g=None,
               notes=None, record_id=None, now=None):
        timestamp = now or utc_now()
        normalized_name = text(food_name, "food_name", 200)
        normalized_reference = text(source_reference, "source_reference", 500)
        normalized_version = text(source_version, "source_version", 100, optional=True)
        normalized_energy_definition = text(energy_definition, "energy_definition", 200, optional=True)
        values = {}
        for name, value in (("energy_kcal", energy_kcal_per_100g), ("protein_g", protein_g_per_100g),
                            ("fat_g", fat_g_per_100g), ("carbohydrates_g", carbohydrates_g_per_100g)):
            values[name] = None if value is None else float(decimal_value(value, name))
        known = sum(value is not None for value in values.values())
        status = "unknown_unverified" if known == 0 else ("complete_unverified" if known == 4 else "partial_unverified")
        snapshot = {"basis": "per_100g", "food_name": normalized_name,
                    "food_source_type": food_source_type, "source_reference": normalized_reference,
                    "source_version": normalized_version,
                    "energy_definition": normalized_energy_definition, "nutrients": values}
        candidate = cls(record_id or str(uuid.uuid4()), profile_id, meal_date, meal_type,
                        normalized_name, amount_g, food_source_type, normalized_reference, normalized_version,
                        normalized_energy_definition,
                        values["energy_kcal"], values["protein_g"], values["fat_g"],
                        values["carbohydrates_g"], status, snapshot_digest(snapshot), notes,
                        "active", timestamp, timestamp)
        return validate_record(candidate)

    def to_dict(self):
        return asdict(self)


def validate_record(record):
    record_id = identifier(record.record_id, "record_id")
    profile_id = identifier(record.profile_id, "profile_id")
    if record.meal_type not in MEAL_TYPES:
        raise DietValidationError("Unsupported meal_type")
    if record.food_source_type not in SOURCE_TYPES:
        raise DietValidationError("Only explicitly unverified sources are currently supported")
    if record.record_status not in RECORD_STATUSES:
        raise DietValidationError("Invalid record_status")
    nutrients = {}
    for name in NUTRIENT_NAMES:
        original = getattr(record, name + "_per_100g")
        nutrients[name] = None if original is None else float(decimal_value(original, name))
    known = sum(value is not None for value in nutrients.values())
    expected_status = "unknown_unverified" if known == 0 else ("complete_unverified" if known == 4 else "partial_unverified")
    if record.nutrient_data_status != expected_status:
        raise DietValidationError("nutrient_data_status does not match snapshot completeness")
    food_name = text(record.food_name, "food_name", 200)
    source_reference = text(record.source_reference, "source_reference", 500)
    source_version = text(record.source_version, "source_version", 100, optional=True)
    energy_definition = text(record.energy_definition, "energy_definition", 200, optional=True)
    if (nutrients["energy_kcal"] is None) != (energy_definition is None):
        raise DietValidationError("energy value and energy_definition must be present together")
    notes = text(record.notes, "notes", 1000, optional=True)
    snapshot = {"basis": "per_100g", "food_name": food_name,
                "food_source_type": record.food_source_type, "source_reference": source_reference,
                "source_version": source_version, "energy_definition": energy_definition,
                "nutrients": nutrients}
    if record.source_snapshot_sha256 != snapshot_digest(snapshot):
        raise DietValidationError("Nutrition source snapshot digest mismatch")
    return replace(record, record_id=record_id, profile_id=profile_id,
                   meal_date=iso_date(record.meal_date), food_name=food_name,
                   amount_g=float(decimal_value(record.amount_g, "amount_g", positive=True)),
                   source_reference=source_reference, source_version=source_version, notes=notes,
                   energy_definition=energy_definition,
                   created_at_utc=utc_timestamp(record.created_at_utc, "created_at_utc"),
                   updated_at_utc=utc_timestamp(record.updated_at_utc, "updated_at_utc"),
                   energy_kcal_per_100g=nutrients["energy_kcal"],
                   protein_g_per_100g=nutrients["protein_g"], fat_g_per_100g=nutrients["fat_g"],
                   carbohydrates_g_per_100g=nutrients["carbohydrates_g"])
