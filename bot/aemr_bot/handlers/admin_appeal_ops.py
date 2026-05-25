"""Действия оператора над конкретным обращением.

Выделено из handlers/admin_commands.py (рефакторинг 2026-05-10).

- ✉️ Ответить (reply_intent + cancel)
- 🔁 Возобновить (reopen)
- ⛔ Закрыть (close)
- 🚫 Заблокировать жителя / ✅ Разблокировать
- 🗑 Удалить ПДн жителя
"""
from __future__ import annotations

import logging

from aemr_bot import keyboards as kbds
from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator, ensure_role
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import operators as operators_service
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import get_user_id, is_admin_chat, send_or_edit_screen

log = logging.getLogger(__name__)


async def _show_appeal_card_or_result(
    event,
    appeal_id: int,
    fallback_text: str,
) -> None:
    """Опубликовать/обновить admin appeal card после действия оператора.

    Freshness-rule (PR #62, унифицировано с меню): admin_card.render
    проверит callback_mid против menu_tracker[admin_group_id]:
    - callback_mid == последняя карточка в чате → edit на месте;
    - иначе (ниже появились другие сообщения/карточки) → send new.

    На случай если обращение не найдено или user пуст — fallback
    короткое сообщение оператору, без card-render.
    """
    from aemr_bot.services import admin_card as admin_card_service

    try:
        async with session_scope() as session:
            appeal = await appeals_service.get_by_id_with_messages(
                session, appeal_id
            )
    except Exception:
        log.exception("appeal card refresh failed for appeal_id=%s", appeal_id)
        appeal = None
    if appeal is not None and appeal.user is not None:
        try:
            # Freshness-rule: пробрасываем callback_mid — render edit'нет
            # карточку только если callback пришёл на последнюю карточку
            # в чате (по menu_tracker). Иначе → send new внизу.
            from aemr_bot.utils.event import get_callback_message_id

            await admin_card_service.render(
                event.bot,
                appeal,
                callback_mid=get_callback_message_id(event),
            )
            return
        except Exception:
            log.exception(
                "admin_card.render failed for appeal_id=%s", appeal_id
            )
    # Fallback — не нашли appeal/user, просто пишем сообщение оператору.
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=fallback_text,
        attachments=[kbds.op_back_to_menu_keyboard()],
        force_new_message=True,
    )


