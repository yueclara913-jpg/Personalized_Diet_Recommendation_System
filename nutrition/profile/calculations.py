"""Explainable estimates for generally healthy adults; never medical diagnosis."""
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from .validation import ProfileValidationError, finite_decimal, require_general_adult

BMI_FORMULA_VERSION = "bmi_metric_v1"
BMI_CLASSIFICATION_VERSION = "who_adult_reference_v1"
BMR_FORMULA_VERSION = "mifflin_st_jeor_adult_v1"
TARGET_POLICY_VERSION = "general_adult_energy_policy_v1"
TARGET_APPLICABILITY_POLICY_VERSION = "general_adult_target_applicability_v1"
MACRO_POLICY_VERSION = "energy_ratio_4_4_9_v1"
GAP_VERSION = "nutrition_gap_v1"

BMI_THRESHOLDS = ((Decimal("18.5"), "below_reference_range"),
                  (Decimal("25"), "reference_range"),
                  (Decimal("30"), "above_reference_range"),
                  (None, "high_reference_range"))
ACTIVITY_FACTORS = {"sedentary": Decimal("1.20"), "lightly_active": Decimal("1.375"),
                    "moderately_active": Decimal("1.55"), "very_active": Decimal("1.725"),
                    "extra_active": Decimal("1.90")}
GOAL_ADJUSTMENTS = {"maintain_weight": Decimal("0"), "lose_weight": Decimal("-300"),
                    "gain_weight": Decimal("250")}
DEFAULT_MACRO_RATIOS = {"protein": Decimal("0.25"), "fat": Decimal("0.30"),
                        "carbohydrates": Decimal("0.45")}
ENERGY_PER_GRAM = {"protein": Decimal("4"), "fat": Decimal("9"), "carbohydrates": Decimal("4")}
NUTRIENT_FIELDS = {"energy": ("target_energy_kcal", "kcal"),
                   "protein": ("protein_target_g", "g"),
                   "fat": ("fat_target_g", "g"),
                   "carbohydrates": ("carbohydrate_target_g", "g")}


@dataclass(frozen=True)
class TargetApplicabilityPolicy:
    version: str = TARGET_APPLICABILITY_POLICY_VERSION
    minimum_bmi: Decimal = Decimal("15")
    maximum_bmi: Decimal = Decimal("40")
    minimum_tdee_kcal: Decimal = Decimal("1000")
    maximum_tdee_kcal: Decimal = Decimal("5000")
    minimum_target_energy_kcal: Decimal = Decimal("1000")
    maximum_target_energy_kcal: Decimal = Decimal("5000")
    minimum_target_to_tdee_ratio: Decimal = Decimal("0.80")
    maximum_target_to_tdee_ratio: Decimal = Decimal("1.20")
    minimum_protein_g_per_kg: Decimal = Decimal("0.60")
    maximum_protein_g_per_kg: Decimal = Decimal("3.00")
    minimum_protein_g: Decimal = Decimal("30")
    maximum_protein_g: Decimal = Decimal("250")
    minimum_fat_g: Decimal = Decimal("20")
    maximum_fat_g: Decimal = Decimal("200")
    minimum_carbohydrate_g: Decimal = Decimal("50")
    maximum_carbohydrate_g: Decimal = Decimal("600")


DEFAULT_TARGET_APPLICABILITY_POLICY = TargetApplicabilityPolicy()


class ManualReviewRequired(ValueError):
    """Fail-closed result with audit details but no usable nutrition targets."""

    def __init__(self, message, *, policy_version=None, issues=None):
        super().__init__(message)
        self.status = "manual_review_required"
        self.policy_version = policy_version
        self.issues = tuple(issues or ())


def _rounded(value, places="0.01"):
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def calculate_bmi(weight_kg, height_cm, thresholds=BMI_THRESHOLDS):
    weight = finite_decimal(weight_kg, "weight_kg", positive=True)
    height = finite_decimal(height_cm, "height_cm", positive=True)
    if not Decimal("100") <= height <= Decimal("250") or not Decimal("30") <= weight <= Decimal("350"):
        raise ProfileValidationError("BMI inputs are outside the supported adult range")
    raw = weight / ((height / Decimal("100")) ** 2)
    category = next(label for upper, label in thresholds if upper is None or raw < upper)
    return {"bmi_raw": float(raw), "bmi_rounded": _rounded(raw), "category": category,
            "formula_version": BMI_FORMULA_VERSION,
            "classification_version": BMI_CLASSIFICATION_VERSION,
            "input_units": {"weight": "kg", "height": "cm"},
            "warning": "BMI is a screening estimate and not a diagnosis or complete health assessment."}


