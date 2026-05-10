"""Главный entry-point обработчика обращений.

После рефакторинга 2026-05-10 этот файл — тонкий dispatcher:
- `register(dp)` подключается из main.py при старте
- Внутри: один `@dp.message_callback()` (большой dispatch по payload)
  и один `@dp.message_created()` (state-таблица + admin-flow)

Реальная логика разнесена по 3 модулям:
- `appeal_runtime.py` — locks, `recover_stuck_funnels`, `persist_and_dispatch_appeal`
- `appeal_funnel.py` — FSM-шаги воронки (ask_*, on_awaiting_*) + followup
- `appeal_geo.py` — geo-flow (geo:* callbacks обрабатываются здесь
  внутри register(), но logic state-handler в appeal_geo)

`recover_stuck_funnels` ре-экспортируется отсюда — main.py делает
`from aemr_bot.handlers.appeal import recover_stuck_funnels` и не
должен знать о внутренней разбивке.
"""
from __future__ import annotations

import logging

from maxapi import Dispatcher
from maxapi.types import MessageCallback, MessageCreated

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.handlers import appeal_funnel, appeal_geo
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
# Импортируем functions из подмодулей.
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
        log.info("on_callback: user=%s payload_prefix=%s", max_user_id, prefix)

        # Коллбэки пользовательского флоу не должны срабатывать в
        # админ-группе. В админ-чате пропускаем только admin-flow:
        # broadcast:{confirm,abort,stop:N} и op:*.
        chat_id = get_chat_id(event)
        if cfg.admin_group_id and chat_id == cfg.admin_group_id:
            is_admin_callback = payload.startswith("op:") or (
                payload.startswith("broadcast:")
                and payload != "broadcast:unsubscribe"
            )
            if not is_admin_callback:
                await ack_callback(event)
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

            await event.bot.send_message(
                chat_id=get_chat_id(event),
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

            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.CANCELLED,
            )
            await open_main_menu(event)
            return

        if payload == "addr:reuse":
            await ack_callback(event)
            async with session_scope() as session:
                user = await users_service.get_or_create(
                    session, max_user_id=max_user_id
                )
                last = await appeals_service.find_last_address_for_user(
                    session, user.id
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
            try:
                idx = int(payload.split(":")[1])
            except (IndexError, ValueError):
                await ack_callback(event)
                return
            async with session_scope() as session:
                localities = await settings_store.get(session, "localities") or []
                if 0 <= idx < len(localities):
                    chosen = localities[idx]
                    await users_service.update_dialog_data(
                        session, max_user_id, {"locality": chosen}
                    )
                else:
                    await ack_callback(event)
                    log.warning(
                        "locality:%s out of range (have %d), user=%s",
                        idx, len(localities), max_user_id,
                    )
                    return
            await ack_callback(event)
            await appeal_funnel.ask_address(event, max_user_id)
            return

        # Подтверждение / редактирование определённого через геолокацию
        # адреса. Все три callback'а guard'им наличием detected_locality
        # в dialog_data — иначе это стейл-кнопка из старого сообщения,
        # ack'аем и молча пропускаем.
        if payload in ("geo:confirm", "geo:edit_address", "geo:other_locality"):
            await ack_callback(event)
            async with session_scope() as session:
                user = await users_service.get_or_create(
                    session, max_user_id=max_user_id
                )
                state = user.dialog_state
                data = dict(user.dialog_data or {})
            if state != DialogState.AWAITING_GEO_CONFIRM.value or not data.get(
                "detected_locality"
            ):
                log.info(
                    "geo callback %s ignored: state=%s, has_detected=%s, user=%s",
                    payload, state, bool(data.get("detected_locality")), max_user_id,
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
                    if full_addr:
                        await users_service.update_dialog_data(
                            session, max_user_id, {"address": full_addr}
                        )
                        await users_service.set_state(
                            session, max_user_id, DialogState.AWAITING_TOPIC
                        )
                    else:
                        await users_service.set_state(
                            session, max_user_id, DialogState.AWAITING_ADDRESS
                        )
                if full_addr:
                    await appeal_funnel.ask_topic(event, max_user_id)
                else:
                    await appeal_funnel.ask_address(event, max_user_id)
                return

            if payload == "geo:edit_address":
                async with session_scope() as session:
                    user = await users_service.get_or_create(
                        session, max_user_id=max_user_id
                    )
                    fresh = dict(user.dialog_data or {})
                    for k in ("detected_street", "detected_house_number"):
                        fresh.pop(k, None)
                    user.dialog_data = fresh
                    await session.flush()
                    await users_service.set_state(
                        session, max_user_id, DialogState.AWAITING_ADDRESS
                    )
                await appeal_funnel.ask_address(event, max_user_id)
                return

            if payload == "geo:other_locality":
                async with session_scope() as session:
                    user = await users_service.get_or_create(
                        session, max_user_id=max_user_id
                    )
                    fresh = dict(user.dialog_data or {})
                    for k in (
                        "locality",
                        "detected_locality",
                        "detected_street",
                        "detected_house_number",
                        "detected_lat",
                        "detected_lon",
                        "detected_confidence",
                    ):
                        fresh.pop(k, None)
                    user.dialog_data = fresh
                    await session.flush()
                await appeal_funnel.ask_locality(event, max_user_id)
                return

        if payload.startswith("topic:"):
            try:
                idx = int(payload.split(":")[1])
            except (IndexError, ValueError):
                await ack_callback(event)
                return
            async with session_scope() as session:
                topics = await settings_store.get(session, "topics") or []
                if 0 <= idx < len(topics):
                    chosen = topics[idx]
                    await users_service.update_dialog_data(
                        session, max_user_id, {"topic": chosen}
                    )
                else:
                    await ack_callback(event)
                    log.warning(
                        "topic:%s out of range (have %d), user=%s",
                        idx, len(topics), max_user_id,
                    )
                    return
            await ack_callback(event)
            await appeal_funnel.ask_summary(event, max_user_id)
            return

        if payload == "appeal:submit":
            # Кнопка «Отправить» осталась в старых сообщениях клиента,
            # которые ещё могут крутиться у жителя в чате. Финализируем.
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
                try:
                    bid = int(payload.split(":", 2)[2])
                except (IndexError, ValueError):
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
                try:
                    aid = int(payload.split(":", 2)[2])
                except (IndexError, ValueError):
                    await ack_callback(event)
                    return
                await admin_commands.run_reply_intent(event, aid)
                return
            if payload == "op:reply_cancel":
                await admin_commands.run_reply_cancel(event)
                return
            if payload.startswith("op:reopen:"):
                try:
                    aid = int(payload.split(":", 2)[2])
                except (IndexError, ValueError):
                    await ack_callback(event)
                    return
                await admin_commands.run_reopen(event, aid)
                return
            if payload.startswith("op:close:"):
                try:
                    aid = int(payload.split(":", 2)[2])
                except (IndexError, ValueError):
                    await ack_callback(event)
                    return
                await admin_commands.run_close(event, aid)
                return
            if payload.startswith("op:erase:"):
                try:
                    aid = int(payload.split(":", 2)[2])
                except (IndexError, ValueError):
                    await ack_callback(event)
                    return
                await admin_commands.run_erase_for_appeal(event, aid)
                return
            if payload.startswith("op:block:"):
                try:
                    aid = int(payload.split(":", 2)[2])
                except (IndexError, ValueError):
                    await ack_callback(event)
                    return
                await admin_commands.run_block_for_appeal(event, aid, blocked=True)
                return
            if payload.startswith("op:unblock:"):
                try:
                    aid = int(payload.split(":", 2)[2])
                except (IndexError, ValueError):
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
                event, text_body
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
                "reply", "reopen", "close", "stats", "broadcast", "erase",
                "setting", "add_operators", "backup", "diag", "op_help",
                "open_tickets",
            }
            citizen = {
                "start", "menu", "help", "policy", "subscribe",
                "unsubscribe", "forget", "cancel",
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
