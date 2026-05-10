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


async def relay_attachments_to_admin(
    bot,
    *,
    appeal_id: int,
    admin_mid: str | None,
    stored_attachments: list[dict],
) -> None:
    """Переслать сохранённые вложения жителя в служебную группу.

    Если admin_mid задан и maxapi предоставляет NewMessageLink, делаем
    relay как reply на исходную карточку обращения — оператор видит
    вложения связкой с обращением. Иначе уходит отдельным сообщением
    с текстовым заголовком.

    Лимит `cfg.attachments_per_relay_message` режет большие наборы на
    батчи: серверный лимит MAX на одно сообщение не задокументирован,
    но 10 вложений за раз стабильно проходят.
    """
    if not cfg.admin_group_id or not stored_attachments:
        return
    relayable = deserialize_for_relay(stored_attachments)
    if not relayable:
        return
    try:
        from maxapi.enums.message_link_type import MessageLinkType
        from maxapi.types.message import NewMessageLink
    except Exception:
        log.exception(
            "типы ссылок maxapi недоступны; пересылка без reply-link"
        )
        MessageLinkType = None  # type: ignore[assignment]
        NewMessageLink = None  # type: ignore[assignment]

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

        await _send_with_retry(
            _factory, batch_idx=idx, total_batches=total_batches,
            appeal_id=appeal_id,
        )