async def run_reply_intent(event, appeal_id: int, *, is_final: bool = True) -> None:
    """Кнопка «✉️ Ответить» под карточкой обращения. Запоминает намерение
    оператора в in-memory словаре. Следующее текстовое сообщение
    оператора в админ-группе доставляется как /reply <appeal_id> <текст>.

    is_final=True (default, «✉️ Ответить и закрыть») — финальный
    ответ, обращение → ANSWERED после доставки.
    is_final=False («💬 Промежуточный ответ») — диалог/уточнение,
    обращение остаётся IN_PROGRESS, можно отправить ещё ответы.

    Боковой эффект: NEW → IN_PROGRESS при нажатии (житель видит
    «в работе» сразу). Если оператор отменил ввод — статус остаётся
    IN_PROGRESS, не откатывается («оператор уже взял в работу»).

    Защиты:
    - запрещаем reply-intent на CLOSED-обращение
    - запрещаем для is_blocked жителя
    - сбрасываем активные wizard'ы (broadcast, add-operator) этого
      оператора, чтобы следующий текст не утёк туда
    """
    from aemr_bot.db.models import AppealStatus
    from aemr_bot.handlers import admin_operators
    from aemr_bot.handlers import broadcast as broadcast_handler
    from aemr_bot.handlers import operator_reply as op_reply
    from aemr_bot.utils.event import ack_callback

    if not is_admin_chat(event):
        await ack_callback(event)
        return
    if not await ensure_operator(event):
        await ack_callback(event)
        return
    operator_id = get_user_id(event)
    if operator_id is None:
        await ack_callback(event)
        return

    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if appeal is None:
            await ack_callback(event)
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id),
                attachments=[kbds.op_back_to_menu_keyboard()],
            )
            return
        if appeal.status == AppealStatus.CLOSED.value:
            await ack_callback(event)
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=(
                    f"Обращение #{appeal_id} закрыто. Сначала верните его в "
                    f"работу кнопкой «🔁 Возобновить» под карточкой."
                ),
                attachments=[kbds.op_back_to_menu_keyboard()],
            )
            return
        if appeal.user is None or appeal.user.is_blocked:
            await ack_callback(event)
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=(
                    f"Житель по обращению #{appeal_id} заблокирован — ответ не "
                    f"будет доставлен через бот. Если ответ всё-таки нужен, "
                    f"сначала снимите блокировку."
                ),
                attachments=[kbds.op_back_to_menu_keyboard()],
            )
            return
        # NEW → IN_PROGRESS: оператор взял в работу. Видно жителю.
        await appeals_service.mark_in_progress(session, appeal_id)

    # Сбрасываем чужие wizard'ы того же оператора.
    broadcast_handler._wizards.pop(operator_id, None)
    admin_operators._op_wizards.pop(operator_id, None)

    # P2 #22 — race rapid double-tap: если оператор уже готовил ответ
    # на ДРУГОЕ обращение, intent перезаписался бы молча, и следующее
    # сообщение оператора ушло бы не туда, куда он думает. Ловим это
    # явно: предупреждаем «отменён ответ на #X, теперь на #Y», чтобы
    # оператор сам решил, продолжить или прервать.
    from aemr_bot.services import wizard_registry as _wr

    existing = _wr.get_reply_intent(operator_id)
    if existing is not None:
        prev_appeal_id, _prev_is_final, _prev_ts = existing
        if prev_appeal_id != appeal_id:
            try:
                await event.bot.send_message(
                    chat_id=cfg.admin_group_id,
                    text=(
                        f"⚠️ Подготовка ответа на обращение #{prev_appeal_id} "
                        f"отменена — вы только что переключились на ответ "
                        f"по обращению #{appeal_id}. Если хотели остаться на "
                        f"#{prev_appeal_id}, нажмите «✉️ Ответить» под его "
                        f"карточкой ещё раз."
                    ),
                )
            except Exception:
                log.exception(
                    "run_reply_intent: failed to warn about intent "
                    "overwrite operator=%s prev=%s new=%s",
                    operator_id, prev_appeal_id, appeal_id,
                )

    op_reply.remember_reply_intent(operator_id, appeal_id, is_final=is_final)
    label = "Ответ" if is_final else "Промежуточный ответ"
    await ack_callback(event, f"{label} на #{appeal_id}")
    prompt_hint = (
        f"✉️ Введите текст ОТВЕТА на обращение #{appeal_id} "
        f"(после отправки обращение закроется в «отвечено»).\n"
        if is_final
        else (
            f"💬 Введите ПРОМЕЖУТОЧНЫЙ ответ на обращение #{appeal_id} "
            f"(обращение останется в работе, можно отправить ещё "
            f"уточнения).\n"
        )
    )
    # SACRED #4: prompt-ввод НЕ должен edit'нуть карточку обращения.
    # send_or_edit_screen без force_new_message edit'нул бы admin appeal
    # card (tracker всё ещё на ней после прошлого render). Карточка с
    # timeline'ом превратилась бы в input-prompt — содержимое потеряно.
    # force_new_message=True гарантирует, что prompt всегда уходит
    # отдельным новым сообщением, а карточка остаётся видимой выше.
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            f"{prompt_hint}"
            f"Лимит {cfg.answer_max_chars} символов. Просто отправьте "
            f"следующее сообщение в этот чат, либо «Отменить» ниже.\n"
            f"\n"
            f"🛡️ Памятка: ссылки только на гос-домены (elizovomr.ru, "
            f"kamgov.ru, gosuslugi.ru, kamchatka.gov.ru). Любая другая "
            f"ссылка будет заблокирована автоматически — ответ не уйдёт "
            f"жителю, чтобы случайный фишинг не прошёл от имени "
            f"Администрации. Подробности — docs/OPERATOR_SECURITY.md §3.2."
        ),
        attachments=[kbds.cancel_reply_intent_keyboard()],
        force_new_message=True,
    )


async def run_reply_cancel(event) -> None:
    """Кнопка «❌ Отменить ответ» под подсказкой ввода."""
    from aemr_bot.handlers import operator_reply as op_reply
    from aemr_bot.utils.event import ack_callback

    operator_id = get_user_id(event)
    if operator_id is None:
        await ack_callback(event)
        return
    cancelled_appeal = op_reply.drop_reply_intent(operator_id)
    await ack_callback(event)
    # SACRED #4: cancel-сообщение тоже отдельным new (не trample prompt).
    if cancelled_appeal is not None:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=f"Ответ на обращение #{cancelled_appeal} отменён.",
            attachments=[kbds.op_back_to_menu_keyboard()],
            force_new_message=True,
        )
    else:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text="Мастер ответа уже закрыт.",
            attachments=[kbds.op_back_to_menu_keyboard()],
            force_new_message=True,
        )


_REOPEN_FALLBACK_TEXT = {
    "reopened": texts.OP_APPEAL_REOPENED,
    "already_open": texts.OP_APPEAL_ALREADY_OPEN,
    "blocked_by_revoke": texts.OP_APPEAL_BLOCKED_BY_REVOKE,
    "not_found": texts.OP_APPEAL_NOT_FOUND,
}


async def run_reopen(event, appeal_id: int) -> None:
    """Кнопочный аналог /reopen N — возобновить обращение.

    Различает в UX четыре исхода (см. appeals_service.reopen):
    - reopened → перерисовываем карточку с обновлённым статусом;
    - already_open → no-op, говорим «уже в работе»;
    - blocked_by_revoke → информативное сообщение про ПДн-гард,
      карточку не трогаем (всё равно бот не доставит ответ);
    - not_found → стандартное «Обращение не найдено».

    Audit пишем только при реальной смене статуса (reopened).
    """
    from aemr_bot.utils.event import ack_callback

    if not await ensure_operator(event):
        return
    async with session_scope() as session:
        result = await appeals_service.reopen(session, appeal_id)
        if result == "reopened":
            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event),
                action="reopen",
                target=f"appeal #{appeal_id}",
            )
    await ack_callback(event)
    fallback = _REOPEN_FALLBACK_TEXT[result].format(number=appeal_id)
    # freshness-rule: edit если карточка ещё последняя в чате, иначе new.
    await _show_appeal_card_or_result(event, appeal_id, fallback)


