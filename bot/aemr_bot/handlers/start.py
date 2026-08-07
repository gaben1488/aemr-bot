import logging
from datetime import UTC, datetime

from maxapi import Dispatcher
from maxapi.types import BotStarted, BotStopped, Command, MessageCreated

from aemr_bot import keyboards, texts
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._common import current_user
from aemr_bot.services import admin_events, card_format, settings_store, uploads
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import broadcasts as broadcasts_service
from aemr_bot.services import operators as ops_service
from aemr_bot.services import policy as policy_service
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


async def _welcome_text() -> str:
    """Актуальный welcome с поддержкой UI-редактирования (C1).

    Если IT обновил welcome_text через меню «⚙️ Настройки бота» — отдаём
    свежий текст из БД с санитизацией. Иначе fallback на texts.WELCOME.
    Открываем отдельную сессию (не зависим от вызывающего контекста).
    """
    async with session_scope() as session:
        return await settings_store.get_text_with_fallback(
            session, "welcome_text", texts.WELCOME
        )


async def cmd_start(event):
    # Welcome шлём через `_send_or_edit_menu` — он обновит menu_tracker
    # на mid отправленного сообщения. Это критично для freshness rule:
    # без tracker.set следующий тап «🛡️ Защита от мошенников» в этом
    # же welcome даст callback_mid != tracker → send_new вместо edit.
    # Жалоба владельца 2026-05-27: «почему нажатие 🛡️ Защита не
    # редактирует карточку меню — кажется, ты всё ещё не понимаешь
    # правил».
    from aemr_bot.handlers.menu import _send_or_edit_menu

    await _ensure_user(event)
    await _reset_funnel_if_stuck(get_user_id(event))
    welcome = await _welcome_text()
    await _send_or_edit_menu(
        event,
        text=welcome,
        attachments=[await _build_main_menu(get_user_id(event))],
    )


async def cmd_help(event):
    from aemr_bot.handlers.menu import _send_or_edit_menu

    await _send_or_edit_menu(
        event,
        text=texts.HELP_USER,
        attachments=[await _build_main_menu(get_user_id(event))],
    )


async def cmd_rules(event):
    await reply(event, texts.RULES_TEXT, attachments=[keyboards.back_to_menu_keyboard()])


async def cmd_menu(event):
    from aemr_bot.handlers.menu import _send_or_edit_menu

    await _reset_funnel_if_stuck(get_user_id(event))
    welcome = await _welcome_text()
    await _send_or_edit_menu(
        event,
        text=welcome,
        attachments=[await _build_main_menu(get_user_id(event))],
    )


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
        # erase_pdn_detailed возвращает список id закрытых обращений
        # (NEW/IN_PROGRESS до erase). Передаём в уведомление оператору
        # «закрыто без ответа: #N, #M» — раньше всегда был [] и
        # оператор не видел какие тикеты осиротели.
        closed_ids = await users_service.erase_pdn_detailed(
            session, max_user_id
        )
    await admin_events.notify_data_erased(
        event.bot,
        max_user_id=max_user_id,
        closed_appeal_ids=closed_ids or [],
    )
    await reply(event, texts.ERASE_REQUESTED)


# Порог разбивки выгрузки на сообщения. У MAX предел около 4000
# символов; берём с запасом на заголовок части и обрамление кода.
EXPORT_CHUNK_CHARS = 3200


def _chunk_for_messenger(text: str, *, limit: int) -> list[str]:
    """Нарезать текст на части не длиннее `limit`, не разрывая строки.

    Зачем. Выгрузка уходила ОДНИМ сообщением, а предел MAX — около
    четырёх тысяч символов. У жителя с несколькими обращениями и
    перепиской отправка просто падала, и он не получал НИЧЕГО: право на
    доступ к своим данным упиралось в лимит мессенджера. Молча — ошибку
    видел только лог.

    Режем по границам строк: JSON, разорванный посреди строки, читать
    невозможно. Строку длиннее лимита (теоретически — очень длинный
    текст обращения в одну строку) дробим принудительно, иначе часть
    снова не уйдёт.
    """
    if len(text) <= limit:
        return [text]

    parts: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.split("\n"):
        # Строка сама по себе длиннее лимита — режем её на куски.
        while len(line) > limit:
            if current:
                parts.append("\n".join(current))
                current, current_len = [], 0
            parts.append(line[:limit])
            line = line[limit:]
        # +1 на перевод строки между строками.
        addition = len(line) + (1 if current else 0)
        if current_len + addition > limit:
            parts.append("\n".join(current))
            current, current_len = [line], len(line)
        else:
            current.append(line)
            current_len += addition
    if current:
        parts.append("\n".join(current))
    return parts


