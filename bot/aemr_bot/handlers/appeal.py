from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from maxapi import Dispatcher
from maxapi.types import MessageCallback, MessageCreated

from aemr_bot import keyboards, texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import card_format
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.attachments import (
    collect_attachments,
    deserialize_for_relay,
    extract_contact_name,
    extract_phone,
)
from aemr_bot.utils.event import (
    ack_callback,
    extract_message_id,
    get_chat_id,
    get_first_name,
    get_message_body,
    get_message_text,
    get_payload,
    get_user_id,
)

log = logging.getLogger(__name__)

# Имя жителя / адрес должны содержать хотя бы один буквенно-цифровой символ — это защищает
# от отправки "👍", "...", "`````" и подобных бессмысленных сообщений (состоящих из одного символа).
_HAS_ALNUM = re.compile(r"[A-Za-zА-Яа-яЁё0-9]")

_user_locks: dict[int, asyncio.Lock] = {}


def _get_user_lock(max_user_id: int) -> asyncio.Lock:
    """Блокировка для каждого пользователя, чтобы параллельные пути отправки/отмены/таймера
    не приводили к двойной диспетчеризации.

    Только для одного экземпляра приложения — при горизонтальном масштабировании
    потребуется pg_advisory_xact_lock или блокировка через Redis.
    """
    lock = _user_locks.get(max_user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[max_user_id] = lock
    return lock


def _drop_user_lock(max_user_id: int) -> None:
    """Освобождает объект блокировки после полного завершения воронки. Предотвращает
    бесконечное разрастание словаря `_user_locks` по мере прохождения пользователей
    через бота. Безопасно вызывать, когда никто не удерживает блокировку — операция dict-pop
    идемпотентна."""
    lock = _user_locks.get(max_user_id)
    if lock is not None and not lock.locked():
        _user_locks.pop(max_user_id, None)


async def recover_stuck_funnels(bot) -> int:
    """Завершает воронки, оставшиеся в состоянии AWAITING_SUMMARY после перезапуска. Запускается один раз при старте."""
    async with session_scope() as session:
        ids = await users_service.find_stuck_in_summary(
            session, idle_seconds=cfg.appeal_collect_timeout_seconds
        )
    if not ids:
        return 0

    results = await asyncio.gather(
        *(_persist_and_dispatch_appeal(bot, uid) for uid in ids),
        return_exceptions=True,
    )

    # Пустые обращения никогда не получают повторный запрос при восстановлении — сбрасываем их
    # в состояние IDLE, чтобы они не появлялись при каждом последующем проходе recover().
    empty_ids = [uid for uid, r in zip(ids, results) if r is False]
    if empty_ids:
        async with session_scope() as session:
            for uid in empty_ids:
                await users_service.reset_state(session, uid)

    finalized = sum(1 for r in results if r is True)
    failed = sum(1 for r in results if isinstance(r, BaseException))
    if failed:
        log.warning("восстановление: %d/%d воронок завершились с ошибкой", failed, len(ids))
    if finalized:
        log.info("восстановлено %d застрявших воронок", finalized)
    return finalized


async def _send_to_admin_card(
    bot,
    text: str,
    *,
    appeal_id: int | None = None,
    status: str | None = None,
    user_blocked: bool = False,
) -> str | None:
    """Отправляет отформатированную карточку в админ-группу. Возвращает
    message_id администратора или None при ошибке.

    Если переданы appeal_id и status — снизу прицепляется клавиатура
    действий («✉️ Ответить», «⛔ Закрыть», «🔁 Возобновить»). Без них
    (например, при followup-сообщении) клавиатуру не добавляем.

    user_blocked — текущее состояние блокировки жителя; влияет на
    label IT-кнопки (Заблокировать ↔ Разблокировать).
    """
    if not cfg.admin_group_id:
        log.warning("ADMIN_GROUP_ID не установлен — карточка для администратора не доставлена")
        return None
    attachments = None
    if appeal_id is not None and status is not None:
        attachments = [
            keyboards.appeal_admin_actions(
                appeal_id, status, user_blocked=user_blocked
            )
        ]
    try:
        kwargs: dict = {"chat_id": cfg.admin_group_id, "text": text}
        if attachments is not None:
            kwargs["attachments"] = attachments
        sent = await bot.send_message(**kwargs)
    except Exception:
        log.exception("не удалось доставить карточку администратора в chat_id=%s", cfg.admin_group_id)
        return None
    return extract_message_id(sent)


async def _relay_attachments_to_admin(
    bot,
    *,
    appeal_id: int,
    admin_mid: str | None,
    stored_attachments: list[dict],
) -> None:
    """Пересылает предоставленные жителем фото/гео/файлы в админ-группу как ответ
    на карточку. Выполняется по мере возможности: ошибки логируются и не прерывают процесс создания обращения."""
    if not cfg.admin_group_id or not stored_attachments:
        return
    relayable = deserialize_for_relay(stored_attachments)
    if not relayable:
        return
    try:
        from maxapi.enums.message_link_type import MessageLinkType
        from maxapi.types.message import NewMessageLink
    except Exception:
        log.exception("типы ссылок maxapi недоступны; пересылка выполняется без ссылки на ответ")
        MessageLinkType = None  # type: ignore[assignment]
        NewMessageLink = None  # type: ignore[assignment]

    link = None
    if admin_mid and MessageLinkType is not None and NewMessageLink is not None:
        try:
            link = NewMessageLink(type=MessageLinkType.REPLY, mid=admin_mid)
        except Exception:
            log.exception("не удалось собрать NewMessageLink для admin_mid=%s", admin_mid)
            link = None

    # Разбиваем вложения на пакеты для сообщений — лимит вложений на сервере MAX
    # не задокументирован; сохранение количества ниже attachments_per_relay_message позволяет
    # каждому send_message комфортно вписываться в любые серверные ограничения.
    chunk_size = max(1, cfg.attachments_per_relay_message)
    batches = [relayable[i:i + chunk_size] for i in range(0, len(relayable), chunk_size)]
    total_batches = len(batches)
    for idx, batch in enumerate(batches, start=1):
        header = (
            f"📎 Вложения к обращению #{appeal_id}"
            if total_batches == 1
            else f"📎 Вложения к обращению #{appeal_id} ({idx}/{total_batches})"
        )
        try:
            await bot.send_message(
                chat_id=cfg.admin_group_id,
                text=header,
                attachments=batch,
                link=link,
            )
        except Exception:
            log.exception(
                "не удалось переслать пакет вложений %d/%d для обращения #%s",
                idx, total_batches, appeal_id,
            )


async def _persist_and_dispatch_appeal(bot, max_user_id: int) -> bool | None:
    """Создает обращение (Appeal) из накопленных данных dialog_data, публикует карточку для админов,
    подтверждает жителю по user_id. Возвращает True при успешном сохранении и отправке, False при
    пустом обращении. Исключения вызываются только при ошибке БД.

    Защищено через asyncio.Lock для каждого пользователя, поэтому двойной клик на «Отправить» (или
    срабатывание таймера во время нажатия пользователем кнопки отправки) не может создать два
    обращения — второй вызов увидит состояние IDLE и прервется.
    """
    # Всегда удаляем запись блокировки пользователя при выходе (успех, бездействие или
    # исключение). Иначе `_user_locks` будет вечно хранить строку для каждого уникального 
    # гражданина — что ограничено населением, но ведет к бессмысленному росту.
    try:
        async with _get_user_lock(max_user_id):
            async with session_scope() as session:
                user = await users_service.get_or_create(session, max_user_id=max_user_id)
                # Идемпотентность: если состояние уже IDLE, то предыдущий параллельный
                # вызов уже завершил эту воронку — не отправляем дважды.
                if user.dialog_state == DialogState.IDLE.value:
                    log.info("отправка пропущена для пользователя %s — состояние уже IDLE", max_user_id)
                    return None
                data: dict[str, Any] = dict(user.dialog_data or {})
                summary = "\n".join(data.get("summary_chunks") or []).strip()
                attachments = data.get("attachments") or []
                if not summary and not attachments:
                    return False
                appeal = await appeals_service.create_appeal(
                    session,
                    user=user,
                    locality=data.get("locality") or None,
                    address=data.get("address", ""),
                    topic=data.get("topic", ""),
                    summary=summary,
                    attachments=attachments,
                )
                await users_service.reset_state(session, max_user_id)

        admin_mid = await _send_to_admin_card(
            bot,
            card_format.admin_card(appeal, user),
            appeal_id=appeal.id,
            status=appeal.status,
            user_blocked=user.is_blocked,
        )
        if admin_mid:
            async with session_scope() as session:
                await appeals_service.set_admin_message_id(session, appeal.id, admin_mid)
        else:
            # Карточка для администраторов не попала в группу — операторы не смогут
            # ответить на неё свайпом. Команда /reply N всё ещё работает, но им нужно
            # знать, что обращение существует. Выводим это ярко в логи, чтобы
            # дежурный оператор мог при необходимости переслать её вручную.
            log.warning(
                "обращение #%s создано, но карточка администратора не была опубликована (admin_mid=None)",
                appeal.id,
            )

        await _relay_attachments_to_admin(
            bot,
            appeal_id=appeal.id,
            admin_mid=admin_mid,
            stored_attachments=attachments,
        )

        try:
            await bot.send_message(
                user_id=max_user_id,
                text=texts.APPEAL_ACCEPTED.format(number=appeal.id),
                attachments=[keyboards.main_menu()],
            )
        except Exception:
            log.exception("подтверждение жителю %s не удалось для обращения #%s", max_user_id, appeal.id)

        return True
    finally:
        _drop_user_lock(max_user_id)


async def _start_appeal_flow(event, max_user_id: int):
    async with session_scope() as session:
        user = await users_service.get_or_create(session, max_user_id=max_user_id)
        # Заблокированный житель (после /forget или ручной блокировки оператором):
        # не можем принимать новые обращения, нужно сначала разблокировать
        # либо самому жителю восстановить согласие через настройки.
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
            await users_service.set_state(session, max_user_id, DialogState.AWAITING_CONSENT, data={})
            policy_url = await settings_store.get(session, "policy_url")
            policy_token = await settings_store.get(session, "policy_pdf_token")
        else:
            policy_url = None
            policy_token = None

    if policy_url is not None or policy_token is not None:
        attachments: list = [keyboards.consent_keyboard()]
        if policy_token:
            from aemr_bot.services.policy import build_file_attachment
            attachments.insert(0, build_file_attachment(policy_token))
            text = (
                "Перед оформлением обращения нужно ваше согласие на обработку "
                "персональных данных в соответствии с 152-ФЗ. Полный текст политики — "
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

    await _ask_contact_or_skip(event, max_user_id)


async def _ask_contact_or_skip(event, max_user_id: int):
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
        await _ask_locality(event, max_user_id)
        return

    prompt_for = {
        DialogState.AWAITING_CONTACT: (texts.CONTACT_REQUEST, keyboards.contact_request_keyboard()),
        DialogState.AWAITING_NAME: (texts.CONTACT_RECEIVED, keyboards.cancel_keyboard()),
    }
    text, keyboard = prompt_for[target_state]
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=text,
        attachments=[keyboard],
    )


async def _ask_locality(event, max_user_id: int):
    """Шаг «Населённый пункт». Перед адресом, после имени.

    Разделение нужно координаторам АЕМО: обращения по разным поселениям
    идут к разным территориальным управлениям. Раньше всё писалось одной
    строкой в поле `address`, и распределить было сложно.
    """
    async with session_scope() as session:
        localities = await settings_store.get(session, "localities") or ["Елизово"]
        await users_service.set_state(session, max_user_id, DialogState.AWAITING_LOCALITY)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.NAME_RECEIVED,
        attachments=[keyboards.localities_keyboard(localities)],
    )


