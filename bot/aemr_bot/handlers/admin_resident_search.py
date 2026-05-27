"""Поиск жителя по телефону или MAX user id для оператора.

Cluster G (Codex PR 9). До этого оператор мог попасть на карточку
жителя только через listing открытых обращений или /erase phone=.
Прямой lookup отсутствовал — приходилось вручную листать длинные
списки или открывать карточки одну за другой.

Команда `/find_resident <phone|max_user_id>` доступна любой роли
оператора (OP/SH/IT — `ensure_operator`). Возвращает карточку:
имя, **маскированный** телефон, статус согласия, подписка,
блокировка, последнее обращение, всего обращений.

Каждый запрос пишется в `audit_log` (152-ФЗ, retention 365 дней).
Без этого оператор мог бы тихо искать жителей по телефону без
следа — нарушение compliance.

Маскировка телефона обязательна на всех уровнях:
- В выводе оператору — `+7***1234` (`services/admin_events._mask_phone`).
- В audit-log запись — хешированный fragment, не plain телефон.
- В логах — никогда полный телефон.
"""
from __future__ import annotations

import logging

from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import operators as ops_svc
from aemr_bot.services import users as users_service
from aemr_bot.services.admin_events import _mask_phone
from aemr_bot.utils.event import is_admin_chat

from aemr_bot import texts

log = logging.getLogger(__name__)


def _detect_query_kind(query: str) -> tuple[str, str]:
    """Определить, чем является `query`: max_user_id или телефон.

    Возвращает `(kind, normalized)`:
    - `("max_user_id", "123456789")` — целое число без `+` или `-`,
      длиной ≥ 4 (избегаем коротких 1-3-значных опечаток).
    - `("phone", "+79991234567")` — содержит `+` или ≥ 10 цифр.
    - `("invalid", query)` — не классифицировался.

    Логика: цифры-только и длина 4-9 → max_user_id (типичный MAX id 6-9
    цифр). Цифры-только длина ≥ 10 ИЛИ символ `+` → phone. Меньше 4
    цифр → invalid.
    """
    stripped = query.strip()
    if not stripped:
        return ("invalid", "")
    # Чисто цифровые без знаков.
    if stripped.isdigit():
        if 4 <= len(stripped) <= 9:
            return ("max_user_id", stripped)
        if len(stripped) >= 10:
            return ("phone", "+" + stripped if not stripped.startswith("+") else stripped)
        return ("invalid", stripped)
    # С `+` — телефон.
    if stripped.startswith("+"):
        return ("phone", stripped)
    # Любое другое (буквы, спец-символы) — невалидно.
    return ("invalid", stripped)


def _mask_query_for_audit(kind: str, value: str) -> str:
    """Подготовить query для audit-log: для phone маскируем последние
    4 цифры, для max_user_id оставляем как есть (это не PII)."""
    if kind == "phone":
        return _mask_phone(value)
    return value


def _format_consent_status(user) -> str:
    """Формальный статус согласия одной строкой."""
    if getattr(user, "consent_revoked_at", None) is not None:
        return "🔁 отозвано"
    if getattr(user, "consent_pdn_at", None) is not None:
        return "✅ активно"
    return "— нет"


def _format_subscribe_status(user) -> str:
    """Подписка на рассылку — короткой строкой."""
    if getattr(user, "subscribed_broadcast", False):
        return "🔔 активна"
    return "🔕 нет"


def _format_last_appeal(appeal) -> str:
    """Последнее обращение жителя — краткое описание для карточки."""
    if appeal is None:
        return "— нет"
    created = appeal.created_at.strftime("%d.%m.%Y") if appeal.created_at else "—"
    topic = (appeal.topic or "—")[:40]
    return f"#{appeal.id} от {created} · {topic} · {appeal.status}"


