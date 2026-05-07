import logging
from datetime import datetime

from maxapi import Dispatcher
from maxapi.types import Command, MessageCreated

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator, ensure_role, get_operator
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import operators as operators_service
from aemr_bot.services import settings_store
from aemr_bot.services import stats as stats_service
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import get_chat_id, get_message_text, get_user_id, is_admin_chat

log = logging.getLogger(__name__)


# Локальные псевдонимы для обратной совместимости с существующими вызовами в этом файле.
_is_admin_chat = is_admin_chat
_get_operator = get_operator
_ensure_operator = ensure_operator
_ensure_role = ensure_role


def _parse_arg(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _get_text(event) -> str:
    """Чтение необработанного текста из события команды (использует utils.event.get_message_text)."""
    return get_message_text(event)


async def _send_stats_xlsx(event, period: str, *, target_chat_id: int | None = None) -> None:
    """Сформировать XLSX за `period` и опубликовать его в админ-группе.

    Единый источник истины как для /stats <период>, так и для кнопки быстрого действия «📊 Статистика
    за сегодня». `target_chat_id` позволяет направлять в текущий чат (по умолчанию: текущее событие) 
    или явно в админскую группу, независимо от того, откуда пришел триггер.
    """
    from aemr_bot.services import uploads

    chat_id = target_chat_id if target_chat_id is not None else get_chat_id(event)
    async with session_scope() as session:
        content, title, count = await stats_service.build_xlsx(session, period)
    if count == 0:
        await event.bot.send_message(chat_id=chat_id, text=texts.OP_STATS_EMPTY)
        return
    filename = f"appeals_{period}_{datetime.now():%Y-%m-%d}.xlsx"
    token = await uploads.upload_bytes(event.bot, content, suffix=".xlsx")
    if token is None:
        await event.bot.send_message(
            chat_id=chat_id,
            text=(
                f"Сформирован XLSX за {title} ({count} обращений), "
                "но загрузить файл не удалось. См. логи бота."
            ),
        )
        return
    await event.bot.send_message(
        chat_id=chat_id,
        text=f"📊 Статистика {title} ({count} обращений). Файл: {filename}",
        attachments=[uploads.file_attachment(token)],
    )


async def run_stats_today(event) -> None:
    """То же действие, что и /stats today, вызывается по кнопке callback.
    Направляет файл в админ-группу (где была нажата кнопка)."""
    if not await _ensure_operator(event):
        return
    await _send_stats_xlsx(event, "today", target_chat_id=cfg.admin_group_id)


async def run_stats(event, period: str) -> None:
    """Универсальный обработчик кнопок «📊 За неделю/За месяц». period —
    одно из today|week|month, как у /stats."""
    if period not in {"today", "week", "month"}:
        return
    if not await _ensure_operator(event):
        return
    await _send_stats_xlsx(event, period, target_chat_id=cfg.admin_group_id)


async def show_full_help(event) -> None:
    """Текстовая команда /op_help, без клавиатуры. Вызывается кнопкой «📋 Все команды»."""
    if not _is_admin_chat(event):
        return
    await event.bot.send_message(chat_id=cfg.admin_group_id, text=texts.OP_HELP)


async def run_open_tickets(event) -> None:
    """То же, что /open_tickets — список неотвеченных обращений в админ-группу.
    Вызывается кнопкой «📋 Открытые обращения»."""
    if not await _ensure_operator(event):
        return
    await _do_open_tickets(event)


async def run_diag(event) -> None:
    """То же, что /diag — короткая сводка состояния бота. Вызывается кнопкой «🛠 Диагностика»."""
    if not await _ensure_operator(event):
        return
    await _do_diag(event)


async def run_backup(event) -> None:
    """То же, что /backup — снять pg_dump в named-volume. Вызывается кнопкой «💾 Снять бэкап»."""
    if not await _ensure_role(event, OperatorRole.IT):
        return
    await _do_backup(event)


async def _do_open_tickets(event) -> None:
    """Список открытых обращений в админ-группу. Общая реализация для
    команды /open_tickets и кнопки «📋 Открытые обращения»."""
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    from aemr_bot.db.models import Appeal, AppealStatus

    async with session_scope() as session:
        query = (
            select(Appeal)
            .where(
                Appeal.status.in_(
                    [AppealStatus.NEW.value, AppealStatus.IN_PROGRESS.value]
                )
            )
            .options(selectinload(Appeal.user))
            .order_by(Appeal.created_at)
        )
        open_appeals = (await session.scalars(query)).all()

    if not open_appeals:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text="🎉 Нет открытых или неотвеченных обращений.",
        )
        return

    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=f"⏳ Найдено неотвеченных обращений: {len(open_appeals)}",
    )

    for appeal in open_appeals:
        user_name = appeal.user.first_name if appeal.user else "—"
        user_id_text = appeal.user.max_user_id if appeal.user else "—"
        # Служебный маркер `[appeal:N]` в конце — это стабильный токен,
        # по которому handlers/operator_reply.py находит обращение при
        # свайп-ответе на эту карточку. Не убирать и не переписывать.
        text = (
            f"❗️ Обращение #{appeal.id}\n"
            f"👤 От: {user_name}\n"
            f"🆔 ID: {user_id_text}\n"
            f"📍 Населённый пункт: {appeal.locality or '—'}\n"
            f"🏠 Адрес: {appeal.address or '—'}\n"
            f"🏷️ Тематика: {appeal.topic or '—'}\n\n"
            f"📝 Текст обращения:\n{appeal.summary or '—'}\n\n"
            f"[appeal:{appeal.id}]"
        )
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=text,
        )