async def _ask_address(event, max_user_id: int):
    async with session_scope() as session:
        await users_service.set_state(session, max_user_id, DialogState.AWAITING_ADDRESS)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.LOCALITY_RECEIVED,
        attachments=[keyboards.cancel_keyboard()],
    )


async def _ask_topic(event, max_user_id: int):
    async with session_scope() as session:
        topics = await settings_store.get(session, "topics") or ["Другое"]
        await users_service.set_state(session, max_user_id, DialogState.AWAITING_TOPIC)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.ADDRESS_RECEIVED,
        attachments=[keyboards.topics_keyboard(topics)],
    )


async def _ask_summary(event, max_user_id: int):
    async with session_scope() as session:
        await users_service.set_state(session, max_user_id, DialogState.AWAITING_SUMMARY)
    await event.bot.send_message(
        chat_id=get_chat_id(event),
        text=texts.TOPIC_RECEIVED,
        attachments=[keyboards.cancel_keyboard()],
    )


async def _finalize_appeal(event, max_user_id: int):
    """Финализация обращения. Вызывается сразу после первого непустого
    сообщения жителя в шаге AWAITING_SUMMARY — без таймера и без
    отдельной кнопки «Отправить». На пустой ввод отвечаем подсказкой."""
    persisted = await _persist_and_dispatch_appeal(event.bot, max_user_id)
    if persisted is False:
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=texts.APPEAL_EMPTY_REJECTED,
            attachments=[keyboards.cancel_keyboard()],
        )


