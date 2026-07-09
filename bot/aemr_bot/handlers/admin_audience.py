"""Меню «📊 Аудитория и согласия» — IT-выборки + точечные действия
над жителем (block / unblock / erase).

После UX-редизайна 2026-05-28 (PR audience-paginated-master) listing
стал master-pattern'ом: одно сообщение со списком 10 жителей на
странице, clickable rows, pagination кнопки. Заменили flood из 20
отдельных сообщений (жалоба owner).

Выделено из handlers/admin_commands.py (рефакторинг 2026-05-10).
"""
from __future__ import annotations

import logging

import time as _time

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.services import operators as operators_service
from aemr_bot.services import users as users_service
from aemr_bot.services.card_format import _citizen_status_line
from aemr_bot.utils.event import (
    get_user_id,
    send_or_edit_screen,
)
from aemr_bot.utils.pii_mask import mask_phone as _mask_phone

log = logging.getLogger(__name__)

# Размер страницы master-listing'а. 10 — компромисс между «много в
# одной карточке» и «не упёрлись в лимит длины кнопок MAX-API».
# Каждая строка ~50 символов = 500 char на page. Плюс 3 row-навигации
# (pagination + 2 back). MAX-keyboard поддерживает заметно больше.
_PAGE_SIZE = 10

# Intent для поиска: оператор тапнул «🔍 Поиск», бот ждёт следующего
# текстового сообщения. TTL 5 минут (как для admin_settings intent).
# Key: operator_max_user_id, value: {"category": str | None, "expires_at": float}.
_search_intents: dict[int, dict] = {}
_SEARCH_INTENT_TTL_SEC = 300.0


def _search_intent_set(operator_id: int, category: str | None) -> None:
    _search_intents[operator_id] = {
        "category": category,
        "expires_at": _time.monotonic() + _SEARCH_INTENT_TTL_SEC,
    }


def _search_intent_pop(operator_id: int) -> dict | None:
    state = _search_intents.pop(operator_id, None)
    if state is None:
        return None
    if _time.monotonic() > state.get("expires_at", 0):
        return None
    return state


async def run_audience_menu(event) -> None:
    """Меню «📊 Аудитория и согласия» для IT — точка входа в три списка."""
    from aemr_bot import keyboards as kbds

    if not await ensure_role(event, OperatorRole.IT):
        return
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            "📊 Аудитория и согласия\n"
            "· · · · · · · ·\n"
            "Выберите выборку. Master-listing с pagination —\n"
            "по 10 жителей на страницу, тап по строке открывает\n"
            "карточку с действиями (блок/erase)."
        ),
        attachments=[kbds.op_audience_menu_keyboard()],
    )


def _format_audience_row(user) -> str:
    """Краткая строка для master-listing: `#max_user_id · Имя · +7***1234 · 🔔✅`.

    Иконки статуса собираются вручную (короче чем _citizen_status_line,
    rendered как одна строка для кнопки): подписан → 🔔, согласие
    активно → ✅, отозвано → 🔁, заблокирован → 🚫.
    Длина ограничивается ~50 символами — MAX-кнопки укладываются.
    """
    name = (user.first_name or "—").strip() or "—"
    if len(name) > 24:
        name = name[:21] + "…"
    phone = _mask_phone(user.phone)
    badges = []
    if getattr(user, "subscribed_broadcast", False):
        badges.append("🔔")
    if getattr(user, "consent_pdn_at", None):
        badges.append("✅")
    elif getattr(user, "consent_revoked_at", None):
        badges.append("🔁")
    if getattr(user, "is_blocked", False):
        badges.append("🚫")
    badge_str = "".join(badges) or "·"
    return f"#{user.max_user_id} · {name} · {phone} · {badge_str}"


