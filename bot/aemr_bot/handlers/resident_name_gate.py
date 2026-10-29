"""Request a resident name without losing an interrupted citizen workflow."""
from __future__ import annotations

from contextvars import ContextVar
from types import SimpleNamespace

from aemr_bot import keyboards
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import DialogState
from aemr_bot.handlers import callback_router
from aemr_bot.handlers._common import current_user
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.attachments import (
    collect_attachments,
    extract_location,
    has_unsupported_appeal_attachments,
)
from aemr_bot.utils.event import send_or_edit_screen
from aemr_bot.utils.resident_names import normalize_resident_name

NAME_PROMPT = (
    "Как вас зовут? Напишите настоящее имя кириллицей, например «Анна». "
    "Имя можно указать первым или вторым: «Лариса Беспаленко» или «Беспаленко Лариса». Можно добавить отчество. "
    "Псевдонимы и имена из профиля автоматически не принимаются."
)
RESUME_KEY = "_name_resume"
_replaying: ContextVar[bool] = ContextVar("resident_name_replaying", default=False)
_SAFE_EXACT = {"menu:new_appeal", "menu:my_appeals", "addr:reuse", "addr:new",
               "geo:confirm", "geo:edit_address", "geo:other_locality", "appeal:submit"}
_SAFE_PREFIXES = ("locality:", "topic:", "appeal:followup:", "appeal:repeat:",
                  "appeal:show:", "appeal:atts:", "appeals:page:")
_CHOICE_SETTINGS = {"locality": "localities", "topic": "topics"}


def needs_name(name) -> bool:
    return normalize_resident_name(name, max_chars=cfg.name_max_chars) is None


def gates_callback(payload: str) -> bool:
    return payload in _SAFE_EXACT or payload.startswith(_SAFE_PREFIXES)


def resume_data(state: str, data: dict) -> dict:
    return {RESUME_KEY: {"state": state, "data": data}}


async def require_name(event, max_user_id: int, *, payload: str | None = None,
                       text: str | None = None, body=None) -> bool:
    """True means the caller must stop; its bounded input is saved for resume."""
    if _replaying.get():
        return False
    replay = None
    async with current_user(max_user_id) as (session, user):
        if max_user_id == -1 or user.is_blocked or not user.consent_pdn_at:
            return False
        data = dict(user.dialog_data or {})
        pending = data.get(RESUME_KEY)
        if not needs_name(user.first_name):
            if not isinstance(pending, dict) or not pending.get("ready"):
                return False
            replay = pending
        elif user.dialog_state in {DialogState.AWAITING_CONTACT.value,
                                   DialogState.AWAITING_CONSENT.value}:
            return False
        elif user.dialog_state == DialogState.AWAITING_NAME.value:
            if text is not None:
                return False
        else:
            if text is not None and user.dialog_state in {
                DialogState.AWAITING_SUMMARY.value, DialogState.AWAITING_FOLLOWUP_TEXT.value,
            }:
                if len(text.strip()) > cfg.summary_max_chars:
                    await event.message.answer(
                        f"Сообщение не отправлено: сейчас можно до {cfg.summary_max_chars} символов. "
                        "Сократите текст и отправьте его снова или приложите документ."
                    )
                    return True
                if has_unsupported_appeal_attachments(body):
                    await event.message.answer(
                        "Сообщение не отправлено: можно приложить фото, видео или файл. "
                        "Удалите остальные вложения и отправьте сообщение снова."
                    )
                    return True
                if len(collect_attachments(body)) > cfg.attachments_max_per_appeal:
                    await event.message.answer(
                        f"Сообщение не отправлено: сейчас можно до {cfg.attachments_max_per_appeal} "
                        "вложений. Уменьшите их число и отправьте сообщение снова."
                    )
                    return True
            pending = {"state": user.dialog_state, "data": data}
            if payload is not None and gates_callback(payload):
                pending["payload"] = payload
                field = payload.split(":", 1)[0]
                if field in _CHOICE_SETTINGS:
                    options = await settings_store.get(session, _CHOICE_SETTINGS[field]) or []
                    index = callback_router.parse_int_tail(payload, f"{field}:")
                    # Settings can change while the resident repairs their name.
                    # Retain the selected value, not its mutable list position.
                    pending["choice"] = (
                        options[index] if index is not None and 0 <= index < len(options) else None
                    )
            if text is not None:
                pending["text"] = text
                pending["attachments"] = collect_attachments(body)
                location = extract_location(body)
                if location is not None:
                    pending["location"] = list(location)
            await users_service.set_state(
                session, max_user_id, DialogState.AWAITING_NAME,
                data={RESUME_KEY: pending},
            )
    if replay is not None:
        if not await resume_after_name(event, max_user_id, replay):
            return True
        # The triggering event may itself be a retry of the name message.
        # Never reinterpret it as input to the newly restored next step.
        if text is not None:
            await event.message.answer(
                "Сохранённое сообщение обработано, диалог восстановлен. "
                "Если текущее сообщение тоже нужно передать, отправьте его ещё раз."
            )
        elif payload is not None:
            await send_or_edit_screen(
                event, text="Сохранённое действие восстановлено. При необходимости нажмите выбранную кнопку ещё раз.",
                force_new_message=True,
            )
        return True
    notice = NAME_PROMPT
    if text is not None:
        notice += "\n\nВаше сообщение сохранено. После ввода имени продолжим с него."
    await send_or_edit_screen(event, text=notice, attachments=[keyboards.cancel_keyboard()],
                              force_new_message=text is not None)
    return True


