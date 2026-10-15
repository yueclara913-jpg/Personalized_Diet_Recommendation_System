"""Central, deny-by-default role based access policy."""

ROLES = ("system_admin", "nutrition_reviewer", "audit_viewer")

ROLE_PERMISSIONS = {
    "system_admin": frozenset({
        "admin.create", "admin.disable", "admin.assign_role", "admin.reset_password",
        "review.evaluate", "review.decide", "review.history", "review.verify",
        "review.summary", "audit.read",
    }),
    "nutrition_reviewer": frozenset({
        "review.evaluate", "review.decide", "review.history", "review.verify",
        "review.summary", "audit.read",
    }),
    "audit_viewer": frozenset({
        "review.history", "review.verify", "review.summary", "audit.read",
    }),
}

KNOWN_PERMISSIONS = frozenset().union(*ROLE_PERMISSIONS.values())


def has_permission(role, permission):
    """Return False for every unknown role or permission."""
    return permission in KNOWN_PERMISSIONS and permission in ROLE_PERMISSIONS.get(role, ())
