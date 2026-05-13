"""Короткие уведомления в служебную группу о действиях жителя."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from aemr_bot.config import settings as cfg

log = logging.getLogger(__name__)


async def _send(bot: Any, text: str) -> None:
    """Отправить служебное уведомление и не ломать действие жителя при сбое MAX."""
    if not cfg.admin_group_id:
        return
    try:
        await bot.send_message(chat_id=cfg.admin_group_id, text=text)
    except Exception:
        log.debug("не удалось отправить служебное уведомление", exc_info=True)


async def notify_consent_given(bot: Any, *, max_user_id: int) -> None:
    await _send(
        bot,
        f"✅ Житель дал согласие на обработку ПДн.\nMAX user id: {max_user_id}",
    )


async def notify_consent_revoked(
    bot: Any,
    *,
    max_user_id: int,
    open_appeal_ids: Sequence[int],
) -> None:
    if open_appeal_ids:
        ids = ", ".join(f"#{appeal_id}" for appeal_id in open_appeal_ids)
        detail = (
            f"Открытые обращения ждут финального ответа через бот: {ids}.\n"
            f"Ответьте по обычной карточке или командой /reply. После ответа "
            f"обращение закроется, новые обращения без нового согласия не принимаются."
        )
    else:
        detail = "Открытых обращений у этого жителя сейчас нет."
    await _send(
        bot,
        f"⚠️ Житель отозвал согласие на ПДн.\n"
        f"MAX user id: {max_user_id}\n"
        f"{detail}",
    )


async def notify_broadcast_subscribed(bot: Any, *, max_user_id: int) -> None:
    await _send(
        bot,
        f"🔔 Житель подписался на муниципальные уведомления.\n"
        f"MAX user id: {max_user_id}",
    )


async def notify_broadcast_unsubscribed(
    bot: Any,
    *,
    max_user_id: int,
    source: str,
) -> None:
    await _send(
        bot,
        f"🔕 Житель отписался от муниципальных уведомлений.\n"
        f"MAX user id: {max_user_id}\n"
        f"Источник: {source}",
    )


async def notify_data_erased(
    bot: Any,
    *,
    max_user_id: int,
    closed_appeal_ids: Sequence[int],
) -> None:
    if closed_appeal_ids:
        ids = ", ".join(f"#{appeal_id}" for appeal_id in closed_appeal_ids)
        detail = f"Закрыто без ответа: {ids}. Карточки в чате устарели."
    else:
        detail = "Открытых обращений для закрытия не найдено."
    await _send(
        bot,
        f"🗑 Житель удалил данные из рабочей базы бота.\n"
        f"MAX user id: {max_user_id}\n"
        f"{detail}",
    )
