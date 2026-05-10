"""FSM-воронка приёма обращения и followup.

Выделено из handlers/appeal.py (рефакторинг 2026-05-10).

Содержит:
- `_start_appeal_flow` — точка входа в воронку из callback `menu:new_appeal`
- `_ask_*` — функции запроса каждого шага (контакт, имя, локалити, адрес,
  тема, суть)
- `on_awaiting_*` — handlers state-таблицы, вызываются когда житель
  прислал что-то нерелевантное на конкретном шаге
- `on_awaiting_followup_text` — обработка дополнения к существующему
  обращению через явную кнопку «📎 Дополнить»

Зависимости:
- appeal_runtime — для finalize, get/drop_user_lock, _HAS_ALNUM
- appeal_geo — для on_awaiting_locality (импортируется лениво в
  state-таблице appeal.py)
"""
from __future__ import annotations

import logging
import re

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.handlers.appeal_runtime import (
    _HAS_ALNUM,
    persist_and_dispatch_appeal,
)
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.attachments import (
    collect_attachments,
    extract_contact_name,
    extract_phone,
)
from aemr_bot.utils.event import (
    get_chat_id,
    get_first_name,
)

log = logging.getLogger(__name__)


# ---- Точка входа ----------------------------------------------------------


async def start_appeal_flow(event, max_user_id: int):
    """Точка входа в воронку при тапе «📝 Написать обращение».

    Проверяет: блокировка, rate-limit, согласие на ПДн. При нужде шлёт
    запрос согласия + клавиатуру; иначе — переход к следующему шагу
    (контакт/имя/адрес).
    """
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if user.is_blocked:
            pass  # обработка ниже
        else:
            recent = await appeals_service.count_recent_for_user(
                session, user.id, hours=1
            )
            if recent >= 3:
                await event.bot.send_message(
                    chat_id=get_chat_id(event),
                    text=(
                        "Вы создали несколько обращений за последний час. "
                        "Чтобы не дублировать, дополните уже открытое — "
                        "просто отправьте сообщение в этот чат, оно "
                        "пришьётся к последнему обращению."
                    ),
                    attachments=[keyboards.back_to_menu_keyboard()],
                )
                return
        if user.is_blocked:
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=(
                    "Сейчас вы не можете подать обращение: ваш аккаунт "
                    "помечен как заблокированный. Если это ошибка — "
                    "обратитесь к оператору."
                ),
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        if not user.consent_pdn_at:
            await users_service.set_state(
                session, max_user_id, DialogState.AWAITING_CONSENT, data={}
            )
            policy_url = await settings_store.get(session, "policy_url")
            policy_token = await settings_store.get(session, "policy_pdf_token")
        else:
            policy_url = None
            policy_token = None

    # Если у жителя НЕТ согласия и НЕТ ни URL, ни PDF — это
    # конфигурационный сбой (settings_store не сидирован). Не пропускаем
    # дальше: иначе житель попадёт на запрос телефона, минуя шаг
    # согласия, что нарушит 152-ФЗ.
    if (
        await _has_consent_step_pending(max_user_id)
        and policy_url is None
        and policy_token is None
    ):
        log.error(
            "policy_url и policy_pdf_token оба пусты — воронка остановлена "
            "для max_user_id=%s. Сидируйте settings_store.",
            max_user_id,
        )
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=(
                "Сервис временно недоступен — не настроен текст политики "
                "обработки данных. Сообщили координатору; попробуйте позже."
            ),
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return

    if policy_url is not None or policy_token is not None:
        attachments: list = [keyboards.consent_keyboard()]
        if policy_token:
            from aemr_bot.services.policy import build_file_attachment
            attachments.insert(0, build_file_attachment(policy_token))
            text = (
                "Перед оформлением обращения нужно ваше согласие на "
                "обработку персональных данных. Полный текст политики — "
                "в прикреплённом PDF.\n\nНажмите «Согласен», чтобы продолжить."
            )
        else:
            text = texts.CONSENT_REQUEST.format(policy_url=policy_url)
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=text,
            attachments=attachments,
        )
        return

    await ask_contact_or_skip(event, max_user_id)


async def _has_consent_step_pending(max_user_id: int) -> bool:
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        return user.consent_pdn_at is None


# ---- _ask_* — переходы на следующий шаг -----------------------------------


