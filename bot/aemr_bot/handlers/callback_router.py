"""Маршрутизация callback payload'ов.

Главный обработчик `handlers.appeal.register()` остаётся entry-point'ом MAX,
но список известных payload-групп вынесен сюда. Это уменьшает риск, что
новая кнопка появится без теста на чат-контекст, роль и fallback.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class CallbackGroup(StrEnum):
    CITIZEN_FLOW = "citizen_flow"
    GEO_FLOW = "geo_flow"
    BROADCAST_ADMIN = "broadcast_admin"
    OPERATOR_ADMIN = "operator_admin"
    MENU_FALLBACK = "menu_fallback"


@dataclass(frozen=True)
class CallbackRoute:
    pattern: str
    group: CallbackGroup
    admin_allowed: bool
    description: str


# Единственный реестр callback-групп. Точные payload'ы перечислены отдельно
# от префиксных маршрутов, чтобы тесты ловили случайное пересечение.
EXACT_ROUTES: tuple[CallbackRoute, ...] = (
    CallbackRoute("menu:new_appeal", CallbackGroup.CITIZEN_FLOW, False, "новое обращение"),
    CallbackRoute("consent:yes", CallbackGroup.CITIZEN_FLOW, False, "согласие на ПДн"),
    CallbackRoute("consent:no", CallbackGroup.CITIZEN_FLOW, False, "отказ от ПДн"),
    CallbackRoute("cancel", CallbackGroup.CITIZEN_FLOW, False, "отмена воронки"),
    CallbackRoute("addr:reuse", CallbackGroup.CITIZEN_FLOW, False, "использовать прошлый адрес"),
    CallbackRoute("addr:new", CallbackGroup.CITIZEN_FLOW, False, "ввести новый адрес"),
    CallbackRoute("geo:confirm", CallbackGroup.GEO_FLOW, False, "подтвердить гео-адрес"),
    CallbackRoute("geo:edit_address", CallbackGroup.GEO_FLOW, False, "исправить адрес"),
    CallbackRoute("geo:other_locality", CallbackGroup.GEO_FLOW, False, "выбрать другой пункт"),
    CallbackRoute("appeal:submit", CallbackGroup.CITIZEN_FLOW, False, "устаревшая кнопка отправки"),
    CallbackRoute("broadcast:confirm", CallbackGroup.BROADCAST_ADMIN, True, "подтвердить рассылку"),
    CallbackRoute("broadcast:abort", CallbackGroup.BROADCAST_ADMIN, True, "отменить мастер рассылки"),
    CallbackRoute("broadcast:edit", CallbackGroup.BROADCAST_ADMIN, True, "изменить текст рассылки"),
    CallbackRoute("op:menu", CallbackGroup.OPERATOR_ADMIN, True, "операторское меню"),
    CallbackRoute("op:stats_menu", CallbackGroup.OPERATOR_ADMIN, True, "меню статистики"),
    CallbackRoute("op:stats_today", CallbackGroup.OPERATOR_ADMIN, True, "статистика за сегодня"),
    CallbackRoute("op:stats_week", CallbackGroup.OPERATOR_ADMIN, True, "статистика за неделю"),
    CallbackRoute("op:stats_month", CallbackGroup.OPERATOR_ADMIN, True, "статистика за месяц"),
    CallbackRoute("op:stats_quarter", CallbackGroup.OPERATOR_ADMIN, True, "статистика за квартал"),
    CallbackRoute("op:stats_half_year", CallbackGroup.OPERATOR_ADMIN, True, "статистика за полугодие"),
    CallbackRoute("op:stats_year", CallbackGroup.OPERATOR_ADMIN, True, "статистика за год"),
    CallbackRoute("op:stats_all", CallbackGroup.OPERATOR_ADMIN, True, "статистика за всё время"),
    CallbackRoute("op:open_tickets", CallbackGroup.OPERATOR_ADMIN, True, "открытые обращения"),
    CallbackRoute("op:diag", CallbackGroup.OPERATOR_ADMIN, True, "диагностика"),
    CallbackRoute("op:backup", CallbackGroup.OPERATOR_ADMIN, True, "бэкап"),
    CallbackRoute("op:broadcast", CallbackGroup.OPERATOR_ADMIN, True, "мастер рассылки"),
    CallbackRoute("op:broadcast_list", CallbackGroup.OPERATOR_ADMIN, True, "история рассылок"),
    CallbackRoute("op:operators", CallbackGroup.OPERATOR_ADMIN, True, "операторы"),
    CallbackRoute("op:settings", CallbackGroup.OPERATOR_ADMIN, True, "настройки"),
    CallbackRoute("op:audience", CallbackGroup.OPERATOR_ADMIN, True, "аудитория"),
    CallbackRoute("op:reply_cancel", CallbackGroup.OPERATOR_ADMIN, True, "отмена ответа"),
)

PREFIX_ROUTES: tuple[CallbackRoute, ...] = (
    CallbackRoute("locality:", CallbackGroup.CITIZEN_FLOW, False, "выбор населённого пункта"),
    CallbackRoute("topic:", CallbackGroup.CITIZEN_FLOW, False, "выбор темы"),
    CallbackRoute("broadcast:stop:", CallbackGroup.BROADCAST_ADMIN, True, "остановить рассылку"),
    CallbackRoute("op:aud:", CallbackGroup.OPERATOR_ADMIN, True, "действие с аудиторией"),
    CallbackRoute("op:reply:", CallbackGroup.OPERATOR_ADMIN, True, "ответ по обращению"),
    CallbackRoute("op:reopen:", CallbackGroup.OPERATOR_ADMIN, True, "вернуть в работу"),
    CallbackRoute("op:close:", CallbackGroup.OPERATOR_ADMIN, True, "закрыть обращение"),
    CallbackRoute("op:erase:", CallbackGroup.OPERATOR_ADMIN, True, "стереть ПДн по обращению"),
    CallbackRoute("op:block:", CallbackGroup.OPERATOR_ADMIN, True, "заблокировать жителя"),
    CallbackRoute("op:unblock:", CallbackGroup.OPERATOR_ADMIN, True, "разблокировать жителя"),
    CallbackRoute("op:opadd:", CallbackGroup.OPERATOR_ADMIN, True, "мастер операторов"),
    CallbackRoute("op:setkey:", CallbackGroup.OPERATOR_ADMIN, True, "мастер настроек"),
)


def route_for(payload: str) -> CallbackRoute:
    """Вернуть группу маршрута для payload.

    Неизвестные payload'ы намеренно уходят в MENU_FALLBACK: это сохраняет
    существующее поведение `handlers.menu.handle_callback`, но делает его
    явным и тестируемым.
    """
    for route in EXACT_ROUTES:
        if payload == route.pattern:
            return route
    for route in PREFIX_ROUTES:
        if payload.startswith(route.pattern):
            return route
    return CallbackRoute(payload, CallbackGroup.MENU_FALLBACK, False, "fallback в меню")


def is_admin_callback(payload: str) -> bool:
    """Можно ли обрабатывать payload в админ-группе."""
    return route_for(payload).admin_allowed


def parse_int_tail(payload: str, prefix: str) -> int | None:
    """Безопасно разобрать целочисленный хвост callback payload.

    Возвращает None для malformed/stale кнопок. Handler обязан в таком
    случае сделать ack и не выполнять действие.
    """
    if not payload.startswith(prefix):
        return None
    value = payload[len(prefix):]
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None
