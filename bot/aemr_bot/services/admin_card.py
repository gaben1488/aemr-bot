"""Единая точка рендера/обновления admin appeal card.

**Контракт:**

- `Appeal.admin_message_id` указывает на ОРИГИНАЛЬНУЮ карточку
  обращения (первую опубликованную при finalize). Это sacred artifact —
  меняется только при изменении статуса самого обращения
  (reply / reopen / close / block / unblock / erase).
- `force_new=False` → edit по admin_message_id. На fail (карточка
  удалена в MAX или другая ошибка) → fallback send-new + обновить
  admin_message_id (старый mid недействителен, надо переезжать).
- `force_new=True` → ВСЕГДА send-new, но admin_message_id НЕ
  обновляется. Это для **следов** активности: followup жителя
  публикует «вторая карточка с дополнением», но оригинал остаётся
  каноническим (где живут reply/reopen/close).

Разделение ответственности: каждая карточка в чате — отдельный
артефакт. Оригинальная = «вот обращение, отвечайте здесь». Follow-up
карточки внизу = «вот ещё информация по тому же обращению».
Оператор работает с оригиналом для финального ответа.

До этого helper'а карточка редактировалась тремя несинхронными
способами (operator_reply прямой edit_message, admin_appeal_ops через
freshness-tracker, appeal_funnel ручной send_message). Helper
унифицирует, сохраняя стабильность оригинала.
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

    # text и attachment_count считаем устойчиво: detached appeal с
    # lazy relationships (messages) может бросить MissingGreenlet.
    # Главное — карточка дойдёт. Без attachment_count = просто без
    # кнопки «Вложения (N)»; без timeline — без блока истории.
    try:
        text = card_format.admin_card(appeal, user)
    except Exception:
        log.exception(
            "admin_card.render: card_format.admin_card failed for #%s, "
            "fallback на минимальный текст",
            appeal.id,
        )
        text = f"Обращение #{appeal.id}\nЖитель: {user.first_name or '—'}"
    try:
        attachment_count = _count_attachments(appeal)
    except Exception:
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

    existing_mid = getattr(appeal, "admin_message_id", None)

    edit_succeeded = False
    if not force_new and existing_mid:
        try:
            await bot.edit_message(
                message_id=existing_mid,
                text=text,
                attachments=attachments,
            )
            edit_succeeded = True
            return existing_mid
        except Exception:
            log.info(
                "admin_card.render: edit_message #%s failed for appeal #%s, "
                "fallback to send_new (admin_message_id будет обновлён "
                "на новый — старый недействителен)",
                existing_mid, appeal.id, exc_info=False,
            )

    # Send new card. Три причины почему попали сюда:
    #   1) force_new=True — следовая карточка (followup от жителя).
    #      admin_message_id НЕ обновляем — оригинал остаётся sacred.
    #   2) admin_message_id пуст — первая публикация (finalize).
    #      Обновляем admin_message_id новым mid.
    #   3) Edit упал — старая карточка в MAX недействительна.
    #      Обновляем admin_message_id (переезжаем на новый).
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
    # Обновляем admin_message_id ТОЛЬКО если:
    #   - existing_mid пуст (это первая публикация), либо
    #   - existing_mid был, но edit упал (нужен переезд).
    # При force_new=True с живым existing_mid — НЕ обновляем
    # (оригинал sacred, force_new=True шлёт всего лишь следовую карточку).
    should_update_mid = new_mid and not (force_new and existing_mid)
    if should_update_mid:
        async with session_scope() as session:
            await appeals_service.set_admin_message_id(
                session, appeal.id, new_mid
            )
    # Подавить unused-var предупреждение про edit_succeeded.
    _ = edit_succeeded
    return new_mid


def _count_attachments(appeal: "Appeal") -> int:
    """Сколько вложений у обращения (исходные + дополнения жителя).

    Устойчиво к detached-state: если appeal без активной сессии,
    `appeal.messages` может бросить MissingGreenlet. В этом случае
    считаем только исходные attachments (scalar JSONB поле, доступно
    без сессии). Это safer, чем падать — caller (например finalize)
    мог передать appeal без selectinload(messages).
    """
    try:
        from aemr_bot.services.admin_relay import _collect_all_user_attachments

        return len(_collect_all_user_attachments(appeal))
    except Exception:
        log.debug(
            "admin_card._count_attachments: lazy-load fail for appeal #%s, "
            "fallback to attachments-only",
            appeal.id,
            exc_info=False,
        )
        # Только исходные. Дополнения посчитать не можем без сессии.
        return len(getattr(appeal, "attachments", None) or [])