EXPORT_FILE_NAME = "Мои данные из чат-бота.txt"


def _export_dt(value) -> str:
    """Дата для жителя: «02.07.2026 11:05» вместо машинного формата."""
    if value is None:
        return "—"
    try:
        return card_format._local(value)
    except Exception:
        return str(value)


def _render_export_for_human(user, appeals) -> str:
    """Собрать выгрузку в виде, понятном человеку без подготовки.

    Раньше жителю уходил JSON — он годится регулятору, но пенсионер видит
    в нём кашу из скобок и латинских ключей. Право на доступ к своим
    данным (статья 14 закона № 152-ФЗ) осмысленно ровно настолько,
    насколько человек может эти данные прочитать.
    """
    L: list[str] = []
    L.append("ВАШИ ДАННЫЕ В ЧАТ-БОТЕ АДМИНИСТРАЦИИ")
    L.append("Елизовского муниципального округа")
    L.append(f"Выгрузка сформирована: {_export_dt(datetime.now(UTC))}")
    L.append("")
    L.append("О ВАС")
    L.append(f"  Имя: {user.first_name or '—'}")
    L.append(f"  Телефон: {user.phone or '—'}")
    L.append(f"  Номер вашего аккаунта в мессенджере MAX: {user.max_user_id}")
    L.append("")
    L.append("СОГЛАСИЯ")
    L.append(f"  Согласие на обработку данных дано: {_export_dt(user.consent_pdn_at)}")
    if user.consent_revoked_at:
        L.append(f"  Согласие отозвано: {_export_dt(user.consent_revoked_at)}")
    else:
        L.append("  Согласие отозвано: нет, действует")
    if user.subscribed_broadcast:
        L.append(
            "  Подписка на оповещения: оформлена "
            f"{_export_dt(user.consent_broadcast_at)}"
        )
    else:
        L.append("  Подписка на оповещения: не оформлена")
    if user.consent_pdn_text_sha256:
        L.append(
            "  Служебная отметка (отпечаток текста согласия): "
            f"{user.consent_pdn_text_sha256[:16]}… — по ней видно, "
            "какую именно редакцию согласия вы принимали"
        )
    L.append("")

    if not appeals:
        L.append("ВАШИ ОБРАЩЕНИЯ")
        L.append("  Обращений пока нет.")
    else:
        L.append(f"ВАШИ ОБРАЩЕНИЯ: {len(appeals)}")
    for ap in appeals:
        _, status_label = texts.STATUS_LABELS.get(
            ap.status, ("", ap.status or "—")
        )
        L.append("")
        L.append(f"── Обращение № {ap.id} " + "─" * 30)
        L.append(f"  Подано: {_export_dt(ap.created_at)}")
        L.append(f"  Состояние: {status_label}")
        L.append(f"  Тема: {ap.topic or '—'}")
        L.append(f"  Населённый пункт: {ap.locality or '—'}")
        L.append(f"  Адрес: {ap.address or '—'}")
        attach_n = len(ap.attachments or [])
        if attach_n:
            L.append(f"  Вы приложили файлов: {attach_n}")
        if ap.answered_at:
            L.append(f"  Ответ дан: {_export_dt(ap.answered_at)}")
        if ap.closed_at:
            L.append(f"  Закрыто: {_export_dt(ap.closed_at)}")
        L.append("")
        L.append("  Текст обращения:")
        for line in (ap.summary or "—").splitlines() or ["—"]:
            L.append(f"    {line}")
        messages = list(ap.messages or [])
        if messages:
            L.append("")
            L.append("  Переписка:")
            for m in messages:
                who = "Оператор" if m.direction == "from_operator" else "Вы"
                L.append(f"    [{_export_dt(m.created_at)}] {who}:")
                for line in (m.text or "—").splitlines() or ["—"]:
                    L.append(f"      {line}")
                m_attach = len(m.attachments or [])
                if m_attach:
                    L.append(f"      (приложено файлов: {m_attach})")

    L.append("")
    L.append("─" * 46)
    L.append(
        "Сами файлы и фотографии хранятся в мессенджере MAX — в этой "
        "выгрузке указано только их количество."
    )
    L.append(
        "Изменить данные, потребовать их удаления или обжаловать наши "
        "действия можно письмом в Администрацию."
    )
    return "\n".join(L)


