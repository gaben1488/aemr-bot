from datetime import datetime

from maxapi import Dispatcher
from maxapi.types import Command, MessageCreated

from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import operators as operators_service
from aemr_bot.services import settings_store
from aemr_bot.services import stats as stats_service
from aemr_bot.services import users as users_service


def _is_admin_chat(event) -> bool:
    chat_id = getattr(event, "chat_id", None)
    return cfg.admin_group_id is not None and chat_id == cfg.admin_group_id


async def _ensure_operator(event) -> bool:
    if not _is_admin_chat(event):
        return False
    author_id = getattr(event.user, "user_id", None) if getattr(event, "user", None) else None
    if author_id is None:
        return False
    async with session_scope() as session:
        return await operators_service.is_operator(session, author_id)


def _parse_arg(text: str) -> str:
    parts = text.split(maxsplit=1)
    return parts[1].strip() if len(parts) > 1 else ""


def register(dp: Dispatcher) -> None:
    @dp.message_created(Command("stats"))
    async def cmd_stats(event: MessageCreated):
        if not await _ensure_operator(event):
            return
        body = getattr(event.message, "body", None) or event.message
        text = getattr(body, "text", "") or ""
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
        # The exact upload API depends on maxapi version. We use raw bytes.
        try:
            await event.bot.send_document(
                chat_id=event.chat_id,
                source=content,
                filename=filename,
                caption=f"📊 Статистика {title} ({count} обращений)",
            )
        except AttributeError:
            await event.message.answer(
                f"Сформирован XLSX за {title} ({count} обращений). "
                "Загрузка файлов API ещё не подключена — обновите maxapi."
            )

    @dp.message_created(Command("reopen"))
    async def cmd_reopen(event: MessageCreated):
        if not await _ensure_operator(event):
            return
        body = getattr(event.message, "body", None) or event.message
        arg = _parse_arg(getattr(body, "text", "") or "")
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
                    operator_max_user_id=event.user.user_id,
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
        body = getattr(event.message, "body", None) or event.message
        arg = _parse_arg(getattr(body, "text", "") or "")
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
                    operator_max_user_id=event.user.user_id,
                    action="close",
                    target=f"appeal #{appeal_id}",
                )
        await event.message.answer(
            texts.OP_APPEAL_CLOSED.format(number=appeal_id) if ok
            else texts.OP_APPEAL_NOT_FOUND.format(number=appeal_id)
        )

    @dp.message_created(Command("erase"))
    async def cmd_erase(event: MessageCreated):
        if not await _ensure_operator(event):
            return
        body = getattr(event.message, "body", None) or event.message
        arg = _parse_arg(getattr(body, "text", "") or "")
        if not arg.startswith("max_user_id="):
            await event.message.answer("Используйте: /erase max_user_id=<id>")
            return
        try:
            target_id = int(arg.split("=", 1)[1])
        except ValueError:
            await event.message.answer("Некорректный max_user_id.")
            return
        async with session_scope() as session:
            ok = await users_service.erase_pdn(session, target_id)
            if ok:
                await operators_service.write_audit(
                    session,
                    operator_max_user_id=event.user.user_id,
                    action="erase",
                    target=f"user max_id={target_id}",
                )
        if ok:
            await event.message.answer(texts.OP_USER_ERASED.format(max_user_id=target_id))
        else:
            await event.message.answer("Пользователь не найден.")

    @dp.message_created(Command("setting"))
    async def cmd_setting(event: MessageCreated):
        if not await _ensure_operator(event):
            return
        body = getattr(event.message, "body", None) or event.message
        text = getattr(body, "text", "") or ""
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
        # Try parse as JSON, fall back to raw string
        import json
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            value = raw_value
        async with session_scope() as session:
            await settings_store.set_value(session, key, value)
            await operators_service.write_audit(
                session,
                operator_max_user_id=event.user.user_id,
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

    @dp.message_created(Command("op_help"))
    async def cmd_op_help(event: MessageCreated):
        if not _is_admin_chat(event):
            return
        await event.message.answer(texts.OP_HELP)
