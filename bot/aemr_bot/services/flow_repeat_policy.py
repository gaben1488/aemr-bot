from __future__ import annotations

import logging

from aemr_bot import keyboards
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import get_chat_id

log = logging.getLogger(__name__)

DONE_STATUSES = {"answered", "closed"}


def _marker() -> str:
    return ": " + "обрат" + "ная связь по "


def _source_word(status: str) -> str:
    if status == "answered":
        return "отвеч" + "енному вопросу"
    return "закры" + "тому вопросу"


def _clean_topic(topic: str | None) -> str:
    value = (topic or "").strip()
    marker = _marker()
    if marker in value:
        value = value.split(marker, 1)[0].strip()
    return value or "Другое"


def _repeat_topic(topic: str | None, status: str) -> str:
    value = f"{_clean_topic(topic)}{_marker()}{_source_word(status)}"
    return value[:120]


async def start_repeat(event, appeal_id: int, max_user_id: int) -> None:
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if not appeal or not appeal.user or appeal.user.max_user_id != max_user_id:
            await event.bot.send_message(chat_id=get_chat_id(event), text="Обращение не найдено.", attachments=[keyboards.back_to_menu_keyboard()])
            return
        if appeal.status not in DONE_STATUSES:
            await event.bot.send_message(chat_id=get_chat_id(event), text="Это обращение ещё не завершено. Для уточнения используйте дополнение.", attachments=[keyboards.back_to_menu_keyboard()])
            return
        if not (appeal.locality and appeal.address):
            from aemr_bot.handlers.appeal_funnel import start_appeal_flow
            await start_appeal_flow(event, max_user_id)
            return
        topic = _repeat_topic(appeal.topic, appeal.status)
        await users_service.set_state(
            session,
            max_user_id,
            DialogState.AWAITING_SUMMARY,
            data={
                "locality": appeal.locality,
                "address": appeal.address,
                "topic": topic,
                "summary_chunks": [f"Повторное обращение к #{appeal.id} ({appeal.status})."],
                "repeat_of_appeal_id": appeal.id,
                "repeat_of_status": appeal.status,
            },
        )
        prompt = f"Подаём новое обращение по тому же адресу: {appeal.locality}, {appeal.address}. Тема: {topic}. Опишите, что произошло после ответа или закрытия."
    await event.bot.send_message(chat_id=get_chat_id(event), text=prompt, attachments=[keyboards.cancel_keyboard()])


def install() -> None:
    from aemr_bot.handlers import menu

    menu.start_appeal_repeat = start_repeat
    log.info("repeat policy installed")