async def _send_export_as_file(event, body: str) -> bool:
    """Отправить выгрузку файлом. True — ушло, False — нужен запасной путь.

    Имя файла берётся из basename загружаемого файла, поэтому пишем во
    временный каталог под русским именем — житель увидит осмысленное
    «Мои данные из чат-бота.txt», а не служебное имя (тот же приём, что
    в `services/policy.py` для политики).
    """
    import tempfile
    from pathlib import Path

    bot = getattr(event, "bot", None)
    if bot is None:
        return False

    tmp_dir = Path(tempfile.mkdtemp(prefix="export_"))
    path = tmp_dir / EXPORT_FILE_NAME
    try:
        path.write_text(body, encoding="utf-8")
        token = await uploads.upload_path(bot, path)
        if token is None:
            log.warning("export: загрузка файла не удалась, уходим в текст")
            return False
        await reply(
            event,
            "Ваши данные — во вложении. Файл можно сохранить и открыть "
            "на телефоне или компьютере.",
            attachments=[uploads.file_attachment(token)],
        )
        return True
    except Exception:
        log.exception("export: отправка файлом не удалась, уходим в текст")
        return False
    finally:
        try:
            path.unlink(missing_ok=True)
            tmp_dir.rmdir()
        except Exception:
            log.debug("export: временный файл не удалён", exc_info=True)


