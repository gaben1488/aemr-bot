"""Короткие уведомления в служебную группу о действиях жителя."""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import users as users_service
from aemr_bot.utils.pii_mask import mask_phone as _mask_phone

log = logging.getLogger(__name__)


async def _send(bot: Any, text: str, *, critical: bool = False) -> None:
    """Отправить служебное уведомление и не ломать действие жителя при сбое MAX.

    Идёт через admin_bus, чтобы каждое уведомление двигало
    `menu_tracker[admin_group_id]`. Без этого freshness-check в
    admin_card.render и send_or_edit_screen после notify_* мог
    ошибочно edit'нуть карточку, выше которой уже физически лежит
    уведомление.

    `critical` пробрасывается в `admin_bus.send` (игнорировать quiet
    hours). 2026-07-09: используется ТОЛЬКО для `notify_consent_revoked`
    и `notify_data_erased` — юридически значимых событий по 152-ФЗ,
    которые не должны молчать до утра. См. docstring этих функций ниже.
    """
    from aemr_bot.services import admin_bus

    if not cfg.admin_group_id:
        return
    try:
        await admin_bus.send(bot, text=text, critical=critical)
    except Exception:
        log.debug("не удалось отправить служебное уведомление", exc_info=True)


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
    """Уведомление о даче согласия на ПДн.

    Гейт `admin_notify_consent` (services/notify_toggles.py): выключен —
    уведомление не отправляется. В отличие от `notify_consent_revoked`
    ниже, дача согласия — рутинное позитивное событие, не юридически
    срочное, поэтому подчиняется тумблеру.
    """
    from aemr_bot.services import notify_toggles

    if not notify_toggles.is_enabled("admin_notify_consent"):
        log.debug("notify_consent_given: admin_notify_consent disabled, suppressed")
        return
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
    """Уведомление об отзыве согласия на ПДн.

    2026-07-09 (находка security-ревью): НЕ подчиняется ни одному
    тумблеру (`admin_notify_*`) и ни quiet hours — отправляется через
    `critical=True`. Отзыв согласия с открытыми обращениями юридически
    значим (152-ФЗ): оператор обязан дать финальный ответ ДО того, как
    данные жителя будут обезличены по retention. Если это уведомление
    потеряется в тихом режиме или будет выключено тумблером — открытое
    обращение рискует остаться без ответа безвозвратно (обезличивание
    убьёт возможность связаться с жителем).
    """
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
        critical=True,
    )


async def notify_broadcast_subscribed(bot: Any, *, max_user_id: int) -> None:
    """Уведомление о подписке на муниципальные уведомления.

    Гейт `admin_notify_subscriptions` (services/notify_toggles.py).
    """
    from aemr_bot.services import notify_toggles

    if not notify_toggles.is_enabled("admin_notify_subscriptions"):
        log.debug(
            "notify_broadcast_subscribed: admin_notify_subscriptions disabled, "
            "suppressed"
        )
        return
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
    """Уведомление об отписке от муниципальных уведомлений.

    Гейт `admin_notify_subscriptions` (services/notify_toggles.py) —
    тот же флаг, что и для подписки (это парная пара событий).
    """
    from aemr_bot.services import notify_toggles

    if not notify_toggles.is_enabled("admin_notify_subscriptions"):
        log.debug(
            "notify_broadcast_unsubscribed: admin_notify_subscriptions disabled, "
            "suppressed"
        )
        return
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
    """Уведомление об удалении данных жителем (/erase).

    2026-07-09 (находка security-ревью): НЕ подчиняется ни одному
    тумблеру и ни quiet hours — отправляется через `critical=True`.
    Удаление данных — юридически значимое событие (152-ФЗ): открытые
    обращения закрываются автоматически, оператор должен узнать об
    этом сразу (карточки в чате устаревают, дальнейшая работа по ним
    бессмысленна). Тот же мотив, что у `notify_consent_revoked` выше.
    """
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
        critical=True,
    )
