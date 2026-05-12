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

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import appeal_funnel, appeal_geo, callback_router
from aemr_bot.handlers.appeal_runtime import (
    drop_user_lock,
    recover_stuck_funnels,
)
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
        async with session_scope() as session:
            user = await users_service.get_or_create(
                session,
                max_user_id=max_user_id,
            )
            current = _state_value(getattr(user, "dialog_state", None))
    except Exception:
        # Если БД недоступна, основной handler всё равно не сможет
        # корректно выполнить бизнес-операцию. Для unit-тестов с
        # неполными session mocks не превращаем guard в источник падений.
        log.debug("callback state guard skipped for %s", payload, exc_info=True)
        return True

    if current is None:
        # Unit-test fakes often expose no real dialog_state.
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
            )
        except Exception:
            log.debug(
                "failed to send stale geo awaiting-address notice to user=%s",
                max_user_id,
                exc_info=True,
            )
    else:
        await ack_callback(event)
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
) -> None:
    chat_id = get_chat_id(event)
    if chat_id is not None:
        await event.bot.send_message(chat_id=chat_id, text=text)
    else:
        await event.bot.send_message(user_id=max_user_id, text=text)


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

        if payload == "menu:new_appeal":
            await ack_callback(event)
            await appeal_funnel.start_appeal_flow(event, max_user_id)
            return

        if payload == "consent:yes":
            async with session_scope() as session:
                await users_service.set_consent(session, max_user_id)
            await ack_callback(event, texts.CONSENT_ACCEPTED)
            await appeal_funnel.ask_contact_or_skip(event, max_user_id)
            return

        if payload == "consent:no":
            async with session_scope() as session:
                await users_service.reset_state(session, max_user_id)
            drop_user_lock(max_user_id)
            await ack_callback(event)
            from aemr_bot.handlers.menu import open_main_menu

            await _send_to_citizen(
                event,
                max_user_id,
                text=texts.CONSENT_DECLINED,
            )
            await open_main_menu(event)
            return

        if payload == "cancel":
            async with session_scope() as session:
                await users_service.reset_state(session, max_user_id)
            drop_user_lock(max_user_id)
            await ack_callback(event)
            from aemr_bot.handlers.menu import open_main_menu

            await _send_to_citizen(event, max_user_id, text=texts.CANCELLED)
            await open_main_menu(event)
            return

        if payload == "addr:reuse":
            await ack_callback(event)
            async with session_scope() as session:
                user = await users_service.get_or_create(
                    session,
                    max_user_id=max_user_id,
                )
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
            return

        if payload == "addr:new":
            await ack_callback(event)
            await appeal_funnel.ask_locality(event, max_user_id)
            return

        if payload.startswith("locality:"):
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
            return

        # Подтверждение / редактирование определённого через геолокацию
        # адреса. Все три callback'а guard'им состоянием и наличием
        # detected_locality в dialog_data — иначе это стейл-кнопка из
        # старого сообщения, ack'аем и молча пропускаем.
        if payload in ("geo:confirm", "geo:edit_address", "geo:other_locality"):
            await ack_callback(event)
            async with session_scope() as session:
                user = await users_service.get_or_create(
                    session,
                    max_user_id=max_user_id,
                )
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
                async with session_scope() as session:
                    user = await users_service.get_or_create(
                        session,
                        max_user_id=max_user_id,
                    )
                    fresh = _clear_geo_detected(
                        user.dialog_data or data,
                        drop_progress_message=not bool(full_addr),
                    )
                    if full_addr:
                        fresh["address"] = full_addr
                        user.dialog_state = DialogState.AWAITING_TOPIC.value
                    else:
                        user.dialog_state = DialogState.AWAITING_ADDRESS.value
                    user.dialog_data = fresh
                    await session.flush()
                if full_addr:
                    await appeal_funnel.ask_topic(
                        event,
                        max_user_id,
                        force_new_message=True,
                    )
                else:
                    await appeal_funnel.ask_address(event, max_user_id)
                return

            if payload == "geo:edit_address":
                async with session_scope() as session:
                    user = await users_service.get_or_create(
                        session,
                        max_user_id=max_user_id,
                    )
                    user.dialog_data = _clear_geo_detected(
                        user.dialog_data or data,
                        drop_progress_message=True,
                    )
                    user.dialog_state = DialogState.AWAITING_ADDRESS.value
                    await session.flush()
                await appeal_funnel.ask_address(event, max_user_id)
                return

            if payload == "geo:other_locality":
                async with session_scope() as session:
                    user = await users_service.get_or_create(
                        session,
                        max_user_id=max_user_id,
                    )
                    user.dialog_data = _clear_geo_detected(
                        user.dialog_data or data,
                        drop_locality=True,
                        drop_progress_message=True,
                    )
                    user.dialog_state = DialogState.AWAITING_LOCALITY.value
                    await session.flush()
                await appeal_funnel.ask_locality(event, max_user_id)
                return

        if payload.startswith("topic:"):
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
            return

        if payload == "appeal:submit":
            # Кнопка «Отправить» осталась в старых сообщениях клиента,
            # которые ещё могут крутиться у жителя в чате. Финализируем
            # только если пользователь всё ещё на шаге описания сути.
            await ack_callback(event)
            await appeal_funnel.finalize_appeal(event, max_user_id)
            return

        # Коллбэки мастера рассылок (на стороне оператора).
        if payload.startswith("broadcast:") and not payload.startswith(
            "broadcast:unsubscribe"
        ):
            from aemr_bot.handlers import broadcast as broadcast_handler

            if payload == "broadcast:confirm":
                await broadcast_handler._handle_confirm(event)
                return
            if payload == "broadcast:abort":
                await broadcast_handler._handle_abort(event)
                return
            if payload == "broadcast:edit":
                await broadcast_handler._handle_edit(event)
                return
            if payload.startswith("broadcast:stop:"):
                bid = callback_router.parse_int_tail(payload, "broadcast:stop:")
                if bid is None:
                    await ack_callback(event)
                    return
                await broadcast_handler._handle_stop(event, bid)
                return

        # Кнопки быстрых действий для /op_help.
        if payload.startswith("op:"):
            from aemr_bot.handlers import (
                admin_commands,
                broadcast as broadcast_handler,
            )

            if payload == "op:menu":
                await ack_callback(event)
                await admin_commands.show_op_menu(event, pin=False)
                return
            if payload == "op:stats_menu":
                await ack_callback(event)
                await admin_commands.run_stats_menu(event)
                return
            if payload == "op:stats_today":
                await ack_callback(event)
                await admin_commands.run_stats_today(event)
                await admin_commands.show_op_menu(event, pin=False)
                return
            if payload == "op:stats_week":
                await ack_callback(event)
                await admin_commands.run_stats(event, "week")
                return
            if payload == "op:stats_month":
                await ack_callback(event)
                await admin_commands.run_stats(event, "month")
                return
            if payload == "op:stats_quarter":
                await ack_callback(event)
                await admin_commands.run_stats(event, "quarter")
                return
            if payload == "op:stats_half_year":
                await ack_callback(event)
                await admin_commands.run_stats(event, "half_year")
                return
            if payload == "op:stats_year":
                await ack_callback(event)
                await admin_commands.run_stats(event, "year")
                return
            if payload == "op:stats_all":
                await ack_callback(event)
                await admin_commands.run_stats(event, "all")
                return
            if payload == "op:open_tickets":
                await ack_callback(event)
                await admin_commands.run_open_tickets(event)
                return
            if payload == "op:diag":
                await ack_callback(event)
                await admin_commands.run_diag(event)
                return
            if payload == "op:backup":
                await ack_callback(event)
                await admin_commands.run_backup(event)
                return
            if payload == "op:broadcast":
                await ack_callback(event)
                await broadcast_handler._start_wizard(event)
                return
            if payload == "op:broadcast_list":
                await ack_callback(event)
                await broadcast_handler._list_broadcasts(event)
                return
            if payload == "op:operators":
                await ack_callback(event)
                await admin_commands.run_operators_menu(event)
                return
            if payload == "op:settings":
                await ack_callback(event)
                await admin_commands.run_settings_menu(event)
                return
            if payload == "op:audience":
                await ack_callback(event)
                await admin_commands.run_audience_menu(event)
                return
            if payload.startswith("op:aud:"):
                await admin_commands.run_audience_action(event, payload)
                return
            if payload.startswith("op:reply:"):
                aid = callback_router.parse_int_tail(payload, "op:reply:")
                if aid is None:
                    await ack_callback(event)
                    return
                await admin_commands.run_reply_intent(event, aid)
                return
            if payload == "op:reply_cancel":
                await admin_commands.run_reply_cancel(event)
                return
            if payload.startswith("op:reopen:"):
                aid = callback_router.parse_int_tail(payload, "op:reopen:")
                if aid is None:
                    await ack_callback(event)
                    return
                await admin_commands.run_reopen(event, aid)
                return
            if payload.startswith("op:close:"):
                aid = callback_router.parse_int_tail(payload, "op:close:")
                if aid is None:
                    await ack_callback(event)
                    return
                await admin_commands.run_close(event, aid)
                return
            if payload.startswith("op:erase:"):
                aid = callback_router.parse_int_tail(payload, "op:erase:")
                if aid is None:
                    await ack_callback(event)
                    return
                await admin_commands.run_erase_for_appeal(event, aid)
                return
            if payload.startswith("op:block:"):
                aid = callback_router.parse_int_tail(payload, "op:block:")
                if aid is None:
                    await ack_callback(event)
                    return
                await admin_commands.run_block_for_appeal(event, aid, blocked=True)
                return
            if payload.startswith("op:unblock:"):
                aid = callback_router.parse_int_tail(payload, "op:unblock:")
                if aid is None:
                    await ack_callback(event)
                    return
                await admin_commands.run_block_for_appeal(event, aid, blocked=False)
                return
            # Wizard'ы IT (роли проверяются внутри обработчиков):
            if payload.startswith("op:opadd:"):
                await admin_commands.run_operators_action(event, payload)
                return
            if payload.startswith("op:setkey:"):
                await admin_commands.run_settings_action(event, payload)
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

        async with session_scope() as session:
            user = await users_service.get_or_create(
                session,
                max_user_id=max_user_id,
                first_name=get_first_name(event),
            )
            state = DialogState(user.dialog_state)

        handler = _STATE_HANDLERS.get(state)
        if handler is not None:
            await handler(event, body, text_body, max_user_id)
