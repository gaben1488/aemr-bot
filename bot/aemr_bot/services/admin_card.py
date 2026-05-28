"""Карточка обращения в админ-чате — sacred event log (PR #100, dual-tracker).

Каждая публикация карточки — новая запись в журнале чата. Edit
после dual-tracker не делается; reply/reopen/close/followup → новая
карточка с обновлённым timeline, старая остаётся как след.

`Appeal.admin_message_id` — mid ПЕРВОЙ публикации (для reply-link
при relay вложений), `last_admin_card_mid` — mid последней публикации
(для всех остальных нужд).

Sacred-event-log контракт, перечень «что НЕ карточка», полная
мотивация edit-removal: см.
`docs/_meta/_archive/CODE_DECISIONS_LOG.md §6`.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm.exc import DetachedInstanceError

from aemr_bot import keyboards
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.utils import menu_tracker
from aemr_bot.utils.event import extract_message_id

if TYPE_CHECKING:
    from aemr_bot.db.models import Appeal

log = logging.getLogger(__name__)


async def render(
    bot,
    appeal: "Appeal",
    *,
    callback_mid: str | None = None,
    is_first_publication: bool = False,
    force_new: bool = False,
    event_header: str | None = None,
) -> str | None:
    """Опубликовать/обновить admin appeal card с freshness-rule.

    Args:
        bot: maxapi Bot.
        appeal: Appeal с подгруженными user и messages.
        callback_mid: mid сообщения, на котором оператор тапнул кнопку
            (callback.message.mid). Если None — это не callback (например
            finalize, followup от жителя) → форсим send_new.
        is_first_publication: True только при finalize — обновляет
            admin_message_id (sacred ссылка для relay вложений).
        force_new: True для событий «появилось новое» (followup
            жителя): даже если карточка ещё последняя в чате,
            нужна новая запись внизу для маркера событий.
        event_header: опциональный маркер-заголовок над карточкой —
            например «📩 Новое дополнение по обращению #N от жителя».
            Нужен, когда карточка приходит как РЕАКЦИЯ на событие, а
            не просто как переотрисовка статуса: оператор сразу видит,
            что именно случилось, без поиска в timeline. Шапка отделена
            от карточки разделителем. Только для send_new веток; на
            edit-in-place не применяется (карточка остаётся «обычной»).

    Returns:
        mid опубликованной/отредактированной карточки, None при сбое.

    Логика freshness:
    1. force_new=True → send new.
    2. callback_mid задан И равен menu_tracker[admin_group_id] → edit
       (карточка последняя в чате).
    3. Иначе → send new (карточка не последняя, или это не callback).
    """
    if not cfg.admin_group_id:
        log.warning("admin_card.render: ADMIN_GROUP_ID не установлен")
        return None

    user = getattr(appeal, "user", None)
    if user is None:
        log.warning(
            "admin_card.render: appeal #%s без user, не могу отрендерить",
            appeal.id,
        )
        return None

    # SACRED #6: auto-warm messages для timeline.
    # `card_format._loaded_messages` намеренно НЕ делает lazy-load (иначе
    # MissingGreenlet в async-сессии после закрытия). Раньше каждый
    # вызывающий должен был сам сделать `get_by_id_with_messages` или
    # выставить `appeal.__dict__["messages"] = []`. Контракт легко
    # нарушался — например, в `menu.do_consent_revoke` через
    # `list_unanswered` (без selectinload) timeline терялся, баг #44.
    #
    # Теперь render сам подгружает messages, если: (а) это не первая
    # публикация (на finalize переписки реально нет), (б) `messages`
    # отсутствует в __dict__ ИЛИ пустой список. На finalize пропускаем —
    # appeal только что создан, messages=[] корректно.
    if (
        not is_first_publication
        and "messages" not in appeal.__dict__
        or (
            not is_first_publication
            and appeal.__dict__.get("messages") == []
        )
    ):
        try:
            async with session_scope() as session:
                fresh = await appeals_service.get_by_id_with_messages(
                    session, appeal.id
                )
            if fresh is not None and fresh.__dict__.get("messages"):
                # Скопируем messages в исходный appeal-объект
                # (он остаётся вызывающему, без detach-проблем).
                appeal.__dict__["messages"] = list(fresh.__dict__["messages"])
        except Exception:
            log.debug(
                "admin_card.render: auto-warm messages для #%s не удалось",
                appeal.id, exc_info=False,
            )
            # Не критично — timeline просто будет пустой, как и было.

    # text и attachment_count устойчиво к detached lazy-load.
    # Reliability-pass: сузили `except Exception` до конкретного набора
    # причин. card_format читает .messages / .events / .user.*; на
    # detached instance это AttributeError либо DetachedInstanceError
    # из sqlalchemy.orm. TypeError ловит случай None в форматтере
    # (например appeal.created_at=None после миграции). Любые другие
    # exception'ы (asyncpg DataError, OperationalError) — баг, пусть
    # всплывает в логе вызывающего, чтобы был виден стек.
    try:
        text = card_format.admin_card(appeal, user)
    except (AttributeError, TypeError, DetachedInstanceError):
        log.exception(
            "admin_card.render: card_format.admin_card failed for #%s",
            appeal.id,
        )
        text = f"Обращение #{appeal.id}\nЖитель: {user.first_name or '—'}"
    try:
        attachment_count = _count_attachments(appeal)
    except (AttributeError, TypeError, DetachedInstanceError):
        log.debug(
            "admin_card.render: attachment_count failed for #%s",
            appeal.id, exc_info=False,
        )
        attachment_count = 0

    kb = keyboards.appeal_admin_actions(
        appeal.id,
        appeal.status,
        is_it=True,
        user_blocked=bool(user.is_blocked),
        closed_due_to_revoke=bool(getattr(appeal, "closed_due_to_revoke", False)),
        attachment_count=attachment_count,
    )
    attachments = [kb]

    # 2026-05-27 sacred event log: карточка обращения **никогда** не
    # редактируется. Каждое op-действие (close, reopen, reply, block)
    # или followup жителя — это **event** в timeline'е обращения,
    # должен появиться новой записью внизу чата. Старая карточка
    # остаётся в истории как иммутабельный slice.
    #
    # Раньше тут была ветка `can_edit` (callback_mid == tracker → edit).
    # Это работало, пока tracker совпадал с editable-mid и не было
    # historic events ниже. Но даже когда edit формально допустим,
    # карточка обращения — sacred (см. жалобу владельца «открыл 2,
    # закрыл, одна обновилась, другая нет» — root cause именно эта
    # ветка). Удалена полностью.
    #
    # `callback_mid` теперь используется только для diagnostics-логов
    # и для пометки force_new=False как ошибка (на самом деле всегда
    # send_new — дальше идёт прямой bot.send_message).
    _ = callback_mid  # noqa: F841 — параметр оставлен для совместимости
    _ = force_new  # noqa: F841 — тоже совместимость; теперь всегда send_new

    text_for_send = (
        f"{event_header}\n· · · · · · · ·\n{text}"
        if event_header
        else text
    )
    try:
        sent = await bot.send_message(
            chat_id=cfg.admin_group_id,
            text=text_for_send,
            attachments=attachments,
        )
    except Exception:
        log.exception(
            "admin_card.render: send_message failed for appeal #%s", appeal.id
        )
        return None

    new_mid = extract_message_id(sent)
    if new_mid:
        # 2026-05-27 dual-tracker: карточка обращения — historic event.
        # Регистрируем через `note_event` — двигаем только physical_mid,
        # editable_mid не трогаем (оно осталось на mid предыдущего
        # editable меню, если было). Когда оператор тапнет кнопку на
        # этой карточке обращения, `can_edit` вернёт False по двум
        # признакам сразу: kind != menu (это карточка, не меню) и
        # editable_mid != callback_mid (editable указывает на меню
        # выше). Sacred event log соблюдён без специальных hack'ов.
        menu_tracker.note_event(cfg.admin_group_id, new_mid)
        async with session_scope() as session:
            await appeals_service.set_last_admin_card_mid(
                session, appeal.id, new_mid
            )
            if is_first_publication:
                await appeals_service.set_admin_message_id(
                    session, appeal.id, new_mid
                )
    return new_mid


def _count_attachments(appeal: "Appeal") -> int:
    """Сколько вложений у обращения (исходные + дополнения жителя).
    Устойчиво к detached state (lazy-fail → fallback на scalar)."""
    try:
        from aemr_bot.services.admin_relay import _collect_all_user_attachments

        return len(_collect_all_user_attachments(appeal))
    except Exception:
        log.debug(
            "admin_card._count_attachments: lazy-load fail for appeal #%s",
            appeal.id, exc_info=False,
        )
        return len(getattr(appeal, "attachments", None) or [])
