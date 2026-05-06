"""Логика ответов операторов и дополнительных сообщений от жителей, вызывается
из единого обработчика message_created в handlers/appeal.py.
"""

import logging
import re

from maxapi import Dispatcher
from maxapi.types import MessageCreated

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import operators as operators_service
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import (
    extract_message_id,
    get_chat_id,
    get_message_link,
    get_user_id,
)

log = logging.getLogger(__name__)


def _mid_from_link(link) -> str | None:
    """Извлекает message-id из Pydantic-модели LinkedMessage или её словарного
    представления (dict fallback). love-apples/maxapi хранит его в `link.message.mid`; 
    в старых версиях было `link.mid`. Пробуем оба варианта."""
    inner = getattr(link, "message", None)
    if inner is not None:
        mid = getattr(inner, "mid", None)
        if mid is not None:
            return str(mid)
    if isinstance(link, dict):
        inner_dict = link.get("message")
        if isinstance(inner_dict, dict) and inner_dict.get("mid"):
            return str(inner_dict["mid"])
        if link.get("mid"):
            return str(link["mid"])
    mid = getattr(link, "mid", None)
    return str(mid) if mid is not None else None


def _extract_reply_target_mid(event) -> str | None:
    """Извлекает `mid` сообщения, на которое отвечают, из `event.message.link`.

    Проверено на love-apples/maxapi: `Message.link: LinkedMessage | None`
    содержит обратную ссылку на ответ/пересылку. ID оригинального сообщения находится в
    `link.message.mid` (вложенный MessageBody), а не в `link.mid`.
    """
    link = get_message_link(event)
    if link is None:
        return None

    link_type = getattr(link, "type", None)
    if link_type is None and isinstance(link, dict):
        link_type = link.get("type")
    # MessageLinkType.REPLY может прийти как значение StrEnum, элемент
    # перечисления или обычная строка — проверяем по суффиксу в нижнем регистре.
    if link_type is None or not str(link_type).lower().endswith("reply"):
        return None

    return _mid_from_link(link)


async def _deliver_operator_reply(
    event,
    *,
    appeal,
    operator,
    text: str,
    audit_action: str,
) -> bool:
    """Общий путь для доставки ответа оператора жителю.

    Используется как в handle_operator_reply (механизм ответа свайпом, который
    зависит от заполнения Message.link клиентом MAX), так и в cmd_reply
    (явная команда /reply <appeal_id> <text>, работающая на любых клиентах
    независимо от поддержки свайпов).

    Возвращает True, если оператору дан окончательный ответ (сообщение доставлено,
    либо вежливо отклонено из-за длины / невозможности доставки). Возвращает
    False только при дедупликации, когда target_mid равен None и оператор
    на самом деле не собирался отвечать.
    """
    if len(text) > cfg.answer_max_chars:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.ADMIN_REPLY_TOO_LONG.format(
                limit=cfg.answer_max_chars, actual=len(text)
            ),
        )
        return True

    # Защита по 152-ФЗ: после /erase или /forget житель отозвал согласие на обработку ПДн.
    # is_blocked является каноничным маркером "не связываться" — никогда не отправляем
    # сообщения заблокированному пользователю, даже если оператор по ошибке ответил на
    # старую карточку.
    if appeal.user.is_blocked:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=(
                f"⚠️ Не могу доставить ответ по обращению #{appeal.id}: "
                f"житель отозвал согласие на обработку ПДн или был "
                f"анонимизирован. Свяжитесь с ним по телефону из карточки."
            ),
        )
        return True

    target_user_id = appeal.user.max_user_id
    formatted_text = card_format.citizen_reply(appeal, text)
    try:
        # ВАЖНО: доставляем сообщение жителю по user_id (а не chat_id) — мы не
        # сохраняли chat_id их личного диалога, только их MAX user_id.
        sent = await event.bot.send_message(user_id=target_user_id, text=formatted_text)
    except Exception as exc:  # noqa: BLE001
        # Показываем в админ-чате только имя класса исключения — `repr(exc)`
        # из maxapi часто содержит тело запроса (текст ответа оператора,
        # целевой user_id), что может осесть в истории админ-группы. Полная
        # ошибка со стеком пишется в логи бота для диагностики.
        log.exception(
            "operator_reply: delivery failed for appeal=%s user_id=%s",
            appeal.id, target_user_id,
        )
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=(
                f"⚠️ Не удалось доставить ответ жителю по обращению #{appeal.id} "
                f"({type(exc).__name__}). Возможно, житель удалил диалог или "
                f"заблокировал бота. Обращение остаётся в работе."
            ),
        )
        return True
    delivered_mid = extract_message_id(sent)

    async with session_scope() as session:
        appeal_full = await appeals_service.get_by_id(session, appeal.id)
        if appeal_full is None:
            log.warning(
                "appeal #%s vanished between lookup and reload", appeal.id
            )
            return True
        await appeals_service.add_operator_message(
            session,
            appeal=appeal_full,
            text=text,
            operator_id=operator.id,
            max_message_id=delivered_mid,
        )
        await operators_service.write_audit(
            session,
            operator_max_user_id=operator.max_user_id,
            action=audit_action,
            target=f"appeal #{appeal.id}",
            details={"chars": len(text)},
        )

    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.ADMIN_REPLY_DELIVERED.format(number=appeal.id),
    )
    return True