def calculate_bmr(profile):
    item = require_general_adult(profile)
    offset = Decimal("5") if item.sex_for_equation == "male_parameter" else Decimal("-161")
    value = (Decimal("10") * Decimal(str(item.weight_kg)) +
             Decimal("6.25") * Decimal(str(item.height_cm)) - Decimal("5") * item.age + offset)
    if value <= 0:
        raise ManualReviewRequired("Estimated BMR is not technically usable")
    return {"estimated_bmr_kcal": _rounded(value), "raw_bmr_kcal": float(value),
            "unit": "kcal/day", "formula_version": BMR_FORMULA_VERSION,
            "sex_for_equation": item.sex_for_equation,
            "assumption": "Mifflin-St Jeor estimate for a generally healthy adult."}


def calculate_tdee(profile):
    item = require_general_adult(profile)
    bmr = calculate_bmr(item)
    factor = ACTIVITY_FACTORS[item.activity_level]
    value = Decimal(str(bmr["raw_bmr_kcal"])) * factor
    return {**bmr, "estimated_tdee_kcal": _rounded(value), "raw_tdee_kcal": float(value),
            "activity_level": item.activity_level, "activity_factor": float(factor),
            "tdee_formula_version": "bmr_times_activity_factor_v1",
            "warning": "TDEE is an estimate, not a precise measurement of daily expenditure."}


def _ratios(values):
    if not isinstance(values, dict) or set(values) != set(DEFAULT_MACRO_RATIOS):
        raise ProfileValidationError("Exactly three macronutrient ratios are required")
    result = {key: finite_decimal(value, key) for key, value in values.items()}
    if any(value <= 0 or value >= 1 for value in result.values()) or sum(result.values()) != Decimal("1"):
        raise ProfileValidationError("Macronutrient ratios must be positive and sum exactly to 1")
    return result


def _outside(value, minimum, maximum):
    return value < minimum or value > maximum


def validate_daily_target_applicability(profile, estimated_tdee_kcal, target_energy_kcal,
                                        macro_ratios, policy=DEFAULT_TARGET_APPLICABILITY_POLICY):
    """Reject candidates outside conservative engineering automation limits."""
    item = require_general_adult(profile)
    if not isinstance(policy, TargetApplicabilityPolicy):
        raise TypeError("policy must be TargetApplicabilityPolicy")
    tdee = finite_decimal(estimated_tdee_kcal, "estimated_tdee_kcal", positive=True)
    target = finite_decimal(target_energy_kcal, "target_energy_kcal", positive=True)
    ratios = _ratios(macro_ratios)
    bmi = Decimal(str(calculate_bmi(item.weight_kg, item.height_cm)["bmi_raw"]))
    grams = {key: target * ratio / ENERGY_PER_GRAM[key] for key, ratio in ratios.items()}
    protein_per_kg = grams["protein"] / Decimal(str(item.weight_kg))
    target_to_tdee = target / tdee
    issues = []

    def check(rule_id, value, minimum, maximum, unit):
        if _outside(value, minimum, maximum):
            issues.append({"rule_id": rule_id, "observed": float(value), "unit": unit,
                           "supported_minimum": float(minimum), "supported_maximum": float(maximum)})

    check("APP001_EXTREME_BMI", bmi, policy.minimum_bmi, policy.maximum_bmi, "kg/m2")
    check("APP002_TDEE_RANGE", tdee, policy.minimum_tdee_kcal, policy.maximum_tdee_kcal, "kcal/day")
    check("APP003_TARGET_ENERGY_RANGE", target, policy.minimum_target_energy_kcal,
          policy.maximum_target_energy_kcal, "kcal/day")
    check("APP004_TARGET_TDEE_RATIO", target_to_tdee, policy.minimum_target_to_tdee_ratio,
          policy.maximum_target_to_tdee_ratio, "ratio")
    check("APP005_PROTEIN_G_PER_KG", protein_per_kg, policy.minimum_protein_g_per_kg,
          policy.maximum_protein_g_per_kg, "g/kg/day")
    check("APP006_PROTEIN_RANGE", grams["protein"], policy.minimum_protein_g,
          policy.maximum_protein_g, "g/day")
    check("APP007_FAT_RANGE", grams["fat"], policy.minimum_fat_g, policy.maximum_fat_g, "g/day")
    check("APP008_CARBOHYDRATE_RANGE", grams["carbohydrates"], policy.minimum_carbohydrate_g,
          policy.maximum_carbohydrate_g, "g/day")
    if issues:
        raise ManualReviewRequired(
            "Candidate daily targets are outside the supported automatic-calculation envelope",
            policy_version=policy.version, issues=issues)
    return {"bmi": bmi, "target_to_tdee_ratio": target_to_tdee,
            "protein_g_per_kg": protein_per_kg, "grams": grams}


