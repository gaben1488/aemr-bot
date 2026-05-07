"""Пересылка вложений жителя в служебную группу (relay).

Раньше функция жила в handlers/appeal.py и импортировалась оттуда же
кросс-хендлерами (operator_reply.py делал
`from aemr_bot.handlers.appeal import _relay_attachments_to_admin`).
Это нарушало слой: services не должны знать о handlers, а handlers не
должны импортироваться друг из друга по приватным символам.

Сюда вынесена чистая сервисная функция; handler-ы импортируют её через
обычный публичный путь.
"""
from __future__ import annotations

import logging

from aemr_bot.config import settings as cfg
from aemr_bot.utils.attachments import deserialize_for_relay

log = logging.getLogger(__name__)


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
        try:
            await bot.send_message(
                chat_id=cfg.admin_group_id,
                text=header,
                attachments=batch,
                link=link,
            )
        except Exception:
            log.exception(
                "не удалось переслать пакет вложений %d/%d для обращения #%s",
                idx, total_batches, appeal_id,
            )