def register(dp: Dispatcher) -> None:
    @dp.message_callback()
    async def on_callback(event: MessageCallback):
        payload = get_payload(event)
        max_user_id = get_user_id(event)
        if max_user_id is None:
            log.warning("коллбэк без user_id, payload=%r — пропущен", payload)
            return

        # Коллбэки пользовательского флоу (menu:*, consent:*, topic:*, appeal:*,
        # info:*, cancel) не должны срабатывать в админ-группе. Иначе
        # любое случайное нажатие на старую цитированную inline-кнопку
        # запустит воронку обращения от имени оператора и засорит таблицу
        # users. В админ-чате пропускаем только admin-flow:
        # broadcast:{confirm,abort,stop:N} и op:*. broadcast:unsubscribe —
        # на стороне гражданина, шлётся из личной рассылки, в админ-чате тоже не нужен.
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
            await _start_appeal_flow(event, max_user_id)
            return

        if payload == "consent:yes":
            async with session_scope() as session:
                await users_service.set_consent(session, max_user_id)
            await ack_callback(event, texts.CONSENT_ACCEPTED)
            await _ask_contact_or_skip(event, max_user_id)
            return

        if payload == "consent:no":
            async with session_scope() as session:
                await users_service.reset_state(session, max_user_id)
            _drop_user_lock(max_user_id)
            await ack_callback(event)
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.CONSENT_DECLINED,
                attachments=[keyboards.main_menu()],
            )
            return

        if payload == "cancel":
            async with session_scope() as session:
                await users_service.reset_state(session, max_user_id)
            _drop_user_lock(max_user_id)
            await ack_callback(event)
            await event.bot.send_message(
                chat_id=get_chat_id(event),
                text=texts.CANCELLED,
                attachments=[keyboards.main_menu()],
            )
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
                    await users_service.update_dialog_data(session, max_user_id, {"locality": chosen})
                else:
                    await ack_callback(event)
                    log.warning(
                        "locality:%s out of range (have %d), user=%s",
                        idx, len(localities), max_user_id,
                    )
                    return
            await ack_callback(event)
            await _ask_address(event, max_user_id)
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
                    await users_service.update_dialog_data(session, max_user_id, {"topic": chosen})
                else:
                    await ack_callback(event)
                    log.warning(
                        "topic:%s out of range (have %d), user=%s",
                        idx, len(topics), max_user_id,
                    )
                    return
            await ack_callback(event)
            await _ask_summary(event, max_user_id)
            return

        if payload == "appeal:submit":
            # Кнопка «Отправить» осталась в старых сообщениях клиента, которые
            # ещё могут крутиться у жителя в чате. Финализируем как обычно.
            await ack_callback(event)
            await _finalize_appeal(event, max_user_id)
            return

        # Коллбэки мастера рассылок (на стороне оператора) находятся в собственном обработчике;
        # делегируем их, чтобы не регистрировать второй @dp.message_callback().
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

        # Кнопки быстрых действий для /op_help. Цель — свести количество
        # команд, которые оператору приходится набирать руками, к минимуму.
        if payload.startswith("op:"):
            from aemr_bot.handlers import admin_commands, broadcast as broadcast_handler
            if payload == "op:stats_today":
                await ack_callback(event)
                await admin_commands.run_stats_today(event)
                return
            if payload == "op:stats_week":
                await ack_callback(event)
                await admin_commands.run_stats(event, "week")
                return
            if payload == "op:stats_month":
                await ack_callback(event)
                await admin_commands.run_stats(event, "month")
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
            if payload == "op:help_full":
                await ack_callback(event)
                await admin_commands.show_full_help(event)
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
            # Кнопки действий под карточкой обращения
            # (op:reply:N, op:reopen:N, op:close:N, op:erase:N).
            if payload.startswith("op:reply:"):
                try:
                    aid = int(payload.split(":", 2)[2])
                except (IndexError, ValueError):
                    await ack_callback(event)
                    return
                await admin_commands.run_reply_intent(event, aid)
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
            # Wizard-ы IT (роли проверяются внутри обработчиков):
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
            # Мастер рассылок имеет приоритет над фильтрацией слэшей, чтобы
            # команда /cancel работала внутри мастера. _handle_wizard_text возвращает False,
            # когда для этого оператора нет активного мастера, поэтому её безопасно
            # вызывать для каждого сообщения в админ-группе.
            from aemr_bot.handlers import (
                admin_commands as admin_cmd_module,
                broadcast as broadcast_handler,
            )
            consumed = await broadcast_handler._handle_wizard_text(event, text_body)
            if consumed:
                return
            # Wizard «👥 Операторы → Добавить» — перехват на шаге awaiting_id
            # / awaiting_name. Возвращает True, если поглощено.
            consumed = await admin_cmd_module.handle_operators_wizard_text(event, text_body)
            if consumed:
                return
            if text_body.startswith("/"):
                # Слэш-команда без активного мастера. Обработчики команд на стороне администратора
                # (admin_commands.py, broadcast.py), зарегистрированные до
                # этого перехватчика, уже имели свой шанс — молча игнорируем.
                return
            await op_reply.handle_operator_reply(event, body, text_body)
            return

        # Личные сообщения гражданина: для текста со слэшем обработчики команд зарегистрированы
        # ранее; если мы попали сюда, ни один не совпал — молча игнорируем.
        if text_body.startswith("/"):
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
            await handler(event, body, text_body, max_user_id, op_reply)