async def run_find_resident(event, query: str) -> None:
    """Главная точка входа `/find_resident <phone|max_user_id>`.

    Только в админ-чате. Любая роль оператора. Один результат на запрос:
    либо карточка найденного жителя, либо not-found / usage / ambiguous.
    Каждый запрос — запись в audit_log (152-ФЗ).
    """
    if not is_admin_chat(event):
        return
    operator = await ensure_operator(event)
    if operator is None:
        return

    if not query or not query.strip():
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=texts.OP_FIND_RESIDENT_USAGE,
        )
        return

    kind, value = _detect_query_kind(query)
    if kind == "invalid":
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=texts.OP_FIND_RESIDENT_USAGE,
        )
        return

    audit_target = _mask_query_for_audit(kind, value)
    async with session_scope() as session:
        if kind == "max_user_id":
            try:
                max_id_int = int(value)
            except ValueError:
                await event.bot.send_message(
                    chat_id=cfg.admin_group_id,
                    text=texts.OP_FIND_RESIDENT_USAGE,
                )
                return
            user = await users_service.find_by_max_id(session, max_id_int)
        else:  # phone
            user = await users_service.find_by_phone(session, value)
            # find_by_phone возвращает None и при множественном совпадении,
            # и при отсутствии. Различить через прямой повторный count —
            # дорого; вместо этого даём отдельную подсказку только когда
            # явно telephone-формат + None.
            if user is None and value.lstrip("+").isdigit() and len(value) >= 11:
                # Audit: фиксируем попытку поиска независимо от результата.
                await ops_svc.write_audit(
                    session,
                    operator_max_user_id=operator.max_user_id,
                    action="resident_search_not_found",
                    target=audit_target,
                    details={"kind": kind},
                )
                # Не различаем not-found vs ambiguous: пишем общий
                # not-found текст (для оператора результат всё равно
                # один — «нужен max_user_id»).
                await event.bot.send_message(
                    chat_id=cfg.admin_group_id,
                    text=texts.OP_FIND_RESIDENT_NOT_FOUND.format(
                        query_masked=audit_target,
                    ),
                )
                return

        if user is None:
            await ops_svc.write_audit(
                session,
                operator_max_user_id=operator.max_user_id,
                action="resident_search_not_found",
                target=audit_target,
                details={"kind": kind},
            )
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=texts.OP_FIND_RESIDENT_NOT_FOUND.format(
                    query_masked=audit_target,
                ),
            )
            return

        # Found. Audit + расчёт последнего обращения.
        await ops_svc.write_audit(
            session,
            operator_max_user_id=operator.max_user_id,
            action="resident_search_found",
            target=audit_target,
            details={
                "kind": kind,
                "found_max_user_id": user.max_user_id,
            },
        )
        last_appeals = await appeals_service.list_for_user(
            session, user.id, limit=1
        )
        count = await appeals_service.count_for_user(session, user.id)
        last_appeal = last_appeals[0] if last_appeals else None

    name = (user.first_name or "—").strip() or "—"
    blocked_line = ""
    if getattr(user, "is_blocked", False):
        blocked_line = "🚫 Заблокирован: да\n"
    text_out = texts.OP_FIND_RESIDENT_CARD.format(
        name=name,
        phone_masked=_mask_phone(user.phone),
        max_user_id=user.max_user_id,
        consent_status=_format_consent_status(user),
        subscribe_status=_format_subscribe_status(user),
        blocked_line=blocked_line,
        last_appeal=_format_last_appeal(last_appeal),
        appeals_count=count,
    )
    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=text_out,
    )


def register(dp) -> None:
    """Регистрация обработчика `/find_resident <query>`.

    Команда работает только в админ-чате (`is_admin_chat` гард внутри
    `run_find_resident`). Аргумент обязательный — без аргумента
    показываем usage-подсказку.
    """
    from maxapi.types import Command, MessageCreated

    @dp.message(Command("find_resident"))
    async def _handler(event: MessageCreated) -> None:
        text = ""
        try:
            text = event.message.body.text or ""
        except AttributeError:
            pass
        # Удаляем команду из начала: «/find_resident +79991234567» → «+79991234567».
        parts = text.split(maxsplit=1)
        query = parts[1] if len(parts) > 1 else ""
        await run_find_resident(event, query)
