"""Human-readable labels for the historical response actually recorded."""


def response_label(message, *, citizen: bool = False) -> str:
    kind = getattr(message, "response_kind", None)
    suffix = "администрации" if citizen else "специалиста"
    if kind == "intermediate":
        return f"Промежуточный ответ {suffix}"
    if kind == "final":
        return f"Окончательный ответ {suffix}"
    return f"Ответ {suffix}"
