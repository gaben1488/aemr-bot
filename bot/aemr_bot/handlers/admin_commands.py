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


# Local aliases for backward-compat with existing call sites in this file.
_is_admin_chat = is_admin_chat
_get_operator = get_operator
_ensure_operator = ensure_operator
_ensure_role = ensure_role


def _parse_arg(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def _get_text(event) -> str:
    """Read raw text from a command event (uses utils.event.get_message_text)."""
    return get_message_text(event)


async def run_stats_today(event) -> None:
    """Same payload as /stats today, but invokable from a callback button.
    Reaches the same code path: sends the XLSX into the admin group."""
    if not await _ensure_operator(event):
        return
    async with session_scope() as session:
        content, title, count = await stats_service.build_xlsx(session, "today")
    if count == 0:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id, text=texts.OP_STATS_EMPTY
        )
        return
    filename = f"appeals_today_{datetime.now():%Y-%m-%d}.xlsx"
    from aemr_bot.services import uploads
    token = await uploads.upload_bytes(event.bot, content, suffix=".xlsx")
    if token is None:
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=(
                f"Сформирован XLSX за {title} ({count} обращений), "
                "но загрузить файл не удалось. См. логи бота."
            ),
        )
        return
    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=f"📊 Статистика {title} ({count} обращений). Файл: {filename}",
        attachments=[uploads.file_attachment(token)],
    )


async def show_full_help(event) -> None:
    """Plain-text /op_help, no keyboard. Triggered by «📋 Все команды» button."""
    if not _is_admin_chat(event):
        return
    await event.bot.send_message(chat_id=cfg.admin_group_id, text=texts.OP_HELP)


def register(dp: Dispatcher) -> None:
    @dp.message_created(Command("stats"))
    async def cmd_stats(event: MessageCreated):
        if not await _ensure_operator(event):
            return
        text = _get_text(event)

        period = (_parse_arg(text) or "today").lower()
        if period not in {"today", "week", "month"}:
            await event.message.answer("Используйте: /stats today | week | month")
            return
        async with session_scope() as session:
            content, title, count = await stats_service.build_xlsx(session, period)
        if count == 0:
            await event.message.answer(texts.OP_STATS_EMPTY)
            return
        filename = f"appeals_{period}_{datetime.now():%Y-%m-%d}.xlsx"
        from aemr_bot.services import uploads
        token = await uploads.upload_bytes(event.bot, content, suffix=".xlsx")
        if token is None:
            await event.message.answer(
                f"Сформирован XLSX за {title} ({count} обращений), "
                "но загрузить файл не удалось. См. логи бота."
            )
            return
        await event.bot.send_message(
            chat_id=get_chat_id(event),
            text=f"📊 Статистика {title} ({count} обращений). Файл: {filename}",
            attachments=[uploads.file_attachment(token)],
        )

    @dp.message_created(Command("reply"))
    async def cmd_reply(event: MessageCreated):
        if not _is_admin_chat(event):
            return
        text = _get_text(event)
        # /reply <appeal_id> <text...>
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
        if arg.startswith("max_user_id="):
            try:
                target_id = int(arg.split("=", 1)[1])
            except ValueError:
                await event.message.answer("Некорректный max_user_id.")
                return
            async with session_scope() as session:
                ok = await users_service.erase_pdn(session, target_id)
        elif arg.startswith("phone="):
            phone = arg.split("=", 1)[1].strip()
            if not phone:
                await event.message.answer("Не указан телефон. Пример: /erase phone=+79001234567")
                return
            async with session_scope() as session:
                target_id = await users_service.erase_pdn_by_phone(session, phone)
            ok = target_id is not None
        else:
            await event.message.answer(usage_msg)
            return

        if ok and target_id is not None:
            async with session_scope() as session:
                await operators_service.write_audit(
                    session,
                    operator_max_user_id=get_user_id(event),
                    action="erase",
                    target=f"user max_id={target_id}",
                )
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
        async with session_scope() as session:
            await settings_store.set_value(session, key, value)
            await operators_service.write_audit(
                session,
                operator_max_user_id=get_user_id(event),
                action="setting_update",
                target=key,
                details={"value": value},
            )
        await event.message.answer(texts.OP_SETTING_UPDATED.format(key=key))

    @dp.message_created(Command("diag"))
    async def cmd_diag(event: MessageCreated):
        if not await _ensure_operator(event):
            return
        from sqlalchemy import func, select

        from aemr_bot.db.models import Appeal, Event, User

        async with session_scope() as session:
            users_total = await session.scalar(select(func.count()).select_from(User))
            appeals_total = await session.scalar(select(func.count()).select_from(Appeal))
            last_event = await session.scalar(select(func.max(Event.received_at)))

        await event.message.answer(
            "🛠️ Диагностика:\n"
            f"• Пользователей: {users_total or 0}\n"
            f"• Обращений: {appeals_total or 0}\n"
            f"• Последнее событие: {last_event or '—'}\n"
            f"• Режим: {cfg.bot_mode}\n"
            f"• Лимит ответа: {cfg.answer_max_chars}\n"
            f"• SLA: {cfg.sla_response_hours}ч"
        )

    @dp.message_created(Command("backup"))
    async def cmd_backup(event: MessageCreated):
        if not await _ensure_role(event, OperatorRole.IT):
            return
        from aemr_bot.services import cron as cron_service

        await event.message.answer(
            "🗄️ Запускаю pg_dump… Это может занять несколько секунд."
        )
        try:
            out = await cron_service._backup_db()
        except Exception as e:
            await event.message.answer(f"⚠️ Бэкап упал: {e}")
            return
        if out is None:
            await event.message.answer(
                "⚠️ Бэкап не выполнен. Проверьте логи бота "
                "(`docker compose logs bot --tail 50`)."
            )
            return
        size_kb = out.stat().st_size // 1024
        await event.message.answer(
            f"✅ Бэкап готов: `{out.name}` ({size_kb} КБ).\n"
            f"Лежит в named-volume `backups` контейнера."
        )

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
        # Best-effort pin so the operator memo is always near the top of the
        # admin group. Если уже что-то закреплено — pin перезапишет, MAX
        # позволяет одно закреплённое сообщение на чат. Не критично если
        # операция упадёт — координатор всегда может вызвать /op_help снова.
        mid = extract_message_id(sent)
        if mid:
            try:
                await event.bot.pin_message(
                    chat_id=cfg.admin_group_id, message_id=mid, notify=False
                )
            except Exception:
                log.exception("pin_message for /op_help failed")

    @dp.message_created(Command("add_operators"))
    async def cmd_add_operators(event: MessageCreated):
        # IT-only: bulk role assignment is a privilege-escalation primitive
        # (the actor controls the role string they hand out). Coordinator
        # role intentionally lacks /erase and /setting; allowing it to grant
        # IT here would let a coordinator promote themselves and then wipe
        # PII or change live settings. Keep this in lockstep with /erase
        # and /setting authorization.
        if not await _ensure_role(event, OperatorRole.IT):
            return
        text = _get_text(event)
        # /add_operators may be followed by either a single line or multiple
        # lines — drop the command token and parse what's left.
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
                # Defense-in-depth: never let an actor rewrite their own role
                # row through this command. Role changes for self must go
                # through psql / runbook escalation so they're explicit.
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
