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

import asyncio
import logging
from weakref import WeakValueDictionary

from maxapi.exceptions import InvalidToken, MaxApiError
from sqlalchemy import update
from sqlalchemy.orm.exc import DetachedInstanceError

from aemr_bot import keyboards
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import Appeal, AppealStatus, User
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.utils import menu_tracker
from aemr_bot.utils.event import extract_message_id

log = logging.getLogger(__name__)
_first_card_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def _first_card_lock(appeal_id: int) -> asyncio.Lock:
    lock = _first_card_locks.get(appeal_id)
    if lock is None:
        lock = asyncio.Lock()
        _first_card_locks[appeal_id] = lock
    return lock


def _eligible_first_card():
    return (
        Appeal.admin_message_id.is_(None),
        Appeal.status.in_([AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]),
        Appeal.closed_due_to_revoke.is_(False),
        Appeal.user.has(User.is_blocked.is_(False) & User.consent_pdn_at.is_not(None)),
    )


async def _claim_first_publication(appeal_id: int) -> bool:
    # Commit intent BEFORE the external effect. A crash or unknown HTTP
    # result leaves sending, which recovery must never automatically replay.
    async with session_scope() as session:
        result = await session.execute(
            update(Appeal)
            .where(
                Appeal.id == appeal_id,
                Appeal.initial_card_state == "pending",
                *_eligible_first_card(),
            )
            .values(initial_card_state="sending")
        )
        claimed = bool(result.rowcount)
    return claimed


async def _set_first_card_state(appeal_id: int, state: str) -> None:
    async with session_scope() as session:
        await session.execute(
            update(Appeal)
            .where(Appeal.id == appeal_id, Appeal.initial_card_state == "sending")
            .values(initial_card_state=state)
        )


async def _warn_uncertain_first_card(bot, appeal_id: int) -> None:
    # The caller holds the shared per-appeal lock. A failed warning remains
    # eligible for retry; a crash after delivery but before commit can repeat
    # the warning, but never replays the original card.
    async with session_scope() as session:
        result = await session.execute(
            update(Appeal)
            .where(
                Appeal.id == appeal_id,
                *_eligible_first_card(),
                Appeal.initial_card_state.in_(["sending", "uncertain"]),
                Appeal.initial_card_notified.is_(False),
            )
            .values(initial_card_state="uncertain")
        )
        eligible = bool(result.rowcount)
    if not eligible:
        return
    log.error(
        "Initial admin-card delivery uncertain for appeal #%s; manual reconciliation required",
        appeal_id,
    )
    try:
        sent = await bot.send_message(
            chat_id=cfg.admin_group_id,
            text=(f"⚠️ Обращение #{appeal_id} сохранено, но результат первой "
                  "публикации карточки неизвестен. Автоматический повтор остановлен. "
                  "Проверьте историю служебного чата и обращение в очереди; "
                  "при необходимости откройте карточку вручную."),
        )
    except Exception:
        log.exception("Could not notify admins of uncertain first card #%s", appeal_id)
        return
    if not extract_message_id(sent):
        log.error("Uncertain first-card warning #%s returned no message id", appeal_id)
        return
    async with session_scope() as session:
        await session.execute(
            update(Appeal).where(Appeal.id == appeal_id)
            .values(initial_card_notified=True)
        )


async def recover_first_publication(bot, appeal_id: int) -> bool:
    """Reload and publish pending work, or surface an uncertain send once."""
    async with _first_card_lock(appeal_id):
        async with session_scope() as session:
            appeal = await appeals_service.get_by_id_with_messages(session, appeal_id)
        if appeal is None or appeal.admin_message_id:
            return False
        if appeal.initial_card_state in {"sending", "uncertain"}:
            await _warn_uncertain_first_card(bot, appeal_id)
            return False
        return bool(await _render(bot, appeal, is_first_publication=True))


async def render(
    bot,
    appeal: Appeal,
    *,
    callback_mid: str | None = None,
    is_first_publication: bool = False,
    force_new: bool = False,
    event_header: str | None = None,
) -> str | None:
    """Publish event cards; first publication uses a durable send claim."""
    if is_first_publication:
        async with _first_card_lock(appeal.id):
            if getattr(appeal, "admin_message_id", None):
                return appeal.admin_message_id
            return await _render(
                bot, appeal, is_first_publication=True, event_header=event_header
            )
    return await _render(
        bot, appeal, callback_mid=callback_mid,
        force_new=force_new, event_header=event_header,
    )


async def _render(
    bot,
    appeal: Appeal,
    *,
    callback_mid: str | None = None,
    is_first_publication: bool = False,
    force_new: bool = False,
    event_header: str | None = None,
) -> str | None:
    """Publish a new immutable event card; return its mid or None on failure.

    callback_mid and force_new remain compatibility parameters. Cards are never
    edited. is_first_publication records the stable first mid for attachment
    replies; event_header adds context to a new event card. Durable first-card
    claiming/recovery is handled by render/recover_first_publication.
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
    _ = callback_mid
    _ = force_new

    text_for_send = (
        f"{event_header}\n· · · · · · · ·\n{text}"
        if event_header
        else text
    )
    if is_first_publication and not await _claim_first_publication(appeal.id):
        return None
    try:
        sent = await bot.send_message(
            chat_id=cfg.admin_group_id,
            text=text_for_send,
            attachments=attachments,
        )
    except Exception as exc:
        log.exception(
            "admin_card.render: send_message failed for appeal #%s", appeal.id
        )
        if is_first_publication:
            # Explicit client rejection proves no message was accepted.
            # 408, 5xx, connection errors and unknown exceptions do not.
            rejected = isinstance(exc, InvalidToken) or (
                isinstance(exc, MaxApiError)
                and exc.code in {400, 401, 403, 404, 405, 413, 415, 422, 429}
            )
            await _set_first_card_state(appeal.id, "pending" if rejected else "uncertain")
        return None

    new_mid = extract_message_id(sent)
    if is_first_publication and not new_mid:
        await _set_first_card_state(appeal.id, "uncertain")
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
                await session.execute(
                    update(Appeal).where(Appeal.id == appeal.id)
                    .values(initial_card_state="published")
                )
        if is_first_publication:
            appeal.admin_message_id = new_mid
    return new_mid


def _count_attachments(appeal: Appeal) -> int:
    """Сколько вложений у обращения и переписки обеих сторон.
    Устойчиво к detached state (lazy-fail → fallback на scalar)."""
    try:
        from aemr_bot.services.admin_relay import collect_appeal_attachments

        return len(collect_appeal_attachments(appeal))
    except Exception:
        log.debug(
            "admin_card._count_attachments: lazy-load fail for appeal #%s",
            appeal.id, exc_info=False,
        )
        return len(getattr(appeal, "attachments", None) or [])
