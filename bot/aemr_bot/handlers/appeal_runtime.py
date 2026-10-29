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
from weakref import WeakValueDictionary

from sqlalchemy import or_, select

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import Appeal, AppealStatus, DialogState, User
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import resident_name_gate
from aemr_bot.handlers._common import current_user
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import users as users_service
from aemr_bot.utils.background import spawn_background_task
from aemr_bot.utils.resident_lifecycle import guard

log = logging.getLogger(__name__)

# Имя жителя / адрес должны содержать хотя бы один буквенно-цифровой
# символ — это защищает от отправки "👍", "...", "`````" и подобных
# бессмысленных сообщений (состоящих из одного символа).
_HAS_ALNUM = re.compile(r"[A-Za-zА-Яа-яЁё0-9]")

# Per-user lock для защиты от двойной финализации воронки. Только
# один-инстанс — при горизонтальном масштабировании потребуется
# pg_advisory_xact_lock или Redis-lock. См. _persist_and_dispatch_appeal.
_user_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
PERSIST_RATE_LIMITED = "rate_limited"
PERSIST_NO_CONSENT = "no_consent"
# Заблокированного нельзя отправлять давать согласие: /start блокировку не
# снимает, житель ходит по кругу и не понимает, почему обращение не уходит.
# Снять блокировку может только ИТ, поэтому исход отдельный.
PERSIST_BLOCKED = "blocked"
PERSIST_NAME_REQUIRED = "name_required"

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
    """Compatibility hook: weak registry removes locks when all users leave.

    Releasing a lock can wake a waiter before it reacquires it. Neither
    ``locked()`` nor a completed funnel proves that no caller still holds
    this object, so explicitly removing the entry would split the lock.
    """
    _ = max_user_id


async def recover_unpublished_appeals(bot) -> int:
    """Publish a bounded batch of saved, eligible first cards.

    Pending means no external send has started. Sending/uncertain records
    receive an operator warning instead of an automatic replay. The card
    service shares a per-appeal lock and a durable compare-and-set claim
    with normal finalization, and reloads relationships before sending.
    """
    from aemr_bot.services import admin_card as admin_card_service

    if not cfg.admin_group_id:
        return 0
    async with session_scope() as session:
        result = await session.scalars(
            select(Appeal.id)
            .join(Appeal.user)
            .where(
                Appeal.admin_message_id.is_(None),
                Appeal.status.in_([AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]),
                Appeal.closed_due_to_revoke.is_(False),
                User.is_blocked.is_(False),
                User.consent_pdn_at.is_not(None),
                or_(
                    Appeal.initial_card_state == "pending",
                    (
                        Appeal.initial_card_state.in_(["sending", "uncertain"])
                        & Appeal.initial_card_notified.is_(False)
                    ),
                ),
            )
            .order_by(Appeal.id)
            .limit(100)
        )
        ids = list(result.all())

    sem = asyncio.Semaphore(_RECOVERY_CONCURRENCY)

    async def recover(appeal_id: int) -> bool:
        async with sem:
            try:
                return await admin_card_service.recover_first_publication(bot, appeal_id)
            except Exception:
                log.exception("first admin-card recovery failed for appeal #%s", appeal_id)
                return False

    return sum(await asyncio.gather(*(recover(appeal_id) for appeal_id in ids)))


async def recover_stuck_funnels(bot) -> int:
    """Keep the startup hook without sending unfinished resident drafts."""
    return 0


