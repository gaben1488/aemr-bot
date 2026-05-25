"""Мастер рассылок и цикл их отправки.

Сценарий оператора в админ-чате:

  1. /broadcast               → бот просит ввести текст.
  2. оператор вводит текст    → бот показывает предпросмотр с числом подписчиков.
  3. оператор жмёт ✅          → бот запускает фоновую задачу отправки.
  4. фоновая задача           → шлёт рассылку со скоростью 1 сообщение в секунду,
                                 редактирует сообщение прогресса в админ-группе
                                 раз в BROADCAST_PROGRESS_UPDATE_SEC секунд.
  5. любой жмёт ⛔ stop       → статус переключается в cancelled, цикл выходит.

Состояние мастера (шаги 1–3) живёт только в памяти процесса. Операторов нет
в таблице `users`, а недозаполненный мастер дёшево пройти заново. Состояние
вытесняется автоматически по истечении BROADCAST_WIZARD_TTL_SEC.
"""

from __future__ import annotations

import asyncio
import re
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from maxapi import Dispatcher
from maxapi.types import Command, MessageCreated
from zoneinfo import ZoneInfo

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import BroadcastStatus, OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator, ensure_role, get_operator
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import operators as operators_service
from aemr_bot.services import settings_store
from aemr_bot.utils import image_attachments as _image_attachments
from aemr_bot.utils.background import spawn_background_task
from aemr_bot.utils.event import (
    ack_callback,
    extract_message_id,
    get_callback_message_id,
    get_message_text,
    get_user_id,
    is_admin_chat,
    send_or_edit_screen,
)

log = logging.getLogger(__name__)

TZ = ZoneInfo(cfg.timezone)


WizardStep = Literal["awaiting_text", "awaiting_confirm"]


@dataclass
class _WizardState:
    step: WizardStep
    text: str = ""
    # Картинки, приложенные оператором к шагу awaiting_text. Сериализованные
    # dict'ы attachment'ов (тот же формат, что Appeal.attachments). Пустой
    # список = text-only рассылка. На confirm уходит в Broadcast.attachments.
    attachments: list = field(default_factory=list)
    expires_at: float = field(
        default_factory=lambda: time.monotonic() + cfg.broadcast_wizard_ttl_sec
    )

    def expired(self) -> bool:
        return time.monotonic() > self.expires_at

    def renew(self) -> None:
        self.expires_at = time.monotonic() + cfg.broadcast_wizard_ttl_sec


# Состояние мастера для каждого оператора. Только для одного экземпляра приложения.
# При горизонтальном масштабировании потребуется хранение в Redis или через pg_advisory_lock.
_wizards: dict[int, _WizardState] = {}


# SECURITY_REVIEW C2: pending-таски на cooldown между confirm и реальной
# отправкой. Ключ — broadcast_id, значение — asyncio.Task. Если оператор
# жмёт «❌ Отменить отправку» во время cooldown'а — task.cancel() и
# рассылка не уходит. Если бот перезагрузился во время cooldown'а —
# task теряется (safe-by-default: оператор увидит что рассылка не дошла
# и переотправит). Cooldown не хранится в БД сознательно: для гос-канала
# лучше «случайно не отправили» чем «отправили автоматически после
# рестарта без подтверждения оператора».
_pending_broadcasts: dict[int, "asyncio.Task"] = {}

# Маркер «срочная рассылка» — текст с [ЧС] в начале или после пробела
# (case-insensitive). Для таких сокращаем cooldown до 30 секунд, чтобы
# оповещение о ЧС не задерживалось на 5 минут.
_EMERGENCY_MARKER = re.compile(r"(?:^|\s)\[ЧС\]", re.IGNORECASE)
_COOLDOWN_NORMAL_SEC = 300   # 5 минут — обычная рассылка
_COOLDOWN_EMERGENCY_SEC = 30  # 30 секунд — [ЧС] рассылка


def _broadcast_cooldown_seconds(text: str) -> int:
    """Сколько ждать перед фактической отправкой рассылки.

    [ЧС] в тексте → 30 сек (оператор всё ещё может отменить, но не
    задерживаем оповещение о реальной ЧС). Иначе — 5 минут.
    """
    return _COOLDOWN_EMERGENCY_SEC if _EMERGENCY_MARKER.search(text) else _COOLDOWN_NORMAL_SEC


