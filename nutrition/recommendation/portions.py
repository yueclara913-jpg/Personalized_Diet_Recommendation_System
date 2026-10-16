"""Bounded Decimal portion enumeration and per-portion scaling."""
from decimal import Decimal, ROUND_HALF_UP

from .validation import RecommendationValidationError

DISPLAY = Decimal("0.01")


def enumerate_portions(minimum, maximum, step, maximum_options):
    count = int(((maximum - minimum) / step).to_integral_value(rounding="ROUND_FLOOR")) + 1
    if count > maximum_options:
        raise RecommendationValidationError("portion range exceeds enumeration limit")
    return tuple(minimum + step * index for index in range(count))


def scale_nutrients(nutrients, portion_g):
    factor = portion_g / Decimal("100")
    return {key: (value * factor).quantize(DISPLAY, rounding=ROUND_HALF_UP)
            for key, value in nutrients.items()}
