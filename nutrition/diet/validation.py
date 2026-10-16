"""Validation rules for local, unverified diet record snapshots."""
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

MEAL_TYPES = frozenset({"breakfast", "lunch", "dinner", "snack"})
SOURCE_TYPES = frozenset({"manual_unverified", "external_unverified"})
RECORD_STATUSES = frozenset({"active", "revoked"})
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
NUTRIENT_NAMES = ("energy_kcal", "protein_g", "fat_g", "carbohydrates_g")


class DietValidationError(ValueError):
    pass


def decimal_value(value, name, *, positive=False):
    if isinstance(value, bool):
        raise DietValidationError(f"{name} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise DietValidationError(f"{name} must be a finite number") from None
    if not number.is_finite() or number < 0 or (positive and number <= 0):
        raise DietValidationError(f"Invalid {name}")
    return number


def iso_date(value):
    if not isinstance(value, str):
        raise DietValidationError("meal_date must be ISO date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise DietValidationError("meal_date must be ISO date") from None
    if parsed.isoformat() != value:
        raise DietValidationError("meal_date must be canonical ISO date")
    return value


def utc_timestamp(value, name):
    if not isinstance(value, str):
        raise DietValidationError(f"Invalid {name}")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise DietValidationError(f"Invalid {name}") from None
    return value


def text(value, name, maximum, *, optional=False):
    if optional and (value is None or value == ""):
        return None
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise DietValidationError(f"Invalid {name}")
    return value.strip()


def identifier(value, name):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise DietValidationError(f"Invalid {name}")
    return value
