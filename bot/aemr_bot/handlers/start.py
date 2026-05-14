import logging

from maxapi import Dispatcher
from maxapi.types import BotStarted, Command, MessageCreated

from aemr_bot import keyboards, texts
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import current_user
from aemr_bot.services import admin_events
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import operators as ops_service
from aemr_bot.services import policy as policy_service
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import (
    get_chat_id,
    get_first_name,
    get_user_id,
    is_admin_chat,
    reply,
    send_or_edit_screen,
)

log = logging.getLogger(__name__)


# Обработчики жителя ниже отбрасываются в админ-группе через is_admin_chat.
# Алиас оставлен с подчёркиванием, чтобы внутри файла читалось как локальная
# гард-функция и не путалось с неймспейсом utils.event.
_is_admin_chat = is_admin_chat


async def _ensure_user(event):
    max_user_id = get_user_id(event)
    first_name = get_first_name(event)
    if max_user_id is None:
        return None
    async with current_user(max_user_id, first_name=first_name) as (_, user):
        return user


async def _build_main_menu(max_user_id: int | None = None):
    """Собирает главное меню с актуальным состоянием кнопки подписки.

    Если жителя удаётся идентифицировать по `max_user_id`, кнопка
    подписки покажет либо «🔔 Подписаться на рассылку» (для не-
    подписанных), либо «🔕 Не хочу получать рассылку» (для подписанных).
    Без идентификации показываем приглашение подписаться по умолчанию.
    """
    subscribed = False
    if max_user_id is not None:
        async with session_scope() as session:
            subscribed = await broadcasts_service.is_subscribed(
                session, max_user_id
            )
    return keyboards.main_menu(subscribed=subscribed)


async def _reset_funnel_if_stuck(max_user_id: int | None) -> None:
    """Если житель набрал /start посреди воронки — сбрасываем состояние.

    Без сброса любое следующее сообщение пошло бы в обработчик того
    шага, в котором житель застрял (адрес, имя и т.п.), и сценарий
    «начать заново» молча не сработал бы.
    """
    if max_user_id is None:
        return
    from aemr_bot.db.models import DialogState

    async with current_user(max_user_id) as (session, user):
        if user.dialog_state and user.dialog_state != DialogState.IDLE.value:
            await users_service.reset_state(session, max_user_id)


async def cmd_start(event):
    await _ensure_user(event)
    await _reset_funnel_if_stuck(get_user_id(event))
    await reply(event, texts.WELCOME, attachments=[await _build_main_menu(get_user_id(event))])


async def cmd_help(event):
    await reply(event, texts.HELP_USER, attachments=[await _build_main_menu(get_user_id(event))])


async def cmd_rules(event):
    await reply(event, texts.RULES_TEXT, attachments=[keyboards.back_to_menu_keyboard()])


async def cmd_menu(event):
    await _reset_funnel_if_stuck(get_user_id(event))
    await reply(event, texts.WELCOME, attachments=[await _build_main_menu(get_user_id(event))])


async def cmd_policy(event):
    """По запросу отправляет жителю PDF с политикой обработки персональных данных."""
    chat_id = get_chat_id(event)
    if chat_id is None:
        return

    async with session_scope() as session:
        token = await settings_store.get(session, policy_service.POLICY_TOKEN_KEY)
        policy_url = await settings_store.get(session, "policy_url")

    bot = getattr(event, "bot", None)

    # Подстраховка на холодном старте: пробуем загрузить PDF, если токен
    # ещё не закэширован, например на первых запусках после деплоя, когда
    # стартовая загрузка молча упала.
    if not token and bot is not None:
        try:
            token = await policy_service.ensure_uploaded(bot)
        except Exception:
            log.exception("on-demand policy upload failed")

    if token and bot is not None:
        try:
            await send_or_edit_screen(
                event,
                text=texts.POLICY_DELIVERED,
                attachments=[
                    policy_service.build_file_attachment(token),
                    keyboards.back_to_settings_keyboard(),
                ],
            )
            return
        except Exception:
            log.exception("policy file delivery failed; falling back to URL")

    if policy_url:
        await send_or_edit_screen(
            event,
            text=texts.POLICY_FALLBACK_URL.format(policy_url=policy_url),
            attachments=[keyboards.back_to_settings_keyboard()],
        )
    else:
        await send_or_edit_screen(
            event,
            text=texts.POLICY_UNAVAILABLE,
            attachments=[keyboards.back_to_settings_keyboard()],
        )