async def handle_operator_reply(event: MessageCreated, body, text: str) -> bool:
    """Оператор ответил на карточку в админ-группе свайпом/«Ответить».

    Возвращает True, если обработано, и False, если сообщение вообще не было
    ответом (чтобы диспетчер мог перенаправить его дальше — на данный момент никуда).
    """
    target_mid = _extract_reply_target_mid(event)
    appeal_id_from_text = None

    # Дополнительная проверка: если target_mid отсутствует (например, для сообщений из /open_tickets),
    # пытаемся извлечь номер обращения прямо из текста сообщения, на которое отвечают.
    link = get_message_link(event)
    if link:
        replied_text = ""
        inner = getattr(link, "message", None)
        if inner:
            replied_text = getattr(inner, "text", "")
        elif isinstance(link, dict):
            inner_dict = link.get("message", {})
            if isinstance(inner_dict, dict):
                replied_text = inner_dict.get("text", "")
            else:
                replied_text = link.get("text", "")
        
        if replied_text:
            match = re.search(r"Обращение #(\d+)", replied_text)
            if match:
                appeal_id_from_text = int(match.group(1))

    if target_mid is None and appeal_id_from_text is None:
        log.info(
            "operator_reply: нет ссылки-ответа в event.message — сообщение проигнорировано "
            "(оператор написал в админ-группу без использования ответа/свайпа)"
        )
        return False

    author_id = get_user_id(event)
    if author_id is None:
        log.warning("operator_reply: нет user_id в событии")
        return False

    async with session_scope() as session:
        operator = await operators_service.get(session, author_id)
        if operator is None:
            log.info(
                "operator_reply: user_id=%s ответил, но не найден в таблице операторов",
                author_id,
            )
            return False
            
        appeal = None
        if target_mid:
            appeal = await appeals_service.get_by_admin_message_id(session, target_mid)
        
        if appeal is None and appeal_id_from_text:
            appeal = await appeals_service.get_by_id(session, appeal_id_from_text)

        if appeal is None:
            await event.bot.send_message(
                chat_id=get_chat_id(event), text=texts.ADMIN_REPLY_NO_APPEAL
            )
            return True

        log.info(
            "operator_reply: обнаружено — operator_id=%s reply_to_mid=%s text_len=%d",
            operator.id, target_mid, len(text),
        )

    return await _deliver_operator_reply(
        event,
        appeal=appeal,
        operator=operator,
        text=text,
        audit_action="reply",
    )


async def handle_command_reply(event, appeal_id: int, text: str) -> None:
    """Команда `/reply N <текст>` из админ-группы — альтернатива ответу свайпом.

    Полезна, когда клиент MAX не прикрепляет ссылку-ответ к сообщению при свайпе
    (зависит от клиента/версии), или если оператор предпочитает использовать
    явные команды. Тот же путь доставки, тот же аудит, те же лимиты на ответ.
    """
    if not cfg.admin_group_id or get_chat_id(event) != cfg.admin_group_id:
        return

    author_id = get_user_id(event)
    if author_id is None:
        return

    async with session_scope() as session:
        operator = await operators_service.get(session, author_id)
        if operator is None:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id, text=texts.OP_NOT_AUTHORIZED
            )
            return
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if appeal is None:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id),
            )
            return

    log.info(
        "command_reply: operator_id=%s appeal=%s text_len=%d",
        operator.id, appeal_id, len(text),
    )
    await _deliver_operator_reply(
        event,
        appeal=appeal,
        operator=operator,
        text=text,
        audit_action="reply_via_command",
    )


async def handle_user_followup(event: MessageCreated, text: str) -> bool:
    """Житель написал в личный диалог, находясь в состоянии ожидания (idle) —
    переоткрываем отвеченное обращение, если оно есть."""
    from aemr_bot.db.models import AppealStatus, DialogState

    max_user_id = get_user_id(event)
    if max_user_id is None:
        return False

    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        if user.dialog_state != DialogState.IDLE.value:
            return False
        # Анонимизированный / заблокированный пользователь — не выводим его текст
        # в админ-группу как "дополнительное сообщение". Его согласие было отозвано.
        if user.is_blocked:
            return False
        active = await appeals_service.find_active_for_user(session, user.id)

    if active is None or active.status != AppealStatus.ANSWERED.value:
        return False

    async with session_scope() as session:
        await appeals_service.reopen(session, active.id)
        await appeals_service.add_user_message(
            session,
            appeal=active,
            text=text,
            attachments=[],
        )
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        followup = card_format.admin_followup(active, user, text)

    if cfg.admin_group_id:
        await event.bot.send_message(chat_id=cfg.admin_group_id, text=followup)
    return True


def register(dp: Dispatcher) -> None:
    """Пустышка (No-op): маршрутизация message_created управляется в handlers/appeal.py."""
    return None