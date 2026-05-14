"""Главный entry-point обработчика обращений.

После рефакторинга 2026-05-10 этот файл — тонкий dispatcher:
- `register(dp)` подключается из main.py при старте
- Внутри: один `@dp.message_callback()` (dispatch по payload)
  и один `@dp.message_created()` (state-таблица + admin-flow)

Реальная логика разнесена по 4 модулям:
- `appeal_runtime.py` — locks, `recover_stuck_funnels`, `persist_and_dispatch_appeal`
- `appeal_funnel.py` — FSM-шаги воронки (ask_*, on_awaiting_*) + followup
- `appeal_geo.py` — geo-flow
- `callback_router.py` — реестр callback-групп, чат-контекст и безопасный parse id

`recover_stuck_funnels` ре-экспортируется отсюда — main.py делает
`from aemr_bot.handlers.appeal import recover_stuck_funnels` и не
должен знать о внутренней разбивке.
"""
from __future__ import annotations

import logging
from typing import Any

from maxapi import Dispatcher
from maxapi.types import MessageCallback, MessageCreated

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import (
    admin_callback_dispatch,
    appeal_funnel,
    appeal_geo,
    callback_router,
)
from aemr_bot.handlers._common import current_user
from aemr_bot.handlers.appeal_runtime import (
    drop_user_lock,
    recover_stuck_funnels,
)
from aemr_bot.services import admin_events
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import (
    ack_callback,
    get_chat_id,
    get_first_name,
    get_message_body,
    get_message_text,
    get_payload,
    get_user_id,
    send_or_edit_screen,
)

log = logging.getLogger(__name__)


# Re-export для обратной совместимости с main.py.
__all__ = ["register", "recover_stuck_funnels"]


# State-таблица: какой handler вызывать в каком DialogState когда
# житель прислал что-то нерелевантное (текст вместо кнопки и т.п.).
_STATE_HANDLERS = {
    DialogState.AWAITING_CONSENT: appeal_funnel.on_awaiting_consent,
    DialogState.AWAITING_CONTACT: appeal_funnel.on_awaiting_contact,
    DialogState.AWAITING_NAME: appeal_funnel.on_awaiting_name,
    DialogState.AWAITING_LOCALITY: appeal_geo.on_awaiting_locality,
    DialogState.AWAITING_GEO_CONFIRM: appeal_geo.on_awaiting_geo_confirm,
    DialogState.AWAITING_ADDRESS: appeal_funnel.on_awaiting_address,
    DialogState.AWAITING_TOPIC: appeal_funnel.on_awaiting_topic,
    DialogState.AWAITING_SUMMARY: appeal_funnel.on_awaiting_summary,
    DialogState.AWAITING_FOLLOWUP_TEXT: appeal_funnel.on_awaiting_followup_text,
    DialogState.IDLE: appeal_funnel.on_idle,
}

_GEO_DETECTED_KEYS = (
    "detected_locality",
    "detected_street",
    "detected_house_number",
    "detected_lat",
    "detected_lon",
    "detected_confidence",
)

_STALE_CALLBACK_NOTICE = "Эта карточка уже не актуальна. Используйте текущий шаг."
_GEO_AWAITING_ADDRESS_NOTICE = "Я уже жду адрес текстом. Введите адрес сообщением."


def _state_value(raw: Any) -> str | None:
    if isinstance(raw, DialogState):
        return raw.value
    if isinstance(raw, str):
        return raw
    return None


def _expected_funnel_callback_states(payload: str) -> tuple[DialogState, ...]:
    """Ожидаемые состояния для пользовательских inline-кнопок воронки.

    MAX-клиент может хранить старые карточки. Пользователь способен
    нажать старую кнопку выбора темы/поселения уже после перехода в
    другой шаг. Поэтому кнопки, которые меняют FSM, должны работать
    только из своего состояния.
    """
    if payload in {"consent:yes", "consent:no"}:
        return (DialogState.AWAITING_CONSENT,)
    if payload in {"addr:reuse", "addr:new"}:
        # reuse-prompt может быть показан после AWAITING_NAME или после
        # ask_contact_or_skip(), где состояние уже AWAITING_LOCALITY.
        return (DialogState.AWAITING_NAME, DialogState.AWAITING_LOCALITY)
    if payload.startswith("locality:"):
        return (DialogState.AWAITING_LOCALITY,)
    if payload in {"geo:confirm", "geo:edit_address", "geo:other_locality"}:
        return (DialogState.AWAITING_GEO_CONFIRM,)
    if payload.startswith("topic:"):
        return (DialogState.AWAITING_TOPIC,)
    if payload == "appeal:submit":
        return (DialogState.AWAITING_SUMMARY,)
    return ()