async def _render_audience_page(
    event, category: str, page: int,
) -> None:
    """Показать страницу `page` категории `subs|consent|blocked` через
    master-listing keyboard."""
    from aemr_bot import keyboards as kbds

    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE

    async with session_scope() as session:
        if category == "subs":
            total = await users_service.count_subscribers_audience(session)
            items = await users_service.list_subscribers(
                session, limit=_PAGE_SIZE, offset=offset,
            )
            title = "📩 Подписчики на рассылку"
        elif category == "consent":
            total = await users_service.count_consented(session)
            items = await users_service.list_consented(
                session, limit=_PAGE_SIZE, offset=offset,
            )
            title = "🔐 Дали согласие на ПДн"
        elif category == "blocked":
            total = await users_service.count_blocked(session)
            items = await users_service.list_blocked(
                session, limit=_PAGE_SIZE, offset=offset,
            )
            title = "🚫 Заблокированные"
        else:
            return

    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    # Если page «улетел» за пределы (оператор сделал bookmark на старую
    # страницу, а жителей с тех пор стало меньше) — clamp к total_pages.
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * _PAGE_SIZE
        # Перечитываем последнюю реальную страницу.
        async with session_scope() as session:
            if category == "subs":
                items = await users_service.list_subscribers(
                    session, limit=_PAGE_SIZE, offset=offset,
                )
            elif category == "consent":
                items = await users_service.list_consented(
                    session, limit=_PAGE_SIZE, offset=offset,
                )
            else:
                items = await users_service.list_blocked(
                    session, limit=_PAGE_SIZE, offset=offset,
                )

    if total == 0:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=(
                f"{title}\n"
                "· · · · · · · ·\n"
                "Список пуст."
            ),
            attachments=[kbds.op_back_to_audience_keyboard()],
        )
        return

    rows = [
        (u.max_user_id, _format_audience_row(u))
        for u in items
    ]
    header = (
        f"{title}\n"
        f"· · · · · · · ·\n"
        f"Страница {page} из {total_pages} · всего: {total}\n"
        f"\n"
        f"Тапните строку — откроется карточка жителя\n"
        f"с действиями (блок/удалить ПДн)."
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=header,
        attachments=[
            kbds.op_audience_paginated_list_keyboard(
                category, rows, page=page, total_pages=total_pages,
            )
        ],
    )


async def _dump_audience_page(
    event, category: str, page: int,
) -> None:
    """Bulk-dump: отправить N отдельных карточек жителей с страницы
    в чат. Используется для распечатки/копирования или массовых
    действий через action-кнопки под каждой карточкой.
    """
    from aemr_bot import keyboards as kbds

    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE
    async with session_scope() as session:
        if category == "subs":
            items = await users_service.list_subscribers(
                session, limit=_PAGE_SIZE, offset=offset,
            )
        elif category == "consent":
            items = await users_service.list_consented(
                session, limit=_PAGE_SIZE, offset=offset,
            )
        elif category == "blocked":
            items = await users_service.list_blocked(
                session, limit=_PAGE_SIZE, offset=offset,
            )
        else:
            return

    if not items:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text="Страница пуста.",
            attachments=[kbds.op_back_to_audience_keyboard()],
        )
        return

    # Отдельная карточка на жителя, каждая со status-линией +
    # action-кнопками. Это «старый» поток, но теперь по явному
    # запросу оператора, а не по умолчанию.
    for u in items:
        name = u.first_name or "—"
        phone = _mask_phone(u.phone)
        status = _citizen_status_line(u)
        line = (
            f"#{u.max_user_id} · {name} · {phone}\n"
            f"{status}"
        )
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=line,
            attachments=[
                kbds.op_audience_user_actions(
                    u.max_user_id, blocked=u.is_blocked,
                )
            ],
        )


async def _start_search_intent(event, category: str | None) -> None:
    """Начать поиск: ставим intent + показываем prompt."""
    from aemr_bot import keyboards as kbds

    operator_id = get_user_id(event)
    if operator_id is None:
        return
    _search_intent_set(operator_id, category)
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            "🔍 Поиск жителя\n"
            "· · · · · · · ·\n"
            "Пришлите одним сообщением:\n"
            "• имя или часть имени (поиск частичный, без учёта регистра);\n"
            "• телефон или его фрагмент (например, последние 4 цифры);\n"
            "• MAX user id (4+ цифр).\n"
            "\n"
            "Найдём всё, что подходит под запрос — до 20 совпадений.\n"
            "Поиск ходит во всех категориях, не только в текущей."
        ),
        attachments=[kbds.op_audience_search_cancel_keyboard(category)],
    )


