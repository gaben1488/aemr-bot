"""Slash-команды оператора в админ-группе.

После рефакторинга 2026-05-10 этот файл — тонкий entry-point:
- `register(dp)` подписывает 11 команд: open_tickets, stats, reply,
  reopen, close, erase, setting, diag, backup, op_help, add_operators
- Каждая команда делегирует в подмодуль admin_*

Реальная логика разнесена по 6 модулям:
- `admin_panel.py` — show_op_menu, /op_help, /open_tickets, /diag, /backup
- `admin_stats.py` — /stats + кнопочные run_stats_*
- `admin_operators.py` — wizard «👥 Добавить оператора», /add_operators
  логика разделена с этим файлом (текстовая команда здесь, wizard там)
- `admin_settings.py` — /setting + кнопочный run_settings_*
- `admin_audience.py` — меню «📊 Аудитория и согласия»
- `admin_appeal_ops.py` — кнопочные операции над обращением:
  reply / reopen / close / block / erase для конкретного appeal_id

Re-exports внизу: appeal.py зовёт `admin_commands.show_op_menu(...)`,
`admin_commands.run_stats(...)` и т.п. — для обратной совместимости
все эти имена доступны из этого модуля.
"""
from __future__ import annotations

import logging

from maxapi import Dispatcher
from maxapi.types import Command, MessageCreated

from aemr_bot import texts
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator, ensure_role
from aemr_bot.handlers.admin_appeal_ops import (
    run_block_for_appeal,
    run_close,
    run_erase_for_appeal,
    run_reopen,
    run_reply_cancel,
    run_reply_intent,
)
from aemr_bot.handlers.admin_audience import (
    run_audience_action,
    run_audience_menu,
)
from aemr_bot.handlers.admin_operators import (
    _op_wizard_drop,
    _op_wizard_get,
    _op_wizard_set,
    _op_wizards,
    handle_operators_wizard_text,
    run_operators_action,
    run_operators_menu,
)
from aemr_bot.handlers.admin_panel import (
    _do_backup,
    _do_diag,
    _do_open_tickets,
    get_text as _get_text,
    parse_arg as _parse_arg,
    run_backup,
    run_diag,
    run_open_tickets,
    show_op_menu,
)
from aemr_bot.handlers.admin_settings import (
    run_settings_action,
    run_settings_menu,
)
from aemr_bot.handlers.admin_stats import (
    _send_stats_xlsx,
    run_stats,
    run_stats_menu,
    run_stats_today,
)
from aemr_bot.services import operators as operators_service
from aemr_bot.services import settings_store
from aemr_bot.services import users as users_service
from aemr_bot.utils.event import get_user_id, is_admin_chat

log = logging.getLogger(__name__)

# Локальные алиасы для обратной совместимости.
_is_admin_chat = is_admin_chat
_ensure_operator = ensure_operator
_ensure_role = ensure_role


# Re-export для appeal.py (callback dispatcher) и других мест.
__all__ = [
    "register",
    # Открытое меню оператора
    "show_op_menu",
    # Stats
    "_send_stats_xlsx",
    "run_stats",
    "run_stats_menu",
    "run_stats_today",
    # Operators wizard
    "_op_wizards",
    "_op_wizard_drop",
    "_op_wizard_get",
    "_op_wizard_set",
    "handle_operators_wizard_text",
    "run_operators_action",
    "run_operators_menu",
    # Settings
    "run_settings_action",
    "run_settings_menu",
    # Audience
    "run_audience_action",
    "run_audience_menu",
    # Per-appeal ops
    "run_block_for_appeal",
    "run_close",
    "run_erase_for_appeal",
    "run_reopen",
    "run_reply_cancel",
    "run_reply_intent",
    # Common
    "_do_backup",
    "_do_diag",
    "_do_open_tickets",
    "run_backup",
    "run_diag",
    "run_open_tickets",
]


def register(dp: Dispatcher) -> None:
    @dp.message_created(Command("open_tickets"))
    async def cmd_open_tickets(event: MessageCreated):
        """Список открытых обращений в админ-группу.

        На swipe-reply по этим карточкам реагирует регулярка
        `r"Обращение #(\\d+)"` в operator_reply.py, потому что у этих
        сообщений нет appeals.admin_message_id — оригинальная карточка
        уже была опубликована при создании.
        """
        if not await _ensure_operator(event):
            return
        await _do_open_tickets(event)

    @dp.message_created(Command("stats"))
    async def cmd_stats(event: MessageCreated):
        from aemr_bot.services.stats import VALID_PERIODS

        if not await _ensure_operator(event):
            return
        period = (_parse_arg(_get_text(event)) or "today").lower()
        if period not in VALID_PERIODS:
            await event.message.answer(
                "Используйте: /stats today | week | month | quarter | "
                "half_year | year | all"
            )
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
        from aemr_bot.services import appeals as appeals_service

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
        from aemr_bot.services import appeals as appeals_service

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
            # Защита от стирания anonymous-user sentinel.
            from aemr_bot.db.models import ANONYMOUS_MAX_USER_ID
            if target_id == ANONYMOUS_MAX_USER_ID:
                await event.message.answer(
                    f"⛔ Запрещено: max_user_id={ANONYMOUS_MAX_USER_ID} — "
                    "это техническая запись anonymous-user, на которой "
                    "висят обезличенные обращения по 152-ФЗ. "
                    "Стирать нельзя."
                )
                return
        elif arg.startswith("phone="):
            phone = arg.split("=", 1)[1].strip()
            if not phone:
                await event.message.answer(
                    "Не указан телефон. Пример: /erase phone=+79001234567"
                )
                return
        else:
            await event.message.answer(usage_msg)
            return

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
            await event.message.answer(
                "Доступные настройки:\n" + "\n".join(f"• {k}" for k in keys)
            )
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
        # Полное новое значение хранится в settings.value — его дублирование
        # в audit_log сделает таблицу аудита вторым хранилищем приветственных
        # текстов и т.п., чего мы не хотим (бесконтрольный рост + ПДн риск).
        # Аудируем только тип/длину.
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
        await show_op_menu(event, pin=True)

    @dp.message_created(Command("add_operators"))
    async def cmd_add_operators(event: MessageCreated):
        # Только для IT: массовое назначение ролей — это примитив повышения
        # привилегий. Роль координатора намеренно не имеет команд /erase
        # и /setting; разрешение ей выдавать права IT здесь позволило бы
        # координатору повысить себя и затем стереть ПДн. Держите в строгом
        # соответствии с авторизацией /erase и /setting.
        if not await _ensure_role(event, OperatorRole.IT):
            return
        text = _get_text(event)
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
                line_parts = line.split(maxsplit=2)
                if len(line_parts) < 3:
                    errors.append(f"«{line}» — нужно: <max_user_id> <role> <ФИО>")
                    continue
                id_str, role_str, full_name = line_parts
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
                # Глубокая защита от self-promotion.
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
