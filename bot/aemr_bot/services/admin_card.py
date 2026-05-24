"""Единая точка рендера/обновления admin appeal card.

**Контракт**: `Appeal.admin_message_id` всегда указывает на актуальную
карточку — последнюю отправленную копию для этого обращения. Все
изменения статуса (reply / reopen / close / block / unblock / erase /
followup от жителя) проходят через `render()` и сохраняют этот
инвариант.

До этого helper'а карточка редактировалась тремя несинхронными способами:
- operator_reply.py: `bot.edit_message(admin_message_id)` напрямую
- admin_appeal_ops._show_appeal_card_or_result: через freshness-tracker
- appeal_funnel followup: ручной `bot.send_message` (новая карточка)

Конкретное нарушение, которое это исправляет: оператор скроллит вниз
чата, видит свежую карточку (после followup жителя), тапает «✉️
Ответить» → reply edit'ит ОРИГИНАЛЬНУЮ карточку вверху по старому
admin_message_id из БД, который не обновлялся при followup. Свежая
карточка остаётся без отметки «отвечено», оригинал не виден. Здесь
после render(force_new=True) admin_message_id обновляется — все
последующие edit'ы попадают в актуальную карточку внизу.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aemr_bot import keyboards
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.utils.event import extract_message_id

if TYPE_CHECKING:
    from aemr_bot.db.models import Appeal

log = logging.getLogger(__name__)


async def render(
    bot,
    appeal: "Appeal",
    *,
    force_new: bool = False,
) -> str | None:
    """Отрисовать актуальное состояние карточки обращения в админ-чате.

    Args:
        bot: maxapi Bot instance.
        appeal: Appeal с подгруженными user и messages (для лента и
            подсчёта attachments). Передавайте загруженный через
            `appeals_service.get_by_id_with_messages`.
        force_new: если True — всегда send новую карточку и update
            admin_message_id. Используется для followup жителя (новая
            информация требует явной отметки внизу чата). По умолчанию
            False — пытаемся edit существующую (если есть
            admin_message_id и edit удался). На fail edit'а — fallback
            на send_new + update admin_message_id.

    Returns:
        mid отправленной/отредактированной карточки, или None при сбое.
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

    text = card_format.admin_card(appeal, user)
    kb = keyboards.appeal_admin_actions(
        appeal.id,
        appeal.status,
        is_it=True,
        user_blocked=bool(user.is_blocked),
        closed_due_to_revoke=bool(getattr(appeal, "closed_due_to_revoke", False)),
        attachment_count=_count_attachments(appeal),
    )
    attachments = [kb]

    existing_mid = getattr(appeal, "admin_message_id", None)

    if not force_new and existing_mid:
        try:
            await bot.edit_message(
                message_id=existing_mid,
                text=text,
                attachments=attachments,
            )
            return existing_mid
        except Exception:
            log.info(
                "admin_card.render: edit_message #%s failed for appeal #%s, "
                "fallback to send_new",
                existing_mid, appeal.id, exc_info=False,
            )

    # Send new card (force_new=True, или edit упал, или admin_message_id пуст).
    try:
        sent = await bot.send_message(
            chat_id=cfg.admin_group_id,
            text=text,
            attachments=attachments,
        )
    except Exception:
        log.exception(
            "admin_card.render: send_message failed for appeal #%s", appeal.id
        )
        return None

    new_mid = extract_message_id(sent)
    if new_mid:
        async with session_scope() as session:
            await appeals_service.set_admin_message_id(
                session, appeal.id, new_mid
            )
    return new_mid


def _count_attachments(appeal: "Appeal") -> int:
    """Сколько вложений у обращения (исходные + дополнения жителя)."""
    from aemr_bot.services.admin_relay import _collect_all_user_attachments

    return len(_collect_all_user_attachments(appeal))
