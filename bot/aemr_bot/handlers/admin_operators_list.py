"""Список операторов и карточка оператора.

Выделено из ``admin_operators.py`` декомпозицией god-объекта. Связная
ответственность: показать перечень операторов кнопками
(``_show_operators_list``) и карточку конкретного оператора
(``_show_operator_card``) с действиями смены роли / деактивации /
реактивации. Сами действия живут в ``admin_operators_roles``; здесь —
только рендер карточки и гарды-предупреждения (самомодификация,
единственный активный IT).
"""
from __future__ import annotations

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as operators_service
from aemr_bot.utils.event import ack_callback, send_or_edit_screen


async def _show_operators_list(event) -> None:

    async with session_scope() as session:
        ops = await operators_service.list_all(session)
    if not ops:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text="Список операторов пуст.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    active_count = sum(1 for op in ops if op.is_active)
    inactive_count = len(ops) - active_count
    header = f"👥 Операторы: активных {active_count}"
    if inactive_count:
        header += f", деактивированных {inactive_count}"
    rows = [(op.max_user_id, op.full_name, op.role, op.is_active) for op in ops]
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=header + "\nТапните оператора, чтобы открыть карточку.",
        attachments=[kbds.op_operators_list_keyboard(rows)],
    )


async def _show_operator_card(event, payload: str, operator_id: int) -> None:

    try:
        target_id = int(payload.removeprefix("op:opcard:"))
    except ValueError:
        await ack_callback(event)
        return
    await ack_callback(event)
    async with session_scope() as session:
        op = await operators_service.get_any(session, target_id)
        active_it_count = await operators_service.count_active_by_role(
            session, OperatorRole.IT
        )
    if op is None:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=f"Оператор с id={target_id} не найден.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    is_self = op.max_user_id == operator_id
    # Защита: единственного IT нельзя деактивировать. Если оператор —
    # IT и других активных IT нет, can_deactivate=False.
    can_deactivate = True
    if op.is_active and op.role == OperatorRole.IT.value and active_it_count <= 1:
        can_deactivate = False

    status_line = "✅ активен" if op.is_active else "💤 деактивирован"
    extra: list[str] = []
    if is_self:
        extra.append("⚠️ Это вы. Себя через меню изменить нельзя.")
    if op.is_active and op.role == OperatorRole.IT.value and active_it_count <= 1:
        extra.append(
            "⚠️ Это единственный активный IT-оператор — деактивация заблокирована."
        )
    lines = [
        f"👤 {op.full_name}",
        "· · · · · · · ·",
        f"ID:       {op.max_user_id}",
        f"Роль:     {op.role}",
        f"Статус:   {status_line}",
        f"Добавлен: {op.created_at.strftime('%d.%m.%Y')}" if op.created_at else "",
    ]
    if extra:
        lines.append("")
        lines.extend(extra)
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text="\n".join(line for line in lines if line),
        attachments=[
            kbds.op_operator_card_keyboard(
                op.max_user_id,
                is_active=op.is_active,
                is_self=is_self,
                can_deactivate=can_deactivate,
            )
        ],
    )
