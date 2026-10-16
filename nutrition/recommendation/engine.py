"""Internal deterministic recommendation service; no HTTP or database writes."""
from decimal import Decimal, ROUND_HALF_UP

from nutrition.profile.validation import validate_profile
from .constraints import exclusion_reasons
from .explanations import build_explanation
from .models import (ALGORITHM_VERSION, BASELINE_VERSION, NUTRIENTS, NUTRIENT_UNITS,
                     RESTRICTION_POLICY_VERSION, RecommendationPolicy)
from .portions import enumerate_portions, scale_nutrients
from .scoring import score_portion
from .validation import (RecommendationValidationError, parse_restrictions, validate_candidate,
                         validate_daily_analysis, validate_policy)


def _targets(analysis):
    values = analysis["targets"]
    return {"energy": Decimal(str(values["target_energy_kcal"])),
            "protein": Decimal(str(values["protein_target_g"])),
            "fat": Decimal(str(values["fat_target_g"])),
            "carbohydrates": Decimal(str(values["carbohydrate_target_g"]))}


def recommend_foods(profile, daily_analysis, food_candidates, *, top_n=5, mode="evaluation",
                    policy=None, algorithm="proposed"):
    item = validate_profile(profile)
    if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= 100:
        raise RecommendationValidationError("top_n must be an integer from 1 to 100")
    if mode not in {"evaluation", "production"}:
        raise RecommendationValidationError("unsupported recommendation mode")
    if algorithm not in {"proposed", "baseline"}:
        raise RecommendationValidationError("unsupported algorithm")
    selected_policy = policy or RecommendationPolicy()
    validate_policy(selected_policy)
    consumed, missing = validate_daily_analysis(item, daily_analysis)
    base = {"status": "ok", "algorithm_version": (selected_policy.version if algorithm == "proposed" else BASELINE_VERSION),
            "policy_version": selected_policy.version, "restriction_policy_version": RESTRICTION_POLICY_VERSION,
            "mode": mode, "algorithm": algorithm, "recommendations": [],
            "excluded_candidates": [], "warnings": []}
    if missing:
        return {**base, "status": "insufficient_intake_data", "missing_nutrients": missing,
                "warnings": ["no_records、unknown 或 partial 不等于零摄入，不能生成精确缺口推荐。"]}
    if mode == "production":
        return {**base, "status": "no_eligible_candidates",
                "warnings": ["当前没有独立核验并正式发布的 production 食品目录。"]}
    if not isinstance(food_candidates, (list, tuple)):
        raise RecommendationValidationError("food_candidates must be a sequence")
    allergens, exclusions, requirements = parse_restrictions(item)
    targets = _targets(daily_analysis)
    ranked = []
    seen = set()
    for candidate in food_candidates:
        candidate_id = getattr(candidate, "food_id", None)
        try:
            nutrients, minimum, maximum, step = validate_candidate(candidate)
            if candidate.food_id in seen:
                raise RecommendationValidationError("duplicate food_id")
            seen.add(candidate.food_id)
            reasons = exclusion_reasons(candidate, allergens, exclusions, requirements)
            if reasons:
                base["excluded_candidates"].append({"food_id": candidate.food_id, "reason_codes": reasons})
                continue
            portions = enumerate_portions(minimum, maximum, step,
                                          selected_policy.maximum_portion_options)
        except RecommendationValidationError as exc:
            base["excluded_candidates"].append({"food_id": candidate_id,
                                                "reason_codes": ["INVALID_CANDIDATE"],
                                                "detail": str(exc)})
            continue
        candidate_ranked = []
        for portion in portions:
            contribution = scale_nutrients(nutrients, portion)
            improvement, overshoot, components = score_portion(
                targets, consumed, contribution, selected_policy, baseline=algorithm == "baseline")
            span = maximum - minimum
            portion_fraction = Decimal("0") if span == 0 else (portion - minimum) / span
            portion_penalty = selected_policy.portion_penalty_weight * portion_fraction
            score = improvement - overshoot - portion_penalty
            display_score = score.quantize(selected_policy.score_places, rounding=ROUND_HALF_UP)
            candidate_ranked.append({"food_id": candidate.food_id, "food_name": candidate.food_name,
                           "portion_g": float(portion), "nutrition_contribution": {k: float(v) for k, v in contribution.items()},
                           "nutrient_units": NUTRIENT_UNITS, "score": float(display_score),
                           "score_components": components,
                           "portion_penalty": float(portion_penalty.quantize(selected_policy.score_places, rounding=ROUND_HALF_UP)),
                           "source": {"type": candidate.source_type, "reference": candidate.source_reference,
                                      "version": candidate.source_version,
                                      "snapshot_sha256": candidate.source_snapshot_sha256,
                                      "nutrition_basis": candidate.nutrition_basis,
                                      "energy_definition": candidate.energy_definition},
                           "test_only": True,
                           "explanation": build_explanation(candidate.food_name, portion, contribution, components)})
        candidate_ranked.sort(key=lambda row: (-Decimal(str(row["score"])),
                                                Decimal(str(row["portion_g"]))))
        ranked.append(candidate_ranked[0])
    ranked.sort(key=lambda row: (-Decimal(str(row["score"])), row["food_id"], Decimal(str(row["portion_g"]))))
    base["recommendations"] = ranked[:top_n]
    if not ranked:
        base["status"] = "no_eligible_candidates"
    base["warnings"] = ["TEST_ONLY evaluation result; not a verified production nutrition recommendation.",
                        "Weights are engineering experiment parameters without clinical validation."]
    return base
