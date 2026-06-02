"""Send pipeline + история рассылок + диспетчер `/broadcast`.

Сценарий оператора в админ-чате:

  1. /broadcast               → бот просит ввести текст.
  2. оператор вводит текст    → бот показывает предпросмотр с числом подписчиков.
  3. оператор жмёт ✅          → бот запускает фоновую задачу отправки.
  4. фоновая задача           → шлёт рассылку со скоростью 1 сообщение в секунду,
                                 редактирует сообщение прогресса в админ-группе
                                 раз в BROADCAST_PROGRESS_UPDATE_SEC секунд.
  5. любой жмёт ⛔ stop       → статус переключается в cancelled, цикл выходит.

ИСТОРИЯ. Cluster C wave 2 (Codex PR 7, 2026-05-28): wizard FSM
(_WizardState, _wizards, _start_wizard, _handle_wizard_text,
_handle_confirm, _handle_abort, _handle_edit, prefill_wizard_from_template,
_drop_expired_wizards, _resolve_broadcast_max_images) физически живёт в
`handlers/broadcast_wizard`. Здесь — send pipeline (`_send_one`,
`_run_broadcast`), cooldown (`_run_with_cooldown`,
`_handle_cancel_cooldown`, `_handle_stop`), история (_list_broadcasts,
_open_broadcast, _clone_broadcast, _list_failed_deliveries) и
`register(dp)`. Wizard символы re-export'нуты ниже, чтобы 12 тестовых
файлов + 4 production callsite (`broadcast_templates.py:601`,
`admin_callback_dispatch.py:31`, `admin_operators.py:730`,
`admin_appeal_ops.py:127`) продолжали импорт `from
aemr_bot.handlers.broadcast import _wizards` без правок.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from maxapi import Dispatcher
from maxapi.types import Command, MessageCreated
from zoneinfo import ZoneInfo

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import BroadcastStatus, OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator, ensure_role, get_operator
from aemr_bot.services import broadcasts as broadcasts_service
# Cluster C (Codex PR 7): pure utility функции вынесены в
# `services/broadcast_utils`. Re-export через `import` чтобы старые
# тестовые импорты `from aemr_bot.handlers.broadcast import _format_progress`
# продолжали работать без правок (см. test_broadcast_handlers.py,
# test_broadcast_429_backoff.py).
from aemr_bot.services.broadcast_utils import (  # noqa: F401
    _COOLDOWN_EMERGENCY_SEC,
    _COOLDOWN_NORMAL_SEC,
    _EMERGENCY_MARKER,
    _broadcast_cooldown_seconds,
    _build_final_text,
    _compute_progress_step,
    _extract_retry_after,
    _format_dt as _format_dt_pure,
    _format_progress,
)
# Cluster C wave 2 (Codex PR 7): wizard FSM физически живёт в
# broadcast_wizard. Re-export нужен, чтобы существующие импорты
# `from aemr_bot.handlers.broadcast import _wizards` продолжали
# работать без правок (12 файлов тестов + broadcast_templates.py +
# admin_callback_dispatch.py + admin_operators.py + admin_appeal_ops.py).
from aemr_bot.handlers.broadcast_wizard import (  # noqa: F401
    WizardStep,
    _WizardState,
    _drop_expired_wizards,
    _handle_abort,
    _handle_confirm,
    _handle_edit,
    _handle_wizard_text,
    _resolve_broadcast_max_images,
    _start_wizard,
    _wizards,
    prefill_wizard_from_template,
)
from aemr_bot.utils import image_attachments as _image_attachments
from aemr_bot.utils.event import (
    get_message_text,
    is_admin_chat,
    send_or_edit_screen,
)
from aemr_bot.utils.event import ack_callback, get_user_id  # noqa: F401

log = logging.getLogger(__name__)

TZ = ZoneInfo(cfg.timezone)


# SECURITY_REVIEW C2: pending-таски на cooldown между confirm и реальной
# отправкой. Ключ — broadcast_id, значение — asyncio.Task. Если оператор
# жмёт «❌ Отменить отправку» во время cooldown'а — task.cancel() и
# рассылка не уходит. Если бот перезагрузился во время cooldown'а —
# task теряется (safe-by-default: оператор увидит что рассылка не дошла
# и переотправит). Cooldown не хранится в БД сознательно: для гос-канала
# лучше «случайно не отправили» чем «отправили автоматически после
# рестарта без подтверждения оператора».
_pending_broadcasts: dict[int, "asyncio.Task"] = {}


# Локальные псевдонимы общих хелперов авторизации. Используются
# `_handle_stop` (оператор завершает идущую рассылку) и cmd_broadcast
# (только админ-чат). Wizard'ы свои дублируют в `broadcast_wizard.py`.
# `_get_operator` сохранён как compat-алиас: после Cluster C wave 2
# wizard функции переехали в broadcast_wizard, но тесты по-прежнему
# делают `patch("aemr_bot.handlers.broadcast._get_operator", ...)`
# и читают `broadcast._get_operator`. Удалять алиас нельзя без
# обновления ~10 test files — это держится здесь как noop-binding.
_is_admin_chat = is_admin_chat
_ensure_role = ensure_role
_ensure_operator = ensure_operator
_get_operator = get_operator


async def _run_with_cooldown(
    bot, broadcast_id: int, text: str, count: int,
    *, admin_mid: str | None, cooldown_sec: int,
) -> None:
    """Подождать cooldown_sec, затем запустить _run_broadcast.

    Если task отменён (оператор нажал «❌ Отменить отправку»),
    `CancelledError` всплывает наружу — рассылка не уходит, статус
    в БД помечается как cancelled через `_handle_cancel_cooldown`.
    После успешного запуска / отмены — убираем себя из
    `_pending_broadcasts`.
    """
    try:
        await asyncio.sleep(cooldown_sec)
    except asyncio.CancelledError:
        log.info("broadcast: cooldown cancelled — broadcast_id=%s", broadcast_id)
        raise
    # Cooldown прошёл. Убираем pending-метку — теперь рассылка идёт,
    # отменить нельзя (только экстренная остановка через _handle_stop).
    _pending_broadcasts.pop(broadcast_id, None)
    await _run_broadcast(bot, broadcast_id, text, count, admin_mid=admin_mid)


async def _handle_cancel_cooldown(event, broadcast_id: int) -> None:
    """SECURITY_REVIEW C2 + F4/F5: отмена рассылки во время cooldown'а.

    Сценарий: оператор нажал «отправить», увидел сообщение «уйдёт через
    5 минут», осознал ошибку и жмёт «❌ Отменить отправку».

    Race-window protection (F4): pop из `_pending_broadcasts` —
    неатомарная операция, между sleep-return и pop'ом другая корутина
    может вытащить task. Используем `task.done()` как **атомарный**
    индикатор: если task завершился (cooldown прошёл, отправка
    стартовала) — отказываем в отмене с явным сообщением.

    Stale-DRAFT protection (F5): если mark_cancelled в БД фейлит
    (timeout, disconnect) — task всё равно отменён в asyncio, рассылка
    не пойдёт, но row в БД останется DRAFT. Логируем + alert. Отдельный
    cron `reap_orphaned_draft` чистит такие row'ы по TTL.
    """
    # SECURITY (audit 2026-05-28): тот же гейт, что в _handle_stop.
    # Случайный гость в админ-группе или удалённый из БД member, у
    # которого ещё не отозван доступ к чату, не должен мочь отменить
    # срочную рассылку (например, объявление о ЧС). Раньше проверка
    # была только у _handle_stop — cancel-cooldown её пропускал
    # (асимметрия авторизации).
    if not _is_admin_chat(event):
        await ack_callback(event)
        return
    if not await _ensure_operator(event):
        await ack_callback(event)
        return
    await ack_callback(event, "Отменяю…")
    # peek, не pop — pop делаем только если реально отменили
    task = _pending_broadcasts.get(broadcast_id)
    if task is None or task.done():
        # task is None — cancel пришёл после того как _run_with_cooldown
        # уже pop'нул себя (cooldown отработал).
        # task.done() — то же самое, но atomically (task ещё в dict, но
        # уже завершён в asyncio).
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=(
                f"⚠️ Рассылка #{broadcast_id} уже стартовала — cooldown "
                f"истёк раньше, чем вы успели нажать «отменить». Для "
                f"остановки уже идущей рассылки используйте кнопку "
                f"«⛔ Экстренно остановить» под progress-карточкой."
            ),
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    task.cancel()
    _pending_broadcasts.pop(broadcast_id, None)
    # F5: mark_cancelled может фейлить — task всё равно отменён, рассылка
    # не пойдёт. Логируем, но не падаем; reaper подберёт DRAFT row позже.
    try:
        from aemr_bot.services import operators as operators_service

        async with session_scope() as session:
            await broadcasts_service.mark_cancelled(session, broadcast_id)
            actor_id = get_user_id(event) or 0
            await operators_service.write_audit(
                session,
                operator_max_user_id=actor_id,
                action="broadcast_cancel_cooldown",
                target=f"broadcast #{broadcast_id}",
                details={"reason": "operator_cancelled_during_cooldown"},
            )
    except Exception:
        log.exception(
            "broadcast: cancel during cooldown — db update failed for "
            "broadcast_id=%s; task cancelled, row stays DRAFT until reaper",
            broadcast_id,
        )
    log.info(
        "broadcast: cancelled during cooldown — operator=%s broadcast_id=%s",
        get_user_id(event), broadcast_id,
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            f"❌ Рассылка #{broadcast_id} отменена. Жителям ничего не "
            f"отправлено. Если нужно переотправить — начните мастер "
            f"заново через «📣 Рассылка»."
        ),
        attachments=[keyboards.op_back_to_menu_keyboard()],
    )


async def _handle_stop(event, broadcast_id: int) -> None:
    """Остановить идущую рассылку. Доступно только зарегистрированным
    операторам — чтобы случайный гость в админ-группе или удалённый из
    БД member, у которого ещё не удалили доступ к чату, не мог
    отменить срочное объявление о ЧС."""
    if not _is_admin_chat(event):
        await ack_callback(event)
        return
    if not await _ensure_operator(event):
        await ack_callback(event)
        return
    async with session_scope() as session:
        flipped = await broadcasts_service.request_cancel(session, broadcast_id)
        if flipped:
            # audit 2026-05-28: экстренная остановка идущей рассылки —
            # действие не менее значимое, чем отмена во время cooldown
            # (которая уже пишет audit). Фиксируем симметрично, чтобы
            # в audit_log была полная картина «кто и когда остановил».
            from aemr_bot.services import operators as operators_service

            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event) or 0,
                action="broadcast_stop",
                target=f"broadcast #{broadcast_id}",
                details={"reason": "operator_emergency_stop"},
            )
    await ack_callback(
        event, "Остановлено." if flipped else "Уже завершено."
    )


# `_format_progress` теперь в services/broadcast_utils (Cluster C, PR 7).


async def _send_one(
    bot,
    max_user_id: int,
    body_text: str,
    *,
    outbound_images: Sequence[Any] = (),
) -> str | None:
    """Возвращает None при успехе и строку с ошибкой при сбое.

    `outbound_images` — уже десериализованные maxapi-объекты картинок
    рассылки (раз-в-цикле через `image_attachments.build_outbound_image_attachments`,
    не на каждого подписчика). Картинки идут впереди клавиатуры отписки,
    чтобы UI MAX отрендерил их как content рассылки, а не как payload
    клавиатуры.

    MAXAPI_DEEP_DIVE §14 fix (P1): maxapi 1.1.0 НЕ ретраит HTTP 429
    и не парсит Retry-After header. Для broadcast на 1000+ жителей
    это критично — без backoff каждый 429 = потерянный получатель и
    риск временного бана токена. Ловим MaxApiError с code=429 явно,
    ждём (Retry-After если есть в raw, иначе exponential 1→2→4 сек),
    повторяем до 3 раз. После 3 неудачных — фиксируем как failed,
    идём дальше (не блокируем всю рассылку из-за одного жителя).
    """
    delay_sec = 1.0
    for attempt in range(3):
        try:
            await bot.send_message(
                user_id=max_user_id,
                text=body_text,
                attachments=[
                    *outbound_images,
                    keyboards.broadcast_unsubscribe_keyboard(),
                ],
            )
            return None
        except Exception as e:
            err_repr = repr(e)
            # Детектируем 429 (Too Many Requests) по тексту ошибки.
            # maxapi.MaxApiError содержит code в repr — проверяем
            # через подстроку, чтобы не тащить imports конкретного
            # exception класса (в разных версиях SDK имя отличается).
            is_rate_limit = (
                "429" in err_repr
                or "rate" in err_repr.lower()
                or "too many" in err_repr.lower()
            )
            if not is_rate_limit:
                # Не rate-limit — обычная ошибка (заблокирован, нет
                # такого user_id, network), не ретраим.
                return err_repr[:500]
            if attempt == 2:
                # Третья попытка тоже 429 — сдаёмся, идём к следующему
                # получателю. Темп между сообщениями держат ДВА уровня:
                # per-broadcast rate_delay (_run_broadcast_impl) и
                # процесс-глобальный лимитер ~1.5 msg/s в
                # admin_bus.install_outgoing_tracker_hook (через него идёт
                # этот send). 429 даже сквозь оба — знак, что нагрузка
                # реально упёрлась (или MAX временно жёстче лимитит).
                log.warning(
                    "broadcast: 429 после 3 попыток для user_id=%s — пропускаем",
                    max_user_id,
                )
                return err_repr[:500]
            # Попробовать достать Retry-After из exception, иначе
            # exponential.
            wait = _extract_retry_after(e) or delay_sec
            log.info(
                "broadcast: 429 для user_id=%s, попытка %d, жду %.1f сек",
                max_user_id, attempt + 1, wait,
            )
            await asyncio.sleep(wait)
            delay_sec *= 2
    return "exhausted"


# `_extract_retry_after` теперь в services/broadcast_utils (Cluster C, PR 7).


async def _run_broadcast(
    bot, broadcast_id: int, text: str, total: int, *, admin_mid: str | None = None
) -> None:
    """Фоновая задача: отправляет подготовленную рассылку всем подходящим подписчикам,
    редактирует сообщение прогресса в админ-группе, реагирует на флаг отмены.

    Все ошибки гасятся и логируются. Задача запускается через asyncio.create_task,
    поэтому необработанное исключение иначе осталось бы незамеченным до сборки мусора.
    """
    try:
        await _run_broadcast_impl(bot, broadcast_id, text, total, admin_mid=admin_mid)
    except Exception:
        log.exception(
            "broadcast: _run_broadcast_impl crashed for broadcast_id=%s",
            broadcast_id,
        )
        # По возможности переводим статус в failed, чтобы /broadcast list это показывал.
        try:
            async with session_scope() as session:
                await broadcasts_service.mark_finished(
                    session,
                    broadcast_id,
                    status=BroadcastStatus.FAILED,
                    delivered=0,
                    failed=0,
                )
        except Exception:
            log.exception(
                "broadcast: failed to mark broadcast_id=%s as failed",
                broadcast_id,
            )


# `_compute_progress_step` и `_build_final_text` теперь в
# services/broadcast_utils (Cluster C, PR 7).


async def _resolve_admin_progress_message(
    bot, broadcast_id: int, total: int, admin_mid: str | None
) -> str | None:
    """Гарантировать карточку прогресса в админ-группе.

    Если confirm-кнопка была под preview-карточкой, preview уже
    превращён в progress-карточку — `admin_mid` придёт заполненным.
    Иначе шлём отдельное стартовое сообщение, чтобы оператор не остался
    без статуса. Возврат None означает «карточки нет, edit_message по
    ходу рассылки будет пропущен».
    """
    if admin_mid is not None:
        return admin_mid
    # SACRED #1: через admin_bus — двигает tracker на mid стартовой
    # карточки прогресса, чтобы прогресс-edit'ы потом могли проверить
    # freshness (admin_mid == tracker).
    from aemr_bot.services import admin_bus

    return await admin_bus.send(
        bot,
        text=texts.OP_BROADCAST_STARTED.format(number=broadcast_id, total=total),
        attachments=[keyboards.broadcast_stop_keyboard(broadcast_id)],
    )


async def _send_final_summary(
    bot,
    *,
    broadcast_id: int,
    total: int,
    delivered: int,
    failed: int,
    cancelled: bool,
    admin_mid: str | None,
) -> None:
    """Опубликовать итог рассылки СВЕЖИМ сообщением через admin_bus.

    SACRED #2: финальная сводка — это event-карточка, иммутабельная по
    смыслу (запись о факте «рассылка завершена с такими-то цифрами»).
    Раньше код пытался edit'нуть progress-карточку (admin_mid) — но за
    время рассылки выше неё в чате могли появиться pulse, ответы
    оператора, другие события. edit на сдвинутой вверх карточке
    оператор не увидит. И сам факт edit нарушает event-log семантику.

    Решение: всегда send_new через admin_bus.send (двигает tracker).
    Параметр `admin_mid` оставлен в сигнатуре ради совместимости
    с вызывающими; теперь используется только в логе.
    """
    from aemr_bot.services import admin_bus

    final_text = _build_final_text(
        broadcast_id=broadcast_id,
        total=total,
        delivered=delivered,
        failed=failed,
        cancelled=cancelled,
    )
    new_mid = await admin_bus.send(
        bot,
        text=final_text,
        attachments=[keyboards.op_back_to_menu_keyboard()],
    )
    if new_mid is None:
        log.warning(
            "broadcast #%s: final summary НЕ опубликован (admin_bus вернул None). "
            "Прежний progress-mid=%s, оператор увидит результат в /broadcast list.",
            broadcast_id, admin_mid,
        )


async def _run_send_loop(
    bot,
    *,
    broadcast_id: int,
    body: str,
    total: int,
    targets: list,
    admin_mid: str | None,
    rate_delay: float,
    progress_step_sec: float,
    outbound_images: Sequence[Any] = (),
) -> tuple[int, int, bool]:
    """Цикл отправки рассылки. Возвращает ``(delivered, failed, cancelled)``.

    Результаты доставки копятся в буфер и сбрасываются батчем в единой
    точке синхронизации с БД (по таймеру progress_step_sec либо при
    переполнении буфера) — там же читается флаг отмены и пишется
    прогресс. Раньше каждый получатель = 2-4 транзакции; на 10k
    подписчиков было ~25000 коммитов за рассылку, теперь ~200.
    """
    delivered = 0
    failed = 0
    cancelled = False
    last_progress_at = time.monotonic()

    _FLUSH_EVERY = 50
    pending: list[tuple[int, str | None]] = []

    async def _flush_pending() -> None:
        """Сбросить буфер доставок в БД. Best-effort: при сбое логируем,
        но не валим рассылку — count_delivery_results в mark_finished
        пересчитает счётчики по факту записанных строк."""
        if not pending:
            return
        try:
            async with session_scope() as flush_session:
                await broadcasts_service.record_deliveries(
                    flush_session,
                    broadcast_id=broadcast_id,
                    results=pending,
                )
            pending.clear()
        except Exception:
            log.exception(
                "broadcast #%s: failed to flush %d pending deliveries",
                broadcast_id, len(pending),
            )

    try:
        for user_db_id, user_max_user_id in targets:
            error = await _send_one(
                bot, user_max_user_id, body,
                outbound_images=outbound_images,
            )
            pending.append((user_db_id, error))
            if error is None:
                delivered += 1
            else:
                failed += 1

            now = time.monotonic()
            # Единая точка синхронизации с БД: flush буфера + чтение
            # флага отмены + запись прогресса + edit карточки. По
            # таймеру либо при переполнении буфера.
            if (
                now - last_progress_at >= progress_step_sec
                or len(pending) >= _FLUSH_EVERY
            ):
                last_progress_at = now
                await _flush_pending()
                async with session_scope() as sync_session:
                    status = await broadcasts_service.get_status(
                        sync_session, broadcast_id
                    )
                    await broadcasts_service.update_progress(
                        sync_session,
                        broadcast_id,
                        delivered=delivered,
                        failed=failed,
                    )
                if status == BroadcastStatus.CANCELLED.value:
                    cancelled = True
                    break
                # SACRED #2: progress edit ТОЛЬКО если карточка прогресса
                # всё ещё последнее сообщение бота в admin chat. Иначе
                # её сдвинуло pulse / ответ оператора / другое событие
                # выше — оператор внизу edit'а не увидит. На таком тике
                # progress скипаем, finalize всё равно опубликует
                # правильный итог свежим сообщением.
                if admin_mid is not None:
                    from aemr_bot.utils import menu_tracker

                    if (
                        cfg.admin_group_id
                        and menu_tracker.get_last_menu_mid(cfg.admin_group_id)
                        == admin_mid
                    ):
                        try:
                            await bot.edit_message(
                                message_id=admin_mid,
                                text=_format_progress(
                                    broadcast_id=broadcast_id,
                                    total=total,
                                    delivered=delivered,
                                    failed=failed,
                                ),
                                attachments=[
                                    keyboards.broadcast_stop_keyboard(broadcast_id)
                                ],
                            )
                        except Exception:
                            log.exception(
                                "failed to edit progress message for broadcast #%s",
                                broadcast_id,
                            )

            await asyncio.sleep(rate_delay)
    finally:
        # Любой выход из цикла (конец списка, break по отмене,
        # исключение) — досбрасываем остаток буфера, иначе уже
        # отправленные сообщения не попадут в broadcast_deliveries
        # и при повторной рассылке жители получат дубль.
        await _flush_pending()

    return delivered, failed, cancelled


async def _run_broadcast_impl(
    bot, broadcast_id: int, text: str, total: int, *, admin_mid: str | None = None
) -> None:
    """Оркестрация фоновой рассылки: подготовка карточки прогресса →
    снимок получателей → цикл отправки → финальный статус и итог.

    Тяжёлая логика вынесена в помощники: `_resolve_admin_progress_message`
    (стартовая карточка), `_run_send_loop` (цикл с буферизацией доставок),
    `_send_final_summary` (итог). Здесь — только последовательность шагов.
    """
    body = f"{texts.BROADCAST_HEADER}\n\n{text}"
    log.info(
        "broadcast: starting send loop — broadcast_id=%s total=%d",
        broadcast_id, total,
    )

    admin_mid = await _resolve_admin_progress_message(
        bot, broadcast_id, total, admin_mid
    )
    log.info(
        "broadcast: admin start-message admin_mid=%s "
        "(None means edit_message will be skipped)",
        admin_mid,
    )

    async with session_scope() as session:
        await broadcasts_service.mark_started(session, broadcast_id, admin_mid)

    # Per-broadcast pacing. Perf-кластер «2 RPS» (2026-06-02): авторитетный
    # потолок исходящих теперь — глобальный token-bucket в
    # `admin_bus.install_outgoing_tracker_hook`, через который проходит КАЖДЫЙ
    # `bot.send_message` (включая этот цикл). Локальный `rate_delay` оставлен
    # СОЗНАТЕЛЬНО как грубый per-broadcast pace: он держит темп одной рассылки
    # (~1 msg/s по умолчанию) НИЖЕ глобального бюджета (~1.5 msg/s), оставляя
    # запас в общем бакете под интерактив оператора и cron — иначе длинная
    # рассылка выгребала бы все токены и подвешивала ответы. Дублирования
    # троттла нет: глобальный лимитер почти не блокирует на этом цикле, пока
    # rate_delay медленнее, и вступает в дело только когда рассылка идёт
    # ОДНОВРЕМЕННО с другими отправками (ровно тот случай, что пробивал 429).
    rate_delay = (
        1.0 / cfg.broadcast_rate_limit_per_sec
        if cfg.broadcast_rate_limit_per_sec > 0
        else 1.0
    )
    progress_step_sec = _compute_progress_step(total, rate_delay)

    # Снимаем список получателей и закрываем сессию. Удержание одной
    # транзакции на всю отправку (одна строка в секунду на N получателей)
    # блокирует VACUUM и раздувает WAL при длинной рассылке.
    async with session_scope() as session:
        targets = await broadcasts_service.list_subscriber_targets(session)

    # Картинки рассылки (если оператор прикрепил на confirm-шаге).
    # Десериализуем ровно один раз: deserialize_for_relay вызывает
    # pydantic-валидацию, не хотим тратить её на каждого подписчика.
    # В send-loop уходят уже готовые maxapi-объекты.
    async with session_scope() as session:
        broadcast_row = await broadcasts_service.get_by_id(session, broadcast_id)
    stored_attachments = (
        broadcast_row.attachments if broadcast_row is not None else []
    )
    outbound_images = _image_attachments.build_outbound_image_attachments(
        stored_attachments
    )

    delivered, failed, cancelled = await _run_send_loop(
        bot,
        broadcast_id=broadcast_id,
        body=body,
        total=total,
        targets=targets,
        admin_mid=admin_mid,
        rate_delay=rate_delay,
        progress_step_sec=progress_step_sec,
        outbound_images=outbound_images,
    )

    final_status = (
        BroadcastStatus.CANCELLED if cancelled else BroadcastStatus.DONE
    )
    async with session_scope() as session:
        await broadcasts_service.mark_finished(
            session,
            broadcast_id,
            status=final_status,
            delivered=delivered,
            failed=failed,
        )
    log.info(
        "broadcast: finished — broadcast_id=%s status=%s delivered=%d failed=%d",
        broadcast_id, final_status.value, delivered, failed,
    )

    await _send_final_summary(
        bot,
        broadcast_id=broadcast_id,
        total=total,
        delivered=delivered,
        failed=failed,
        cancelled=cancelled,
        admin_mid=admin_mid,
    )


def _format_dt(dt: datetime | None) -> str:
    """Локализованная дата/время в формате `DD.MM.YYYY HH:MM`.

    Тонкий wrapper над `_format_dt_pure` из services/broadcast_utils —
    подставляет TZ модуля. Существующие call-сайты внутри broadcast.py
    зовут без явного tz, поэтому wrapper остаётся ради удобства.
    """
    return _format_dt_pure(dt, TZ)


async def _list_broadcasts(event) -> None:
    if not await _ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    async with session_scope() as session:
        items = await broadcasts_service.list_recent(session, limit=10)
    if not items:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_BROADCAST_LIST_EMPTY,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    lines = [texts.OP_BROADCAST_LIST_HEADER.rstrip()]
    for bc in items:
        lines.append(
            texts.OP_BROADCAST_LIST_ITEM.format(
                number=bc.id,
                created_at=_format_dt(bc.created_at),
                status=bc.status,
                delivered=bc.delivered_count,
                total=bc.subscriber_count_at_start,
            )
        )
    # PR G: кнопки-строки списка → клик открывает карточку рассылки.
    # Текст списка остаётся (быстрый обзор), кнопки выше дублируют
    # цифры и кликабельны.
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text="\n".join(lines),
        attachments=[keyboards.broadcast_history_list_keyboard(items)],
    )


async def _open_broadcast(event, broadcast_id: int) -> None:
    """`op:bc:open:<id>` — карточка одной рассылки (PR G).

    Показывает текст, картинки, счётчики доставки и две кнопки:
    «📝 Создать на основе» (prefill /broadcast wizard) и «👥 Не
    доставлено» (если failed_count > 0).
    """
    if not await _ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    async with session_scope() as session:
        bc = await broadcasts_service.get_by_id(session, broadcast_id)
    if bc is None:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_BROADCAST_NOT_FOUND,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    failed_line = (
        texts.OP_BROADCAST_CARD_FAILED_LINE.format(failed=bc.failed_count)
        if bc.failed_count
        else ""
    )
    body = texts.OP_BROADCAST_CARD.format(
        number=bc.id,
        status=bc.status,
        created_at=_format_dt(bc.created_at),
        delivered=bc.delivered_count,
        total=bc.subscriber_count_at_start,
        failed_line=failed_line,
        image_count=len(bc.attachments or []),
        text=bc.text,
    )
    preview_images = _image_attachments.build_outbound_image_attachments(
        bc.attachments or []
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=body,
        attachments=[
            *preview_images,
            keyboards.broadcast_history_card_keyboard(
                bc.id, has_failures=bool(bc.failed_count)
            ),
        ],
    )


async def _clone_broadcast(event, broadcast_id: int) -> None:
    """`op:bc:clone:<id>` — взять рассылку за основу новой (PR G).

    Заряжает /broadcast wizard данными существующей рассылки (text +
    attachments) в шаг awaiting_confirm — оператор видит обычный
    confirm-preview и либо «Разослать», либо «Изменить текст».
    """
    if not await _ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    async with session_scope() as session:
        bc = await broadcasts_service.get_by_id(session, broadcast_id)
        if bc is None:
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_BROADCAST_NOT_FOUND,
                attachments=[keyboards.op_back_to_menu_keyboard()],
            )
            return
        subscribers = await broadcasts_service.count_subscribers(session)
    if subscribers == 0:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_BROADCAST_CLONE_NO_SUBSCRIBERS,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    prefill_wizard_from_template(
        actor_id,
        text=bc.text,
        attachments=list(bc.attachments or []),
    )
    preview_images = _image_attachments.build_outbound_image_attachments(
        bc.attachments or []
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_BROADCAST_PREVIEW.format(
            text=bc.text,
            count=subscribers,
            image_count=len(bc.attachments or []),
            image_warning="",
        ),
        attachments=[
            *preview_images,
            keyboards.broadcast_confirm_keyboard(),
        ],
    )
    log.info(
        "broadcast: clone from #%s by operator=%s — wizard pre-filled "
        "(subscribers=%d)",
        bc.id, actor_id, subscribers,
    )


async def _list_failed_deliveries(event, broadcast_id: int) -> None:
    """`op:bc:failed:<id>` — кому не дошло (PR G)."""
    if not await _ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    LIMIT = 50
    async with session_scope() as session:
        bc = await broadcasts_service.get_by_id(session, broadcast_id)
        if bc is None:
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_BROADCAST_NOT_FOUND,
                attachments=[keyboards.op_back_to_menu_keyboard()],
            )
            return
        rows = await broadcasts_service.list_failed_deliveries(
            session, broadcast_id, limit=LIMIT
        )
    if not rows:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_BROADCAST_FAILED_LIST_EMPTY.format(number=bc.id),
            attachments=[
                keyboards.broadcast_failed_list_keyboard(broadcast_id)
            ],
        )
        return
    lines = [
        texts.OP_BROADCAST_FAILED_LIST_HEADER.format(
            number=bc.id, count=bc.failed_count
        ).rstrip()
    ]
    for _user_id, name, err in rows:
        # Обрезаем ошибку, чтобы не разорвать сообщение MAX-лимитом
        # длины: типичный repr-RuntimeError помещается, но иногда
        # бывает огромный traceback от bot.send_message.
        err_short = (err or "").strip()[:100]
        lines.append(
            texts.OP_BROADCAST_FAILED_LIST_ITEM.format(
                name=name, error=err_short or "—"
            )
        )
    if bc.failed_count > len(rows):
        lines.append(
            texts.OP_BROADCAST_FAILED_LIST_TRUNCATED.format(
                more=bc.failed_count - len(rows), limit=len(rows)
            )
        )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text="\n".join(lines),
        attachments=[keyboards.broadcast_failed_list_keyboard(broadcast_id)],
    )


def register(dp: Dispatcher) -> None:
    """Регистрируем только `/broadcast`. Коллбэки мастера (confirm/abort/stop)
    маршрутизируются из `handlers.appeal.on_callback` делегированием, а кнопка
    жителя `broadcast:unsubscribe` обрабатывается в `handlers.menu`. Второй
    `@dp.message_callback()` намеренно не добавляем, чтобы избежать двойной
    диспетчеризации: maxapi вызывает каждый зарегистрированный обработчик для
    каждого события, и второй такой обработчик дублировал бы каждый ack."""

    @dp.message_created(Command("broadcast"))
    async def cmd_broadcast(event: MessageCreated):
        if not _is_admin_chat(event):
            return
        text = get_message_text(event)
        parts = text.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        if arg.lower() == "list":
            await _list_broadcasts(event)
            return
        await _start_wizard(event)
