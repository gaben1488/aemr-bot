"""Пересылка вложений жителя в служебную группу (relay).

Раньше функция жила в handlers/appeal.py и импортировалась оттуда же
кросс-хендлерами (operator_reply.py делал
`from aemr_bot.handlers.appeal import _relay_attachments_to_admin`).
Это нарушало слой: services не должны знать о handlers, а handlers не
должны импортироваться друг из друга по приватным символам.

Сюда вынесена чистая сервисная функция; handler-ы импортируют её через
обычный публичный путь.

Неизвестный результат отправки не повторяется автоматически: MAX мог
принять вложение до сетевого тайм-аута. Вложения остаются в базе,
оператору отправляется уведомление о необходимости проверки.
"""
from __future__ import annotations

import logging

from aemr_bot.config import settings as cfg
from aemr_bot.utils.attachments import deserialize_for_relay

log = logging.getLogger(__name__)

def _chunk_relayable(relayable: list) -> list[list]:
    """MAX разрешает группу фото/видео, но файлы — только по одному."""
    chunk_size = max(1, min(12, cfg.attachments_per_relay_message))
    batches: list[list] = []
    media: list = []
    for item in relayable:
        raw_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
        kind = str(raw_type or "").lower().rsplit(".", 1)[-1]
        if kind in {"image", "video"}:
            media.append(item)
            if len(media) >= chunk_size:
                batches.append(media)
                media = []
        else:
            if media:
                batches.append(media)
                media = []
            batches.append([item])
    if media:
        batches.append(media)
    return batches


def _build_reply_link(mid: str | None):
    """Собрать `NewMessageLink(REPLY)` на `mid` либо None, если `mid`
    пуст или типы maxapi недоступны. Единый источник reply-link для
    обеих relay-функций (raw-mid → связка с карточкой обращения).
    """
    if not mid:
        return None
    try:
        from maxapi.enums.message_link_type import MessageLinkType
        from maxapi.types.message import NewMessageLink

        return NewMessageLink(type=MessageLinkType.REPLY, mid=mid)
    except Exception:
        log.exception("NewMessageLink build failed; relay без reply-link")
        return None


async def _send_once(send_coro_factory, *, batch_idx: int,
                     total_batches: int, appeal_id: int) -> bool:
    """Одна попытка: исключение не доказывает, что MAX не принял сообщение."""
    try:
        await send_coro_factory()
        return True
    except Exception as exc:
        log.warning("relay batch %d/%d for #%s has an uncertain outcome (%s)",
                    batch_idx, total_batches, appeal_id, type(exc).__name__)
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


def collect_appeal_attachments(appeal) -> list[dict]:
    """Вложения исходного обращения и переписки обеих сторон, по порядку."""
    out = list(appeal.attachments or [])
    for msg in getattr(appeal, "messages", None) or []:
        if getattr(msg, "direction", None) in {"from_user", "from_operator"}:
            out.extend(getattr(msg, "attachments", None) or [])
    return out


async def render_appeal_attachments(
    bot,
    *,
    chat_id: int | None,
    user_id: int | None,
    appeal,
    header_template: str = "Вложения обращения #{appeal_id}",
    reply_to_mid: str | None = None,
    attachments_override: list[dict] | None = None,
) -> None:
    """Переотправить сохранённые вложения обращения и переписки в
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
    all_atts = (collect_appeal_attachments(appeal) if attachments_override is None
                else attachments_override)
    if not all_atts:
        return
    relayable = deserialize_for_relay(all_atts)
    unprepared = max(0, len(all_atts) - len(relayable))

    link = _build_reply_link(reply_to_mid if chat_id is not None else None)

    batches = _chunk_relayable(relayable)
    total = len(batches)
    failed = 0
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
        except Exception as exc:
            failed += len(batch)
            log.warning(
                "render_appeal_attachments: batch %d/%d for #%s has uncertain outcome (%s)",
                idx, total, appeal.id, type(exc).__name__,
            )
    if unprepared or failed:
        try:
            notice = f"⚠️ Вложения обращения № {appeal.id}:"
            if unprepared:
                notice += f" не удалось подготовить {unprepared} из {len(all_atts)}."
            if failed:
                notice += (f" Результат отправки {failed} вложений неизвестен. "
                           "Проверьте чат перед повторным показом.")
            await bot.send_message(
                chat_id=chat_id,
                user_id=None if chat_id is not None else user_id,
                text=notice,
            )
        except Exception:
            log.exception("render_appeal_attachments: could not report missing attachments")


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

    Возвращает True, если все вложения подготовлены и отправлены; иначе
    False и по возможности сообщает оператору о неполной выдаче.
    """
    if not cfg.admin_group_id or not stored_attachments:
        return True
    relayable = deserialize_for_relay(stored_attachments)
    unprepared = max(0, len(stored_attachments) - len(relayable))

    link = _build_reply_link(admin_mid)

    batches = _chunk_relayable(relayable)
    total_batches = len(batches)
    succeeded = 0
    for idx, batch in enumerate(batches, start=1):
        header = (
            f"Вложения к обращению #{appeal_id}"
            if total_batches == 1
            else f"Вложения к обращению #{appeal_id} ({idx}/{total_batches})"
        )

        def _factory(_batch=batch, _header=header):
            return bot.send_message(
                chat_id=cfg.admin_group_id,
                text=_header,
                attachments=_batch,
                link=link,
            )

        ok = await _send_once(
            _factory, batch_idx=idx, total_batches=total_batches,
            appeal_id=appeal_id,
        )
        if ok:
            succeeded += 1

    if succeeded < total_batches or unprepared:
        try:
            from aemr_bot.services import admin_bus

            await admin_bus.send(
                bot,
                text=(
                    f"⚠️ Не все вложения обращения #{appeal_id} показаны в чате: "
                    f"не подготовлено {unprepared}, результат отправки "
                    f"{total_batches - succeeded} из {total_batches} групп неизвестен. "
                    "Проверьте чат и вложения в карточке; не повторяйте отправку вслепую."
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
    return True