async def _resolve_broadcast_max_images(session) -> int:
    """Текущий лимит картинок в рассылке из настроек БД.

    Возвращает `settings_store.broadcast_max_images` (диапазон 1–20).
    DEFAULTS гарантирует число — функция никогда не вернёт None или 0.
    IT-оператор меняет лимит через UI «⚙️ Настройки бота» без редеплоя;
    значение применяется со следующего нажатия `/broadcast`.

    Устойчив к DB-проблемам: если query упал (нет таблицы, потеря
    соединения), молча падаем на DEFAULTS — рассылка не должна
    блокироваться технической проблемой админ-таблицы.
    """
    try:
        value = await settings_store.get(session, "broadcast_max_images")
    except Exception:
        log.warning(
            "settings_store.broadcast_max_images недоступен, "
            "используем DEFAULTS",
            exc_info=True,
        )
        value = None
    # Защитная нормализация: если кто-то вручную записал в БД не int
    # (например, через psql), скатываемся к DEFAULTS.
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return int(settings_store.DEFAULTS["broadcast_max_images"])


# Локальные псевдонимы общих хелперов авторизации. Подчёркивание в начале имени
# подчёркивает, что это служебные средства для админ-стороны, не для жителя.
_is_admin_chat = is_admin_chat
_get_operator = get_operator
_ensure_role = ensure_role
_ensure_operator = ensure_operator


def _drop_expired_wizards() -> None:
    """Чистит просроченные мастера. Вызывается попутно при каждом новом событии мастера."""
    stale = [uid for uid, st in _wizards.items() if st.expired()]
    for uid in stale:
        _wizards.pop(uid, None)


async def _start_wizard(event) -> None:
    if not await _ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        log.info(
            "broadcast: wizard NOT started — caller failed _ensure_role "
            "(needs it/coordinator)"
        )
        return
    _drop_expired_wizards()
    actor_id = get_user_id(event)
    if actor_id is None:
        log.warning("broadcast: wizard NOT started — no user_id in event")
        return
    # Сбрасываем чужие wizard'ы и reply-intent этого оператора — иначе
    # текст рассылки уйдёт в wizard добавления оператора или жителю
    # как ответ. См. F-003 в operator-аудите.
    try:
        from aemr_bot.handlers import admin_commands as admin_cmd_module
        from aemr_bot.handlers import operator_reply as op_reply

        admin_cmd_module._op_wizards.pop(actor_id, None)
        op_reply.drop_reply_intent(actor_id)
    except Exception:
        log.exception("broadcast: cleanup чужих wizard'ов упал, продолжаем")

    _wizards[actor_id] = _WizardState(step="awaiting_text")
    log.info("broadcast: wizard started for operator max_user_id=%s", actor_id)
    async with session_scope() as session:
        max_images = await _resolve_broadcast_max_images(session)
    prompt = texts.OP_BROADCAST_PROMPT.format(
        limit=cfg.broadcast_max_chars,
        max_images=max_images,
    )
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=prompt,
        attachments=[keyboards.broadcast_cancel_keyboard()],
    )