async def handle_audience_search_text(event, text: str) -> bool:
    """Перехватчик: если у оператора активен search intent — выполняем
    поиск и рендерим результаты. Возвращает True если поглотил
    сообщение.
    """
    from aemr_bot import keyboards as kbds

    operator_id = get_user_id(event)
    if operator_id is None:
        return False
    state = _search_intent_pop(operator_id)
    if state is None:
        return False
    if not await ensure_role(event, OperatorRole.IT):
        return False

    query = (text or "").strip()
    if not query:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text="Пустой запрос — поиск отменён.",
            attachments=[kbds.op_back_to_audience_keyboard()],
        )
        return True

    async with session_scope() as session:
        users = await users_service.search_audience(
            session, query, limit=20,
        )

    if not users:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=(
                f"🔍 По запросу «{query[:60]}» ничего не найдено.\n"
                f"· · · · · · · ·\n"
                f"Попробуйте короче или другой фрагмент."
            ),
            attachments=[kbds.op_back_to_audience_keyboard()],
        )
        return True

    rows = [
        (u.max_user_id, _format_audience_row(u))
        for u in users
    ]
    header = (
        f"🔍 Найдено: {len(users)} по запросу «{query[:60]}»\n"
        f"· · · · · · · ·\n"
        f"Тапните строку — откроется карточка жителя."
    )
    # Используем тот же paginated-keyboard, но без pagination
    # (всё на одной странице). category='subs' как fallback —
    # back-кнопка вернёт в общий audience-меню.
    cat_fallback = state.get("category") or "subs"
    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=header,
        attachments=[
            kbds.op_audience_paginated_list_keyboard(
                cat_fallback, rows, page=1, total_pages=1,
            )
        ],
    )
    return True


async def _render_user_card(
    event, max_user_id: int, category: str | None = None,
) -> None:
    """Карточка отдельного жителя из master-listing — расширенная
    информация + кнопки действий (блок/erase) + кнопка возврата на
    исходный список."""
    from aemr_bot import keyboards as kbds

    async with session_scope() as session:
        user = await users_service.find_by_max_id(session, max_user_id)
    if user is None:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=f"Житель #{max_user_id} не найден.",
            attachments=[kbds.op_back_to_audience_keyboard()],
        )
        return

    name = (user.first_name or "—").strip() or "—"
    phone = _mask_phone(user.phone)
    status_line = _citizen_status_line(user)
    body = (
        f"👤 Карточка жителя\n"
        f"· · · · · · · ·\n"
        f"Имя:     {name}\n"
        f"Телефон: {phone}\n"
        f"MAX id:  {user.max_user_id}\n"
        f"\n"
        f"{status_line}\n"
        f"\n"
        f"Действия — кнопками ниже."
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=body,
        attachments=[
            kbds.op_audience_user_card_keyboard(
                user.max_user_id,
                blocked=user.is_blocked,
                category=category,
            )
        ],
    )


