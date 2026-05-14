"""Geo-flow для FSM-воронки обращения.

Выделено из handlers/appeal.py (рефакторинг 2026-05-10).

Логика:
1. Житель на шаге AWAITING_LOCALITY либо тапает кнопку населённого
   пункта (callback `locality:N`), либо тапает «📍 Поделиться
   геолокацией». Во втором случае MAX шлёт MESSAGE_CREATED с
   location-attachment, которое попадает в `_on_awaiting_locality`.
2. `extract_location` достаёт координаты, `services.geo.find_address`
   определяет посёлок + улицу + дом по локальной OSM-базе.
3. State переходит в AWAITING_GEO_CONFIRM, бот шлёт подтверждающий
   экран с тремя кнопками (✅/✏️/🔙).
4. Тап по `geo:confirm` → AWAITING_TOPIC. `geo:edit_address` →
   AWAITING_ADDRESS. `geo:other_locality` → AWAITING_LOCALITY (с
   обнулённым detected_*). Эти callback'и обрабатываются в
   register() в appeal.py — здесь только state-handlers.

Зависимости:
- appeal_funnel: для `_ask_locality` (fallback при ошибке) и
  для типа функций — импортируется лениво внутри handler'ов
  чтобы избежать циклической зависимости funnel ↔ geo.
"""
from __future__ import annotations

import logging

from aemr_bot import keyboards, texts
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import current_user
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service

log = logging.getLogger(__name__)


async def on_awaiting_locality(event, body, text_body, max_user_id):
    """Житель прислал что-то вместо нажатия на кнопку населённого пункта.

    Если это **геолокация** — определяем населённый пункт и адрес через
    локальную базу OSM (`services/geo.py`) и переходим в подтверждение
    `AWAITING_GEO_CONFIRM`. Если просто текст — повторно показываем
    клавиатуру со списком поселений (свободный ввод не принимаем —
    координаторам нужны стабильные категории для маршрутизации).
    """
    from aemr_bot.utils.attachments import extract_location

    raw_atts = getattr(body, "attachments", None) or []
    log.info(
        "awaiting_locality: user=%s text=%r attachments_count=%d",
        max_user_id, (text_body or "")[:50], len(raw_atts),
    )

    location = extract_location(body)
    if location is not None:
        log.info("awaiting_locality: got location user=%s", max_user_id)
        await handle_location_for_locality(event, max_user_id, location)
        return

    async with session_scope() as session:
        localities = await settings_store.get(session, "localities") or [
            "Елизовское ГП"
        ]
    await event.message.answer(
        texts.LOCALITY_REQUEST,
        attachments=[keyboards.localities_keyboard(localities)],
    )


async def handle_location_for_locality(
    event, max_user_id: int, location: tuple[float, float]
) -> None:
    """Житель поделился координатами на шаге AWAITING_LOCALITY.

    Определяем поселение и адрес через `services.geo`, сохраняем в
    dialog_data как `detected_*`, переводим в AWAITING_GEO_CONFIRM и
    показываем подтверждающий экран. Право жителя исправить — через
    кнопки экрана.
    """
    from aemr_bot.services import geo as geo_service

    lat, lon = location
    result = geo_service.find_address(lat, lon)
    log.info(
        "geo result for user=%s: locality=%r conf=%s",
        max_user_id, result.locality, result.confidence,
    )

    if result.locality is None:
        # Точка вне ЕМО — оставляем шаг как есть, просим выбрать вручную
        async with session_scope() as session:
            localities = await settings_store.get(session, "localities") or [
                "Елизовское ГП"
            ]
        await event.message.answer(
            texts.GEO_OUTSIDE_EMO,
            attachments=[keyboards.localities_keyboard(localities)],
        )
        return

    detected_data = {
        "locality": result.locality,
        "detected_locality": result.locality,
        "detected_street": result.street or "",
        "detected_house_number": result.house_number or "",
        "detected_lat": lat,
        "detected_lon": lon,
        "detected_confidence": result.confidence,
    }
    async with session_scope() as session:
        await users_service.update_dialog_data(
            session, max_user_id, detected_data
        )
        await users_service.set_state(
            session, max_user_id, DialogState.AWAITING_GEO_CONFIRM
        )

    if result.street and result.house_number:
        text = texts.GEO_DETECTED_FULL.format(
            locality=result.locality,
            address=f"{result.street}, д. {result.house_number}",
        )
    elif result.street:
        text = texts.GEO_DETECTED_FULL.format(
            locality=result.locality,
            address=result.street,
        )
    else:
        text = texts.GEO_DETECTED_LOCALITY_ONLY.format(locality=result.locality)

    try:
        sent = await event.message.answer(
            text, attachments=[keyboards.geo_confirm_keyboard()]
        )
        from aemr_bot.utils.event import extract_message_id

        progress_mid = extract_message_id(sent)
        if progress_mid:
            async with session_scope() as session:
                await users_service.update_dialog_data(
                    session,
                    max_user_id,
                    {"progress_message_id": progress_mid},
                )
        log.info("geo: sent confirm screen to user=%s", max_user_id)
    except Exception:
        log.exception("geo: failed to send confirm screen to user=%s", max_user_id)


async def on_awaiting_geo_confirm(event, body, text_body, max_user_id):
    """Житель прислал что-то вместо нажатия кнопки на экране
    подтверждения. Просто повторно показываем подтверждающий экран —
    кнопки решают за житель что делать дальше."""
    async with current_user(max_user_id) as (_, user):
        data = dict(user.dialog_data or {})
    locality = data.get("detected_locality") or data.get("locality") or "?"
    street = data.get("detected_street") or ""
    house = data.get("detected_house_number") or ""
    if street and house:
        text = texts.GEO_DETECTED_FULL.format(
            locality=locality, address=f"{street}, д. {house}"
        )
    elif street:
        text = texts.GEO_DETECTED_FULL.format(locality=locality, address=street)
    else:
        text = texts.GEO_DETECTED_LOCALITY_ONLY.format(locality=locality)
    await event.message.answer(
        text, attachments=[keyboards.geo_confirm_keyboard()]
    )
