"""Central validation for supported general-adult health profiles."""
from dataclasses import replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
import re

ADULT_AGE_RANGE = (18, 100)
HEIGHT_CM_RANGE = (100, 250)
WEIGHT_KG_RANGE = (30, 350)
SEX_EQUATION_OPTIONS = frozenset({"male_parameter", "female_parameter"})
ACTIVITY_LEVELS = frozenset({"sedentary", "lightly_active", "moderately_active",
                             "very_active", "extra_active"})
HEALTH_GOALS = frozenset({"maintain_weight", "lose_weight", "gain_weight"})
POPULATION_STATUSES = frozenset({"general_adult", "pregnant", "lactating", "special_assessment"})
PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


class ProfileValidationError(ValueError):
    pass


def finite_decimal(value, name, *, positive=False, allow_zero=True):
    if isinstance(value, bool):
        raise ProfileValidationError(f"{name} must be a finite number")
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise ProfileValidationError(f"{name} must be a finite number") from None
    if not number.is_finite() or (positive and number <= 0) or (not allow_zero and number == 0):
        raise ProfileValidationError(f"{name} is outside the supported range")
    return number


def _timestamp(value, name):
    if not isinstance(value, str) or len(value) > 40:
        raise ProfileValidationError(f"Invalid {name}")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise ProfileValidationError(f"Invalid {name}") from None
    return value


def validate_profile(profile):
    if not isinstance(profile.profile_id, str) or not PROFILE_ID.fullmatch(profile.profile_id):
        raise ProfileValidationError("Invalid local profile ID")
    if isinstance(profile.age, bool) or not isinstance(profile.age, int) or not ADULT_AGE_RANGE[0] <= profile.age <= ADULT_AGE_RANGE[1]:
        raise ProfileValidationError("Only supported adult ages are accepted")
    height = finite_decimal(profile.height_cm, "height_cm", positive=True)
    weight = finite_decimal(profile.weight_kg, "weight_kg", positive=True)
    if not Decimal(HEIGHT_CM_RANGE[0]) <= height <= Decimal(HEIGHT_CM_RANGE[1]):
        raise ProfileValidationError("height_cm is outside the supported range")
    if not Decimal(WEIGHT_KG_RANGE[0]) <= weight <= Decimal(WEIGHT_KG_RANGE[1]):
        raise ProfileValidationError("weight_kg is outside the supported range")
    if profile.sex_for_equation not in SEX_EQUATION_OPTIONS:
        raise ProfileValidationError("Unsupported equation parameter option")
    if profile.activity_level not in ACTIVITY_LEVELS:
        raise ProfileValidationError("Unsupported activity level")
    if profile.health_goal not in HEALTH_GOALS:
        raise ProfileValidationError("Unsupported health goal")
    if profile.population_status not in POPULATION_STATUSES:
        raise ProfileValidationError("Unsupported population status")
    if not isinstance(profile.dietary_restrictions, (tuple, list)) or len(profile.dietary_restrictions) > 20:
        raise ProfileValidationError("dietary_restrictions must be a short list")
    restrictions = []
    for value in profile.dietary_restrictions:
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
            raise ProfileValidationError("Invalid dietary restriction")
        normalized = value.strip()
        if normalized not in restrictions:
            restrictions.append(normalized)
    return replace(profile, height_cm=float(height), weight_kg=float(weight),
                   dietary_restrictions=tuple(restrictions),
                   created_at_utc=_timestamp(profile.created_at_utc, "created_at_utc"),
                   updated_at_utc=_timestamp(profile.updated_at_utc, "updated_at_utc"))


def require_general_adult(profile):
    validated = validate_profile(profile)
    if validated.population_status != "general_adult":
        raise ProfileValidationError(
            "Pregnancy, lactation and other special populations require professional assessment")
    return validated
