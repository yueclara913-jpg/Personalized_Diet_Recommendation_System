"""Validation and trust-boundary checks for recommendation inputs."""
from decimal import Decimal, InvalidOperation
import re

from nutrition.profile.calculations import calculate_daily_targets, calculate_nutrition_gaps
from nutrition.profile.validation import validate_profile
from .models import FoodCandidate, NUTRIENTS, RecommendationPolicy

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ALLOWED_ALLERGENS = frozenset({"milk", "egg", "peanut", "tree_nut", "soy", "wheat",
                               "fish", "shellfish", "sesame"})
ALLOWED_DIETARY_TAGS = frozenset({"vegetarian", "vegan", "contains_pork", "contains_alcohol"})
ALLOWED_EXCLUSIONS = frozenset({"contains_pork", "contains_alcohol"})
ALLOWED_REQUIREMENTS = frozenset({"vegetarian", "vegan"})


class RecommendationValidationError(ValueError):
    pass


def decimal_value(value, name, *, positive=False):
    if isinstance(value, bool):
        raise RecommendationValidationError(f"{name} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise RecommendationValidationError(f"{name} must be a finite number") from None
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise RecommendationValidationError(f"invalid {name}")
    return result


def _text(value, name, maximum=500):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise RecommendationValidationError(f"invalid {name}")
    return value.strip()


def validate_policy(policy):
    if not isinstance(policy, RecommendationPolicy):
        raise RecommendationValidationError("policy must be RecommendationPolicy")
    if not ID.fullmatch(policy.version):
        raise RecommendationValidationError("invalid policy version")
    expected = set(NUTRIENTS)
    result = {}
    for label, pairs in (("improvement", policy.improvement_weights),
                         ("overshoot", policy.overshoot_weights)):
        if not isinstance(pairs, tuple) or {key for key, _ in pairs} != expected or len(pairs) != 4:
            raise RecommendationValidationError(f"invalid {label} weights")
        values = {key: decimal_value(value, f"{label}.{key}") for key, value in pairs}
        if any(value < 0 for value in values.values()) or sum(values.values()) != Decimal("1"):
            raise RecommendationValidationError(f"{label} weights must be nonnegative and sum to 1")
        result[label] = values
    penalty = decimal_value(policy.portion_penalty_weight, "portion_penalty_weight")
    if penalty > Decimal("1"):
        raise RecommendationValidationError("portion penalty exceeds supported range")
    if isinstance(policy.maximum_portion_options, bool) or not 1 <= policy.maximum_portion_options <= 1000:
        raise RecommendationValidationError("invalid maximum_portion_options")
    if policy.score_places not in {Decimal("0.000001"), Decimal("0.0001")}:
        raise RecommendationValidationError("unsupported score precision")
    return result["improvement"], result["overshoot"], penalty


def validate_candidate(candidate):
    if not isinstance(candidate, FoodCandidate):
        raise RecommendationValidationError("food candidates must be FoodCandidate objects")
    for name in ("food_id", "source_version"):
        if not ID.fullmatch(_text(getattr(candidate, name), name, 128)):
            raise RecommendationValidationError(f"invalid {name}")
    _text(candidate.food_name, "food_name", 200)
    _text(candidate.source_reference, "source_reference")
    if candidate.source_type != "TEST_ONLY":
        raise RecommendationValidationError("candidate source is not eligible for evaluation")
    if candidate.candidate_status != "TEST_ONLY_EVALUATION":
        raise RecommendationValidationError("candidate status is not eligible for evaluation")
    if candidate.nutrition_basis != "per_100g":
        raise RecommendationValidationError("only explicit per_100g candidates are supported")
    _text(candidate.energy_definition, "energy_definition", 200)
    if not SHA256.fullmatch(candidate.source_snapshot_sha256):
        raise RecommendationValidationError("invalid source snapshot SHA-256")
    nutrients = {nutrient: decimal_value(getattr(candidate, nutrient + ("_kcal_per_100g" if nutrient == "energy" else "_g_per_100g")), nutrient)
                 for nutrient in NUTRIENTS}
    minimum = decimal_value(candidate.minimum_portion_g, "minimum_portion_g", positive=True)
    maximum = decimal_value(candidate.maximum_portion_g, "maximum_portion_g", positive=True)
    step = decimal_value(candidate.portion_step_g, "portion_step_g", positive=True)
    if minimum > maximum:
        raise RecommendationValidationError("minimum portion exceeds maximum portion")
    for name, tags, allowed in (("allergen_tags", candidate.allergen_tags, ALLOWED_ALLERGENS),
                                ("dietary_tags", candidate.dietary_tags, ALLOWED_DIETARY_TAGS)):
        if tags is not None and (not isinstance(tags, tuple) or len(tags) != len(set(tags)) or
                                 any(tag not in allowed for tag in tags)):
            raise RecommendationValidationError(f"invalid {name}")
    return nutrients, minimum, maximum, step


def parse_restrictions(profile):
    allergens, exclusions, requirements = set(), set(), set()
    for raw in profile.dietary_restrictions:
        if raw.startswith("allergen:") and raw[9:] in ALLOWED_ALLERGENS:
            allergens.add(raw[9:])
        elif raw.startswith("exclude:") and raw[8:] in ALLOWED_EXCLUSIONS:
            exclusions.add(raw[8:])
        elif raw.startswith("require:") and raw[8:] in ALLOWED_REQUIREMENTS:
            requirements.add(raw[8:])
        else:
            raise RecommendationValidationError(
                "dietary restrictions require controlled_restrictions_v1 tokens")
    return allergens, exclusions, requirements


def validate_daily_analysis(profile, analysis):
    item = validate_profile(profile)
    if not isinstance(analysis, dict) or analysis.get("profile_id") != item.profile_id:
        raise RecommendationValidationError("daily analysis profile mismatch")
    expected_targets = calculate_daily_targets(item)  # ManualReviewRequired intentionally propagates.
    targets = analysis.get("targets")
    if not isinstance(targets, dict) or targets.get("target_status") != "estimated_general_adult_reference":
        raise RecommendationValidationError("daily target status is not usable")
    checked_target_keys = ("target_energy_kcal", "protein_target_g", "fat_target_g",
                           "carbohydrate_target_g", "formula_version", "applicability_policy_version")
    if any(targets.get(key) != expected_targets.get(key) for key in checked_target_keys):
        raise RecommendationValidationError("daily targets do not match the validated profile")
    intake = analysis.get("intake")
    if not isinstance(intake, dict) or intake.get("profile_id") != item.profile_id:
        raise RecommendationValidationError("daily intake profile mismatch")
    consumed = {}
    missing = []
    mapping = {"energy": "energy_kcal", "protein": "protein_g", "fat": "fat_g",
               "carbohydrates": "carbohydrates_g"}
    for nutrient, field in mapping.items():
        entry = intake.get(field)
        if not isinstance(entry, dict) or entry.get("data_status") != "complete" or entry.get("total") is None:
            missing.append(nutrient)
        else:
            consumed[nutrient] = float(decimal_value(entry["total"], f"intake.{field}"))
    if missing:
        return None, missing
    expected_gaps = calculate_nutrition_gaps(expected_targets, consumed)
    supplied_gaps = analysis.get("gaps")
    if not isinstance(supplied_gaps, dict) or supplied_gaps.get("data_completeness") != "complete":
        raise RecommendationValidationError("nutrition gaps are not complete")
    for nutrient in NUTRIENTS:
        supplied = supplied_gaps.get("nutrients", {}).get(nutrient)
        expected = expected_gaps["nutrients"][nutrient]
        if not isinstance(supplied, dict) or any(supplied.get(key) != expected.get(key)
                for key in ("target", "consumed", "remaining", "exceeded", "excess", "data_status")):
            raise RecommendationValidationError("nutrition gap arithmetic is inconsistent")
    return {key: Decimal(str(value)) for key, value in consumed.items()}, []
