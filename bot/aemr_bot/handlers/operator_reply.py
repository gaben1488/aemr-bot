"""Логика ответов операторов и дополнительных сообщений от жителей, вызывается
из единого обработчика message_created в handlers/appeal.py.
"""

import logging
import re
import time as _time

from maxapi import Dispatcher
from maxapi.types import MessageCreated

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import operators as operators_service
from aemr_bot.utils.event import (
    extract_message_id,
    get_chat_id,
    get_message_link,
    get_user_id,
)

log = logging.getLogger(__name__)


# Защита от двойного ответа: оператор за пару секунд может нажать
# свайп-reply и параллельно набрать /reply N. Оба пути доходят до
# `_deliver_operator_reply` независимо. Без дедупликации житель
# получит две одинаковые копии ответа, в `messages` ляжет два
# дубля. Запоминаем последний ответ в памяти процесса и режем
# повтор того же текста на то же обращение в окне 10 секунд.
_recent_replies: dict[tuple[int, int], tuple[str, float]] = {}
_REPLY_DEDUPE_WINDOW_SEC = 10.0


# Намерение оператора ответить на обращение (после нажатия кнопки
# «✉️ Ответить» в карточке): operator_id → (appeal_id, expires_at).
# Следующее текстовое сообщение от этого оператора в админ-группе
# доставляется как /reply <appeal_id> <текст> без нужды свайпать
# или помнить номер. Окно — 5 минут; дольше держать опасно (оператор
# может забыть про намерение и случайно отправить житель чужой текст).
_reply_intent: dict[int, tuple[int, float]] = {}
_REPLY_INTENT_TTL_SEC = 300.0


def remember_reply_intent(operator_id: int, appeal_id: int) -> None:
    """Запомнить, что оператор сейчас собирается отвечать на обращение."""
    _reply_intent[operator_id] = (appeal_id, _time.monotonic() + _REPLY_INTENT_TTL_SEC)


def consume_reply_intent(operator_id: int) -> int | None:
    """Достать и сбросить намерение, если оно ещё не протухло.

    Возвращает appeal_id, если оператор недавно нажимал «✉️ Ответить» и
    окно не истекло. Сбрасывает запись — namesake intent одноразовое.
    """
    item = _reply_intent.pop(operator_id, None)
    if item is None:
        return None
    appeal_id, expires_at = item
    if _time.monotonic() > expires_at:
        return None
    return appeal_id


def drop_reply_intent(operator_id: int) -> int | None:
    """Сбросить намерение принудительно (кнопка «❌ Отменить ответ» или
    /cancel в админ-чате). Возвращает appeal_id, на который было
    нацелено, чтобы вызывающий код мог показать «отменено для #N»."""
    item = _reply_intent.pop(operator_id, None)
    if item is None:
        return None
    return item[0]


def _is_duplicate_reply(operator_id: int, appeal_id: int, text: str) -> bool:
    """Дедуп ответа оператора в памяти процесса (быстрый первый рубеж).

    Окно 10 секунд, ключ — (operator_id, appeal_id, text). Если за это
    окно тот же оператор отправил тот же текст по тому же обращению —
    дубль. Защищает от двойного клика «✉️ Ответить» + параллельной
    команды /reply.

    Только для одного процесса. Многопроцессный дедуп — через БД,
    см. `_is_duplicate_reply_db` в этом же файле.
    """
    key = (operator_id, appeal_id)
    prev = _recent_replies.get(key)
    now = _time.monotonic()
    if prev is not None:
        prev_text, prev_at = prev
        if prev_text == text and now - prev_at <= _REPLY_DEDUPE_WINDOW_SEC:
            return True
    _recent_replies[key] = (text, now)
    if len(_recent_replies) > 256:
        cutoff = now - _REPLY_DEDUPE_WINDOW_SEC * 6
        for k in list(_recent_replies.keys()):
            _, t = _recent_replies[k]
            if t < cutoff:
                _recent_replies.pop(k, None)
    return False


