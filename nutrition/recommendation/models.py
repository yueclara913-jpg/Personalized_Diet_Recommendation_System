"""Typed models for the explainable recommendation experiment."""
from dataclasses import dataclass
from decimal import Decimal

ALGORITHM_VERSION = "nutrition_gap_recommender_v1"
BASELINE_VERSION = "energy_gap_baseline_v1"
RESTRICTION_POLICY_VERSION = "controlled_restrictions_v1"


@dataclass(frozen=True)
class FoodCandidate:
    food_id: str
    food_name: str
    source_type: str
    source_reference: str
    source_version: str
    nutrition_basis: str
    energy_definition: str
    energy_kcal_per_100g: object
    protein_g_per_100g: object
    fat_g_per_100g: object
    carbohydrates_g_per_100g: object
    allergen_tags: tuple[str, ...] | None
    dietary_tags: tuple[str, ...] | None
    minimum_portion_g: object
    maximum_portion_g: object
    portion_step_g: object
    candidate_status: str
    source_snapshot_sha256: str


@dataclass(frozen=True)
class RecommendationPolicy:
    version: str = ALGORITHM_VERSION
    improvement_weights: tuple[tuple[str, Decimal], ...] = (
        ("energy", Decimal("0.25")), ("protein", Decimal("0.35")),
        ("fat", Decimal("0.20")), ("carbohydrates", Decimal("0.20")))
    overshoot_weights: tuple[tuple[str, Decimal], ...] = (
        ("energy", Decimal("0.35")), ("protein", Decimal("0.20")),
        ("fat", Decimal("0.25")), ("carbohydrates", Decimal("0.20")))
    portion_penalty_weight: Decimal = Decimal("0.02")
    maximum_portion_options: int = 100
    score_places: Decimal = Decimal("0.000001")


NUTRIENTS = ("energy", "protein", "fat", "carbohydrates")
NUTRIENT_UNITS = {"energy": "kcal", "protein": "g", "fat": "g", "carbohydrates": "g"}