async def cmd_subscribe(event):
    """Команда /subscribe — единый путь с кнопкой «🔔 Подписаться».

    Раньше команда требовала полного consent_pdn_at и не записывала
    consent_broadcast_at — что нарушало 152-ФЗ ст. 9 ч. 1 (конкретное
    согласие именно на цель «рассылка»). Теперь делегирует в
    `menu.do_subscribe`, который покажет короткий экран мини-согласия
    при первом тапе и проставит consent_broadcast_at в `do_subscribe_confirm`.
    """
    from aemr_bot.handlers.menu import do_subscribe

    max_user_id = get_user_id(event)
    if max_user_id is None:
        return
    await do_subscribe(event, max_user_id)


async def cmd_unsubscribe(event):
    """Команда /unsubscribe — единый путь с кнопкой «🔕 Отписаться»."""
    from aemr_bot.handlers.menu import do_unsubscribe

    max_user_id = get_user_id(event)
    if max_user_id is None:
        return
    await do_unsubscribe(event, max_user_id)


async def cmd_forget(event):
    max_user_id = get_user_id(event)
    if max_user_id is None:
        return
    # Аудит ставим ДО erase, потому что после удаления записи user
    # пропадает max_user_id из таблицы users — но в audit_log
    # operator_max_user_id остаётся как метка «было такое действие
    # от такого человека».
    async with session_scope() as session:
        await ops_service.write_audit(
            session,
            operator_max_user_id=max_user_id,
            action="self_erase",
            target=f"user max_id={max_user_id}",
        )
        await users_service.erase_pdn(session, max_user_id)
    await admin_events.notify_data_erased(
        event.bot,
        max_user_id=max_user_id,
        closed_appeal_ids=[],
    )
    await reply(event, texts.ERASE_REQUESTED)


async def cmd_export(event):
    """Скрытая команда: житель получает JSON со своими обращениями
    (право субъекта по 152-ФЗ ст. 14). Не публикуется в /-меню MAX.

    Состав: список обращений с темой, статусом, датами, ответом
    оператора. Без admin-пометок и системных полей.
    """
    import json
    from datetime import datetime

    max_user_id = get_user_id(event)
    if max_user_id is None:
        return
    async with current_user(max_user_id) as (session, user):
        appeals = await appeals_service.list_for_user(session, user.id, limit=500)
        appeals_payload = []
        for ap in appeals:
            answer = next(
                (
                    m.text
                    for m in reversed(ap.messages or [])
                    if m.direction == "from_operator"
                ),
                None,
            )
            appeals_payload.append(
                {
                    "id": ap.id,
                    "created_at": ap.created_at.isoformat() if ap.created_at else None,
                    "status": ap.status,
                    "locality": ap.locality,
                    "address": ap.address,
                    "topic": ap.topic,
                    "summary": ap.summary,
                    "answered_at": ap.answered_at.isoformat() if ap.answered_at else None,
                    "closed_at": ap.closed_at.isoformat() if ap.closed_at else None,
                    "operator_answer": answer,
                }
            )
        export = {
            "exported_at": datetime.now().isoformat(),
            "max_user_id": user.max_user_id,
            "first_name": user.first_name,
            "phone": user.phone,
            "consent_pdn_at": user.consent_pdn_at.isoformat() if user.consent_pdn_at else None,
            "consent_revoked_at": user.consent_revoked_at.isoformat() if user.consent_revoked_at else None,
            "consent_broadcast_at": user.consent_broadcast_at.isoformat() if user.consent_broadcast_at else None,
            "subscribed_broadcast": user.subscribed_broadcast,
            "appeals": appeals_payload,
        }
    await reply(
        event,
        "Ваши данные:\n\n```\n"
        + json.dumps(export, ensure_ascii=False, indent=2)
        + "\n```",
    )