async def resume_after_name(event, max_user_id: int, pending: dict) -> bool:
    """Retain the replay marker until the interrupted action succeeds.

    Appeal/followup persistence clears it atomically with the accepted input.
    Intermediate steps are idempotent. Summary accumulation marks its input
    applied in the same write, so a crash before finalization cannot append twice.
    """
    token = _replaying.set(True)
    try:
        if await _resume(event, max_user_id, pending) is False:
            # A temporary rejection (for example a follow-up rate limit)
            # has not consumed the saved message. Keep it for the next entry.
            return False
        async with current_user(max_user_id) as (session, user):
            data = dict(user.dialog_data or {})
            if data.get(RESUME_KEY, {}).get("ready"):
                data.pop(RESUME_KEY)
                await users_service.set_state(session, max_user_id, DialogState(user.dialog_state), data=data)
        return True
    finally:
        _replaying.reset(token)


async def _resume(event, max_user_id: int, pending: dict) -> bool:
    from aemr_bot.handlers import appeal, appeal_funnel, appeal_geo, menu

    payload = pending.get("payload")
    if isinstance(payload, str) and gates_callback(payload):
        field = payload.split(":", 1)[0]
        if field in _CHOICE_SETTINGS:
            choice = pending.get("choice")
            async with current_user(max_user_id) as (session, _user):
                options = await settings_store.get(session, _CHOICE_SETTINGS[field]) or []
                valid = isinstance(choice, str) and choice in options
                if valid:
                    await users_service.update_dialog_data(session, max_user_id, {field: choice})
            if valid:
                next_prompt = (
                    appeal_funnel.ask_address if field == "locality" else appeal_funnel.ask_summary
                )
                await next_prompt(event, max_user_id, force_new_message=True)
            else:
                # Also safe for drafts saved before semantic snapshots existed:
                # an old index is not evidence of the resident's original choice.
                await event.message.answer(
                    "Список вариантов изменился. Выберите подходящий вариант ещё раз."
                )
                prompt = (
                    appeal_funnel.ask_locality if field == "locality" else appeal_funnel.ask_topic
                )
                await prompt(event, max_user_id, force_new_message=True)
            return True
        await appeal._route_callback(event, max_user_id, payload)
        return True
    state = DialogState(pending["state"])
    if state == DialogState.AWAITING_SUMMARY and pending.get("applied"):
        await appeal_funnel.finalize_appeal(event, max_user_id)
        return True
    if "text" in pending:
        if pending.get("location") and state == DialogState.AWAITING_LOCALITY:
            await appeal_geo.handle_location_for_locality(event, max_user_id, tuple(pending["location"]))
        else:
            handler = appeal._STATE_HANDLERS.get(state)
            if handler is not None:
                result = await handler(
                    event, SimpleNamespace(attachments=pending.get("attachments", [])),
                    pending["text"], max_user_id,
                )
                return result is not False
        return True
    if state == DialogState.AWAITING_SUMMARY and (
        pending["data"].get("summary_chunks") or pending["data"].get("attachments")
    ):
        await appeal_funnel.finalize_appeal(event, max_user_id)
        return True
    prompts = {
        DialogState.AWAITING_LOCALITY: appeal_funnel.ask_locality,
        DialogState.AWAITING_ADDRESS: appeal_funnel.ask_address,
        DialogState.AWAITING_TOPIC: appeal_funnel.ask_topic,
        DialogState.AWAITING_SUMMARY: appeal_funnel.ask_summary,
    }
    if state in prompts:
        await prompts[state](event, max_user_id, force_new_message=True)
    elif state == DialogState.AWAITING_GEO_CONFIRM:
        await appeal_geo.on_awaiting_geo_confirm(event, None, "", max_user_id)
    elif state == DialogState.AWAITING_FOLLOWUP_TEXT:
        await menu.start_appeal_followup(event, pending["data"]["appeal_id"], max_user_id)
    else:
        await menu.open_main_menu(event)
    return True
