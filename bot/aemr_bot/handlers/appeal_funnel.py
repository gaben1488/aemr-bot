"""FSM-воронка приёма обращения и явного дополнения.

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
import secrets
from datetime import UTC, datetime, timedelta

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import AppealStatus, DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import resident_name_gate
from aemr_bot.handlers._common import current_user
from aemr_bot.handlers.appeal_runtime import (
    _HAS_ALNUM,
    PERSIST_BLOCKED,
    PERSIST_NO_CONSENT,
    PERSIST_RATE_LIMITED,
    persist_and_dispatch_appeal,
)
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.attachments import (
    collect_attachments,
    extract_resident_contact,
    has_unsupported_appeal_attachments,
)
from aemr_bot.utils.event import (
    extract_message_id,
    get_callback_message_id,
    get_chat_id,
    get_user_id,
    send_or_edit_screen,
)
from aemr_bot.utils.resident_names import normalize_resident_name

# Регекс на горячем пути: вызывается на каждом контактном шаге жителя
# (on_awaiting_contact). Компиляция модуля — однократно, чтобы не
# тратить CPU на пересборку регекса при каждом сообщении.
# Module-level compile вместо `re.search(...)` внутри функции.
_PHONE_DIGITS_RE = re.compile(r"\+?\d[\d\s\-()]{9,}\d")

log = logging.getLogger(__name__)


# ---- Точка входа ----------------------------------------------------------


async def start_appeal_flow(event, max_user_id: int):
    """Точка входа в воронку при тапе «📝 Написать обращение».

    Проверяет: блокировка, rate-limit, согласие на ПДн. При нужде шлёт
    запрос согласия + клавиатуру; иначе — переход к следующему шагу
    (контакт/имя/адрес).
    """
    from aemr_bot.services import resident_drafts
    if await resident_drafts.offer_saved(event, max_user_id):
        return
    async with current_user(max_user_id) as (session, user):
        if user.is_blocked:
            pass  # обработка ниже
        else:
            recent = await appeals_service.count_recent_for_user(
                session, user.id, hours=1
            )
            if recent >= 3:
                active = await appeals_service.find_active_for_user(session, user.id)
                await _send_rate_limit_message(
                    event,
                    has_open_unanswered=active is not None,
                )
                return
        if user.is_blocked:
            await send_or_edit_screen(
                event,
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
        await send_or_edit_screen(
            event,
            text=(
                "Сервис временно недоступен — не настроен текст политики "
                "обработки данных. Попробуйте позже или обратитесь в администрацию."
            ),
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return

    if policy_url is not None or policy_token is not None:
        attachments: list = [keyboards.consent_keyboard()]
        if policy_token:
            from aemr_bot.services.policy import build_file_attachment
            attachments.insert(0, build_file_attachment(policy_token))
        # Текст согласия ВСЕГДА берём из настроек (БД → fallback), а PDF
        # прикладываем отдельно, вложением. Раньше ветка с PDF подменяла
        # текст заглушкой «Полный текст политики — в прикреплённом PDF»,
        # и consent_text не читался вообще: токен политики на проде есть
        # всегда (Dockerfile кладёт PRIVACY.pdf в seed, main заливает его
        # на старте), поэтому жила только заглушка. Житель нажимал
        # «Согласен», не видя ни перечня данных, ни целей, ни срока — а
        # 152-ФЗ ст. 9 ч. 1 требует согласия конкретного, предметного и
        # информированного. Плюс та же норма требует, чтобы согласие было
        # оформлено ОТДЕЛЬНО от иных документов: приложенная политика —
        # это односторонний документ оператора (ст. 18.1), она не может
        # нести содержание согласия и не заменяет его.
        async with session_scope() as text_session:
            text, consent_hash = await settings_store.get_consent_request_and_hash(
                text_session,
                # policy_url в DEFAULTS есть всегда, но его могли явно
                # стереть при живом PDF. Тогда подставляем отсылку к
                # вложению — иначе в текст жителю уехало бы «None».
                policy_url=policy_url or "в прикреплённом файле",
                fallback=texts.CONSENT_REQUEST,
            )
        from aemr_bot.utils.text_pages import fits_max_message
        if not fits_max_message(text):
            log.error("consent text exceeds MAX transport limit; administrator action required")
            await send_or_edit_screen(
                event,
                text="Текст согласия временно недоступен. Попробуйте позже или обратитесь в администрацию.",
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        sent = await send_or_edit_screen(
            event,
            text=text,
            attachments=attachments,
            force_new_message=True,
        )
        async with session_scope() as session:
            await users_service.set_state(
                session, max_user_id, DialogState.AWAITING_CONSENT,
                data={"consent_prompt_hash": consent_hash, "consent_prompt_text": text,
                      "consent_prompt_mid": extract_message_id(sent)},
            )
        return

    await ask_contact_or_skip(event, max_user_id)


async def _has_consent_step_pending(max_user_id: int) -> bool:
    async with current_user(max_user_id) as (_session, user):
        return user.consent_pdn_at is None


def _format_reset_hint(reset_at: datetime | None) -> str:
    """«примерно через N мин» для сообщения о лимите.

    reset_at — момент, когда самое старое обращение выпадет из часового
    окна. Если он в прошлом или неизвестен — слот уже свободен, подсказку
    про ожидание не даём.
    """
    if reset_at is None:
        return ""
    wait = reset_at - datetime.now(UTC)
    minutes = int(wait.total_seconds() // 60) + 1
    if minutes <= 0:
        return ""
    if minutes == 1:
        return "Лимит освободится примерно через минуту."
    if minutes < 5:
        return f"Лимит освободится примерно через {minutes} минуты."
    return f"Лимит освободится примерно через {minutes} минут."


async def _send_rate_limit_message(
    event, *, has_open_unanswered: bool, reset_at: datetime | None = None
) -> None:
    reset_hint = _format_reset_hint(reset_at)
    if has_open_unanswered:
        text = (
            "Лимит новых обращений временно исчерпан: можно создать не больше "
            "3 обращений за последний час.\n\n"
            "У вас есть неотвеченное обращение. Если нужно уточнить детали "
            "по нему, откройте «📂 Мои обращения», выберите это обращение "
            "и нажмите «📎 Дополнить»."
        )
    else:
        tail = reset_hint or "Пожалуйста, попробуйте позже."
        text = (
            "Лимит новых обращений временно исчерпан: можно создать не больше "
            "3 обращений за последний час.\n\n"
            "Сейчас у вас нет неотвеченного обращения, которое можно "
            f"дополнить. {tail}"
        )
    await send_or_edit_screen(
        event,
        text=text,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


# ---- _ask_* — переходы на следующий шаг -----------------------------------


async def ask_contact_or_skip(
    event,
    max_user_id: int,
    *,
    force_new_message: bool = False,
):
    async with current_user(max_user_id) as (session, user):
        if not user.phone:
            target_state = DialogState.AWAITING_CONTACT
        elif resident_name_gate.needs_name(user.first_name):
            target_state = DialogState.AWAITING_NAME
        else:
            target_state = DialogState.AWAITING_LOCALITY
        await users_service.set_state(session, max_user_id, target_state, data={})

    if target_state == DialogState.AWAITING_LOCALITY:
        # Перед обычной клавиатурой со списком поселений пробуем
        # предложить «использовать тот же адрес» — экономит два шага
        # для жителей, которые подают повторное обращение по тому же
        # объекту. Если прошлого адреса нет — обычный путь.
        if await ask_address_or_reuse(
            event,
            max_user_id,
            force_new_message=force_new_message,
        ):
            return
        await ask_locality(event, max_user_id, force_new_message=force_new_message)
        return

    prompt_for = {
        DialogState.AWAITING_CONTACT: (
            texts.CONTACT_REQUEST,
            keyboards.contact_request_keyboard(),
        ),
        DialogState.AWAITING_NAME: (
            resident_name_gate.NAME_PROMPT,
            keyboards.cancel_keyboard(),
        ),
    }
    text, keyboard = prompt_for[target_state]
    await send_or_edit_screen(
        event,
        text=text,
        attachments=[keyboard],
        force_new_message=force_new_message,
    )


async def ask_address_or_reuse(
    event,
    max_user_id: int,
    *,
    force_new_message: bool = False,
) -> bool:
    """Предложить жителю «использовать тот же адрес» если он уже подавал
    обращение. Возвращает True, если показали reuse-prompt — тогда
    воронка ждёт callback addr:reuse / addr:new и не идёт в ask_locality.
    False означает «прошлого адреса нет, спрашивайте обычным путём».
    """
    async with current_user(max_user_id) as (session, user):
        last = await appeals_service.find_last_address_for_user(session, user.id)
    if last is None:
        return False
    locality, address = last
    await send_or_edit_screen(
        event,
        text=(
            f"В прошлый раз вы писали по этому адресу:\n"
            f"📍 {locality}, {address}\n\n"
            f"Использовать его снова или указать новый?"
        ),
        attachments=[keyboards.reuse_address_keyboard()],
        force_new_message=force_new_message,
    )
    return True


async def _show_progress_step(
    event,
    max_user_id: int,
    *,
    stage: str,
    next_state: DialogState,
    keyboard,
    force_new_message: bool = False,
) -> None:
    """Общий помощник для шагов воронки: рендерит прогресс-карту,
    обновляет существующее сообщение через edit_message либо шлёт новое
    (см. services/progress.send_or_edit_progress), сохраняет mid в
    dialog_data['progress_message_id'].

    `force_new_message=True` используем после видимого ввода жителя:
    текст, контакт, гео или файл должны остаться в ленте перед следующим
    шагом бота, а кнопочные переходы спокойно редактируют текущую карточку.
    """
    from aemr_bot.services.progress import render_progress, send_or_edit_progress

    async with current_user(max_user_id) as (session, user):
        data = dict(user.dialog_data or {})
        if not force_new_message and not data.get("progress_message_id"):
            callback_mid = get_callback_message_id(event)
            if callback_mid:
                data["progress_message_id"] = callback_mid
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
        user_id=get_user_id(event) or max_user_id,
        dialog_data=data,
        text=text,
        attachments=[keyboard],
        force_new_message=force_new_message,
    )

    # Сохранить mid если новое сообщение (edit использовал прежний mid —
    # ничего сохранять не нужно).
    if not edited and new_mid:
        async with session_scope() as session:
            await users_service.update_dialog_data(
                session, max_user_id, {"progress_message_id": new_mid}
            )


async def ask_locality(
    event,
    max_user_id: int,
    *,
    force_new_message: bool = False,
):
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
        force_new_message=force_new_message,
    )


async def ask_address(
    event,
    max_user_id: int,
    *,
    force_new_message: bool = False,
):
    """Шаг «Адрес». Прогресс-карта с галочкой локалити."""
    await _show_progress_step(
        event,
        max_user_id,
        stage="address",
        next_state=DialogState.AWAITING_ADDRESS,
        keyboard=keyboards.cancel_keyboard(),
        force_new_message=force_new_message,
    )


async def ask_topic(
    event,
    max_user_id: int,
    *,
    force_new_message: bool = False,
):
    """Шаг «Тема». Прогресс-карта с галочкой адреса."""
    async with session_scope() as session:
        topics = await settings_store.get(session, "topics") or ["Другое"]
    await _show_progress_step(
        event,
        max_user_id,
        stage="topic",
        next_state=DialogState.AWAITING_TOPIC,
        keyboard=keyboards.topics_keyboard(topics),
        force_new_message=force_new_message,
    )


async def ask_summary(
    event,
    max_user_id: int,
    *,
    force_new_message: bool = False,
):
    """Шаг «Описание сути». Прогресс-карта с галочкой темы.

    На этом шаге показываем cancel-клавиатуру и ждём следующее
    непустое сообщение (текст / фото / видео / файл) — финализация
    в on_awaiting_summary.
    """
    async with session_scope() as session:
        data = await users_service.update_dialog_data(
            session, max_user_id,
            lambda old: {"draft_token": old.get("draft_token") or secrets.token_urlsafe(12)},
        )
    token = data.get("draft_token")
    if not token:
        return
    await _show_progress_step(
        event,
        max_user_id,
        stage="summary",
        next_state=DialogState.AWAITING_SUMMARY,
        keyboard=keyboards.appeal_draft_keyboard(
            token, has_content=bool(data.get("summary_chunks") or data.get("attachments"))
        ),
        force_new_message=force_new_message,
    )


async def finalize_appeal(event, max_user_id: int, *, draft_token: str | None = None):
    """Сохранить накопленный черновик после явного нажатия «Отправить»."""
    persisted = await persist_and_dispatch_appeal(event.bot, max_user_id, draft_token=draft_token)
    if persisted == PERSIST_RATE_LIMITED:
        async with current_user(max_user_id) as (session, user):
            active = await appeals_service.find_active_for_user(session, user.id)
            earliest = await appeals_service.earliest_recent_for_user(
                session, user.id, hours=1
            )
        reset_at = earliest + timedelta(hours=1) if earliest else None
        await _send_rate_limit_message(
            event,
            has_open_unanswered=active is not None,
            reset_at=reset_at,
        )
    elif persisted == PERSIST_NO_CONSENT:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=(
                "Чтобы подать обращение, нужно согласие на обработку "
                "персональных данных. Откройте /start и дайте согласие."
            ),
            attachments=[keyboards.back_to_menu_keyboard()],
        )
    elif persisted == PERSIST_BLOCKED:
        # Заблокированному нельзя советовать /start: согласие блокировку
        # не снимает, и житель ходит по кругу, не понимая, что происходит.
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=(
                "Ваш аккаунт заблокирован — подача обращений недоступна. "
                "Если блокировка ошибочна, обратитесь к координатору "
                "Администрации."
            ),
            attachments=[keyboards.back_to_menu_keyboard()],
        )
    elif persisted is False:
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
    phone, contact_name, foreign_contact = extract_resident_contact(body, max_user_id)
    if foreign_contact:
        await event.message.answer("Пришлите свой контакт, а не контакт другого человека.",
                                   attachments=[keyboards.contact_request_keyboard()])
        return
    if phone is None and text_body:
        digits_match = _PHONE_DIGITS_RE.search(text_body)
        if digits_match:
            phone = digits_match.group(0)
    # The profile column is VARCHAR(32). Reject malformed/oversized
    # text or contact payloads before opening the write transaction.
    if phone is None or len(phone) > 32 or not any(ch.isdigit() for ch in phone):
        await event.message.answer(
            texts.CONTACT_RETRY,
            attachments=[keyboards.contact_request_keyboard()],
        )
        return

    async with current_user(max_user_id) as (session, user):
        await users_service.set_phone(session, max_user_id, phone)
        if contact_name and resident_name_gate.needs_name(user.first_name):
            cleaned = normalize_resident_name(contact_name, max_chars=cfg.name_max_chars)
            if cleaned:
                await users_service.set_first_name(session, max_user_id, cleaned)
                user.first_name = cleaned

    if resident_name_gate.needs_name(user.first_name):
        async with session_scope() as session:
            await users_service.set_state(
                session, max_user_id, DialogState.AWAITING_NAME
            )
        await event.message.answer(
            resident_name_gate.NAME_PROMPT, attachments=[keyboards.cancel_keyboard()]
        )
    else:
        await ask_contact_or_skip(event, max_user_id, force_new_message=True)


async def on_awaiting_name(event, body, text_body, max_user_id):
    _, contact_name, foreign_contact = extract_resident_contact(body, max_user_id)
    name = None if foreign_contact else normalize_resident_name(
        text_body or contact_name, max_chars=cfg.name_max_chars
    )
    if name is None:
        await event.message.answer(resident_name_gate.NAME_PROMPT,
                                   attachments=[keyboards.cancel_keyboard()])
        return
    async with current_user(max_user_id) as (session, user):
        pending = (user.dialog_data or {}).get(resident_name_gate.RESUME_KEY)
        await users_service.set_first_name(session, max_user_id, name)
        if pending:
            pending = {**pending, "ready": True}
            await users_service.set_state(
                session, max_user_id, DialogState(pending["state"]),
                data={**pending["data"], resident_name_gate.RESUME_KEY: pending},
            )
    if pending:
        await resident_name_gate.resume_after_name(event, max_user_id, pending)
        return
    if await ask_address_or_reuse(event, max_user_id, force_new_message=True):
        return
    await ask_locality(event, max_user_id, force_new_message=True)


async def on_awaiting_address(event, body, text_body, max_user_id):
    address = text_body.strip()
    if len(address) > cfg.address_max_chars:
        await event.message.answer(
            f"Адрес не сохранён: сейчас можно до {cfg.address_max_chars} символов. "
            "Укажите адрес короче и отправьте снова."
        )
        return
    if not address or not _HAS_ALNUM.search(address):
        await event.message.answer(texts.ADDRESS_EMPTY)
        return
    async with session_scope() as session:
        await users_service.update_dialog_data(
            session, max_user_id, {"address": address}
        )
    await ask_topic(event, max_user_id, force_new_message=True)


async def on_awaiting_summary(event, body, text_body, max_user_id):
    """Добавить часть текста и вложения в устойчивый черновик."""
    chunk = text_body or ""
    if has_unsupported_appeal_attachments(body):
        await event.message.answer(
            "Обращение не отправлено: можно приложить фото, видео или файл. "
            "Удалите остальные вложения и отправьте сообщение снова.",
            attachments=[keyboards.cancel_keyboard()],
        )
        return
    atts = collect_attachments(body)
    if not chunk.strip() and not atts:
        await event.message.answer(
            texts.APPEAL_EMPTY_REJECTED,
            attachments=[keyboards.cancel_keyboard()],
        )
        return

    if len(atts) > cfg.attachments_max_per_appeal:
        await event.message.answer(
            f"Обращение не отправлено: приложено {len(atts)} файлов, "
            f"а сейчас можно до {cfg.attachments_max_per_appeal}. "
            "Уменьшите число вложений и отправьте сообщение снова.",
            attachments=[keyboards.cancel_keyboard()],
        )
        return

    def _merge_summary(data: dict) -> dict:
        """Накопить chunks/attachments поверх СВЕЖЕГО dialog_data.

        Вызывается внутри update_dialog_data ПОД pg_advisory_xact_lock —
        прямой read-modify-write здесь терял одно из двух сообщений,
        пришедших подряд (текст и фото): оба читали старый список,
        последний writer затирал изменения первого. list() — отдельные
        копии: append не должен мутировать list, лежащий в
        SQLAlchemy-tracked user.dialog_data до flush.
        """
        from aemr_bot.services.resident_drafts import append_part
        replacing = bool(data.get("replace_text") and chunk.strip())
        source = {**data, "summary_chunks": []} if replacing else data
        patch = append_part(source, chunk, atts, max_chars=cfg.summary_max_chars,
                            max_files=cfg.attachments_max_per_appeal)
        if replacing:
            patch["replace_text"] = False
        patch["draft_token"] = data.get("draft_token") or secrets.token_urlsafe(12)
        pending = data.get(resident_name_gate.RESUME_KEY)
        if pending and pending.get("ready"):
            patch[resident_name_gate.RESUME_KEY] = {**pending, "applied": True}
        return patch

    from aemr_bot.services.resident_drafts import DraftLimitError
    try:
        async with session_scope() as session:
            await users_service.update_dialog_data(session, max_user_id, _merge_summary)
    except DraftLimitError:
        await event.message.answer(
            f"Эта часть не добавлена: в черновике может быть до {cfg.summary_max_chars} "
            f"символов и {cfg.attachments_max_per_appeal} вложений. "
            "Ранее добавленные части сохранены. Сократите новую часть или приложите документ."
        )
        await show_summary_draft(event, max_user_id)
        return
    await show_summary_draft(event, max_user_id)


async def show_summary_draft(event, max_user_id: int) -> None:
    async with current_user(max_user_id) as (_, user):
        data = dict(user.dialog_data or {})
        token = data.get("draft_token")
        followup = user.dialog_state == DialogState.AWAITING_FOLLOWUP_TEXT.value
    if not token:
        async with session_scope() as session:
            data = await users_service.update_dialog_data(
                session, max_user_id,
                lambda old: {"draft_token": old.get("draft_token") or secrets.token_urlsafe(12)},
            )
        token = data.get("draft_token")
        if not token:
            return
    if data.get("replace_text"):
        await send_or_edit_screen(
            event,
            text="Пришлите новый текст одним сообщением. Прежний текст сохранится, пока вы не пришлёте замену; вложения останутся.",
            attachments=[keyboards.draft_edit_cancel_keyboard(token)],
        )
        return
    chunks = data.get("summary_chunks") or []
    files = data.get("attachments") or []
    full = "\n".join(chunks)
    preview = full[:400] + ("…" if len(full) > 400 else "")
    title = f"Черновик дополнения к № {data.get('appeal_id')}" if followup else "Черновик обращения"
    await send_or_edit_screen(
        event, text=(f"📝 {title}\n\nЧастей текста: {len(chunks)} · "
                     f"Символов: {len(full)} из {cfg.summary_max_chars}\n"
                     f"Вложений: {len(files)} из {cfg.attachments_max_per_appeal}\n\n"
                     + (f"{preview}\n\n" if preview else "")
                     + "Можно прислать ещё текст или файлы. Когда всё готово, нажмите кнопку отправки."),
        attachments=[keyboards.appeal_draft_keyboard(token, has_content=bool(full.strip() or files), followup=followup)],
    )


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
        policy_token = await settings_store.get(session, "policy_pdf_token")
        if not policy_url and not policy_token:
            await event.message.answer(
                "Сервис временно недоступен — не настроена политика обработки данных.",
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        text, consent_hash = await settings_store.get_consent_request_and_hash(
            session, policy_url=policy_url or "в прикреплённом файле",
            fallback=texts.CONSENT_REQUEST,
        )
    from aemr_bot.utils.text_pages import fits_max_message
    if not fits_max_message(text):
        log.error("consent text exceeds MAX transport limit; administrator action required")
        await event.message.answer(
            "Текст согласия временно недоступен. Попробуйте позже или обратитесь в администрацию.",
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return
    attachments = [keyboards.consent_keyboard()]
    if policy_token:
        from aemr_bot.services.policy import build_file_attachment
        attachments.insert(0, build_file_attachment(policy_token))
    sent = await event.message.answer(text, attachments=attachments)
    async with session_scope() as session:
        await users_service.set_state(
            session, max_user_id, DialogState.AWAITING_CONSENT,
            data={"consent_prompt_hash": consent_hash, "consent_prompt_text": text,
                  "consent_prompt_mid": extract_message_id(sent)},
        )


async def on_idle(event, body, text_body, max_user_id):
    """IDLE — нет активной воронки. Раньше был «магический followup»;
    теперь дополнение работает только через явную кнопку «📎 Дополнить»."""
    async with current_user(max_user_id) as (session, user):
        active = await appeals_service.find_active_for_user(session, user.id)

    if active is not None:
        await event.message.answer(
            "Не понял сообщение. Если хотите дополнить уже поданное "
            "обращение — откройте «📂 Мои обращения» и нажмите "
            "«📎 Дополнить» в карточке нужного обращения.",
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return
    await event.message.answer(
        texts.UNKNOWN_INPUT,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def on_awaiting_followup_text(event, body, text_body, max_user_id):
    """Collect parts; delivery requires an explicit, token-bound confirmation."""
    await on_awaiting_summary(event, body, text_body, max_user_id)


async def submit_followup(event, body, text_body, max_user_id, *, draft_token: str):
    """Житель нажал «📎 Дополнить» в карточке обращения. Принимаем текст
    и/или вложения — пришиваем к обращению из dialog_data, обновляем
    карточку в админ-чате и подтверждаем жителю одной карточкой с
    кнопкой главного меню.

    Отвеченные и закрытые обращения не переоткрываем. Повтор по ним
    оформляется новым связанным обращением через «🔁 Подать похожее».
    """
    from aemr_bot.services.admin_relay import relay_attachments_to_admin

    async with current_user(max_user_id) as (session, user):
        appeal_id = (user.dialog_data or {}).get("appeal_id")
        appeal = (
            await appeals_service.get_by_id(session, int(appeal_id))
            if appeal_id
            else None
        )

    if appeal is None or appeal.user_id != user.id:
        await event.message.answer(
            "Обращение, которое вы хотели дополнить, недоступно. "
            "Откройте «📂 Мои обращения» — выберите актуальное.",
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return

    # Если согласие отозвано между нажатием «📎 Дополнить» и присылкой
    # текста — дополнение в админ-чат не идёт. 152-ФЗ ст. 21 ч. 5: после
    # отзыва обработка прекращается, в том числе входящих сообщений.
    if user.consent_pdn_at is None:
        await event.message.answer(
            "Согласие на обработку отозвано — дополнение не отправлено. "
            "Чтобы продолжить, откройте /start и дайте согласие заново.",
            attachments=[keyboards.back_to_menu_keyboard()],
        )
        return

    # Если оператор ответил или закрыл обращение между нажатием и
    # присылкой текста — не возвращаем его в работу дополнением.
    if appeal.status in {AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value}:
        await event.message.answer(
            "Обращение уже завершено. Если ситуация повторилась — "
            "откройте его в «📂 Мои обращения» и нажмите «🔁 Подать похожее». "
            "Текст черновика сохранён — его можно посмотреть и скопировать.",
            attachments=[keyboards.appeal_draft_keyboard(draft_token, followup=True)],
        )
        return

    _raw_followup = text_body or ""
    if len(_raw_followup) > cfg.summary_max_chars:
        await event.message.answer(
            f"Дополнение не отправлено: в нём {len(_raw_followup)} символов, "
            f"а сейчас можно до {cfg.summary_max_chars}. Сократите текст и "
            "отправьте его снова или приложите документ.",
            attachments=[keyboards.cancel_keyboard()],
        )
        return
    text = _raw_followup
    if has_unsupported_appeal_attachments(body):
        await event.message.answer(
            "Дополнение не отправлено: можно приложить фото, видео или файл. "
            "Удалите остальные вложения и отправьте сообщение снова.",
            attachments=[keyboards.cancel_keyboard()],
        )
        return
    attachments = collect_attachments(body)
    if len(attachments) > cfg.attachments_max_per_appeal:
        await event.message.answer(
            f"Дополнение не отправлено: приложено {len(attachments)} файлов, "
            f"а сейчас можно до {cfg.attachments_max_per_appeal}. "
            "Уменьшите число вложений и отправьте сообщение снова.",
            attachments=[keyboards.cancel_keyboard()],
        )
        return
    if not text.strip() and not attachments:
        await event.message.answer(
            "Опишите дополнение к обращению одним сообщением или "
            "приложите фото, видео или файл.",
            attachments=[keyboards.cancel_keyboard()],
        )
        return

    # SEC #5: followup flood rate-limit.
    # 1. min-interval — не чаще раз в N секунд (защита от двойного тапа /
    #    скрипт-флуда).
    # 2. max-per-hour — не больше M на одно обращение в час (защита от
    #    долгого медленного флуда).
    # Если лимит нарушен — НЕ принимаем followup, не сбрасываем state,
    # житель может попробовать ещё раз позже.
    async with session_scope() as _rl_session:
        recent_count, last_at = await appeals_service.followup_rate_limit_stats(
            _rl_session, appeal.id, hours=1
        )
    pending = (user.dialog_data or {}).get(resident_name_gate.RESUME_KEY) or {}
    replay_pending = bool(pending.get("ready"))
    retry_hint = "\n\nЧерновик сохранён. После паузы снова нажмите «Отправить»."
    if last_at is not None:
        elapsed = (datetime.now(UTC) - last_at).total_seconds()
        if elapsed < cfg.followup_min_interval_seconds:
            wait = int(cfg.followup_min_interval_seconds - elapsed)
            await event.message.answer(
                f"Слишком часто. Подождите {wait}с перед следующим "
                f"дополнением — это защита от случайного двойного "
                f"сообщения.{retry_hint}",
                attachments=[keyboards.appeal_draft_keyboard(draft_token, has_content=True, followup=True)],
            )
            return False
    if recent_count >= cfg.followup_max_per_hour_per_appeal:
        await event.message.answer(
            f"По этому обращению уже {recent_count} дополнений за час — "
            f"лимит {cfg.followup_max_per_hour_per_appeal}. Подождите час "
            f"или дождитесь ответа оператора — он видит всю историю и "
            f"свяжется по сути вопроса.{retry_hint}",
            attachments=[keyboards.appeal_draft_keyboard(draft_token, has_content=True, followup=True)],
        )
        if replay_pending:
            return False
        # Keep the draft for explicit retry after the rate window clears.
        return

    async with session_scope() as session:
        # The earlier checks only guide the UI. Reload and lock the rows in
        # the insertion transaction: an operator may have closed the appeal
        # or blocked the resident while the rate-limit query was running.
        # The same lock order as erasure: User before Appeal.
        locked_user = await users_service.find_by_max_id(
            session, max_user_id, for_update=True
        )
        locked_appeal = await appeals_service.get_by_id(
            session, appeal.id, for_update=True
        )
        if (
            locked_appeal is None
            or locked_user is None
            or locked_appeal.user_id != locked_user.id
            or locked_appeal.status not in {AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value}
            or locked_user.consent_pdn_at is None
            or locked_user.is_blocked
            or locked_user.dialog_state != DialogState.AWAITING_FOLLOWUP_TEXT.value
            or (locked_user.dialog_data or {}).get("appeal_id") != appeal.id
            or (locked_user.dialog_data or {}).get("draft_token") != draft_token
        ):
            await event.message.answer(
                "Дополнение не сохранено: обращение или ваше разрешение на "
                "отправку изменилось. Откройте «📂 Мои обращения» и проверьте статус.",
                attachments=[keyboards.back_to_menu_keyboard()],
            )
            return
        # Read the draft again under its lock. Another message may have
        # appended to it after the callback first read the screen.
        current = locked_user.dialog_data or {}
        text = "\n".join(current.get("summary_chunks") or [])
        attachments = list(current.get("attachments") or [])
        if not text.strip() and not attachments:
            await event.message.answer("Черновик пуст. Добавьте текст или вложение.")
            return
        recent_count, last_at = await appeals_service.followup_rate_limit_stats(session, appeal.id, hours=1)
        if (recent_count >= cfg.followup_max_per_hour_per_appeal or
                (last_at is not None and
                 (datetime.now(UTC) - last_at).total_seconds() < cfg.followup_min_interval_seconds)):
            await event.message.answer(
                "Слишком часто. Черновик сохранён: немного подождите и снова нажмите «Отправить».",
                attachments=[keyboards.appeal_draft_keyboard(draft_token, has_content=True, followup=True)],
            )
            return
        appeal = locked_appeal
        await appeals_service.add_user_message(
            session,
            appeal=appeal,
            text=text or None,
            attachments=attachments,
        )
        updated_appeal = await appeals_service.get_by_id_with_messages(
            session, appeal.id
        )
        await users_service.reset_state(session, max_user_id)
        appeal_for_card = updated_appeal or appeal
        user_for_card = getattr(appeal_for_card, "user", None) or user

    # Единое уведомление: ПОЛНАЯ admin appeal card с timeline (где видно
    # свежее дополнение жителя как последнее сообщение) + маркер-шапка
    # `📩 Новое дополнение по обращению #N от жителя`. Один объект в
    # чате вместо двух — оператор не сканирует две сущности «короткое
    # уведомление + карточка», а видит сразу контекст + содержание +
    # все кнопки действий.
    #
    # force_new=True: дополнение = «новая инфа», нужна запись внизу
    # чата даже если предыдущая карточка ещё была last_in_chat.
    # admin_message_id оригинала НЕ двигаем (sacred-правило, PR #54),
    # но last_admin_card_mid в БД обновится новым mid'ом.
    new_card_mid: str | None = None
    if cfg.admin_group_id:
        try:
            from aemr_bot.services import admin_card as admin_card_service

            if getattr(appeal_for_card, "user", None) is None:
                appeal_for_card.user = user_for_card
            new_card_mid = await admin_card_service.render(
                event.bot,
                appeal_for_card,
                force_new=True,
                event_header=(
                    f"📩 Новое дополнение по обращению #{appeal.id} от "
                    f"жителя — см. ниже в истории переписки."
                ),
            )
        except Exception:
            log.exception("admin_card.render after followup failed")

        if attachments:
            try:
                # Relay вложений жителя — reply-link на свежую карточку
                # (new_card_mid). Без link — relay уйдёт отдельным
                # сообщением, оператор всё равно увидит вложения.
                await relay_attachments_to_admin(
                    event.bot,
                    appeal_id=appeal.id,
                    admin_mid=new_card_mid,
                    stored_attachments=attachments,
                )
            except Exception:
                log.exception("relay followup attachments failed")

    confirm_text = f"✅ Дополнение сохранено в обращении № {appeal.id}."
    if not new_card_mid:
        confirm_text += " Не удалось подтвердить уведомление специалиста; обращение остаётся в очереди."
    await event.message.answer(
        confirm_text,
        attachments=[keyboards.back_to_menu_keyboard()],
    )
