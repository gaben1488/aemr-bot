"""Runtime-helpers и финализация обращения.

Выделено из handlers/appeal.py (рефакторинг 2026-05-10) для разделения
крупного 1400-строчного файла. Сюда попало то, что:
- Не привязано к шагам FSM (helper-уровень)
- Используется ВНУТРИ финализации обращения
- Импортируется из main.py (recover_stuck_funnels)

Не зависит от других appeal_*-модулей. Может импортироваться откуда
угодно без риска цикла.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import AppealStatus, DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import current_user
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import users as users_service

log = logging.getLogger(__name__)

# Имя жителя / адрес должны содержать хотя бы один буквенно-цифровой
# символ — это защищает от отправки "👍", "...", "`````" и подобных
# бессмысленных сообщений (состоящих из одного символа).
_HAS_ALNUM = re.compile(r"[A-Za-zА-Яа-яЁё0-9]")

# Per-user lock для защиты от двойной финализации воронки. Только
# один-инстанс — при горизонтальном масштабировании потребуется
# pg_advisory_xact_lock или Redis-lock. См. _persist_and_dispatch_appeal.
_user_locks: dict[int, asyncio.Lock] = {}
PERSIST_RATE_LIMITED = "rate_limited"


def get_user_lock(max_user_id: int) -> asyncio.Lock:
    """Блокировка для каждого пользователя, чтобы параллельные пути
    отправки, отмены и восстановления после перезапуска не приводили к
    двойной диспетчеризации.

    Только для одного экземпляра приложения — при горизонтальном
    масштабировании потребуется pg_advisory_xact_lock или Redis.
    """
    lock = _user_locks.get(max_user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[max_user_id] = lock
    return lock


def drop_user_lock(max_user_id: int) -> None:
    """Освобождает объект блокировки после полного завершения воронки.
    Предотвращает бесконечное разрастание словаря `_user_locks` по мере
    прохождения пользователей через бота. Безопасно вызывать когда
    никто не удерживает блокировку — операция dict-pop идемпотентна."""
    lock = _user_locks.get(max_user_id)
    if lock is not None and not lock.locked():
        _user_locks.pop(max_user_id, None)


async def recover_stuck_funnels(bot) -> int:
    """Завершает воронки, оставшиеся в состоянии AWAITING_SUMMARY после
    перезапуска. Запускается один раз при старте бота.
    """
    async with session_scope() as session:
        ids = await users_service.find_stuck_in_summary(
            session, idle_seconds=cfg.appeal_collect_timeout_seconds
        )
    if not ids:
        return 0

    results = await asyncio.gather(
        *(persist_and_dispatch_appeal(bot, uid) for uid in ids),
        return_exceptions=True,
    )

    # Пустые обращения никогда не получают повторный запрос при
    # восстановлении — сбрасываем их в IDLE, чтобы они не появлялись
    # при каждом последующем проходе recover().
    empty_ids = [uid for uid, r in zip(ids, results, strict=True) if r is False]
    if empty_ids:
        async with session_scope() as session:
            for uid in empty_ids:
                await users_service.reset_state(session, uid)

    finalized = sum(1 for r in results if r is True)
    failed = sum(1 for r in results if isinstance(r, BaseException))
    if failed:
        log.warning("восстановление: %d/%d воронок завершились с ошибкой", failed, len(ids))
    if finalized:
        log.info("восстановлено %d застрявших воронок", finalized)
    return finalized


def _apply_repeat_context(
    *,
    topic: str,
    summary: str,
    data: dict[str, Any],
) -> tuple[str, str]:
    source_id = data.get("repeat_source_appeal_id")
    source_status = data.get("repeat_source_status")
    if not source_id or source_status not in {
        AppealStatus.ANSWERED.value,
        AppealStatus.CLOSED.value,
    }:
        return topic, summary

    if source_status == AppealStatus.ANSWERED.value:
        label = "обратная связь по отвеченному вопросу"
    else:
        label = "обратная связь по закрытому вопросу"

    base_topic = (data.get("repeat_source_topic") or topic or "без темы").strip()
    marked_topic = f"{label.capitalize()}: {base_topic}"[:120]
    marked_summary = (
        f"Связано с обращением #{source_id}: {label}.\n\n{summary}"
    )[: cfg.summary_max_chars]
    return marked_topic, marked_summary


async def persist_and_dispatch_appeal(bot, max_user_id: int) -> bool | str | None:
    """Создает обращение (Appeal) из накопленных данных dialog_data,
    публикует карточку для админов, подтверждает жителю по user_id.
    Возвращает True при успешном сохранении и отправке, False при
    пустом обращении, PERSIST_RATE_LIMITED при превышении лимита, None —
    если состояние уже IDLE.

    Защищено через asyncio.Lock для каждого пользователя, поэтому
    повторная доставка одного и того же события или восстановление после
    перезапуска не может создать два обращения — второй вызов увидит
    состояние IDLE и прервется.

    Rate-limit ВНУТРИ lock'а закрывает TOCTOU-окно: ранее проверка
    делалась только в _start_appeal_flow, а финализация шла без
    re-check.
    """
    try:
        async with get_user_lock(max_user_id):
            async with current_user(max_user_id) as (session, user):
                if user.dialog_state == DialogState.IDLE.value:
                    log.info(
                        "отправка пропущена для пользователя %s — состояние уже IDLE",
                        max_user_id,
                    )
                    return None
                recent = await appeals_service.count_recent_for_user(
                    session, user.id, hours=1
                )
                if recent >= 3:
                    log.warning(
                        "лимит новых обращений при финализации: user=%s, "
                        "recent=%d, обращение не создано",
                        max_user_id, recent,
                    )
                    await users_service.reset_state(session, max_user_id)
                    return PERSIST_RATE_LIMITED
                data: dict[str, Any] = dict(user.dialog_data or {})
                summary = "\n".join(data.get("summary_chunks") or []).strip()
                attachments = data.get("attachments") or []
                if not summary and not attachments:
                    return False
                topic, summary = _apply_repeat_context(
                    topic=data.get("topic", ""),
                    summary=summary,
                    data=data,
                )
                appeal = await appeals_service.create_appeal(
                    session,
                    user=user,
                    locality=data.get("locality") or None,
                    address=data.get("address", ""),
                    topic=topic,
                    summary=summary,
                    attachments=attachments,
                )
                await users_service.reset_state(session, max_user_id)

        # Single source of truth для admin appeal card —
        # services/admin_card.render. Helper send новую карточку
        # (admin_message_id ещё пуст) и обновит Appeal.admin_message_id
        # в БД. Все последующие изменения статуса тоже через этот helper.
        from aemr_bot.services import admin_card as admin_card_service

        # appeal был загружен внутри уже закрытой session_scope —
        # любое обращение к relationships (user, messages, attachments)
        # вне сессии вызывает MissingGreenlet. Делаем snapshot:
        # - appeal.user = user — копируем уже-загруженный объект
        # - appeal.__dict__["messages"] = [] — на finalize история пуста;
        #   без этого _loaded_messages в card_format и
        #   _collect_all_user_attachments в admin_relay попытаются
        #   lazy-load → exception → обращение не доходит до админа.
        appeal.user = user
        appeal.__dict__["messages"] = []
        admin_mid = await admin_card_service.render(
            bot, appeal, is_first_publication=True
        )
        if not admin_mid:
            log.warning(
                "обращение #%s создано, но карточка администратора не была "
                "опубликована (admin_mid=None)",
                appeal.id,
            )

        from aemr_bot.services.admin_relay import relay_attachments_to_admin

        await relay_attachments_to_admin(
            bot,
            appeal_id=appeal.id,
            admin_mid=admin_mid,
            stored_attachments=attachments,
        )

        try:
            # «Обращение N принято» — это EVENT-сообщение (запись о
            # факте принятия), не навигация. Без клавиатуры. Главное
            # меню жителю всегда доступно через /menu — не нужно
            # дублировать кнопками в каждом event-ack.
            await bot.send_message(
                user_id=max_user_id,
                text=texts.APPEAL_ACCEPTED.format(number=appeal.id),
            )
        except Exception:
            log.exception(
                "подтверждение жителю %s не удалось для обращения #%s",
                max_user_id, appeal.id,
            )

        return True
    finally:
        drop_user_lock(max_user_id)
