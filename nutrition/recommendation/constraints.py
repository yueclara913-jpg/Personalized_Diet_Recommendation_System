"""Hard safety constraints shared by baseline and proposed ranking."""

def exclusion_reasons(candidate, allergens, exclusions, requirements):
    reasons = []
    if allergens:
        if candidate.allergen_tags is None:
            reasons.append("ALLERGEN_INFORMATION_UNKNOWN")
        elif allergens.intersection(candidate.allergen_tags):
            reasons.append("ALLERGEN_CONFLICT")
    if exclusions or requirements:
        if candidate.dietary_tags is None:
            reasons.append("DIETARY_TAGS_UNKNOWN")
        else:
            if exclusions.intersection(candidate.dietary_tags):
                reasons.append("DIETARY_RESTRICTION_CONFLICT")
            if not requirements.issubset(candidate.dietary_tags):
                reasons.append("REQUIRED_DIETARY_TAG_MISSING")
    return reasons
