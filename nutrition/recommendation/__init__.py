"""Deterministic, TEST_ONLY nutrition-gap recommendation core."""

from .engine import recommend_foods
from .models import FoodCandidate, RecommendationPolicy
from .validation import RecommendationValidationError

__all__ = ["FoodCandidate", "RecommendationPolicy", "RecommendationValidationError",
           "recommend_foods"]