async def _handle_wizard_text(event, text_body: str) -> bool:
    """Вызывается из глобального обработчика on_message, когда у автора активен
    мастер в шаге awaiting_text. Возвращает True, если сообщение поглощено."""
    actor_id = get_user_id(event)
    if actor_id is None:
        return False
    state = _wizards.get(actor_id)
    if state is None or state.step != "awaiting_text":
        return False
    log.info(
        "broadcast: wizard text accepted — operator=%s text_len=%d",
        actor_id, len(text_body),
    )

    if state.expired():
        _wizards.pop(actor_id, None)
        await event.message.answer(
            texts.OP_BROADCAST_WIZARD_EXPIRED,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return True

    if text_body.strip() == "/cancel":
        _wizards.pop(actor_id, None)
        await event.message.answer(
            texts.OP_BROADCAST_CANCELLED_BY_USER,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return True

    text = text_body.strip()
    if len(text) > cfg.broadcast_max_chars:
        await event.message.answer(
            texts.OP_BROADCAST_TOO_LONG.format(
                limit=cfg.broadcast_max_chars, actual=len(text)
            ),
            attachments=[keyboards.broadcast_cancel_keyboard()],
        )
        return True
    # SECURITY_REVIEW C2: URL-whitelist на текст рассылки. Симметрично
    # M3 для operator reply. Защищает от ошибки оператора (вставил
    # анонс с ссылкой на сторонний сайт по копи-пейсту) и от
    # компрометации аккаунта (фишинг-рассылка всем подписчикам). Если
    # есть хоть один URL не из гос-whitelist — отказываем, перечисляем
    # плохие ссылки оператору в чате.
    bad_urls = settings_store.find_non_whitelisted_urls(text)
    if bad_urls:
        await event.message.answer(
            "❌ В тексте рассылки найдены ссылки на сторонние сайты: "
            f"{', '.join(bad_urls[:3])}"
            f"{'…' if len(bad_urls) > 3 else ''}.\n\n"
            "Разрешены только официальные ресурсы: elizovomr.ru, "
            "kamgov.ru, gosuslugi.ru, kamchatka.gov.ru.\n\n"
            "Уберите ссылку или замените на гос-домен и пришлите текст "
            "заново.",
            attachments=[keyboards.broadcast_cancel_keyboard()],
        )
        log.warning(
            "broadcast: blocked non-whitelisted URLs in wizard text — "
            "operator=%s count=%d", actor_id, len(bad_urls),
        )
        return True
    if not text:
        # Пусто. Просим ввести ещё раз, состояние не меняем. Для re-
        # prompt'a показываем DEFAULTS-значение max_images: открывать
        # session ради этого редкого пути с пустой строкой избыточно
        # (это early-return). Реальный flow ниже читает актуальное
        # значение из БД.
        await event.message.answer(
            texts.OP_BROADCAST_PROMPT.format(
                limit=cfg.broadcast_max_chars,
                max_images=int(
                    settings_store.DEFAULTS["broadcast_max_images"]
                ),
            ),
            attachments=[keyboards.broadcast_cancel_keyboard()],
        )
        return True

    # Текущий лимит картинок — из настроек БД (settings_store), а не
    # из env. IT-оператор может поменять оперативно через UI
    # «⚙️ Настройки бота» → «broadcast_max_images» без редеплоя.
    async with session_scope() as session:
        max_images = await _resolve_broadcast_max_images(session)
        count = await broadcasts_service.count_subscribers(session)

    if count == 0:
        _wizards.pop(actor_id, None)
        await event.message.answer(
            texts.OP_BROADCAST_NO_SUBSCRIBERS,
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return True

    state.text = text
    # Захват картинок оператора (если в том же сообщении были). Лимит —
    # из settings_store, по умолчанию 5: афиша, схема, фото-комплект
    # из 2-3 кадров укладываются, multi-image-спам отрезается.
    # ВАЖНО: считаем сколько ВСЕГО было — для warning'а оператору, если
    # обрезалось. Тихая обрезка (приложили 7, разошлось 5) ломает UX.
    all_images_in_event = _image_attachments.image_attachments_from_event(
        event, limit=0  # 0 = unlimited, чтобы подсчитать «приложено»
    )
    provided = len(all_images_in_event)
    state.attachments = all_images_in_event[:max_images]
    state.step = "awaiting_confirm"
    state.renew()
    # Превью включает все приложенные картинки рядом с confirm-клавой,
    # чтобы оператор видел, что именно увидит подписчик. До этой правки
    # preview был text-only, оператор «вслепую» подтверждал.
    preview_outbound_images = _image_attachments.build_outbound_image_attachments(
        state.attachments
    )
    image_warning = ""
    if provided > max_images:
        image_warning = texts.OP_BROADCAST_PREVIEW_TRIM_WARN.format(
            provided=provided, limit=max_images
        )
    await event.message.answer(
        texts.OP_BROADCAST_PREVIEW.format(
            text=text,
            count=count,
            image_count=len(state.attachments),
            image_warning=image_warning,
        ),
        attachments=[
            *preview_outbound_images,
            keyboards.broadcast_confirm_keyboard(),
        ],
    )
    return True


async def _handle_confirm(event) -> None:
    actor_id = get_user_id(event)
    if actor_id is None:
        return
    state = _wizards.pop(actor_id, None)
    if state is None or state.step != "awaiting_confirm" or state.expired():
        await ack_callback(event, "Мастер закрыт.")
        return
    await ack_callback(event)
    op = await _get_operator(event)
    if op is None:
        return

    async with session_scope() as session:
        count = await broadcasts_service.count_subscribers(session)
        if count == 0:
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_BROADCAST_NO_SUBSCRIBERS,
                attachments=[keyboards.op_back_to_menu_keyboard()],
            )
            return
        broadcast = await broadcasts_service.create_broadcast(
            session,
            text=state.text,
            operator_id=op.id,
            subscriber_count=count,
            attachments=list(state.attachments),
        )
        await operators_service.write_audit(
            session,
            operator_max_user_id=actor_id,
            action="broadcast_send",
            target=f"broadcast #{broadcast.id}",
            # Не дублируем полный текст в audit_log: он уже хранится в broadcasts.text.
            # Оставляем только метаданные, чтобы audit_log оставался лёгким и не
            # превращался во второе хранилище тел рассылок.
            details={"chars": len(state.text), "subscriber_count": count},
        )
        broadcast_id = broadcast.id

    log.info(
        "broadcast: confirmed by operator=%s — broadcast_id=%s subscribers=%d",
        actor_id, broadcast_id, count,
    )

    # SECURITY_REVIEW C2: cooldown между confirm и реальной отправкой.
    # Стандарт — 5 минут. Для рассылок с маркером [ЧС] (ситуация
    # требует немедленного оповещения) — 30 секунд: оператор всё ещё
    # может отменить, но мы не задерживаем ЧС-сигнал.
    cooldown_sec = _broadcast_cooldown_seconds(state.text)
    minutes = cooldown_sec // 60
    seconds = cooldown_sec % 60
    eta_text = (
        f"{minutes} мин {seconds} сек" if minutes else f"{seconds} сек"
    )
    is_emergency = cooldown_sec == _COOLDOWN_EMERGENCY_SEC
    cooldown_label = "🚨 ЧС-рассылка" if is_emergency else "📤 Рассылка"
    sent = await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            f"{cooldown_label} #{broadcast_id} уйдёт жителям через "
            f"{eta_text} ({count} получателей).\n\n"
            f"Если заметили ошибку или передумали — нажмите «❌ Отменить "
            f"отправку». После окна cooldown'а рассылка стартует "
            f"автоматически и появится клавиша экстренной остановки."
        ),
        attachments=[keyboards.broadcast_cooldown_keyboard(broadcast_id)],
    )
    admin_mid = extract_message_id(sent) or get_callback_message_id(event)

    # Strong ref: без spawn_background_task GC может прервать рассылку
    # посреди списка получателей (Python 3.11+ держит только weakref на
    # таску из голого create_task). Конкретно для рассылки это значило
    # бы потерянные доставки и broadcast в статусе SENDING без
    # завершения.
    cooldown_task = spawn_background_task(
        _run_with_cooldown(
            event.bot,
            broadcast_id,
            state.text,
            count,
            admin_mid=admin_mid,
            cooldown_sec=cooldown_sec,
        ),
        name=f"broadcast_cooldown_{broadcast_id}",
    )
    _pending_broadcasts[broadcast_id] = cooldown_task