async def run_close(event, appeal_id: int) -> None:
    """Кнопочный аналог /close N — закрыть обращение без ответа.

    P2 #23: если в обращении уже есть промежуточный ответ оператора,
    «Закрыть без ответа» — спорное действие (формально ответ ушёл, но
    обращение закрыто как «не отвечено»). Не блокируем, но добавляем
    в сопровождающий текст подсказку про «✉️ Ответить и закрыть» —
    финальный ответ корректнее закрывает воронку для жителя.
    """
    from aemr_bot.utils.event import ack_callback

    if not await ensure_operator(event):
        return
    had_intermediate_reply = False
    async with session_scope() as session:
        had_intermediate_reply = await appeals_service.has_operator_message(
            session, appeal_id
        )
        ok = await appeals_service.close(session, appeal_id)
        if ok:
            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event),
                action="close",
                target=f"appeal #{appeal_id}",
                details=(
                    {"after_intermediate_reply": True}
                    if had_intermediate_reply else None
                ),
            )
    await ack_callback(event)
    if ok:
        fallback = texts.OP_APPEAL_CLOSED.format(number=appeal_id)
        if had_intermediate_reply:
            fallback += (
                "\n\n⚠️ У обращения уже есть промежуточный ответ оператора. "
                "В следующий раз для финального ответа удобнее использовать "
                "«✉️ Ответить и закрыть» — житель получит полное письмо."
            )
    else:
        fallback = texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id)
    # freshness-rule: edit если карточка ещё последняя в чате, иначе new.
    await _show_appeal_card_or_result(event, appeal_id, fallback)


async def run_block_for_appeal(
    event, appeal_id: int, *, blocked: bool
) -> None:
    """Кнопки «🚫 Заблокировать жителя» / «✅ Разблокировать»."""
    from aemr_bot.utils.event import ack_callback

    if not await ensure_role(event, OperatorRole.IT):
        return
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if appeal is None or appeal.user is None:
            await ack_callback(event)
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id),
                attachments=[kbds.op_back_to_menu_keyboard()],
            )
            return
        target_id = appeal.user.max_user_id
        ok = await users_service.set_blocked(session, target_id, blocked=blocked)
        if ok:
            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event),
                action="block" if blocked else "unblock",
                target=f"user max_id={target_id}",
            )
    await ack_callback(event)
    if ok:
        msg = (
            texts.OP_USER_BLOCKED if blocked else texts.OP_USER_UNBLOCKED
        ).format(max_user_id=target_id)
    else:
        msg = "Не удалось обновить статус. См. логи."
    await _show_appeal_card_or_result(event, appeal_id, msg)


async def run_show_attachments(event, appeal_id: int) -> None:
    """Кнопка «📎 Вложения (N)» в карточке обращения (PR-fix-hang).

    Раньше вложения переотправлялись автоматически в `_do_open_tickets`
    под каждое обращение в цикле, что давало 50-80 sequential
    `bot.send_message` и hang handler'а на десятки секунд. Теперь —
    только по явному тапу оператора, и только для одного обращения за
    раз. Bot.send_message внутри `render_appeal_attachments` бьёт на
    батчи по `cfg.attachments_per_relay_message`.
    """
    from aemr_bot.services.admin_relay import render_appeal_attachments
    from aemr_bot.utils.event import ack_callback

    if not await ensure_operator(event):
        await ack_callback(event)
        return
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id_with_messages(
            session, appeal_id
        )
    await ack_callback(event)
    if appeal is None:

        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id),
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return
    await render_appeal_attachments(
        event.bot,
        chat_id=cfg.admin_group_id,
        user_id=None,
        appeal=appeal,
        header_template="📎 Вложения к обращению #{appeal_id}",
        reply_to_mid=getattr(appeal, "admin_message_id", None),
    )


async def run_erase_for_appeal(event, appeal_id: int) -> None:
    """Кнопка «🗑 Удалить ПДн жителя» в карточке обращения (только для it)."""
    from aemr_bot.utils.event import ack_callback

    if not await ensure_role(event, OperatorRole.IT):
        return
    async with session_scope() as session:
        appeal = await appeals_service.get_by_id(session, appeal_id)
        if appeal is None or appeal.user is None:
            await ack_callback(event)
            await send_or_edit_screen(
                event,
                chat_id=cfg.admin_group_id,
                text=texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id),
                attachments=[kbds.op_back_to_menu_keyboard()],
            )
            return
        target_id = appeal.user.max_user_id
        ok = await users_service.erase_pdn(session, target_id)
        if ok:
            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event),
                action="erase",
                target=f"user max_id={target_id}",
            )
    await ack_callback(event)
    if ok:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=texts.OP_USER_ERASED.format(max_user_id=target_id),
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
    else:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text="Пользователь не найден.",
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