def calculate_daily_targets(profile, *, macro_ratios=None, goal_adjustments=None):
    item = require_general_adult(profile)
    tdee = calculate_tdee(item)
    adjustments = dict(GOAL_ADJUSTMENTS if goal_adjustments is None else goal_adjustments)
    if set(adjustments) != set(GOAL_ADJUSTMENTS):
        raise ProfileValidationError("All supported goal adjustments are required")
    adjustments = {key: finite_decimal(value, key) for key, value in adjustments.items()}
    if any(value < -500 or value > 500 for value in adjustments.values()):
        raise ProfileValidationError("Goal adjustment exceeds the conservative configured bounds")
    adjustment = adjustments[item.health_goal]
    raw_tdee = Decimal(str(tdee["raw_tdee_kcal"]))
    target = raw_tdee + adjustment
    ratios = _ratios(DEFAULT_MACRO_RATIOS if macro_ratios is None else macro_ratios)
    applicability = validate_daily_target_applicability(
        item, raw_tdee, target, ratios, DEFAULT_TARGET_APPLICABILITY_POLICY)
    grams = applicability["grams"]
    return {"estimated_bmr_kcal": tdee["estimated_bmr_kcal"],
            "estimated_tdee_kcal": tdee["estimated_tdee_kcal"],
            "goal_adjustment_kcal": float(adjustment), "target_energy_kcal": _rounded(target),
            "protein_target_g": _rounded(grams["protein"]), "fat_target_g": _rounded(grams["fat"]),
            "carbohydrate_target_g": _rounded(grams["carbohydrates"]),
            "macro_energy_ratios": {key: float(value) for key, value in ratios.items()},
            "activity_factor": tdee["activity_factor"], "formula_version": TARGET_POLICY_VERSION,
            "applicability_policy_version": DEFAULT_TARGET_APPLICABILITY_POLICY.version,
            "bmr_formula_version": BMR_FORMULA_VERSION, "macro_formula_version": MACRO_POLICY_VERSION,
            "target_status": "estimated_general_adult_reference",
            "assumptions": ["Generally healthy adult", "Stable metric inputs", "4-4-9 macro conversion"],
            "warnings": ["Reference estimate only; not diagnosis, treatment or guaranteed weight change.",
                         "BMI does not determine the energy adjustment."],
            "rounding": "ROUND_HALF_UP to 0.01 for displayed energy and grams",
            "units": {"energy": "kcal/day", "protein": "g/day", "fat": "g/day",
                      "carbohydrates": "g/day"}}


def calculate_nutrition_gaps(targets, consumed):
    if not isinstance(targets, dict) or not isinstance(consumed, dict):
        raise ProfileValidationError("targets and consumed must be mappings")
    result, complete = {}, True
    for nutrient, (target_key, unit) in NUTRIENT_FIELDS.items():
        if target_key not in targets:
            raise ProfileValidationError(f"Missing target for {nutrient}")
        target = finite_decimal(targets[target_key], target_key, positive=True)
        if nutrient not in consumed or consumed[nutrient] is None:
            complete = False
            result[nutrient] = {"target": float(target), "consumed": None, "difference": None,
                                "remaining": None, "completion_ratio": None, "exceeded": None,
                                "excess": None, "nutrient_unit": unit,
                                "data_status": "unknown_consumption"}
            continue
        amount = finite_decimal(consumed[nutrient], nutrient)
        if amount < 0:
            raise ProfileValidationError(f"Negative consumption for {nutrient}")
        difference = target - amount
        result[nutrient] = {"target": float(target), "consumed": float(amount),
                            "difference": _rounded(difference),
                            "remaining": _rounded(max(difference, Decimal("0"))),
                            "completion_ratio": _rounded(amount / target, "0.0001"),
                            "exceeded": amount > target,
                            "excess": _rounded(max(-difference, Decimal("0"))),
                            "nutrient_unit": unit, "data_status": "complete"}
    return {"formula_version": GAP_VERSION,
            "data_completeness": "complete" if complete else "partial",
            "nutrients": result,
            "warning": "Nutrition gaps describe arithmetic differences only and are not disease-risk diagnoses."}
