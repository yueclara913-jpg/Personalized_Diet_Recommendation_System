"""Minimal, identity-free local health profile model."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import uuid


def utc_now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


@dataclass(frozen=True)
class HealthProfile:
    profile_id: str
    age: int
    height_cm: float
    weight_kg: float
    sex_for_equation: str
    activity_level: str
    health_goal: str
    dietary_restrictions: tuple[str, ...] = ()
    population_status: str = "general_adult"
    created_at_utc: str = ""
    updated_at_utc: str = ""

    @classmethod
    def create(cls, *, age, height_cm, weight_kg, sex_for_equation, activity_level,
               health_goal, dietary_restrictions=(), population_status="general_adult",
               profile_id=None, now=None):
        timestamp = now or utc_now()
        candidate = cls(profile_id or str(uuid.uuid4()), age, height_cm, weight_kg,
                        sex_for_equation, activity_level, health_goal,
                        dietary_restrictions, population_status, timestamp, timestamp)
        from .validation import validate_profile
        return validate_profile(candidate)

    def to_dict(self):
        result = asdict(self)
        result["dietary_restrictions"] = list(self.dietary_restrictions)
        return result