async def _is_duplicate_reply_db(
    operator_id: int, appeal_id: int, text: str
) -> bool:
    """Дедуп через таблицу events (idempotency_key) — второй рубеж.

    Работает между процессами: если завтра поднимем второй экземпляр
    бота, оба будут видеть общий events.idempotency_key и одна реплика
    отбросит дубль другой.

    Ключ — `reply:{operator}:{appeal}:{hash(text)}`. TTL 30 дней
    (стандартный retention events). За окно дедупа 10 секунд этого
    хватает с запасом.
    """
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    key = f"reply:{operator_id}:{appeal_id}:{digest}"
    from aemr_bot.services import idempotency

    # `idempotency.try_mark_processed_raw` возвращает True если ключ
    # был свободен и мы его заняли. False — значит уже был дубль.
    try:
        return not await idempotency.try_mark_processed_raw(key, "reply_dedup")
    except Exception:
        # БД-дедуп — best-effort: если что-то упало, fallback на
        # in-memory _is_duplicate_reply, который уже защитил выше.
        log.exception("DB dedup failed, fallback to in-memory only")
        return False


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

    if _is_duplicate_reply(operator.id, appeal.id, text):
        log.info(
            "operator_reply: дубль за %.1fс отбит (in-memory) — operator=%s appeal=%s",
            _REPLY_DEDUPE_WINDOW_SEC, operator.id, appeal.id,
        )
        return True
    # Второй рубеж — БД-уровень. Защищает от race между процессами,
    # если когда-то будет horizontal scaling. Сейчас работает один
    # процесс, БД-дедуп страхует in-memory от падения.
    if await _is_duplicate_reply_db(operator.id, appeal.id, text):
        log.info(
            "operator_reply: дубль отбит (БД) — operator=%s appeal=%s",
            operator.id, appeal.id,
        )
        return True

    # Защита от доставки. Парадигма «прощальный ответ»: после отзыва
    # согласия оператор может ответить через бот ОДИН раз по обращениям,
    # поданным ДО точки отзыва. Это закрывает обещание текста кнопки
    # «👋 Хочу попрощаться, но дождаться ответа» и одновременно
    # соблюдает 152-ФЗ ст. 21 ч. 5 — для НОВЫХ обращений после отзыва
    # обработки нет.
    #
    # Жёсткие отказы (без исключений):
    # - is_blocked: IT-блокировка за злоупотребления;
    # - first_name == 'Удалено': житель полностью удалён, max_user_id
    #   был переподвешен на anonymous-user (либо это сам anonymous);
    #   персональные данные физически отсутствуют.
    #
    # Условный отказ (если согласие отозвано):
    # - consent_pdn_at IS NULL И обращение подано ПОСЛЕ revoked_at →
    #   отказ (новое обращение после отзыва — обработки быть не должно).
    # - consent_pdn_at IS NULL И обращение подано ДО revoked_at →
    #   доставка разрешена (прощальный ответ оператора по уже принятому
    #   до отзыва обращению).
    # Перечитываем User свежей сессией непосредственно перед отправкой.
    # Защита от гонки: житель мог тапнуть «🗑 Стереть» в момент, когда
    # оператор печатал ответ. erase_pdn перевешивает appeals на
    # anonymous-user и физически удаляет запись жителя; объект `appeal.user`
    # в памяти оператора остался устаревшим. Если не перечитать —
    # отправим ответ постфактум удалённому жителю.
    async with session_scope() as session:
        fresh_appeal = await appeals_service.get_by_id(session, appeal.id)
    if fresh_appeal is None or fresh_appeal.user is None:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=(
                f"⚠️ Не могу доставить ответ по обращению #{appeal.id}: "
                f"обращение или его автор не найдены."
            ),
        )
        return True
    user = fresh_appeal.user
    hard_forbidden = user.is_blocked or user.first_name == "Удалено"
    revoked_after_appeal = (
        user.consent_pdn_at is None
        and user.consent_revoked_at is not None
        and fresh_appeal.created_at is not None
        and fresh_appeal.created_at >= user.consent_revoked_at
    )
    no_consent_ever = (
        user.consent_pdn_at is None and user.consent_revoked_at is None
    )
    if hard_forbidden or revoked_after_appeal or no_consent_ever:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=(
                f"⚠️ Не могу доставить ответ по обращению #{appeal.id}: "
                f"житель отозвал согласие или его данные удалены. "
                f"Свяжитесь по телефону, если он сохранён в карточке."
            ),
        )
        return True

    target_user_id = user.max_user_id
    formatted_text = card_format.citizen_reply(fresh_appeal, text)
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

    Сначала смотрим, есть ли «намерение ответить» от кнопки «✉️ Ответить»
    под карточкой обращения. Если есть — следующий текст оператора в
    админ-группе уходит как ответ по этому обращению, без свайпа и без
    /reply N. Это третий путь ответа после свайпа и команды.
    """
    author_id = get_user_id(event)
    if author_id is not None:
        intent_appeal_id = consume_reply_intent(author_id)
        if intent_appeal_id is not None:
            log.info(
                "operator_reply: kbd-intent — operator=%s appeal=%s text_len=%d",
                author_id, intent_appeal_id, len(text),
            )
            await handle_command_reply(event, intent_appeal_id, text)
            return True

    target_mid = _extract_reply_target_mid(event)
    appeal_id_from_text = None

    # Запасной путь для свайп-ответов на сообщения из /open_tickets:
    # у них нет admin_message_id в БД, потому что карточка опубликована
    # отдельно от обращения. В тексте таких сообщений в самом конце
    # стоит стабильный служебный маркер вида «🆔 №NNN» — ищем именно его.
    # Не использовать «Обращение #N» в качестве запасного: операторы в
    # админ-чате обсуждают обращения этой же фразой, и свайп на их
    # сообщение отправил бы текст случайному жителю.
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
            # ТОЛЬКО служебный маркер «🆔 №N» — его генерирует сам бот в
            # карточках /open_tickets и followup. Комбинация эмодзи и №
            # уникальна, в обычном тексте обращения не встречается.
            # Прежний «[appeal:N]» был стабилен по regex, но выглядел как
            # код; новый формат читаем оператором глазами.
            match = re.search(r"🆔 №(\d+)", replied_text)
            if match:
                appeal_id_from_text = int(match.group(1))

    if target_mid is None and appeal_id_from_text is None:
        log.info(
            "operator_reply: нет ссылки-ответа в event.message — сообщение проигнорировано "
            "(оператор написал в админ-группу без использования ответа/свайпа)"
        )
        return False

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


def register(dp: Dispatcher) -> None:
    """Пустышка (No-op): маршрутизация message_created управляется в handlers/appeal.py."""
    return None