"""Короткие уведомления в служебную группу о действиях жителя."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import users as users_service

log = logging.getLogger(__name__)


async def _send(bot: Any, text: str) -> None:
    """Отправить служебное уведомление и не ломать действие жителя при сбое MAX."""
    if not cfg.admin_group_id:
        return
    try:
        await bot.send_message(chat_id=cfg.admin_group_id, text=text)
    except Exception:
        log.debug("не удалось отправить служебное уведомление", exc_info=True)


def _mask_phone(phone: str | None) -> str:
    """Маскированный телефон для admin-чата: «+7***1234». 152-ФЗ:
    операторы видят 4 последние цифры для идентификации, но не полный
    номер (он попадает в backup MAX и в скриншоты)."""
    if not phone:
        return "—"
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return phone
    tail = digits[-4:]
    prefix = "+7" if digits[0] in {"7", "8"} and len(digits) >= 11 else "+"
    return f"{prefix}***{tail}"


async def _describe_user(max_user_id: int) -> str:
    """Многострочное описание жителя для admin-уведомлений.

    Возвращает блок:
        Житель: Сергей · +7***4567 · MAX id 12345
        Подписки: 🔔 на рассылку (если подписан)
        Согласие: ✅ активно / 🔁 отозвано / —

    Если пользователь не найден в БД — вернёт только MAX id (например,
    при первом старте до создания записи).
    """
    try:
        async with session_scope() as session:
            user = await users_service.find_by_max_id(session, max_user_id)
    except Exception:
        log.debug(
            "describe_user: не удалось получить user из БД", exc_info=True
        )
        user = None
    if user is None:
        return f"Житель: — · — · MAX id {max_user_id}"

    name = (user.first_name or "—").strip() or "—"
    phone = _mask_phone(user.phone)
    parts_status = []
    if getattr(user, "subscribed_broadcast", False):
        parts_status.append("🔔 подписан на рассылку")
    else:
        parts_status.append("🔕 без подписки")
    if getattr(user, "consent_revoked_at", None) is not None:
        parts_status.append("🔁 согласие отозвано")
    elif getattr(user, "consent_pdn_at", None) is not None:
        parts_status.append("✅ согласие активно")
    if getattr(user, "is_blocked", False):
        parts_status.append("🚫 заблокирован")
    return (
        f"Житель: {name} · {phone} · MAX id {max_user_id}\n"
        f"Статус: {' · '.join(parts_status)}"
    )


async def notify_consent_given(bot: Any, *, max_user_id: int) -> None:
    desc = await _describe_user(max_user_id)
    await _send(
        bot,
        f"✅ Житель дал согласие на обработку ПДн.\n{desc}",
    )


async def notify_consent_revoked(
    bot: Any,
    *,
    max_user_id: int,
    open_appeal_ids: Sequence[int],
) -> None:
    desc = await _describe_user(max_user_id)
    if open_appeal_ids:
        ids = ", ".join(f"#{appeal_id}" for appeal_id in open_appeal_ids)
        detail = (
            f"Открытые обращения ждут финального ответа через бот: {ids}.\n"
            f"Ответьте по обычной карточке или командой /reply. После ответа "
            f"обращение закроется, новые обращения без нового согласия не "
            f"принимаются."
        )
    else:
        detail = "Открытых обращений у этого жителя сейчас нет."
    await _send(
        bot,
        f"⚠️ Житель отозвал согласие на ПДн.\n{desc}\n\n{detail}",
    )


async def notify_broadcast_subscribed(bot: Any, *, max_user_id: int) -> None:
    desc = await _describe_user(max_user_id)
    await _send(
        bot,
        f"🔔 Житель подписался на муниципальные уведомления.\n{desc}",
    )


async def notify_broadcast_unsubscribed(
    bot: Any,
    *,
    max_user_id: int,
    source: str,
) -> None:
    desc = await _describe_user(max_user_id)
    await _send(
        bot,
        f"🔕 Житель отписался от муниципальных уведомлений.\n{desc}\n"
        f"Источник отписки: {source}",
    )


async def notify_data_erased(
    bot: Any,
    *,
    max_user_id: int,
    closed_appeal_ids: Sequence[int],
) -> None:
    # Здесь _describe_user намеренно НЕ зовём: данные жителя уже
    # erase'нуты к этому моменту (first_name='Удалено', phone обнулён).
    # Описание не информативно. Пишем только id и факт.
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
