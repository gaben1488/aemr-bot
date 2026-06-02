"""Anti-drift: каждый admin/operator callback, который реально обрабатывает
`admin_callback_dispatch`, обязан классифицироваться роутером
`callback_router` как admin-allowed.

Зачем. Гейт чат-контекста в `appeal._route_callback` срабатывает ТОЛЬКО
для payload'ов, которые роутер относит к admin-группам (admin_allowed=True).
Если кто-то добавит новый `op:<verb>:` в диспетчер, но забудет завести
admin-маршрут в роутере, payload уйдёт в MENU_FALLBACK — мимо гейта — и
станет доступен жителю из лички (класс «забытого authz», к которому
относился исторический баг /find_resident). Каждый leaf сейчас ещё и сам
вызывает `ensure_operator`/`ensure_role` (defense-in-depth), но полагаться
только на это хрупко — этот тест ловит рассинхрон таблиц на CI.

Сегодня все префиксы/точные payload'ы покрыты; тест фиксирует инвариант
на будущее. См. также docstring `callback_router` и `test_cron_docs_sync`/
`test_wiki_in_sync` — та же дисциплина anti-drift.
"""

from __future__ import annotations

from aemr_bot.handlers.admin_callback_dispatch import _EXACT, _PREFIX_ID, _PREFIX_RAW
from aemr_bot.handlers.callback_router import route_for


def _prefixes() -> list[str]:
    return [p for p, _ in _PREFIX_ID] + [p for p, _ in _PREFIX_RAW]


def test_exact_admin_payloads_are_gated() -> None:
    """Каждый точный admin-payload роутится в admin-группу (gate сработает)."""
    bad = [key for key in _EXACT if not route_for(key).admin_allowed]
    assert not bad, (
        "admin-payload(ы) уходят мимо chat-context гейта (route_for != "
        "admin_allowed): " + ", ".join(bad) + ". Заведи admin-маршрут в "
        "callback_router.EXACT_ROUTES."
    )


def test_prefix_admin_payloads_are_gated() -> None:
    """Каждый admin-префикс роутится в admin-группу на репрезентативном payload."""
    # Для маршрутизации важен только startswith, поэтому `<prefix>1`
    # (op:reply: → op:reply:1) — достаточная выборка.
    bad = [p for p in _prefixes() if not route_for(p + "1").admin_allowed]
    assert not bad, (
        "admin-префикс(ы) уходят мимо chat-context гейта: "
        + ", ".join(bad)
        + ". Заведи admin-маршрут в callback_router.PREFIX_ROUTES."
    )