async def ask_contact_or_skip(event, max_user_id: int):
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if not user.phone:
            target_state = DialogState.AWAITING_CONTACT
        elif not user.first_name or user.first_name == "Удалено":
            target_state = DialogState.AWAITING_NAME
        else:
            target_state = DialogState.AWAITING_LOCALITY
        await users_service.set_state(session, max_user_id, target_state, data={})

    if target_state == DialogState.AWAITING_LOCALITY:
        # Перед обычной клавиатурой со списком поселений пробуем
        # предложить «использовать тот же адрес» — экономит два шага
        # для жителей, которые подают повторное обращение по тому же
        # объекту. Если прошлого адреса нет — обычный путь.
        if await ask_address_or_reuse(event, max_user_id):
            return
        await ask_locality(event, max_user_id)
        return

    prompt_for = {
        DialogState.AWAITING_CONTACT: (
            texts.CONTACT_REQUEST,
            keyboards.contact_request_keyboard(),
        ),
        DialogState.AWAITING_NAME: (
            texts.CONTACT_RECEIVED,
            keyboards.cancel_keyboard(),
        ),
    }
    text, keyboard = prompt_for[target_state]
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=text,
        attachments=[keyboard],
    )


async def ask_address_or_reuse(event, max_user_id: int) -> bool:
    """Предложить жителю «использовать тот же адрес» если он уже подавал
    обращение. Возвращает True, если показали reuse-prompt — тогда
    воронка ждёт callback addr:reuse / addr:new и не идёт в ask_locality.
    False означает «прошлого адреса нет, спрашивайте обычным путём».
    """
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        last = await appeals_service.find_last_address_for_user(session, user.id)
    if last is None:
        return False
    locality, address = last
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=(
            f"В прошлый раз вы писали по этому адресу:\n"
            f"📍 {locality}, {address}\n\n"
            f"Использовать его снова или указать новый?"
        ),
        attachments=[keyboards.reuse_address_keyboard()],
    )
    return True


async def _show_progress_step(
    event,
    max_user_id: int,
    *,
    stage: str,
    next_state: DialogState,
    keyboard,
) -> None:
    """Универсальный helper для шагов воронки: рендерит прогресс-карту,
    обновляет существующее сообщение через edit_message либо шлёт новое
    (см. services/progress.send_or_edit_progress), сохраняет mid в
    dialog_data['progress_message_id'].

    После рефакторинга 2026-05-10 заменяет 5 отдельных echo-сообщений
    одним постоянно-обновляемым. См. services/progress.py для деталей.
    """
    from aemr_bot.services.progress import render_progress, send_or_edit_progress

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        data = dict(user.dialog_data or {})
        await users_service.set_state(session, max_user_id, next_state)

    # Завершённые шаги собираем из dialog_data + user.first_name.
    name = (user.first_name or "").strip() or None
    text = render_progress(
        stage=stage,  # type: ignore[arg-type]
        name=name,
        locality=data.get("locality") or None,
        address=data.get("address") or None,
        topic=data.get("topic") or None,
    )

    new_mid, edited = await send_or_edit_progress(
        event.bot,
        chat_id=get_chat_id(event),
        dialog_data=data,
        text=text,
        attachments=[keyboard],
    )

    # Сохранить mid если новое сообщение (edit использовал прежний mid —
    # ничего сохранять не нужно).
    if not edited and new_mid:
        async with session_scope() as session:
            await users_service.update_dialog_data(
                session, max_user_id, {"progress_message_id": new_mid}
            )


async def ask_locality(event, max_user_id: int):
    """Шаг «Населённый пункт». Прогресс-карта с галочкой имени."""
    async with session_scope() as session:
        localities = await settings_store.get(session, "localities") or [
            "Елизовское ГП"
        ]
    await _show_progress_step(
        event,
        max_user_id,
        stage="locality",
        next_state=DialogState.AWAITING_LOCALITY,
        keyboard=keyboards.localities_keyboard(localities),
    )


async def ask_address(event, max_user_id: int):
    """Шаг «Адрес». Прогресс-карта с галочкой локалити."""
    await _show_progress_step(
        event,
        max_user_id,
        stage="address",
        next_state=DialogState.AWAITING_ADDRESS,
        keyboard=keyboards.cancel_keyboard(),
    )


async def ask_topic(event, max_user_id: int):
    """Шаг «Тема». Прогресс-карта с галочкой адреса."""
    async with session_scope() as session:
        topics = await settings_store.get(session, "topics") or ["Другое"]
    await _show_progress_step(
        event,
        max_user_id,
        stage="topic",
        next_state=DialogState.AWAITING_TOPIC,
        keyboard=keyboards.topics_keyboard(topics),
    )


async def ask_summary(event, max_user_id: int):
    """Шаг «Описание сути». Прогресс-карта с галочкой темы.

    На этом шаге показываем cancel-клавиатуру и ждём следующее
    непустое сообщение (текст / фото / видео / файл) — финализация
    в on_awaiting_summary.
    """
    await _show_progress_step(
        event,
        max_user_id,
        stage="summary",
        next_state=DialogState.AWAITING_SUMMARY,
        keyboard=keyboards.cancel_keyboard(),
    )


