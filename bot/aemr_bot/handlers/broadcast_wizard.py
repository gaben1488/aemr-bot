"""Мастер рассылок (FSM): state, helpers, command handlers.

Сценарий оператора в админ-чате:

  1. /broadcast               → бот просит ввести текст.
  2. оператор вводит текст    → бот показывает предпросмотр с числом подписчиков.
  3. оператор жмёт ✅          → бот переводит в `_pending_broadcasts`
                                 на cooldown, по истечении — фоновая отправка.

Состояние мастера (шаги 1–3) живёт только в памяти процесса.
Операторов нет в таблице `users`, а недозаполненный мастер дёшево
пройти заново. Состояние вытесняется автоматически по истечении
BROADCAST_WIZARD_TTL_SEC.

ИСТОРИЯ. Cluster C wave 2 (Codex PR 7, 2026-05-28): вынесено из
`handlers/broadcast.py`. Send pipeline (`_send_one`, `_run_broadcast`,
история, `register(dp)`) остаются там же. Wizard FSM — здесь.
Compat re-export wizard-символов сохранён в broadcast.py для всех
12 файлов тестов + 4 production callsites.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Literal

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator, ensure_role, get_operator
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import operators as operators_service
from aemr_bot.services import settings_store
from aemr_bot.services.broadcast_utils import (
    _COOLDOWN_EMERGENCY_SEC,
    _broadcast_cooldown_seconds,
)
from aemr_bot.utils import image_attachments as _image_attachments
from aemr_bot.utils.background import spawn_background_task
from aemr_bot.utils.event import (
    ack_callback,
    extract_message_id,
    get_callback_message_id,
    get_user_id,
    send_or_edit_screen,
)

log = logging.getLogger(__name__)


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


# Локальные псевдонимы общих хелперов авторизации. Подчёркивание в начале имени
# подчёркивает, что это служебные средства для админ-стороны, не для жителя.
# Дублируем алиасы из broadcast.py, чтобы тесты могли патчить через
# `patch.object(broadcast_wizard, "_ensure_role", ...)` без обращения к
# исходному модулю.
_get_operator = get_operator
_ensure_role = ensure_role
_ensure_operator = ensure_operator


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
    # SECURITY (audit 2026-05-28): повторная проверка роли на confirm.
    # Между _start_wizard (требует IT/COORDINATOR) и нажатием
    # «Разослать» оператора могли понизить в роли. get_operator
    # фильтрует только is_active, не роль — поэтому пере-проверяем
    # явно, чтобы понижённый оператор не выпустил рассылку. Состояние
    # мастера уже снято (pop выше) — незаконный черновик отбрасываем.
    # _ensure_role сам делает ack и шлёт текст-отказ.
    if not await _ensure_role(event, OperatorRole.IT, OperatorRole.COORDINATOR):
        return
    # SECURITY_REVIEW P1-2: URL-whitelist gate на CONFIRM — последний
    # рубеж перед create_broadcast. _handle_wizard_text форсит whitelist
    # только на free-text пути; пути «применить шаблон» (_apply →
    # prefill_wizard_from_template) и любой будущий prefill минуют его,
    # ставя state сразу в awaiting_confirm. Поэтому проверяем здесь, в
    # единой точке отправки: даже если фишинг-URL попал в шаблон до
    # write-time валидации (legacy-шаблон, прямой psql), он не уйдёт
    # подписчикам. Fail-closed: state уже снят pop'ом выше — незаконный
    # черновик отброшен; перечисляем плохие ссылки оператору и возвращаем
    # его в меню.
    bad_urls = settings_store.find_non_whitelisted_urls(state.text)
    if bad_urls:
        log.warning(
            "broadcast: blocked non-whitelisted URLs at confirm — "
            "operator=%s count=%d", actor_id, len(bad_urls),
        )
        await ack_callback(event, "Рассылка отклонена.")
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=(
                "❌ Рассылка отклонена: в тексте найдены ссылки на "
                "сторонние сайты: "
                f"{', '.join(bad_urls[:3])}"
                f"{'…' if len(bad_urls) > 3 else ''}.\n\n"
                "Разрешены только официальные ресурсы: elizovomr.ru, "
                "kamgov.ru, gosuslugi.ru, kamchatka.gov.ru.\n\n"
                "Уберите ссылку или замените на гос-домен и начните "
                "рассылку заново."
            ),
            attachments=[keyboards.op_back_to_menu_keyboard()],
        )
        return
    # ack с фидбеком: оператор сразу видит «принято», broadcast wizard
    # переходит в подготовку. Без notification ack — тихий, оператор
    # тапает «Отправить» и думает, ушла ли команда.
    await ack_callback(event, "Готовлю рассылку…")
    # Индикатор набора: подсчёт подписчиков, создание рассылки и
    # запуск планировщика могут занять секунды на большой базе. Без
    # индикатора кажется, что бот завис.
    from aemr_bot.utils.typing_indicator import mark_typing
    await mark_typing(event, cfg.admin_group_id)

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
    #
    # Lazy import: send pipeline (_run_with_cooldown) живёт в
    # handlers/broadcast вместе с _pending_broadcasts. Top-level import
    # сюда вызвал бы циркулярную зависимость с compat-фасадом.
    from aemr_bot.handlers.broadcast import (
        _pending_broadcasts,
        _run_with_cooldown,
    )
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