async def _run_with_cooldown(
    bot, broadcast_id: int, text: str, count: int,
    *, admin_mid: int | None, cooldown_sec: int,
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
    """SECURITY_REVIEW C2: отмена рассылки во время cooldown'а.

    Сценарий: оператор нажал «отправить», увидел сообщение «уйдёт через
    5 минут», осознал ошибку (опечатка / не тот текст / передумал) и
    жмёт «❌ Отменить отправку». Мы отменяем pending-task (рассылка
    не уходит) и переводим broadcast в статус CANCELLED.
    """
    await ack_callback(event, "Отменяю…")
    task = _pending_broadcasts.pop(broadcast_id, None)
    if task is None:
        # Cooldown уже истёк — рассылка пошла, отмена не возможна
        # (только экстренная остановка по «⛔ Экстренно остановить»).
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=(
                f"⚠️ Рассылка #{broadcast_id} уже стартовала или была "
                f"отменена ранее. Для остановки уже идущей рассылки — "
                f"кнопка «⛔ Экстренно остановить»."
            ),
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    task.cancel()
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


async def _handle_abort(event) -> None:
    actor_id = get_user_id(event)
    if actor_id is not None:
        _wizards.pop(actor_id, None)
    await ack_callback(event, "Отменено.")
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_BROADCAST_CANCELLED_BY_USER,
        attachments=[keyboards.op_back_to_menu_keyboard()],
    )