async def finalize_appeal(event, max_user_id: int):
    """Финализация. Вызывается сразу после первого непустого сообщения
    жителя в шаге AWAITING_SUMMARY — без таймера и без отдельной
    кнопки «Отправить». На пустой ввод отвечаем подсказкой."""
    persisted = await persist_and_dispatch_appeal(event.bot, max_user_id)
    if persisted is False:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.APPEAL_EMPTY_REJECTED,
            attachments=[keyboards.cancel_keyboard()],
        )


# ---- on_awaiting_* — state-handlers ---------------------------------------


async def on_awaiting_contact(event, body, text_body, max_user_id):
    # Сначала пробуем достать телефон из contact-вложения. Если его
    # нет (старые клиенты MAX, либо житель напечатал номер текстом) —
    # берём цифры из текстового тела как запасной путь.
    phone = extract_phone(body)
    if phone is None and text_body:
        digits_match = re.search(r"\+?\d[\d\s\-()]{9,}\d", text_body)
        if digits_match:
            phone = digits_match.group(0)
    if phone is None:
        await event.message.answer(
            texts.CONTACT_RETRY,
            attachments=[keyboards.contact_request_keyboard()],
        )
        return

    contact_name = extract_contact_name(body)

    async with session_scope() as session:
        await users_service.set_phone(session, max_user_id, phone)
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if contact_name and (not user.first_name or user.first_name == "Удалено"):
            cleaned = contact_name.strip()[: cfg.name_max_chars]
            if cleaned and _HAS_ALNUM.search(cleaned):
                await users_service.set_first_name(session, max_user_id, cleaned)
                user.first_name = cleaned

    if not user.first_name or user.first_name == "Удалено":
        async with session_scope() as session:
            await users_service.set_state(
                session, max_user_id, DialogState.AWAITING_NAME
            )
        await event.message.answer(
            texts.CONTACT_RECEIVED, attachments=[keyboards.cancel_keyboard()]
        )
    else:
        await ask_contact_or_skip(event, max_user_id)


async def on_awaiting_name(event, body, text_body, max_user_id):
    name = text_body.strip()[: cfg.name_max_chars]
    if not name or not _HAS_ALNUM.search(name):
        # Пустая строка / только пробелы / только эмодзи / только
        # пунктуация. Пытаемся подтянуть имя из профиля MAX.
        name = get_first_name(event)
        if not name or name == "Удалено":
            await event.message.answer(texts.NAME_EMPTY)
            return
        name = name[: cfg.name_max_chars]

    async with session_scope() as session:
        await users_service.set_first_name(session, max_user_id, name)
    if await ask_address_or_reuse(event, max_user_id):
        return
    await ask_locality(event, max_user_id)


async def on_awaiting_address(event, body, text_body, max_user_id):
    address = text_body.strip()[: cfg.address_max_chars]
    if not address or not _HAS_ALNUM.search(address):
        await event.message.answer(texts.ADDRESS_EMPTY)
        return
    async with session_scope() as session:
        await users_service.update_dialog_data(
            session, max_user_id, {"address": address}
        )
    await ask_topic(event, max_user_id)


async def on_awaiting_summary(event, body, text_body, max_user_id):
    """Один шаг сути: первое же непустое сообщение или вложение —
    это и есть обращение."""
    chunk = text_body.strip()
    atts = collect_attachments(body)
    if not chunk and not atts:
        await event.message.answer(
            texts.APPEAL_EMPTY_REJECTED,
            attachments=[keyboards.cancel_keyboard()],
        )
        return

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        # dict() — shallow copy. Nested list (summary_chunks, attachments)
        # должен быть отдельной копией, иначе append мутирует list,
        # лежащий в SQLAlchemy-tracked user.dialog_data ДО flush.
        data = dict(user.dialog_data or {})
        data["summary_chunks"] = list(data.get("summary_chunks") or [])
        data["attachments"] = list(data.get("attachments") or [])

        if chunk:
            data["summary_chunks"].append(chunk[: cfg.summary_max_chars])

        if atts:
            data["attachments"].extend(atts[: cfg.attachments_max_per_appeal])

        user.dialog_data = data
        await session.flush()

    await finalize_appeal(event, max_user_id)


