"""Daily diet records, intake aggregation and profile-target integration."""
from .calculations import calculate_daily_intake, calculate_meal_intake, scale_record_nutrients
from .models import DietRecord
from .services import analyze_daily_nutrition

__all__ = ["DietRecord", "scale_record_nutrients", "calculate_meal_intake",
           "calculate_daily_intake", "analyze_daily_nutrition"]