async def _do_diag(event) -> None:
    """Сводка состояния. Общая реализация для команды /diag и кнопки «🛠 Диагностика»."""
    from sqlalchemy import func, select

    from aemr_bot.db.models import (
        Appeal,
        AppealStatus,
        Broadcast,
        BroadcastStatus,
        Event,
        User,
    )

    async with session_scope() as session:
        users_total = await session.scalar(select(func.count()).select_from(User))
        users_blocked = await session.scalar(
            select(func.count()).select_from(User).where(User.is_blocked.is_(True))
        )
        users_subscribed = await session.scalar(
            select(func.count()).select_from(User).where(
                User.subscribed_broadcast.is_(True),
                User.is_blocked.is_(False),
            )
        )
        appeals_total = await session.scalar(select(func.count()).select_from(Appeal))
        appeals_in_progress = await session.scalar(
            select(func.count()).select_from(Appeal).where(
                Appeal.status.in_([
                    AppealStatus.NEW.value,
                    AppealStatus.IN_PROGRESS.value,
                ])
            )
        )
        broadcasts_done = await session.scalar(
            select(func.count()).select_from(Broadcast).where(
                Broadcast.status == BroadcastStatus.DONE.value
            )
        )
        broadcasts_failed = await session.scalar(
            select(func.count()).select_from(Broadcast).where(
                Broadcast.status == BroadcastStatus.FAILED.value
            )
        )
        events_total = await session.scalar(select(func.count()).select_from(Event))
        last_event = await session.scalar(select(func.max(Event.received_at)))

    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=(
            "🛠️ Диагностика:\n"
            f"• Жителей: {users_total or 0} "
            f"(подписаны: {users_subscribed or 0}, заблокированы: {users_blocked or 0})\n"
            f"• Обращений: {appeals_total or 0} "
            f"(в работе: {appeals_in_progress or 0})\n"
            f"• Рассылок: ✅ {broadcasts_done or 0} / ⚠️ {broadcasts_failed or 0}\n"
            f"• События: всего {events_total or 0}, последнее {last_event or '—'}\n"
            f"• Режим: {cfg.bot_mode}\n"
            f"• Лимит ответа: {cfg.answer_max_chars}\n"
            f"• SLA: {cfg.sla_response_hours}ч"
        ),
    )