async def on_awaiting_topic(event, body, text_body, max_user_id):
    """Житель пишет текст вместо тапа по кнопке тематики."""
    async with session_scope() as session:
        topics = await settings_store.get(session, "topics") or []
    if not topics:
        async with session_scope() as session:
            await users_service.reset_state(session, max_user_id)
        await event.message.answer(
            "Список тем сейчас пуст — сообщили координатору. Попробуйте позже.",
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return
    await event.message.answer(
        "Выберите тематику кнопкой ниже:",
        attachments=[keyboards.topics_keyboard(topics)],
    )


async def on_awaiting_consent(event, body, text_body, max_user_id):
    """Житель пишет текст вместо тапа кнопок «Согласен/Отказаться»."""
    async with session_scope() as session:
        policy_url = await settings_store.get(session, "policy_url")
    if policy_url:
        text = texts.CONSENT_REQUEST.format(policy_url=policy_url)
    else:
        text = (
            "Чтобы принять обращение, нам нужно ваше согласие на "
            "обработку персональных данных. Нажмите «Согласен», "
            "чтобы продолжить."
        )
    await event.message.answer(text, attachments=[keyboards.consent_keyboard()])


async def on_idle(event, body, text_body, max_user_id):
    """IDLE — нет активной воронки. Раньше был «магический followup»;
    теперь дополнение работает только через явную кнопку «📎 Дополнить»."""
    from aemr_bot.handlers.menu import open_main_menu

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        active = await appeals_service.find_active_for_user(session, user.id)

    if active is not None:
        await event.message.answer(
            "Не понял сообщение. Если хотите дополнить уже поданное "
            "обращение — откройте «📂 Мои обращения» и нажмите "
            "«📎 Дополнить» в карточке нужного обращения.",
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return
    await event.message.answer(texts.UNKNOWN_INPUT)
    await open_main_menu(event)


async def on_awaiting_followup_text(event, body, text_body, max_user_id):
    """Житель нажал «📎 Дополнить» в карточке обращения. Принимаем текст
    и/или вложения — пришиваем к обращению из dialog_data, отправляем в
    админ-чат как «📩 Дополнение к обращению #N», подтверждаем жителю
    и возвращаем в меню.

    Если обращение было ANSWERED — переоткрываем (житель пришёл с
    уточнением после ответа).
    """
    from aemr_bot.config import settings as cfg
    from aemr_bot.db.models import AppealStatus
    from aemr_bot.handlers.menu import open_main_menu
    from aemr_bot.services import card_format
    from aemr_bot.services.admin_relay import relay_attachments_to_admin

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        appeal_id = (user.dialog_data or {}).get("appeal_id")
        appeal = (
            await appeals_service.get_by_id(session, int(appeal_id))
            if appeal_id
            else None
        )

    if appeal is None or appeal.user_id != user.id:
        async with session_scope() as session:
            await users_service.reset_state(session, max_user_id)
        await event.message.answer(
            "Обращение, которое вы хотели дополнить, недоступно. "
            "Откройте «📂 Мои обращения» — выберите актуальное.",
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return

    # Если согласие отозвано между нажатием «📎 Дополнить» и присылкой
    # текста — followup в админ-чат не идёт. 152-ФЗ ст. 21 ч. 5: после
    # отзыва обработка прекращается, в том числе входящих сообщений.
    if user.consent_pdn_at is None:
        async with session_scope() as session:
            await users_service.reset_state(session, max_user_id)
        await event.message.answer(
            "Согласие на обработку отозвано — дополнение не отправлено. "
            "Чтобы продолжить, откройте /start и дайте согласие заново.",
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return

    # Если оператор закрыл обращение между нажатием и присылкой —
    # не «оживляем» его followup'ом.
    if appeal.status == AppealStatus.CLOSED.value:
        async with session_scope() as session:
            await users_service.reset_state(session, max_user_id)
        await event.message.answer(
            "Обращение уже закрыто. Если ситуация повторилась — "
            "откройте его в «📂 Мои обращения» и нажмите «🔁 Подать похожее».",
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return

    text = (text_body or "").strip()
    attachments = collect_attachments(body)
    if not text and not attachments:
        await event.message.answer(
            "Опишите дополнение к обращению одним сообщением или "
            "приложите фото, видео или файл.",
            attachments=[keyboards.cancel_keyboard()],
        )
        return

    async with session_scope() as session:
        if appeal.status == AppealStatus.ANSWERED.value:
            await appeals_service.reopen(session, appeal.id)
        await appeals_service.add_user_message(
            session,
            appeal=appeal,
            text=text or None,
            attachments=attachments,
        )
        await users_service.reset_state(session, max_user_id)
        followup = card_format.admin_followup(appeal, user, text or "(без текста)")

    if cfg.admin_group_id:
        await event.bot.send_message(chat_id=cfg.admin_group_id, text=followup)
        if attachments:
            try:
                await relay_attachments_to_admin(
                    event.bot,
                    appeal_id=appeal.id,
                    admin_mid=None,
                    stored_attachments=attachments,
                )
            except Exception:
                log.exception("relay followup attachments failed")

    await event.message.answer(
        f"✅ Дополнение отправлено оператору по обращению #{appeal.id}."
    )
    await open_main_menu(event)