async def _on_awaiting_contact(event, body, text_body, max_user_id, op_reply):
    # Сначала пробуем достать телефон из contact-вложения. Если его нет
    # (старые клиенты MAX, либо житель напечатал номер текстом) — берём
    # цифры из текстового тела как запасной путь.
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

    # Имя из contact-вложения. Если житель шарит свой профиль через
    # RequestContactButton — оно уже там, ручной шаг AWAITING_NAME можно
    # пропустить.
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
            await users_service.set_state(session, max_user_id, DialogState.AWAITING_NAME)
        await event.message.answer(texts.CONTACT_RECEIVED, attachments=[keyboards.cancel_keyboard()])
    else:
        await _ask_contact_or_skip(event, max_user_id)


async def _on_awaiting_name(event, body, text_body, max_user_id, op_reply):
    name = text_body.strip()[: cfg.name_max_chars]
    if not name or not _HAS_ALNUM.search(name):
        # Пустая строка / только пробелы / только эмодзи / только пунктуация.
        # Пытаемся подтянуть имя из профиля MAX как запасной путь.
        name = get_first_name(event)
        if not name or name == "Удалено":
            await event.message.answer(texts.NAME_EMPTY)
            return
        name = name[: cfg.name_max_chars]

    async with session_scope() as session:
        await users_service.set_first_name(session, max_user_id, name)
    # Дальше — выбор населённого пункта. Само сообщение со списком уходит
    # из `_ask_locality`, чтобы поведение совпадало с веткой повторного
    # обращения, где имя и телефон уже заполнены.
    await _ask_locality(event, max_user_id)


