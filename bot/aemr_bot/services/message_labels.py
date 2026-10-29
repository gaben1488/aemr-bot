"""Human-readable labels for the historical response actually recorded."""


def response_label(message, *, citizen: bool = False) -> str:
    kind = getattr(message, "response_kind", None)
    suffix = "администрации" if citizen else "специалиста"
    if kind == "intermediate":
        label = f"Промежуточный ответ {suffix}"
    elif kind == "final":
        label = f"Окончательный ответ {suffix}"
    else:
        label = f"Ответ {suffix}"
    # The timestamp on a manually reconciled message is the time the
    # operator confirmed delivery, not a claimed timestamp from MAX.
    if getattr(message, "delivery_source", None) == "manual":
        label += " · доставка подтверждена сотрудником"
    return label