def prefill_wizard_from_template(
    actor_id: int, *, text: str, attachments: list
) -> None:
    """Зарядить state мастера рассылок данными из шаблона (PR H).

    Используется handlers/broadcast_templates.py при «📨 Отправить как
    рассылку»: создаёт state со step=awaiting_confirm, чтобы оператор
    увидел preview и нажал «Разослать» либо «Изменить текст» — точно
    тот же UX, что после набора текста с нуля. Прежний state (если был
    в любом шаге) затирается полностью.
    """
    state = _WizardState(
        step="awaiting_confirm",
        text=text,
        attachments=list(attachments),
    )
    _wizards[actor_id] = state


async def _handle_edit(event) -> None:
    """Кнопка «✏️ Изменить текст» в превью. Возвращает мастер в шаг
    ожидания текста, обнуляя предыдущий текст И ранее приложенные
    картинки: следующее сообщение оператора полностью пересоберёт
    черновик. Без явного обнуления `state.attachments` старые картинки
    тихо сохранялись бы между попытками — UX-ловушка («исправил текст,
    а тут ещё и старые картинки всплывают»). Текст подсказки
    OP_BROADCAST_EDIT_HINT явно предупреждает оператора, чтобы он
    приложил картинки заново."""
    actor_id = get_user_id(event)
    if actor_id is None:
        await ack_callback(event)
        return
    state = _wizards.get(actor_id)
    if state is None:
        await ack_callback(event, "Мастер закрыт.")
        return
    state.step = "awaiting_text"
    state.text = ""
    # Чистим картинки тоже: «Изменить текст» = новый черновик целиком.
    state.attachments = []
    state.renew()
    await ack_callback(event)
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=texts.OP_BROADCAST_EDIT_HINT.format(
            limit=cfg.broadcast_max_chars,
        ),
        attachments=[keyboards.broadcast_cancel_keyboard()],
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
    await ack_callback(
        event, "Остановлено." if flipped else "Уже завершено."
    )


def _format_progress(
    *, broadcast_id: int, total: int, delivered: int, failed: int
) -> str:
    failed_suffix = (
        texts.OP_BROADCAST_FAILED_SUFFIX.format(failed=failed) if failed else ""
    )
    return texts.OP_BROADCAST_PROGRESS.format(
        number=broadcast_id,
        total=total,
        delivered=delivered,
        failed_suffix=failed_suffix,
    )


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
    """
    try:
        await bot.send_message(
            user_id=max_user_id,
            text=body_text,
            attachments=[
                *outbound_images,
                keyboards.broadcast_unsubscribe_keyboard(),
            ],
        )
    except Exception as e:
        # Обрезаем, чтобы поле с ошибкой не разрасталось. Полный стек живёт в логах.
        return repr(e)[:500]
    return None


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


def _compute_progress_step(total: int, rate_delay: float) -> float:
    """Адаптивный шаг обновления прогресс-карточки (в секундах).

    BROADCAST_PROGRESS_UPDATE_SEC (5 сек по умолчанию) рассчитан на
    рассылку 50–200 получателей: оператор видит около 10 обновлений.
    На совсем короткой рассылке (5 получателей × 1 сек) полоска
    обновилась бы один раз в самом конце; на очень длинной (1000
    получателей) MAX начнёт ограничивать частоту правок. Для коротких
    отправок ужимаем шаг, чтобы прогресс двигался заметно.
    """
    estimated_total_sec = max(1.0, total * rate_delay)
    return min(cfg.broadcast_progress_update_sec, estimated_total_sec / 10)


def _build_final_text(
    *, broadcast_id: int, total: int, delivered: int, failed: int, cancelled: bool
) -> str:
    """Итоговый текст рассылки для админ-карточки (отмена / готово)."""
    if cancelled:
        return texts.OP_BROADCAST_CANCELLED.format(
            number=broadcast_id, delivered=delivered, total=total
        )
    failed_line = (
        texts.OP_BROADCAST_FAILED_LINE.format(failed=failed) if failed else ""
    )
    return texts.OP_BROADCAST_DONE.format(
        number=broadcast_id,
        delivered=delivered,
        total=total,
        failed_line=failed_line,
    )


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
    if dt is None:
        return "—"
    return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")


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