async def _ensure_funnel_callback_state(
    event: MessageCallback,
    max_user_id: int,
    payload: str,
) -> bool:
    expected = _expected_funnel_callback_states(payload)
    if not expected:
        return True

    try:
        async with current_user(max_user_id) as (_, user):
            current = _state_value(getattr(user, "dialog_state", None))
    except Exception:
        # Если БД недоступна, основной handler всё равно не сможет
        # корректно выполнить бизнес-операцию. Для unit-тестов с
        # неполными session mocks не превращаем guard в источник падений.
        log.debug("callback state guard skipped for %s", payload, exc_info=True)
        return True

    if current is None:
        # Тестовые заглушки иногда не содержат настоящего dialog_state.
        return True

    expected_values = {state.value for state in expected}
    if current in expected_values:
        return True

    log.info(
        "stale citizen callback ignored: payload=%s state=%s expected=%s user=%s",
        payload,
        current,
        sorted(expected_values),
        max_user_id,
    )
    if payload.startswith("geo:") and current == DialogState.AWAITING_ADDRESS.value:
        await ack_callback(event, _GEO_AWAITING_ADDRESS_NOTICE)
        try:
            await _send_to_citizen(
                event,
                max_user_id,
                text=_GEO_AWAITING_ADDRESS_NOTICE,
                attachments=[keyboards.back_to_menu_keyboard()],
            )
        except Exception:
            log.debug(
                "failed to send stale geo awaiting-address notice to user=%s",
                max_user_id,
                exc_info=True,
            )
    else:
        await ack_callback(event, _STALE_CALLBACK_NOTICE)
    return False


def _clear_geo_detected(
    data: dict | None,
    *,
    drop_locality: bool = False,
    drop_progress_message: bool = False,
) -> dict:
    cleaned = dict(data or {})
    for key in _GEO_DETECTED_KEYS:
        cleaned.pop(key, None)
    if drop_locality:
        cleaned.pop("locality", None)
    if drop_progress_message:
        # После geo-confirm старый progress_message_id обычно указывает
        # на карточку выбора населённого пункта выше по чату. Если его
        # оставить, следующий шаг редактируется в старом сообщении, а
        # житель продолжает видеть активную geo-карточку как «зависшую».
        cleaned.pop("progress_message_id", None)
    return cleaned


async def _send_to_citizen(
    event: MessageCallback,
    max_user_id: int,
    *,
    text: str,
    attachments: list | None = None,
) -> None:
    await send_or_edit_screen(
        event,
        user_id=max_user_id,
        text=text,
        attachments=attachments or [],
    )


# ============================================================================
# Callback'и воронки жителя — именованные handler'ы
# ============================================================================
# Вынесены из on_callback (раньше — ~195-строчный if-elif). on_callback
# стал тонким диспетчером (_dispatch_citizen_callback ниже). Каждый _cb_*
# принимает (event, max_user_id, payload): префиксные (locality:/topic:)
# разбирают payload, точные — игнорируют третий аргумент.
#
# _cb_* — функции этого же модуля, поэтому в таблицах _CITIZEN_EXACT /
# _CITIZEN_PREFIX лежат прямые ссылки: тесты патчат не их, а то, что
# внутри (appeal_funnel.*, users_service.*, ack_callback) — а это
# резолвится в момент вызова.


async def _cb_new_appeal(event, max_user_id: int, payload: str) -> None:
    await ack_callback(event)
    await appeal_funnel.start_appeal_flow(event, max_user_id)


async def _cb_consent_yes(event, max_user_id: int, payload: str) -> None:
    async with session_scope() as session:
        await users_service.set_consent(session, max_user_id)
    await ack_callback(event, texts.CONSENT_ACCEPTED)
    await admin_events.notify_consent_given(event.bot, max_user_id=max_user_id)
    await appeal_funnel.ask_contact_or_skip(event, max_user_id)