async def _on_awaiting_address(event, body, text_body, max_user_id, op_reply):
    address = text_body.strip()[: cfg.address_max_chars]
    if not address or not _HAS_ALNUM.search(address):
        await event.message.answer(texts.ADDRESS_EMPTY)
        return
    async with session_scope() as session:
        await users_service.update_dialog_data(session, max_user_id, {"address": address})
    await _ask_topic(event, max_user_id)


async def _on_awaiting_summary(event, body, text_body, max_user_id, op_reply):
    """Один шаг сути: первое же непустое сообщение или вложение —
    это и есть обращение. Сохраняем текст, режем по жёстким лимитам,
    собираем все вложения этого сообщения и сразу финализируем.

    Без таймера тишины и без кнопки «Отправить»: житель не должен
    ждать минуту и нажимать дополнительную кнопку. Если в одном
    сообщении нет ни текста, ни вложений — отвечаем подсказкой и
    остаёмся в этом же состоянии."""
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
        data = dict(user.dialog_data or {})

        summary_chunks: list[str] = data.setdefault("summary_chunks", [])
        if chunk:
            summary_chunks.append(chunk[: cfg.summary_max_chars])

        if atts:
            existing_atts: list = data.setdefault("attachments", [])
            existing_atts.extend(atts[: cfg.attachments_max_per_appeal])

        user.dialog_data = data
        await session.flush()

    await _finalize_appeal(event, max_user_id)


