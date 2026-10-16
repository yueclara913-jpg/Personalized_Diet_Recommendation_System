"""Normalized gap-improvement and incremental-overshoot scoring."""
from decimal import Decimal, ROUND_HALF_UP

from .models import NUTRIENTS


def score_portion(targets, consumed, contribution, policy, *, baseline=False):
    improvement_weights = dict(policy.improvement_weights)
    overshoot_weights = dict(policy.overshoot_weights)
    components = {}
    improvement_total = Decimal("0")
    overshoot_total = Decimal("0")
    for nutrient in NUTRIENTS:
        target = targets[nutrient]
        before = consumed[nutrient]
        after = before + contribution[nutrient]
        before_distance = abs(target - before) / target
        after_distance = abs(target - after) / target
        improvement = before_distance - after_distance
        incremental_overshoot = (max(after - target, Decimal("0")) -
                                 max(before - target, Decimal("0"))) / target
        iw = Decimal("1") if baseline and nutrient == "energy" else (
            Decimal("0") if baseline else improvement_weights[nutrient])
        ow = Decimal("1") if baseline and nutrient == "energy" else (
            Decimal("0") if baseline else overshoot_weights[nutrient])
        weighted_improvement = improvement * iw
        weighted_overshoot = incremental_overshoot * ow
        improvement_total += weighted_improvement
        overshoot_total += weighted_overshoot
        components[nutrient] = {
            "normalized_gap_improvement": float(improvement.quantize(policy.score_places, rounding=ROUND_HALF_UP)),
            "incremental_overshoot": float(incremental_overshoot.quantize(policy.score_places, rounding=ROUND_HALF_UP)),
            "weighted_improvement": float(weighted_improvement.quantize(policy.score_places, rounding=ROUND_HALF_UP)),
            "weighted_overshoot_penalty": float(weighted_overshoot.quantize(policy.score_places, rounding=ROUND_HALF_UP))}
    return improvement_total, overshoot_total, components
