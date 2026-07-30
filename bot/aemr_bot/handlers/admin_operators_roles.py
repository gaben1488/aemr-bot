"""Операции над ролью/статусом оператора: смена роли, деактивация,
реактивация.

Выделено из ``admin_operators.py`` декомпозицией god-объекта. Связная
ответственность: изменяющие операции над существующей записью
оператора, с гардами 152-ФЗ и записью в ``audit_log``.

Контракты audit_log (наблюдаемые, рефактор обязан сохранить):
- смена роли   → action="operator_role_change", details={old_role,new_role}
- деактивация  → action="operator_deactivate", details={role,full_name}
- реактивация  → action="operator_reactivate", details={role,full_name}

Гарды:
- самомодификация (смена своей роли / деактивация себя) запрещена;
- забрать IT-роль / деактивировать единственного активного IT нельзя —
  иначе организация останется без IT-управления.
"""
from __future__ import annotations

from aemr_bot import keyboards as kbds
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as operators_service
from aemr_bot.handlers._common import op_screen
from aemr_bot.utils.event import ack_callback


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Смена роли
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


async def _show_role_change(event, payload: str, operator_id: int) -> None:

    try:
        target_id = int(payload.removeprefix("op:oprole:"))
    except ValueError:
        await ack_callback(event)
        return
    await ack_callback(event)
    if target_id == operator_id:
        await op_screen(
            event,
            "Изменить свою роль через меню нельзя.",
            kbds.op_back_to_operators_keyboard(),
        )
        return
    async with session_scope() as session:
        op = await operators_service.get_any(session, target_id)
    if op is None or not op.is_active:
        await op_screen(
            event,
            "Оператор не найден или деактивирован.",
            kbds.op_back_to_operators_keyboard(),
        )
        return
    await op_screen(
        event,
        f"✏️ Смена роли: {op.full_name}\n"
        f"Текущая роль: {op.role}\n\n"
        f"Выберите новую:",
        kbds.op_operator_role_change_keyboard(op.max_user_id, op.role),
    )


async def _apply_role_change(event, payload: str, operator_id: int) -> None:

    rest = payload.removeprefix("op:opchrole:")
    parts = rest.split(":", 1)
    if len(parts) != 2:
        await ack_callback(event)
        return
    try:
        target_id = int(parts[0])
    except ValueError:
        await ack_callback(event)
        return
    new_role_value = parts[1]
    await ack_callback(event)
    valid_roles = {r.value for r in OperatorRole}
    if new_role_value not in valid_roles:
        await op_screen(
            event,
            f"Роль «{new_role_value}» неизвестна.",
            kbds.op_back_to_operators_keyboard(),
        )
        return
    if target_id == operator_id:
        await op_screen(
            event,
            "Изменить свою роль нельзя.",
            kbds.op_back_to_operators_keyboard(),
        )
        return
    async with session_scope() as session:
        op = await operators_service.get_any(session, target_id)
        if op is None or not op.is_active:
            await op_screen(
                event,
                "Оператор не найден или деактивирован.",
                kbds.op_back_to_operators_keyboard(),
            )
            return
        # Если меняем IT на не-IT, нужно убедиться, что есть ещё хотя бы
        # один активный IT — иначе организация останется без IT-управления.
        if op.role == OperatorRole.IT.value and new_role_value != OperatorRole.IT.value:
            active_it = await operators_service.count_active_by_role(
                session, OperatorRole.IT
            )
            if active_it <= 1:
                await op_screen(
                    event,
                    "❌ Нельзя забрать IT-роль у единственного активного "
                    "IT-оператора. Сначала добавьте второго IT, потом "
                    "повторите смену роли.",
                    kbds.op_back_to_operators_keyboard(),
                )
                return
        old_role = op.role
        await operators_service.change_role(
            session, target_id, OperatorRole(new_role_value)
        )
        await operators_service.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="operator_role_change",
            target=f"user max_id={target_id}",
            details={"old_role": old_role, "new_role": new_role_value},
        )
    await op_screen(
        event,
        f"✅ Роль изменена: {op.full_name}\n"
        f"{old_role} → {new_role_value}",
        kbds.op_back_to_operators_keyboard(),
    )


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Деактивация / Реактивация
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


