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

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import AppealStatus, DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import current_user
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import users as users_service
from aemr_bot.utils.background import spawn_background_task

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

# Потолок одновременных финализаций при восстановлении на старте (P2-2).
# `recover_stuck_funnels` поднимает до `recover_batch_size` (default 1000)
# застрявших воронок. Без bound каждый `persist_and_dispatch_appeal` —
# отдельная корутина, которая берёт соединение из пула БД (всего 15) И
# шлёт несколько запросов к MAX API. 1000 одновременных корутин = мгновенное
# исчерпание пула (остальная работа бота встаёт на ожидание connection) и
# burst к MAX за пределами его 2 RPS → 429-штормы. Семафор 5 держит
# параллелизм восстановления заведомо ниже размера пула и оставляет
# соединения живому трафику. Восстановление не интерактивно — небольшая
# сериализация на старте безопасна; важно лишь не уронить пул и не упереться
# в rate-limit MAX. Симметрично webhook/polling-семафорам в main.py.
_RECOVERY_CONCURRENCY = 5


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

    # Bounded fan-out (P2-2): не больше _RECOVERY_CONCURRENCY одновременных
    # финализаций, чтобы пул БД (15) и rate-limit MAX (2 RPS) не легли при
    # большой пачке застрявших воронок. gather сохраняет порядок результатов,
    # поэтому zip(ids, results) ниже остаётся корректным.
    sem = asyncio.Semaphore(_RECOVERY_CONCURRENCY)

    async def _bounded(uid: int) -> bool | str | None:
        async with sem:
            return await persist_and_dispatch_appeal(bot, uid)

    results = await asyncio.gather(
        *(_bounded(uid) for uid in ids),
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


def _safe_float(value: Any) -> float | None:
    """Привести значение из dialog_data к float или None.

    Координаты в dialog_data проходят через JSONB и могут вернуться
    как число либо строку; мусор/None → None, обращение всё равно
    создаётся (координаты необязательны).
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
                    # Координаты доезжают только если житель делился
                    # геолокацией (appeal_geo кладёт detected_* в
                    # dialog_data); при ручном вводе адреса их нет — тогда
                    # None. JSONB мог сохранить их как строку, приводим.
                    latitude=_safe_float(data.get("detected_lat")),
                    longitude=_safe_float(data.get("detected_lon")),
                    geo_confidence=_safe_float(data.get("detected_confidence")),
                    topic=topic,
                    summary=summary,
                    attachments=attachments,
                )
                await users_service.reset_state(session, max_user_id)

        # Latency-UX (Волна 2): подтверждение жителю отправляем СРАЗУ
        # после успешного commit обращения — ДО рендера админ-карточки и
        # ДО relay вложений. Гарантия порядка сохранена: обращение уже
        # закоммичено и state сброшен (блок `current_user` выше закрыт),
        # поэтому ack не может опередить запись в БД. Раньше житель ждал
        # `admin_card.render` + `relay_attachments_to_admin` (батчи под
        # 2 RPS + retry-backoff) — секунды молчания при вложениях/сетевой
        # дрожи. Теперь его latency = один send_message, а доставка
        # оператору идёт следом и на ack не влияет.
        try:
            # «Обращение N принято» — event-уведомление, но с **полным
            # главным меню** для конверсии. Восстановлено 2026-05-27
            # после жалобы владельца: ранее в cfcf372 (PR #62) я ошибочно
            # убрал кнопки под предлогом «event без CTA». Это заставляло
            # пожилых жителей вводить /menu вручную — они так не делают
            # и просто закрывали диалог. Жалоба: «на принято было 6 кнопок,
            # почему убрал? — идиотское решение, ломаешь хорошее».
            #
            # Sacred-rule всё равно соблюдается: APPEAL_ACCEPTED — это
            # event, прямой `bot.send_message(user_id=...)` НЕ обновляет
            # menu_tracker. Когда житель тапает «↩️ В меню» на ack,
            # freshness rule в `_send_or_edit_menu` (Шаг 1 sweep
            # 2026-05-27) видит callback_mid != tracker (tracker на mid
            # предыдущего экрана воронки) → send_new menu, ack остаётся
            # в чате как иммутабельный slice истории.
            async with session_scope() as _sub_session:
                subscribed = await broadcasts_service.is_subscribed(
                    _sub_session, max_user_id
                )
            await bot.send_message(
                user_id=max_user_id,
                text=texts.APPEAL_ACCEPTED.format(number=appeal.id),
                attachments=[keyboards.main_menu(subscribed=subscribed)],
            )
        except Exception:
            log.exception(
                "подтверждение жителю %s не удалось для обращения #%s",
                max_user_id, appeal.id,
            )

        # Единая точка рендера админской карточки обращения —
        # services/admin_card.render. Helper отправляет новую карточку
        # (admin_message_id ещё пуст) и сохранит admin_message_id в БД.
        # Все последующие смены статуса проходят через этот же helper,
        # чтобы edit-vs-new политика была централизована.
        #
        # Карточку рендерим СИНХРОННО (не в фоне): это основной артефакт
        # для оператора, его доставка — часть гарантии «обращение дошло до
        # служебной группы». В фон уносим только relay вложений (ниже) —
        # он best-effort и сам по себе самый медленный шаг.
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

        # Relay вложений в служебную группу — в фон. Это best-effort шаг
        # (батчи под 2 RPS + retry-backoff), и держать на нём latency
        # жителя или event-loop незачем: ack уже ушёл, карточка уже
        # опубликована, сами вложения сохранены в записи обращения
        # (доступны оператору и без relay). Ошибки проглатываем+логируем
        # внутри обёртки, чтобы фоновая задача не оставляла «Task
        # exception was never retrieved».
        spawn_background_task(
            _relay_attachments_background(
                bot,
                appeal_id=appeal.id,
                admin_mid=admin_mid,
                stored_attachments=attachments,
            ),
            name=f"relay-attachments-{appeal.id}",
        )

        return True
    finally:
        drop_user_lock(max_user_id)


async def _relay_attachments_background(
    bot,
    *,
    appeal_id: int,
    admin_mid: str | None,
    stored_attachments: list[dict[str, Any]],
) -> None:
    """Фоновая обёртка над `relay_attachments_to_admin`.

    Relay вложений жителя в служебную группу best-effort и медленный
    (батчи под 2 RPS + retry-backoff). Запускается в фоне через
    `spawn_background_task`, чтобы не держать latency жителя на цепочке
    отправки вложений. Любое исключение проглатываем+логируем здесь —
    иначе фоновая задача завершится с необработанным исключением
    («Task exception was never retrieved») и ошибка пройдёт незаметно.
    """
    from aemr_bot.services.admin_relay import relay_attachments_to_admin

    try:
        await relay_attachments_to_admin(
            bot,
            appeal_id=appeal_id,
            admin_mid=admin_mid,
            stored_attachments=stored_attachments,
        )
    except Exception:
        log.exception(
            "фоновый relay вложений не удался для обращения #%s",
            appeal_id,
        )