async def cmd_cancel(event):
    """Сбрасывает текущий шаг воронки и даёт быстрый возврат в меню. Без этого
    житель набирающий /cancel мог получить тишину (если в каком-то
    шаге не было ясной кнопки «Отмена»).
    """
    max_user_id = get_user_id(event)
    if max_user_id is None:
        return
    async with session_scope() as session:
        await users_service.reset_state(session, max_user_id)
    await reply(event, texts.CANCELLED, attachments=[keyboards.back_to_menu_keyboard()])


def register(dp: Dispatcher) -> None:
    # Все citizen-flow обработчики ниже стоят на guard'е _is_admin_chat:
    # в админ-группе они тихо отбрасываются, чтобы операторы не получали
    # welcome-меню и не попадали в `users` как «жители».
    # /whoami — единственное исключение, оно работает в обоих направлениях:
    # нужно как для жителя (узнать свой max_user_id), так и для оператора
    # (узнать chat_id админ-группы при первом старте).

    # /start, /menu, /help работают в обоих контекстах:
    # • в личке с жителем — показывают welcome-меню (cmd_start/menu/help);
    # • в админ-группе — открывают памятку оператора с кнопками быстрых
    #   действий. Цель: оператор не должен запоминать, что в его чате
    #   команда называется /op_help, а в личке у жителя — /help. Любая
    #   привычная команда работает в обоих местах.
    @dp.bot_started()
    async def _(event: BotStarted):
        if _is_admin_chat(event):
            return
        await cmd_start(event)

    @dp.message_created(Command("start"))
    async def _(event: MessageCreated):
        if _is_admin_chat(event):
            from aemr_bot.handlers import admin_commands

            await admin_commands.show_op_menu(event, pin=False)
            return
        await cmd_start(event)

    @dp.message_created(Command("help"))
    async def _(event: MessageCreated):
        if _is_admin_chat(event):
            from aemr_bot.handlers import admin_commands

            await admin_commands.show_op_menu(event, pin=False)
            return
        await cmd_help(event)

    @dp.message_created(Command("menu"))
    async def _(event: MessageCreated):
        if _is_admin_chat(event):
            from aemr_bot.handlers import admin_commands

            await admin_commands.show_op_menu(event, pin=False)
            return
        await cmd_menu(event)

    # Жильцовые команды в админ-чате не работают, но раньше тихо
    # игнорировались — оператор тапал и не понимал почему ничего не
    # происходит. Теперь отвечаем явной подсказкой: «команда для жителя,
    # тут /op_help». MAX Bot API не поддерживает per-scope команды, и
    # эти имена остаются в /-меню для всех чатов.
    @dp.message_created(Command("forget"))
    async def _(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_forget(event)

    @dp.message_created(Command("cancel"))
    async def _(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_cancel(event)

    # /export — скрытая команда, не публикуется в /-меню MAX. Право
    # субъекта на выгрузку своих ПДн (152-ФЗ ст. 14). Реальные
    # запросы редкие; нужно для регуляторных проверок.
    @dp.message_created(Command("export"))
    async def _(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_export(event)

    @dp.message_created(Command("policy"))
    async def _(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_policy(event)

    @dp.message_created(Command("rules"))
    async def _(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_rules(event)

    @dp.message_created(Command("subscribe"))
    async def _(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_subscribe(event)

    @dp.message_created(Command("unsubscribe"))
    async def _(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_unsubscribe(event)

    @dp.message_created(Command("whoami"))
    async def _(event: MessageCreated):
        # /whoami работает ТОЛЬКО в админ-группе. У жителя в личке эта
        # команда не нужна и сбивает с толку — IDs не используются в
        # пользовательских сценариях. В личке тихо игнорируем.
        if not _is_admin_chat(event):
            return
        max_user_id = get_user_id(event) or "?"
        first_name = get_first_name(event) or ""
        chat_id = get_chat_id(event) or "?"
        await reply(
            event,
            "🛠 whoami\n"
            f"max_user_id: {max_user_id}\n"
            f"first_name: {first_name}\n"
            f"chat_id: {chat_id}",
        )