async def run_audience_action(event, payload: str) -> None:
    """Обработчик `op:aud:*`. Подменю — три категории, master-listing
    с pagination + clickable rows; точечные действия — блок/разблок и
    удаление ПДн через карточку жителя.

    Формат payload:

    - `op:aud:subs|consent|blocked` — открыть категорию (page 1).
    - `op:aud:page:<cat>:<N>` — открыть страницу N категории.
    - `op:aud:page:<cat>:noop` — disabled-кнопка `page/total`, тап
      просто ack без действия.
    - `op:aud:show:<max_user_id>` — открыть карточку жителя.
    - `op:aud:block|unblock|erase:<max_user_id>` — точечное действие.
    """
    from aemr_bot import keyboards as kbds
    from aemr_bot.utils.event import ack_callback

    if not await ensure_role(event, OperatorRole.IT):
        return
    suffix = payload.removeprefix("op:aud:")
    await ack_callback(event)
    actor_id = get_user_id(event)

    # Pagination ветка: `page:<cat>:<N>` или `page:<cat>:noop`.
    if suffix.startswith("page:"):
        rest = suffix.removeprefix("page:")
        parts = rest.split(":", 1)
        if len(parts) != 2:
            return
        cat, page_str = parts[0], parts[1]
        if page_str == "noop":
            return  # disabled-кнопка page/total — только ack
        if cat not in {"subs", "consent", "blocked"}:
            return
        try:
            page = int(page_str)
        except ValueError:
            return
        await _render_audience_page(event, cat, page)
        return

    # Bulk dump: `dump:<cat>:<page>`.
    if suffix.startswith("dump:"):
        rest = suffix.removeprefix("dump:")
        parts = rest.split(":", 1)
        if len(parts) != 2:
            return
        cat, page_str = parts[0], parts[1]
        if cat not in {"subs", "consent", "blocked"}:
            return
        try:
            page = int(page_str)
        except ValueError:
            return
        await _dump_audience_page(event, cat, page)
        return

    # Search intent: `search` либо `search:<cat>`.
    if suffix == "search" or suffix.startswith("search:"):
        # Mypy: используем тип-аннотацию `str | None` явно — переменная
        # выше уже `cat: str` из page/dump веток, иначе reassignment
        # в `None` вызывает «Incompatible types in assignment».
        search_cat: str | None = None
        if suffix.startswith("search:"):
            cat_candidate = suffix.removeprefix("search:")
            if cat_candidate in {"subs", "consent", "blocked"}:
                search_cat = cat_candidate
        await _start_search_intent(event, search_cat)
        return

    # Карточка жителя: `show:<max_user_id>`.
    if suffix.startswith("show:"):
        try:
            target_id = int(suffix.removeprefix("show:"))
        except ValueError:
            return
        await _render_user_card(event, target_id)
        return

    # Точечные действия с `<action>:<max_user_id>`.
    if ":" in suffix:
        action, target_str = suffix.split(":", 1)
        try:
            target_id = int(target_str)
        except ValueError:
            return
        if action == "block":
            async with session_scope() as session:
                ok = await users_service.set_blocked(
                    session, target_id, blocked=True
                )
                if ok:
                    await operators_service.write_audit(
                        session,
                        operator_max_user_id=actor_id,
                        action="block",
                        target=f"user max_id={target_id}",
                    )
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_USER_BLOCKED.format(max_user_id=target_id)
                if ok
                else "Не удалось.",
                attachments=[kbds.op_back_to_audience_keyboard()],
            )
            return
        if action == "unblock":
            async with session_scope() as session:
                ok = await users_service.set_blocked(
                    session, target_id, blocked=False
                )
                if ok:
                    await operators_service.write_audit(
                        session,
                        operator_max_user_id=actor_id,
                        action="unblock",
                        target=f"user max_id={target_id}",
                    )
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_USER_UNBLOCKED.format(max_user_id=target_id)
                if ok
                else "Не удалось.",
                attachments=[kbds.op_back_to_audience_keyboard()],
            )
            return
        if action == "erase":
            async with session_scope() as session:
                ok = await users_service.erase_pdn(session, target_id)
                if ok:
                    await operators_service.write_audit(
                        session,
                        operator_max_user_id=actor_id,
                        action="erase",
                        target=f"user max_id={target_id}",
                    )
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_USER_ERASED.format(max_user_id=target_id)
                if ok
                else "Не удалось.",
                attachments=[kbds.op_back_to_audience_keyboard()],
            )
            return

    # Иначе — открыть категорию (page 1).
    if suffix in {"subs", "consent", "blocked"}:
        await _render_audience_page(event, suffix, page=1)