async def cmd_export(event):
    """Скрытая команда: житель получает свои данные файлом
    (право субъекта по 152-ФЗ ст. 14). Не публикуется в /-меню MAX.

    Состав: профиль (имя, телефон, отметки согласий), обращения и ВСЯ
    переписка по каждому — уточнения жителя и ответы оператора, плюс
    количество вложений. Без admin-пометок и системных полей.

    Формат — ЧЕЛОВЕКОЧИТАЕМЫЙ текст, а не JSON: выгрузку читает житель,
    в том числе пожилой, а не программа. Машинный формат право на доступ
    формально закрывал, но фактически оставлял человека наедине с
    латинскими ключами и скобками.
    """
    max_user_id = get_user_id(event)
    if max_user_id is None:
        return
    async with current_user(max_user_id) as (session, user):
        # ВСЯ переписка, а не только последний ответ оператора: статья 14
        # даёт право на доступ к обрабатываемым данным, а уточнения жителя
        # и его вложения — такие же его данные, как текст обращения.
        appeals = await appeals_service.list_for_user(session, user.id, limit=500)
        body = _render_export_for_human(user, appeals)

    # Основной путь — ФАЙЛ: он не упирается ни в лимит длины сообщения,
    # ни в разметку (на этом выгрузка молча срывалась). Текстовая нарезка
    # остаётся запасным путём — право по статье 14 закона № 152-ФЗ не
    # должно зависеть от доступности загрузки файлов.
    if await _send_export_as_file(event, body):
        return

    parts = _chunk_for_messenger(body, limit=EXPORT_CHUNK_CHARS)
    total = len(parts)
    delivered = False
    for index, part in enumerate(parts, start=1):
        header = (
            "Ваши данные:"
            if total == 1
            else f"Ваши данные — часть {index} из {total}:"
        )
        try:
            await reply(event, f"{header}\n\n```\n{part}\n```")
            delivered = True
        except Exception:
            log.exception("export: часть %d из %d не ушла жителю", index, total)

    if not delivered:
        # Молчать нельзя: житель ждёт свои данные и не понимает, почему
        # тишина. Раньше сбой доставки виден был только в логе.
        try:
            await reply(
                event,
                "Не удалось отправить выгрузку — техническая ошибка. "
                "Сообщите об этом в Администрацию, данные подготовим "
                "и передадим другим способом.",
            )
        except Exception:
            log.exception("export: не удалось сообщить жителю о сбое выгрузки")


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
    async def _on_bot_started(event: BotStarted):
        if _is_admin_chat(event):
            return
        await cmd_start(event)

    @dp.bot_stopped()
    async def _on_bot_stopped(event: BotStopped):
        """MAXAPI_DEEP_DIVE §17 fix (P1): житель остановил бота
        (нажал «остановить» в MAX-клиенте, что эквивалентно блокировке
        бота). До этого фикса мы продолжали слать broadcast, каждое
        сообщение возвращалось ошибкой, БД заполнялась failed-записями,
        и реальная аудитория broadcast'а была меньше декларируемой.

        Здесь мы снимаем подписку на рассылку — broadcast-фильтр в
        `services/broadcasts.list_subscriber_targets` использует
        `subscribed_broadcast=True`, теперь житель туда не попадёт. Это
        мягкая мера: житель может в любой момент написать /start снова
        и снова подписаться через UI. Audit-log пишется, чтобы остался
        след для расследования (например, если массовый bot_stopped —
        знак что в рассылке было что-то отталкивающее).
        """
        # Admin-чат не должен генерировать BotStopped (бот всегда в нём),
        # но проверка дешёвая.
        if _is_admin_chat(event):
            return
        try:
            user_id = event.user.user_id
        except AttributeError:
            return
        try:
            async with session_scope() as session:
                user = await users_service.get_or_create(session, user_id)
                if user.subscribed_broadcast:
                    await broadcasts_service.set_subscription(
                        session, user_id, subscribed=False
                    )
                    log.info(
                        "bot_stopped: житель max_user_id=%s остановил бота — "
                        "сняли с рассылки", user_id,
                    )
        except Exception:
            log.exception(
                "bot_stopped: failed to unsubscribe max_user_id=%s",
                user_id,
            )

    @dp.message_created(Command("start"))
    async def _on_start_command(event: MessageCreated):
        if _is_admin_chat(event):
            from aemr_bot.handlers import admin_commands

            await admin_commands.show_op_menu(event, pin=False)
            return
        await cmd_start(event)

    @dp.message_created(Command("help"))
    async def _on_help_command(event: MessageCreated):
        if _is_admin_chat(event):
            from aemr_bot.handlers import admin_commands

            await admin_commands.show_op_menu(event, pin=False)
            return
        await cmd_help(event)

    @dp.message_created(Command("menu"))
    async def _on_menu_command(event: MessageCreated):
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
    async def _on_forget_command(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_forget(event)

    @dp.message_created(Command("cancel"))
    async def _on_cancel_command(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_cancel(event)

    # /export — скрытая команда, не публикуется в /-меню MAX. Право
    # субъекта на выгрузку своих ПДн (152-ФЗ ст. 14). Реальные
    # запросы редкие; нужно для регуляторных проверок.
    @dp.message_created(Command("export"))
    async def _on_export_command(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_export(event)

    @dp.message_created(Command("policy"))
    async def _on_policy_command(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_policy(event)

    @dp.message_created(Command("rules"))
    async def _on_rules_command(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_rules(event)

    @dp.message_created(Command("subscribe"))
    async def _on_subscribe_command(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_subscribe(event)

    @dp.message_created(Command("unsubscribe"))
    async def _on_unsubscribe_command(event: MessageCreated):
        if _is_admin_chat(event):
            await reply(event, texts.CITIZEN_COMMAND_IN_ADMIN_CHAT)
            return
        await cmd_unsubscribe(event)

    @dp.message_created(Command("whoami"))
    async def _on_whoami_command(event: MessageCreated):
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