async def persist_and_dispatch_appeal(
    bot, max_user_id: int, *, reset_empty: bool = False, draft_token: str | None = None
) -> bool | str | None:
    """Создает обращение (Appeal) из накопленных данных dialog_data,
    публикует карточку для админов, подтверждает жителю по user_id.
    Возвращает True при успешном сохранении и отправке, False при
    пустом обращении, PERSIST_RATE_LIMITED при превышении лимита, None —
    если состояние уже не AWAITING_SUMMARY.

    Защищено через asyncio.Lock для каждого пользователя, поэтому
    повторная доставка одного и того же события или восстановление после
    перезапуска не может создать два обращения — второй вызов увидит
    состояние IDLE и прервется.

    Rate-limit ВНУТРИ lock'а закрывает TOCTOU-окно: ранее проверка
    делалась только в _start_appeal_flow, а финализация шла без
    re-check.
    """
    try:
        # Recovery also participates in the inbox/erasure lifecycle gate:
        # a startup scan must not overwrite a newer live citizen action.
        async with guard(max_user_id), get_user_lock(max_user_id), current_user(max_user_id) as (session, user):
            user = await users_service.find_by_max_id(session, max_user_id, for_update=True)
            if user is None:
                return None
            if user.dialog_state != DialogState.AWAITING_SUMMARY.value:
                log.info(
                    "отправка пропущена для пользователя %s — состояние уже не AWAITING_SUMMARY",
                    max_user_id,
                )
                return None
            if not draft_token or (user.dialog_data or {}).get("draft_token") != draft_token:
                return None
            # Единый гейт согласия/блокировки в точке создания: путь
            # «Подать похожее» (menu.start_appeal_repeat) выставляет
            # AWAITING_SUMMARY напрямую, минуя проверки start_appeal_flow.
            # Заблокированный или отозвавший согласие иначе создал бы
            # неотвечаемое обращение. Один гейт здесь закрывает все
            # входы в финализацию.
            if user.is_blocked:
                await users_service.reset_state(session, max_user_id)
                return PERSIST_BLOCKED
            if user.consent_pdn_at is None:
                await users_service.reset_state(session, max_user_id)
                return PERSIST_NO_CONSENT
            if resident_name_gate.needs_name(user.first_name):
                await users_service.set_state(
                    session, max_user_id, DialogState.AWAITING_NAME,
                    data=resident_name_gate.resume_data(user.dialog_state, dict(user.dialog_data or {})),
                )
                try:
                    await bot.send_message(
                        user_id=max_user_id,
                        text=resident_name_gate.NAME_PROMPT + "\n\nЧерновик обращения сохранён.",
                        attachments=[keyboards.cancel_keyboard()],
                    )
                except Exception:
                    log.exception("name request delivery failed for resident %s", max_user_id)
                return PERSIST_NAME_REQUIRED
            recent = await appeals_service.count_recent_for_user(
                session, user.id, hours=1
            )
            if recent >= 3:
                log.warning(
                    "лимит новых обращений при финализации: user=%s, "
                    "recent=%d, обращение не создано",
                    max_user_id, recent,
                )
                # Keep the draft so the resident can submit it once the
                # rate-limit window clears, without typing it all again.
                return PERSIST_RATE_LIMITED
            data: dict[str, Any] = dict(user.dialog_data or {})
            replay = data.get(resident_name_gate.RESUME_KEY) or {}
            if replay.get("ready") and "text" in replay and not replay.get("applied"):
                # A crash after name restoration but before replay must not
                # let startup cleanup discard the saved input as an empty draft.
                return None
            summary = "\n".join(data.get("summary_chunks") or [])
            attachments = data.get("attachments") or []
            if not summary.strip() and not attachments:
                if reset_empty:
                    # Same lock and transaction as the state check; a later
                    # cleanup transaction could erase a newly started funnel.
                    await users_service.reset_state(session, max_user_id)
                return False
            topic = data.get("topic", "")
            related_id = data.get("repeat_source_appeal_id")
            # A previously stored button or modified draft must never link
            # another resident's appeal to this submission.
            if related_id:
                try:
                    related = await appeals_service.get_by_id(session, int(related_id))
                except (TypeError, ValueError):
                    related = None
                related_id = related.id if related is not None and related.user_id == user.id else None
            appeal = await appeals_service.create_appeal(
                session,
                user=user,
                locality=data.get("locality") or None,
                address=data.get("address", ""),
                topic=topic,
                summary=summary,
                attachments=attachments,
                related_appeal_id=related_id,
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