async def _show_deactivate_confirm(event, payload: str, operator_id: int) -> None:

    try:
        target_id = int(payload.removeprefix("op:opdeact:"))
    except ValueError:
        await ack_callback(event)
        return
    await ack_callback(event)
    if target_id == operator_id:
        await op_screen(
            event,
            "Себя деактивировать нельзя.",
            kbds.op_back_to_operators_keyboard(),
        )
        return
    async with session_scope() as session:
        op = await operators_service.get(session, target_id)
        if op is None:
            await op_screen(
                event,
                "Активный оператор не найден.",
                kbds.op_back_to_operators_keyboard(),
            )
            return
        active_it = await operators_service.count_active_by_role(
            session, OperatorRole.IT
        )
    if op.role == OperatorRole.IT.value and active_it <= 1:
        await op_screen(
            event,
            "❌ Нельзя деактивировать единственного активного IT.\n"
            "Сначала добавьте второго IT-оператора.",
            kbds.op_back_to_operators_keyboard(),
        )
        return
    await op_screen(
        event,
        f"⚠️ Деактивировать оператора?\n"
        f"· · · · · · · ·\n"
        f"{op.full_name} ({op.role})\n\n"
        f"Сотрудник потеряет доступ к командам бота.\n"
        f"Данные сохранятся — при необходимости его\n"
        f"можно будет восстановить через карточку.",
        kbds.op_operator_deactivate_confirm_keyboard(op.max_user_id),
    )


async def _apply_deactivate(event, payload: str, operator_id: int) -> None:

    try:
        target_id = int(payload.removeprefix("op:opdeact_ok:"))
    except ValueError:
        await ack_callback(event)
        return
    await ack_callback(event)
    if target_id == operator_id:
        await op_screen(
            event,
            "Себя деактивировать нельзя.",
            kbds.op_back_to_operators_keyboard(),
        )
        return
    async with session_scope() as session:
        op = await operators_service.get(session, target_id)
        if op is None:
            await op_screen(
                event,
                "Активный оператор не найден.",
                kbds.op_back_to_operators_keyboard(),
            )
            return
        if op.role == OperatorRole.IT.value:
            active_it = await operators_service.count_active_by_role(
                session, OperatorRole.IT
            )
            if active_it <= 1:
                await op_screen(
                    event,
                    "❌ Нельзя деактивировать единственного активного IT.",
                    kbds.op_back_to_operators_keyboard(),
                )
                return
        saved_name = op.full_name
        saved_role = op.role
        await operators_service.deactivate(session, target_id)
        await operators_service.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="operator_deactivate",
            target=f"user max_id={target_id}",
            details={"role": saved_role, "full_name": saved_name},
        )
    await op_screen(
        event,
        f"🚫 Деактивирован: {saved_name} ({saved_role})",
        kbds.op_back_to_operators_keyboard(),
    )


async def _apply_reactivate(event, payload: str, operator_id: int) -> None:

    try:
        target_id = int(payload.removeprefix("op:opreact:"))
    except ValueError:
        await ack_callback(event)
        return
    await ack_callback(event)
    async with session_scope() as session:
        op = await operators_service.get_any(session, target_id)
        if op is None:
            await op_screen(
                event,
                "Оператор не найден.",
                kbds.op_back_to_operators_keyboard(),
            )
            return
        if op.is_active:
            await op_screen(
                event,
                "Оператор уже активен.",
                kbds.op_back_to_operators_keyboard(),
            )
            return
        await operators_service.upsert(
            session,
            max_user_id=op.max_user_id,
            full_name=op.full_name,
            role=OperatorRole(op.role),
        )
        await operators_service.write_audit(
            session,
            operator_max_user_id=operator_id,
            action="operator_reactivate",
            target=f"user max_id={op.max_user_id}",
            details={"role": op.role, "full_name": op.full_name},
        )
        saved_name = op.full_name
        saved_role = op.role
    await op_screen(
        event,
        f"🔄 Реактивирован: {saved_name} ({saved_role})",
        kbds.op_back_to_operators_keyboard(),
    )