async def _cb_consent_no(event, max_user_id: int, payload: str) -> None:
    async with session_scope() as session:
        await users_service.reset_state(session, max_user_id)
    drop_user_lock(max_user_id)
    await ack_callback(event)
    await _send_to_citizen(
        event,
        max_user_id,
        text=texts.CONSENT_DECLINED,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def _cb_cancel(event, max_user_id: int, payload: str) -> None:
    async with session_scope() as session:
        await users_service.reset_state(session, max_user_id)
    drop_user_lock(max_user_id)
    await ack_callback(event)
    await _send_to_citizen(
        event,
        max_user_id,
        text=texts.CANCELLED,
        attachments=[keyboards.back_to_menu_keyboard()],
    )


async def _cb_addr_reuse(event, max_user_id: int, payload: str) -> None:
    await ack_callback(event)
    async with current_user(max_user_id) as (session, user):
        last = await appeals_service.find_last_address_for_user(
            session,
            user.id,
        )
    if last is None:
        # Между показом промпта и кликом обращение могло быть
        # обезличено retention-кроном — fallback к обычному пути.
        await appeal_funnel.ask_locality(event, max_user_id)
        return
    locality, address = last
    async with session_scope() as session:
        await users_service.set_state(
            session,
            max_user_id,
            DialogState.AWAITING_TOPIC,
            data={"locality": locality, "address": address},
        )
    await appeal_funnel.ask_topic(event, max_user_id)


async def _cb_addr_new(event, max_user_id: int, payload: str) -> None:
    await ack_callback(event)
    await appeal_funnel.ask_locality(event, max_user_id)


async def _cb_locality(event, max_user_id: int, payload: str) -> None:
    idx = callback_router.parse_int_tail(payload, "locality:")
    if idx is None:
        await ack_callback(event)
        return
    async with session_scope() as session:
        localities = await settings_store.get(session, "localities") or []
        if 0 <= idx < len(localities):
            chosen = localities[idx]
            await users_service.update_dialog_data(
                session,
                max_user_id,
                {"locality": chosen},
            )
        else:
            await ack_callback(event)
            log.warning(
                "locality:%s out of range (have %d), user=%s",
                idx,
                len(localities),
                max_user_id,
            )
            return
    await ack_callback(event)
    await appeal_funnel.ask_address(event, max_user_id)


async def _cb_geo(event, max_user_id: int, payload: str) -> None:
    # Подтверждение / редактирование определённого через геолокацию
    # адреса. Все три callback'а guard'им состоянием и наличием
    # detected_locality в dialog_data — иначе это стейл-кнопка из
    # старого сообщения.
    await ack_callback(event)
    async with current_user(max_user_id) as (_, user):
        state = user.dialog_state
        data = dict(user.dialog_data or {})
    if state != DialogState.AWAITING_GEO_CONFIRM.value or not data.get(
        "detected_locality"
    ):
        log.info(
            "geo callback %s ignored: state=%s, has_detected=%s, user=%s",
            payload,
            state,
            bool(data.get("detected_locality")),
            max_user_id,
        )
        return

    if payload == "geo:confirm":
        detected_street = (data.get("detected_street") or "").strip()
        detected_house = (data.get("detected_house_number") or "").strip()
        if detected_street and detected_house:
            full_addr = f"{detected_street}, д. {detected_house}"
        elif detected_street:
            full_addr = detected_street
        else:
            full_addr = ""
        async with current_user(max_user_id) as (session, user):
            fresh = _clear_geo_detected(user.dialog_data or data)
            if full_addr:
                fresh["address"] = full_addr
                user.dialog_state = DialogState.AWAITING_TOPIC.value
            else:
                user.dialog_state = DialogState.AWAITING_ADDRESS.value
            user.dialog_data = fresh
            await session.flush()
        if full_addr:
            await appeal_funnel.ask_topic(event, max_user_id)
        else:
            await appeal_funnel.ask_address(event, max_user_id)
        return

    if payload == "geo:edit_address":
        async with current_user(max_user_id) as (session, user):
            user.dialog_data = _clear_geo_detected(user.dialog_data or data)
            user.dialog_state = DialogState.AWAITING_ADDRESS.value
            await session.flush()
        await appeal_funnel.ask_address(event, max_user_id)
        return

    if payload == "geo:other_locality":
        async with current_user(max_user_id) as (session, user):
            user.dialog_data = _clear_geo_detected(
                user.dialog_data or data,
                drop_locality=True,
            )
            user.dialog_state = DialogState.AWAITING_LOCALITY.value
            await session.flush()
        await appeal_funnel.ask_locality(event, max_user_id)
        return


async def _cb_topic(event, max_user_id: int, payload: str) -> None:
    idx = callback_router.parse_int_tail(payload, "topic:")
    if idx is None:
        await ack_callback(event)
        return
    async with session_scope() as session:
        topics = await settings_store.get(session, "topics") or []
        if 0 <= idx < len(topics):
            chosen = topics[idx]
            await users_service.update_dialog_data(
                session,
                max_user_id,
                {"topic": chosen},
            )
        else:
            await ack_callback(event)
            log.warning(
                "topic:%s out of range (have %d), user=%s",
                idx,
                len(topics),
                max_user_id,
            )
            return
    await ack_callback(event)
    await appeal_funnel.ask_summary(event, max_user_id)


async def _cb_appeal_submit(event, max_user_id: int, payload: str) -> None:
    # Кнопка «Отправить» осталась в старых сообщениях клиента, которые
    # ещё могут крутиться у жителя в чате. Финализируем только если
    # пользователь всё ещё на шаге описания сути.
    await ack_callback(event)
    await appeal_funnel.finalize_appeal(event, max_user_id)


# Точные payload'ы воронки → handler. geo:* — три payload'а на один
# _cb_geo (он сам различает их внутри по payload).
_CITIZEN_EXACT = {
    "menu:new_appeal": _cb_new_appeal,
    "consent:yes": _cb_consent_yes,
    "consent:no": _cb_consent_no,
    "cancel": _cb_cancel,
    "addr:reuse": _cb_addr_reuse,
    "addr:new": _cb_addr_new,
    "geo:confirm": _cb_geo,
    "geo:edit_address": _cb_geo,
    "geo:other_locality": _cb_geo,
    "appeal:submit": _cb_appeal_submit,
}

# Префикс payload'а → handler. Числовой хвост парсит сам handler через
# callback_router.parse_int_tail.
_CITIZEN_PREFIX = (
    ("locality:", _cb_locality),
    ("topic:", _cb_topic),
)


async def _dispatch_citizen_callback(
    event, max_user_id: int, payload: str
) -> bool:
    """Маршрутизатор callback'ов воронки жителя. Возвращает True, если
    payload обработан; False — если это не воронка-callback, и вызывающий
    продолжает разбор (admin-dispatch, затем menu.handle_callback)."""
    handler = _CITIZEN_EXACT.get(payload)
    if handler is None:
        for prefix, prefix_handler in _CITIZEN_PREFIX:
            if payload.startswith(prefix):
                handler = prefix_handler
                break
    if handler is None:
        return False
    await handler(event, max_user_id, payload)
    return True


def register(dp: Dispatcher) -> None:
    @dp.message_callback()
    async def on_callback(event: MessageCallback):
        payload = get_payload(event)
        max_user_id = get_user_id(event)
        if max_user_id is None:
            log.warning("коллбэк без user_id, payload=%r — пропущен", payload)
            return
        # Только префикс payload в info — полный payload может содержать
        # appeal_id жителя или другие идентификаторы. Полный — debug.
        prefix = payload.split(":", 1)[0] if payload else ""
        log.debug("on_callback: user=%s payload=%r", max_user_id, payload)
        if prefix == "geo":
            log.info("on_callback: user=%s payload=%s", max_user_id, payload)
        else:
            log.info("on_callback: user=%s payload_prefix=%s", max_user_id, prefix)

        # Коллбэки пользовательского флоу не должны срабатывать в
        # админ-группе. В админ-чате пропускаем только admin-flow, который
        # явно перечислен в callback_router.EXACT_ROUTES/PREFIX_ROUTES.
        chat_id = get_chat_id(event)
        if cfg.admin_group_id and chat_id == cfg.admin_group_id:
            if not callback_router.is_admin_callback(payload):
                await ack_callback(event)
                return

        if not await _ensure_funnel_callback_state(event, max_user_id, payload):
            return

        # Коллбэки воронки жителя (menu:new_appeal, consent:*, cancel,
        # addr:*, locality:, geo:*, topic:, appeal:submit) — единая
        # dispatch-таблица (_dispatch_citizen_callback + _cb_* выше).
        # Раньше здесь был ~195-строчный if-elif. dispatch вернёт True,
        # если обработал; False — если payload не воронка-callback,
        # тогда продолжаем fallthrough в admin-dispatch и menu.
        if await _dispatch_citizen_callback(event, max_user_id, payload):
            return

        # Коллбэки мастера рассылок (на стороне оператора).
        # Admin/operator callback'и (broadcast:* / op:*) — единая
        # dispatch-таблица в handlers/admin_callback_dispatch.py.
        # Раньше здесь был ~155-строчный if-elif. dispatch вернёт True,
        # если обработал; False — если payload не admin-callback (или
        # `op:`/`broadcast:` с неизвестным хвостом), тогда продолжаем
        # обычный fallthrough в menu.handle_callback — поведение
        # сохранено в точности (см. docstring диспетчера).
        if await admin_callback_dispatch.dispatch_admin_callback(event, payload):
            return

        # Переход к обработчикам меню/контактов/просмотра обращений
        from aemr_bot.handlers import menu as menu_handlers

        await menu_handlers.handle_callback(event, payload, max_user_id)

    @dp.message_created()
    async def on_message(event: MessageCreated):
        from aemr_bot.handlers import operator_reply as op_reply

        chat_id = get_chat_id(event)
        if chat_id is None:
            log.warning("message_created без chat_id — event.get_ids() вернул None")
            return

        text_body = get_message_text(event)
        body = get_message_body(event)

        if cfg.admin_group_id and chat_id == cfg.admin_group_id:
            from aemr_bot.handlers import (
                admin_commands as admin_cmd_module,
                broadcast as broadcast_handler,
            )

            # /cancel в админ-чате — глобальный сброс
            if text_body.strip().lower() in ("/cancel", "/cancel@aemo_chat_bot"):
                operator_id = get_user_id(event)
                if operator_id is not None:
                    broadcast_handler._wizards.pop(operator_id, None)
                    admin_cmd_module._op_wizards.pop(operator_id, None)
                    op_reply.drop_reply_intent(operator_id)
                await event.bot.send_message(
                    chat_id=cfg.admin_group_id,
                    text="Текущие мастера и черновики ответа сброшены.",
                    attachments=[keyboards.op_back_to_menu_keyboard()],
                )
                return

            consumed = await broadcast_handler._handle_wizard_text(event, text_body)
            if consumed:
                return
            consumed = await admin_cmd_module.handle_operators_wizard_text(
                event,
                text_body,
            )
            if consumed:
                return
            if text_body.startswith("/"):
                return
            await op_reply.handle_operator_reply(event, body, text_body)
            return

        # Личные сообщения гражданина: текст со слэшем не дошёл ни до
        # одного зарегистрированного хендлера команды.
        if text_body.startswith("/"):
            head = text_body.split(maxsplit=1)[0]
            cmd = head.lstrip("/").split("@", 1)[0].lower()
            operator_only = {
                "reply",
                "reopen",
                "close",
                "stats",
                "broadcast",
                "erase",
                "setting",
                "add_operators",
                "backup",
                "diag",
                "op_help",
                "open_tickets",
            }
            citizen = {
                "start",
                "menu",
                "help",
                "policy",
                "subscribe",
                "unsubscribe",
                "forget",
                "cancel",
            }
            if cmd in operator_only:
                await event.message.answer(
                    "Эта команда работает только в служебной группе у "
                    "операторов. Жителю она недоступна. Откройте /menu "
                    "или /help — там что доступно вам."
                )
            elif cmd not in citizen:
                await event.message.answer(
                    f"Команда /{cmd} не распознана. Откройте /menu или /help — "
                    f"там полный список доступных команд."
                )
            return

        max_user_id = get_user_id(event)
        if max_user_id is None:
            return

        async with current_user(
            max_user_id, first_name=get_first_name(event)
        ) as (_, user):
            state = DialogState(user.dialog_state)

        handler = _STATE_HANDLERS.get(state)
        if handler is not None:
            await handler(event, body, text_body, max_user_id)
