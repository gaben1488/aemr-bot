"""Управление операторами через wizard «👥 Добавить».

Выделено из handlers/admin_commands.py (рефакторинг 2026-05-10).

Wizard в три шага:
- awaiting_id   — оператор вводит max_user_id будущего сотрудника
- awaiting_role — выбор роли через op_role_picker_keyboard
- awaiting_name — ввод ФИО

State хранится in-memory в _op_wizards с TTL 5 минут. Cross-handler
доступ (cancel в appeal.py) идёт через .pop() — будет переведён на
services/wizard_registry в следующих итерациях.
"""
from __future__ import annotations

import logging
import time as _time_op

from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.services import operators as operators_service
from aemr_bot.utils.event import get_user_id

log = logging.getLogger(__name__)

# Wizard state, in-memory.
# Шаги: idle → awaiting_id → awaiting_role → awaiting_name. ID и ФИО —
# текстом, роль — отдельной кнопкой. На каждом шаге доступна «Отмена»;
# по таймауту 5 минут wizard сбрасывается.
_op_wizards: dict[int, dict] = {}
_OP_WIZARD_TTL_SEC = 300.0


def _op_wizard_get(operator_id: int) -> dict | None:
    state = _op_wizards.get(operator_id)
    if state is None:
        return None
    if _time_op.monotonic() > state.get("expires_at", 0):
        _op_wizards.pop(operator_id, None)
        return None
    return state


def _op_wizard_set(operator_id: int, **kwargs) -> dict:
    state = _op_wizards.get(operator_id) or {}
    state.update(kwargs)
    state["expires_at"] = _time_op.monotonic() + _OP_WIZARD_TTL_SEC
    _op_wizards[operator_id] = state
    # Best-effort persist в БД, чтобы wizard переживал рестарт бота
    # (миграция 0011 + services/wizard_persist). expires_at — monotonic
    # offset, в БД не нужен (там свой DateTime ttl).
    snapshot = {k: v for k, v in state.items() if k != "expires_at"}
    from aemr_bot.services import wizard_registry as _wr
    _wr.schedule_persist_op(operator_id, snapshot)
    return state


def _op_wizard_drop(operator_id: int) -> None:
    _op_wizards.pop(operator_id, None)
    from aemr_bot.services import wizard_registry as _wr
    _wr.schedule_persist_op(operator_id, None)


async def run_operators_menu(event) -> None:
    """Меню «👥 Операторы» в админ-панели для роли it. Точка входа в
    кнопочный wizard добавления и просмотр списка."""
    from aemr_bot import keyboards as kbds

    if not await ensure_role(event, OperatorRole.IT):
        return
    await event.bot.send_message(
        chat_id=cfg.admin_group_id,
        text=(
            "👥 Управление операторами\n"
            "──────────\n"
            "Здесь можно зарегистрировать нового сотрудника или посмотреть "
            "текущий список. Снять оператора с роли пока можно только через "
            "/add_operators с тем же max_user_id и нужной ролью."
        ),
        attachments=[kbds.op_operators_menu_keyboard()],
    )


async def run_operators_action(event, payload: str) -> None:
    """Подменю «Операторы»: добавить, список, отмена. payload вида
    `op:opadd:start` / `op:opadd:role:N` / `op:opadd:cancel` /
    `op:opadd:list`."""
    from aemr_bot.utils.event import ack_callback

    if not await ensure_role(event, OperatorRole.IT):
        return
    operator_id = get_user_id(event)
    if operator_id is None:
        await ack_callback(event)
        return
    suffix = payload.removeprefix("op:opadd:")
    await ack_callback(event)
    if suffix == "start":
        # Сбрасываем чужие wizard'ы и reply-intent этого оператора.
        from aemr_bot.handlers import broadcast as broadcast_handler
        from aemr_bot.handlers import operator_reply as op_reply

        broadcast_handler._wizards.pop(operator_id, None)
        op_reply.drop_reply_intent(operator_id)

        _op_wizard_set(operator_id, step="awaiting_id")
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=(
                "👥 Шаг 1 из 3 — введите max_user_id будущего оператора.\n"
                "Узнать его — попросите человека написать боту в личке /whoami "
                "и прислать вам число из ответа."
            ),
        )
        return
    if suffix == "list":
        async with session_scope() as session:
            ops = await operators_service.list_active(session)
        if not ops:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text="Список операторов пуст.",
            )
            return
        lines = ["👥 Активные операторы:"]
        for op in ops:
            lines.append(f"• #{op.max_user_id} · {op.role} · {op.full_name}")
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text="\n".join(lines),
        )
        return
    if suffix == "cancel":
        _op_wizard_drop(operator_id)
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text="Регистрация оператора отменена.",
        )
        return
    if suffix.startswith("role:"):
        role = suffix.split(":", 1)[1]
        valid = {r.value for r in OperatorRole}
        if role not in valid:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text=f"Роль «{role}» неизвестна.",
            )
            return
        state = _op_wizard_get(operator_id)
        if state is None or state.get("step") != "awaiting_role":
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text="Мастер закрыт. Откройте «👥 Операторы → Добавить» заново.",
            )
            return
        _op_wizard_set(operator_id, role=role, step="awaiting_name")
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=(
                f"👥 Шаг 3 из 3 — роль {role} выбрана. Теперь введите ФИО "
                f"оператора одним сообщением. Например: «Иванова Анна Петровна»."
            ),
        )


async def handle_operators_wizard_text(event, text: str) -> bool:
    """Перехватчик текстовых сообщений в админ-группе на стороне wizard'а.
    Возвращает True, если сообщение поглощено."""
    operator_id = get_user_id(event)
    if operator_id is None:
        return False
    state = _op_wizard_get(operator_id)
    if state is None:
        return False
    step = state.get("step")
    if step == "awaiting_id":
        try:
            target_id = int(text.strip())
        except ValueError:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text="Это не число. Введите max_user_id (целое положительное).",
            )
            return True
        _op_wizard_set(operator_id, target_id=target_id, step="awaiting_role")
        from aemr_bot import keyboards as kbds

        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=f"👥 Шаг 2 из 3 — id {target_id} принят. Выберите роль:",
            attachments=[kbds.op_role_picker_keyboard()],
        )
        return True
    if step == "awaiting_name":
        full_name = text.strip()
        if len(full_name) < 2:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text="ФИО слишком короткое. Введите полностью.",
            )
            return True
        target_id = int(state["target_id"])
        role = state["role"]
        # Самомодификация через wizard заблокирована, как и в /add_operators.
        if target_id == operator_id:
            _op_wizard_drop(operator_id)
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text="Изменить свою роль через мастера нельзя.",
            )
            return True
        async with session_scope() as session:
            existed = await operators_service.get(session, target_id) is not None
            await operators_service.upsert(
                session,
                max_user_id=target_id,
                full_name=full_name,
                role=OperatorRole(role),
            )
            await operators_service.write_audit(
                session,
                operator_max_user_id=operator_id,
                action="operator_upsert",
                target=f"user max_id={target_id}",
                details={"role": role, "full_name": full_name},
            )
        _op_wizard_drop(operator_id)
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=(
                f"✅ {'Обновлено' if existed else 'Добавлено'}: "
                f"{full_name} · {role} · #{target_id}"
            ),
        )
        return True
    return False
