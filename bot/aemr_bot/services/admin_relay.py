"""Пересылка вложений жителя в служебную группу (relay).

Раньше функция жила в handlers/appeal.py и импортировалась оттуда же
кросс-хендлерами (operator_reply.py делал
`from aemr_bot.handlers.appeal import _relay_attachments_to_admin`).
Это нарушало слой: services не должны знать о handlers, а handlers не
должны импортироваться друг из друга по приватным символам.

Сюда вынесена чистая сервисная функция; handler-ы импортируют её через
обычный публичный путь.

Retry-loop: каждый batch отправляется до 3 раз с экспоненциальным
бэкофом (0.5/1.0/2.0 сек). Без этого сетевая дрожь в момент
finalize_appeal приводила к потере вложений у оператора (БД-запись
уже сделана commit'ом, retry на handler-уровне нет). Полноценный
outbox-pattern (durable queue + worker) отложен до v2.
"""
from __future__ import annotations

import asyncio
import logging

from aemr_bot.config import settings as cfg
from aemr_bot.utils.attachments import deserialize_for_relay

log = logging.getLogger(__name__)

_RELAY_MAX_ATTEMPTS = 3
# Экспоненциальный бэкоф: 0.5s, 1.0s, 2.0s. Между попытками —
# короткая пауза, чтобы не поджарить MAX rate-limit (2 RPS).
_RELAY_BASE_DELAY_SEC = 0.5


async def _send_with_retry(send_coro_factory, *, batch_idx: int,
                           total_batches: int, appeal_id: int) -> bool:
    """Запустить send_coro_factory() с retry. Возвращает True при успехе.

    `send_coro_factory` — callable без аргументов, возвращающий
    свежую coroutine. Вызывается заново на каждой попытке: однажды
    выполненную coroutine пере-await'ить нельзя.
    """
    delay = _RELAY_BASE_DELAY_SEC
    last_exc: BaseException | None = None
    for attempt in range(1, _RELAY_MAX_ATTEMPTS + 1):
        try:
            await send_coro_factory()
            return True
        except Exception as exc:
            last_exc = exc
            if attempt < _RELAY_MAX_ATTEMPTS:
                log.info(
                    "relay batch %d/%d for #%s — attempt %d failed (%s), "
                    "retry через %.1fs",
                    batch_idx, total_batches, appeal_id, attempt,
                    type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)
                delay *= 2
    log.exception(
        "relay batch %d/%d for #%s ОКОНЧАТЕЛЬНО не удался после %d "
        "попыток: %r",
        batch_idx, total_batches, appeal_id, _RELAY_MAX_ATTEMPTS,
        last_exc,
    )
    return False


def _collect_all_user_attachments(appeal) -> list[dict]:
    """Собрать все вложения, отправленные жителем по обращению —
    исходные (`appeal.attachments`) + дополнения (`Message.attachments`
    для `direction == 'from_user'`). Сохраняет хронологический порядок.

    Используется при повторном показе обращения: «📋 Открытые
    обращения» у оператора, «📂 Мои обращения → карточка» у жителя.
    Без этого helper'а первичный relay (см. relay_attachments_to_admin
    при finalize) виден только в момент подачи; при возврате к карточке
    позже вложения «теряются» визуально, контекст обращения искажается.
    """
    out: list[dict] = []
    out.extend(appeal.attachments or [])
    messages = getattr(appeal, "messages", None) or []
    for msg in messages:
        if getattr(msg, "direction", None) != "from_user":
            continue
        atts = getattr(msg, "attachments", None) or []
        out.extend(atts)
    return out


async def render_appeal_attachments(
    bot,
    *,
    chat_id: int | None,
    user_id: int | None,
    appeal,
    header_template: str = "Вложения обращения #{appeal_id}",
    reply_to_mid: str | None = None,
) -> None:
    """Переотправить все вложения обращения (исходник + дополнения) в
    указанный чат/диалог.

    Универсальная функция для повторного показа обращения с
    вложениями:
    - админ-чат при «📋 Открытые обращения» (chat_id = admin_group_id);
    - личка жителя при «📂 Мои обращения» (user_id = житель).

    Бьёт на батчи по `cfg.attachments_per_relay_message`, чтобы не
    переборщить со server-side лимитом MAX. Retry-loop не используется
    — это не критичный путь (исходный relay уже произошёл при
    создании обращения), а отдельный «удобный» показ.
    """
    if not bot:
        return
    all_atts = _collect_all_user_attachments(appeal)
    if not all_atts:
        return
    relayable = deserialize_for_relay(all_atts)
    if not relayable:
        return

    link = None
    if reply_to_mid and chat_id is not None:
        try:
            from maxapi.enums.message_link_type import MessageLinkType
            from maxapi.types.message import NewMessageLink

            link = NewMessageLink(type=MessageLinkType.REPLY, mid=reply_to_mid)
        except Exception:
            log.exception("NewMessageLink build failed; relay без reply-link")
            link = None

    chunk_size = max(1, cfg.attachments_per_relay_message)
    batches = [
        relayable[i:i + chunk_size]
        for i in range(0, len(relayable), chunk_size)
    ]
    total = len(batches)
    for idx, batch in enumerate(batches, start=1):
        header = header_template.format(appeal_id=appeal.id)
        if total > 1:
            header = f"{header} ({idx}/{total})"
        try:
            await bot.send_message(
                chat_id=chat_id,
                user_id=None if chat_id is not None else user_id,
                text=header,
                attachments=batch,
                link=link,
            )
        except Exception:
            log.exception(
                "render_appeal_attachments: batch %d/%d for #%s failed",
                idx, total, appeal.id,
            )