async def _do_backup(event) -> None:
    """Снять pg_dump прямо сейчас. Общая реализация для команды /backup и
    кнопки «💾 Снять бэкап»."""
    from aemr_bot.services import cron as cron_service

    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text="🗄️ Запускаю pg_dump… Это может занять несколько секунд.",
    )
    try:
        out = await cron_service._backup_db()
    except Exception as e:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id, text=f"⚠️ Бэкап упал: {e}"
        )
        return
    if out is None:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=(
                "⚠️ Бэкап не выполнен. Проверьте логи бота "
                "(`docker compose logs bot --tail 50`)."
            ),
        )
        return
    size_kb = out.stat().st_size // 1024
    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=(
            f"✅ Бэкап готов: `{out.name}` ({size_kb} КБ).\n"
            f"Лежит в named-volume `backups` контейнера."
        ),
    )


def register(dp: Dispatcher) -> None:
    @dp.message_created(Command("open_tickets"))
    async def cmd_open_tickets(event: MessageCreated):
        """Список открытых обращений в админ-группу.

        Доступно любой роли оператора. На swipe-reply по этим карточкам
        реагирует регулярка `r"Обращение #(\\d+)"` в `operator_reply.py`,
        потому что у этих сообщений нет `appeals.admin_message_id` —
        оригинальная карточка уже была опубликована при создании.
        """
        if not await _ensure_operator(event):
            return
        await _do_open_tickets(event)

    @dp.message_created(Command("stats"))
    async def cmd_stats(event: MessageCreated):
        if not await _ensure_operator(event):
            return
        period = (_parse_arg(_get_text(event)) or "today").lower()
        if period not in {"today", "week", "month"}:
            await event.message.answer("Используйте: /stats today | week | month")
            return
        await _send_stats_xlsx(event, period)

    @dp.message_created(Command("reply"))
    async def cmd_reply(event: MessageCreated):
        if not _is_admin_chat(event):
            return
        text = _get_text(event)
        # /reply <id_обращения> <текст...>
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await event.message.answer(
                "Используйте: /reply <номер_обращения> <текст ответа>\n"
                "Например: /reply 42 Здравствуйте, ваше обращение рассмотрено."
            )
            return
        try:
            appeal_id = int(parts[1])
        except ValueError:
            await event.message.answer(
                f"«{parts[1]}» — не номер обращения. Пример: /reply 42 ваш текст."
            )
            return
        reply_text = parts[2].strip()
        if not reply_text:
            await event.message.answer("Текст ответа не может быть пустым.")
            return
        from aemr_bot.handlers import operator_reply as op_reply
        await op_reply.handle_command_reply(event, appeal_id, reply_text)

    @dp.message_created(Command("reopen"))
    async def cmd_reopen(event: MessageCreated):
        if not await _ensure_operator(event):
            return
        arg = _parse_arg(_get_text(event))
        try:
            appeal_id = int(arg)
        except ValueError:
            await event.message.answer("Используйте: /reopen <номер>")
            return
        async with session_scope() as session:
            ok = await appeals_service.reopen(session, appeal_id)
            if ok:
                await operators_service.write_audit(
                    session,
                    operator_max_user_id=get_user_id(event),
                    action="reopen",
                    target=f"appeal #{appeal_id}",
                )
        await event.message.answer(
            texts.OP_APPEAL_REOPENED.format(number=appeal_id) if ok
            else texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id)
        )

    @dp.message_created(Command("close"))
    async def cmd_close(event: MessageCreated):
        if not await _ensure_operator(event):
            return
        arg = _parse_arg(_get_text(event))
        try:
            appeal_id = int(arg)
        except ValueError:
            await event.message.answer("Используйте: /close <номер>")
            return
        async with session_scope() as session:
            ok = await appeals_service.close(session, appeal_id)
            if ok:
                await operators_service.write_audit(
                    session,
                    operator_max_user_id=get_user_id(event),
                    action="close",
                    target=f"appeal #{appeal_id}",
                )
        await event.message.answer(
            texts.OP_APPEAL_CLOSED.format(number=appeal_id) if ok
            else texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id)
        )

    @dp.message_created(Command("erase"))
    async def cmd_erase(event: MessageCreated):
        if not await _ensure_role(event, OperatorRole.IT):
            return
        arg = _parse_arg(_get_text(event))
        usage_msg = (
            "Используйте: /erase max_user_id=<id> или /erase phone=+7..."
        )
        if not arg:
            await event.message.answer(usage_msg)
            return

        target_id: int | None = None
        phone: str = ""
        if arg.startswith("max_user_id="):
            try:
                target_id = int(arg.split("=", 1)[1])
            except ValueError:
                await event.message.answer("Некорректный max_user_id.")
                return
        elif arg.startswith("phone="):
            phone = arg.split("=", 1)[1].strip()
            if not phone:
                await event.message.answer("Не указан телефон. Пример: /erase phone=+79001234567")
                return
        else:
            await event.message.answer(usage_msg)
            return

        # Анонимизация и запись в audit_log должны фиксироваться атомарно
        # согласно 152-ФЗ — без этого сбой в БД между этими двумя действиями мог бы
        # оставить ПДн стертыми без следа того, кто это инициировал.
        async with session_scope() as session:
            if target_id is not None:
                ok = await users_service.erase_pdn(session, target_id)
            else:
                target_id = await users_service.erase_pdn_by_phone(session, phone)
                ok = target_id is not None
            if ok and target_id is not None:
                await operators_service.write_audit(
                    session,
                    operator_max_user_id=get_user_id(event),
                    action="erase",
                    target=f"user max_id={target_id}",
                )

        if ok and target_id is not None:
            await event.message.answer(
                texts.OP_USER_ERASED.format(max_user_id=target_id)
            )
        else:
            await event.message.answer("Пользователь не найден.")

    @dp.message_created(Command("setting"))
    async def cmd_setting(event: MessageCreated):
        if not await _ensure_role(event, OperatorRole.IT):
            return
        text = _get_text(event)

        arg = _parse_arg(text)

        if not arg or arg == "list":
            async with session_scope() as session:
                keys = await settings_store.list_keys(session)
            await event.message.answer("Доступные настройки:\n" + "\n".join(f"• {k}" for k in keys))
            return

        parts = arg.split(maxsplit=1)
        if len(parts) != 2:
            await event.message.answer("Используйте: /setting <key> <value>")
            return
        key, raw_value = parts
        import json
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        ok, reason = settings_store.validate(key, value)
        if not ok:
            await event.message.answer(f"⚠️ Настройка не обновлена: {reason}")
            return
        # Полное новое значение хранится в `settings.value` — его дублирование в
        # audit_log сделает таблицу аудита вторым хранилищем приветственных текстов,
        # списков контактов и т.п., чего мы не хотим, так как она будет бесконтрольно расти
        # и может содержать ПДн, если оператор по ошибке вставит данные гражданина в
        # текстовый ключ. Аудируем только тип/длину.
        details_meta: dict[str, object] = {"kind": type(value).__name__}
        if isinstance(value, str):
            details_meta["chars"] = len(value)
        elif isinstance(value, list):
            details_meta["items"] = len(value)
        async with session_scope() as session:
            await settings_store.set_value(session, key, value)
            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event),
                action="setting_update",
                target=key,
                details=details_meta,
            )
        await event.message.answer(texts.OP_SETTING_UPDATED.format(key=key))

    @dp.message_created(Command("diag"))
    async def cmd_diag(event: MessageCreated):
        if not await _ensure_operator(event):
            return
        await _do_diag(event)

    @dp.message_created(Command("backup"))
    async def cmd_backup(event: MessageCreated):
        if not await _ensure_role(event, OperatorRole.IT):
            return
        await _do_backup(event)

    @dp.message_created(Command("op_help"))
    async def cmd_op_help(event: MessageCreated):
        if not _is_admin_chat(event):
            return
        from aemr_bot import keyboards as kbds
        from aemr_bot.utils.event import extract_message_id

        sent = await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=texts.OP_HELP,
            attachments=[kbds.op_help_keyboard()],
        )
        # Попытка закрепить (best-effort), чтобы памятка оператора всегда была близко к верху
        # админ-группы. Если уже что-то закреплено — pin перезапишет, MAX
        # позволяет одно закреплённое сообщение на чат. Не критично если
        # операция упадёт — координатор всегда может вызвать /op_help снова.
        mid = extract_message_id(sent)
        if mid:
            try:
                await event.bot.pin_message(
                    chat_id=cfg.admin_group_id, message_id=mid, notify=False
                )
            except Exception:
                log.exception("pin_message для /op_help не удался")

    @dp.message_created(Command("add_operators"))
    async def cmd_add_operators(event: MessageCreated):
        # Только для IT: массовое назначение ролей — это примитив повышения привилегий
        # (актор контролирует строку роли, которую он выдает). Роль координатора
        # намеренно не имеет команд /erase и /setting; разрешение ей выдавать права
        # IT здесь позволило бы координатору повысить себя и затем стереть
        # ПДн или изменить настройки. Держите это в строгом соответствии
        # с авторизацией /erase и /setting.
        if not await _ensure_role(event, OperatorRole.IT):
            return
        text = _get_text(event)
        # /add_operators может сопровождаться либо одной строкой, либо несколькими
        # строками — отбрасываем токен команды и разбираем то, что осталось.
        parts = text.split(maxsplit=1)
        body = parts[1] if len(parts) > 1 else ""
        if not body.strip():
            await event.message.answer(texts.OP_ADD_OPERATORS_USAGE)
            return

        valid_roles = {r.value for r in OperatorRole}
        added = 0
        updated = 0
        errors: list[str] = []
        actor_id = get_user_id(event)

        async with session_scope() as session:
            for raw_line in body.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(maxsplit=2)
                if len(parts) < 3:
                    errors.append(f"«{line}» — нужно: <max_user_id> <role> <ФИО>")
                    continue
                id_str, role_str, full_name = parts
                try:
                    target_id = int(id_str)
                except ValueError:
                    errors.append(f"«{line}» — max_user_id не число")
                    continue
                role_value = role_str.lower()
                if role_value not in valid_roles:
                    errors.append(
                        f"«{line}» — роль «{role_str}» неизвестна, "
                        f"доступны: {', '.join(sorted(valid_roles))}"
                    )
                    continue
                role_enum = OperatorRole(role_value)
                # Глубокая защита: никогда не позволяйте актору переписывать свою собственную
                # строку роли через эту команду. Изменения ролей для себя должны происходить
                # через psql / эскалацию runbook, чтобы они были явными.
                if actor_id is not None and target_id == actor_id:
                    errors.append(
                        f"«{line}» — нельзя изменить свою роль через эту команду"
                    )
                    continue
                existed = await operators_service.get(session, target_id) is not None
                await operators_service.upsert(
                    session, max_user_id=target_id, full_name=full_name, role=role_enum
                )
                await operators_service.write_audit(
                    session,
                    operator_max_user_id=actor_id,
                    action="operator_upsert",
                    target=f"user max_id={target_id}",
                    details={"role": role_value, "full_name": full_name},
                )
                if existed:
                    updated += 1
                else:
                    added += 1

        report = texts.OP_ADD_OPERATORS_RESULT.format(
            added=added, updated=updated, errors=len(errors)
        )
        if errors:
            report += "\n\nОшибки:\n" + "\n".join(f"• {e}" for e in errors)
        await event.message.answer(report)