async def _on_awaiting_locality(event, body, text_body, max_user_id, op_reply):
    """Житель прислал текст вместо нажатия на кнопку населённого пункта.
    Повторно показываем клавиатуру со списком, чтобы выбор оставался
    предсказуемым (свободный ввод сюда не закладываем — координаторам
    проще работать со стандартным списком поселений)."""
    async with session_scope() as session:
        localities = await settings_store.get(session, "localities") or ["Елизово"]
    await event.message.answer(
        texts.LOCALITY_REQUEST,
        attachments=[keyboards.localities_keyboard(localities)],
    )


async def _on_idle(event, body, text_body, max_user_id, op_reply):
    handled = await op_reply.handle_user_followup(event, text_body)
    if not handled:
        await event.message.answer(texts.UNKNOWN_INPUT, attachments=[keyboards.main_menu()])


_STATE_HANDLERS = {
    DialogState.AWAITING_CONTACT: _on_awaiting_contact,
    DialogState.AWAITING_NAME: _on_awaiting_name,
    DialogState.AWAITING_LOCALITY: _on_awaiting_locality,
    DialogState.AWAITING_ADDRESS: _on_awaiting_address,
    DialogState.AWAITING_SUMMARY: _on_awaiting_summary,
    DialogState.IDLE: _on_idle,
}