async def relay_attachments_to_admin(
    bot,
    *,
    appeal_id: int,
    admin_mid: str | None,
    stored_attachments: list[dict],
) -> bool:
    """Переслать сохранённые вложения жителя в служебную группу.

    Если admin_mid задан и maxapi предоставляет NewMessageLink, делаем
    relay как reply на исходную карточку обращения — оператор видит
    вложения связкой с обращением. Иначе уходит отдельным сообщением
    с текстовым заголовком.

    Лимит `cfg.attachments_per_relay_message` режет большие наборы на
    батчи: серверный лимит MAX на одно сообщение не задокументирован,
    но 10 вложений за раз стабильно проходят.

    Возвращает True, если ВСЕ батчи доставлены (или вложений не было).
    False — если хотя бы один батч не удался после всех retry-попыток.
    При ПОЛНОМ провале (ни один батч не доставлен, хотя бы один был) —
    шлём critical-алёрт в админ-группу: без него вложения жителя
    (фото места ямы, скан документа) молча пропадают из виду оператора
    — БД-запись уже закоммичена, а relay это best-effort фон, который
    раньше проигрывал только в лог.
    """
    if not cfg.admin_group_id or not stored_attachments:
        return True
    relayable = deserialize_for_relay(stored_attachments)
    if not relayable:
        return True
    try:
        from maxapi.enums.message_link_type import MessageLinkType
        from maxapi.types.message import NewMessageLink
    except Exception:
        log.exception(
            "типы ссылок maxapi недоступны; пересылка без reply-link"
        )
        MessageLinkType = None  # type: ignore[misc,assignment]
        NewMessageLink = None  # type: ignore[misc,assignment]

    link = None
    if admin_mid and MessageLinkType is not None and NewMessageLink is not None:
        try:
            link = NewMessageLink(type=MessageLinkType.REPLY, mid=admin_mid)
        except Exception:
            log.exception(
                "не удалось собрать NewMessageLink для admin_mid=%s", admin_mid
            )
            link = None

    chunk_size = max(1, cfg.attachments_per_relay_message)
    batches = [
        relayable[i:i + chunk_size]
        for i in range(0, len(relayable), chunk_size)
    ]
    total_batches = len(batches)
    succeeded = 0
    for idx, batch in enumerate(batches, start=1):
        header = (
            f"Вложения к обращению #{appeal_id}"
            if total_batches == 1
            else f"Вложения к обращению #{appeal_id} ({idx}/{total_batches})"
        )

        # Замыкание: каждая попытка строит свежий kwargs (на случай
        # если maxapi мутирует переданное при ошибке).
        def _factory(_batch=batch, _header=header):
            return bot.send_message(
                chat_id=cfg.admin_group_id,
                text=_header,
                attachments=_batch,
                link=link,
            )

        ok = await _send_with_retry(
            _factory, batch_idx=idx, total_batches=total_batches,
            appeal_id=appeal_id,
        )
        if ok:
            succeeded += 1

    if succeeded == 0:
        # Полный провал: ни один батч не дошёл. Частичный провал
        # (succeeded < total_batches, но > 0) НЕ алёртим — оператор уже
        # видит часть вложений и карточку обращения, это не «пропало
        # незаметно», а деградация, видная по логам relay batch.
        try:
            from aemr_bot.services import admin_bus

            await admin_bus.send(
                bot,
                text=(
                    f"⚠️ Вложения обращения #{appeal_id} не доставлены "
                    f"(сбой сети), проверьте вручную."
                ),
                critical=True,
            )
        except Exception:
            log.exception(
                "relay_attachments_to_admin: алёрт о провале доставки "
                "для #%s тоже не удался",
                appeal_id,
            )
        return False
    return succeeded == total_batches
