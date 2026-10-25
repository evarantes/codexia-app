"""Small, dependency-free ownership checks for assistant projects."""


def is_project_owner(brief: dict, user_id: int) -> bool:
    """Fail closed for missing, malformed, or differently owned projects."""
    production = brief.get("_production")
    return (
        isinstance(production, dict)
        and type(production.get("user_id")) is int
        and production["user_id"] == user_id
    )
