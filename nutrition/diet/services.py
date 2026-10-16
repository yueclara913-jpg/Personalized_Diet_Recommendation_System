"""Profile-scoped daily analysis reusing PR #11 target and gap services."""
from nutrition.profile.calculations import calculate_daily_targets, calculate_nutrition_gaps
from nutrition.profile.validation import validate_profile
from .calculations import calculate_daily_intake
from .models import validate_record
from .validation import DietValidationError


def analyze_daily_nutrition(profile, records, meal_date):
    item = validate_profile(profile)
    checked = [validate_record(record) for record in records]
    if any(record.profile_id != item.profile_id for record in checked):
        raise DietValidationError("Diet records do not belong to the selected profile")
    targets = calculate_daily_targets(item)  # ManualReviewRequired intentionally propagates.
    intake = calculate_daily_intake(checked, meal_date, profile_id=item.profile_id)
    consumed = {}
    for nutrient, target_name in (("energy", "energy_kcal"), ("protein", "protein_g"),
                                  ("fat", "fat_g"), ("carbohydrates", "carbohydrates_g")):
        field = intake[target_name]
        consumed[nutrient] = field["total"] if field["data_status"] == "complete" else None
    gaps = calculate_nutrition_gaps(targets, consumed)
    return {"profile_id": item.profile_id, "meal_date": meal_date, "targets": targets,
            "intake": intake, "gaps": gaps,
            "warning": "Reference arithmetic only; no disease diagnosis or treatment advice."}
