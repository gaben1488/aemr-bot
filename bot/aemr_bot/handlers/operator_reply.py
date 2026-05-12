"""Логика ответов операторов и дополнительных сообщений от жителей, вызывается
из единого обработчика message_created в handlers/appeal.py.
"""

from __future__ import annotations

import hashlib
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
from aemr_bot.services import idempotency
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
# дубля. Запоминаем только УСПЕШНО завершённый ответ: до доставки и
# записи в БД этот guard не должен отравлять retry после технического
# сбоя.
_recent_replies: dict[tuple[int, int], tuple[str, float]] = {}
_REPLY_DEDUPE_WINDOW_SEC = 10.0


# Намерение оператора ответить на обращение хранится в едином
# хранилище services/wizard_registry — раньше существовало две копии
# (тут и в registry), что ломалось при `clear_all_for(operator_id)`
# из /cancel. Сейчас этот модуль — тонкий wrapper над registry для
# обратной совместимости (тесты и внешние вызовы импортируют отсюда).
_REPLY_INTENT_TTL_SEC = 300.0


def remember_reply_intent(operator_id: int, appeal_id: int) -> None:
    """Запомнить, что оператор сейчас собирается отвечать на обращение."""
    from aemr_bot.services import wizard_registry as _wr

    _wr.set_reply_intent(
        operator_id, appeal_id, _time.monotonic() + _REPLY_INTENT_TTL_SEC
    )


def consume_reply_intent(operator_id: int) -> int | None:
    """Достать и сбросить намерение, если оно ещё не протухло.

    Возвращает appeal_id, если оператор недавно нажимал «✉️ Ответить» и
    окно не истекло. Сбрасывает запись — intent одноразовое.
    """
    from aemr_bot.services import wizard_registry as _wr

    item = _wr.get_reply_intent(operator_id)
    if item is None:
        return None
    appeal_id, expires_at = item
    _wr.drop_reply_intent(operator_id)
    if _time.monotonic() > expires_at:
        return None
    return appeal_id


def drop_reply_intent(operator_id: int) -> int | None:
    """Сбросить намерение принудительно (кнопка «❌ Отменить ответ» или
    /cancel в админ-чате). Возвращает appeal_id, на который было
    нацелено, чтобы вызывающий код мог показать «отменено для #N»."""
    from aemr_bot.services import wizard_registry as _wr

    item = _wr.get_reply_intent(operator_id)
    if item is None:
        return None
    _wr.drop_reply_intent(operator_id)
    return item[0]


def _has_recent_successful_reply(operator_id: int, appeal_id: int, text: str) -> bool:
    """Проверить короткий UX-дедуп без изменения состояния.

    В старой реализации сама проверка сразу записывала ключ. Из-за этого
    retry после ошибки доставки или записи в БД мог быть отвергнут как
    дубль. Теперь проверка и фиксация разделены.
    """
    key = (operator_id, appeal_id)
    prev = _recent_replies.get(key)
    now = _time.monotonic()
    if prev is None:
        return False
    prev_text, prev_at = prev
    return prev_text == text and now - prev_at <= _REPLY_DEDUPE_WINDOW_SEC


def _remember_successful_reply(operator_id: int, appeal_id: int, text: str) -> None:
    """Запомнить только успешно завершённый ответ."""
    now = _time.monotonic()
    _recent_replies[(operator_id, appeal_id)] = (text, now)
    if len(_recent_replies) > 256:
        cutoff = now - _REPLY_DEDUPE_WINDOW_SEC * 6
        for k in list(_recent_replies.keys()):
            _, t = _recent_replies[k]
            if t < cutoff:
                _recent_replies.pop(k, None)


def _is_duplicate_reply(operator_id: int, appeal_id: int, text: str) -> bool:
    """Backward-compatible alias для тестов/старых импортов.

    Семантика теперь безопасная: функция только проверяет recent-success
    guard и не занимает ключ. Для записи успешного ответа использовать
    `_remember_successful_reply`.
    """
    return _has_recent_successful_reply(operator_id, appeal_id, text)


def _reply_success_key(
    event,
    *,
    operator_id: int,
    appeal_id: int,
    text: str,
) -> str | None:
    """Идемпотентность успешного ответа по source message/update.

    Ключ строится от входящего MAX-сообщения оператора, а не только от
    пары (оператор, обращение, текст). Поэтому повторная доставка того
    же update после успешной обработки будет отбита, но новое сообщение
    с тем же текстом после технического сбоя не будет заблокировано.
    """
    source_key = idempotency.build_idempotency_key(event)
    if source_key is None:
        return None
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    key = f"reply_ok:{operator_id}:{appeal_id}:{digest}:{source_key}"
    return key[: idempotency.MAX_KEY_LENGTH]


async def _is_reply_success_recorded(key: str | None) -> bool:
    if key is None:
        return False
    return await idempotency.has_processed_raw(key)


async def _mark_reply_success_recorded(key: str | None) -> None:
    if key is None:
        return
    await idempotency.try_mark_processed_raw(key, "reply_success")


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

    if _has_recent_successful_reply(operator.id, appeal.id, text):
        log.info(
            "operator_reply: дубль за %.1fс отбит (recent-success) — operator=%s appeal=%s",
            _REPLY_DEDUPE_WINDOW_SEC, operator.id, appeal.id,
        )
        return True

    success_key = _reply_success_key(
        event, operator_id=operator.id, appeal_id=appeal.id, text=text
    )
    if await _is_reply_success_recorded(success_key):
        log.info(
            "operator_reply: повтор уже успешно обработанного source-update отбит — operator=%s appeal=%s",
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

    try:
        async with session_scope() as session:
            appeal_full = await appeals_service.get_by_id(session, appeal.id)
            if appeal_full is None:
                log.warning(
                    "appeal #%s vanished between delivery and DB write", appeal.id
                )
                await event.bot.send_message(
                    chat_id=get_chat_id(event),
                    text=(
                        f"⚠️ Ответ по обращению #{appeal.id} доставлен жителю, "
                        f"но обращение исчезло перед записью в БД. "
                        f"Не повторяйте ответ вслепую; проверьте логи."
                    ),
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
    except Exception:
        log.exception(
            "operator_reply: delivered but local DB/audit write failed — appeal=%s delivered_mid=%s",
            appeal.id, delivered_mid,
        )
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=(
                f"⚠️ Ответ по обращению #{appeal.id} доставлен жителю, "
                f"но запись в базе или audit_log не завершилась. "
                f"Не повторяйте ответ вслепую; проверьте логи и состояние БД."
            ),
        )
        return True

    await _mark_reply_success_recorded(success_key)
    _remember_successful_reply(operator.id, appeal.id, text)

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
