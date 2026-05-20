"""Управление операторами через кнопочные wizard'ы.

Сценарии:

1. ДОБАВЛЕНИЕ ИЗ УЧАСТНИКОВ ГРУППЫ (основной путь).
   IT-админ открывает «👥 Операторы → ➕ Добавить из участников». Бот
   вызывает MAX API `get_chat_members(ADMIN_GROUP_ID)`, фильтрует уже
   зарегистрированных, показывает кнопки. Тап → шаг выбора роли → шаг
   выбора имени (из MAX-профиля либо ввод полного ФИО) → подтверждение.

2. ДОБАВЛЕНИЕ ПО ID ВРУЧНУЮ (fallback).
   Прежний wizard для случая, когда человека ещё нет в группе (его
   надо зарегистрировать заранее).

3. КАРТОЧКА ОПЕРАТОРА.
   Тап по любому оператору в списке → карточка с действиями: смена
   роли, деактивация, реактивация (для деактивированных).

4. УДАЛЕНИЕ.
   Через кнопку «🚫 Деактивировать» в карточке — мягкое удаление
   (is_active=false). Физического DELETE нет: история ответов жителю
   должна сохраниться по требованиям журналирования 152-ФЗ.

Защиты:
- Самомодификация (попытка деактивировать или сменить роль себе)
  блокируется в обработчике.
- Деактивация единственного активного IT блокируется — иначе можно
  отрезать организацию от управления.
- Назначение IT-роли «себе» через wizard блокируется (как и раньше).
"""
from __future__ import annotations

import logging
import time as _time_op

from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.services import operators as operators_service
from aemr_bot.utils.event import ack_callback, get_user_id, send_or_edit_screen

log = logging.getLogger(__name__)

# Wizard state, in-memory + persist через services/wizard_persist.
# Шаги:
#   idle → awaiting_id → picked_id → awaiting_role → picked_role
#       → awaiting_name (если выбрана ручная правка имени)
#       → ready_to_confirm → done
# Wizard допускает «откат» (Изменить роль) — возврат на picked_id.
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
    snapshot = {k: v for k, v in state.items() if k != "expires_at"}
    from aemr_bot.services import wizard_registry as _wr
    _wr.schedule_persist_op(operator_id, snapshot)
    return state


def _op_wizard_drop(operator_id: int) -> None:
    _op_wizards.pop(operator_id, None)
    from aemr_bot.services import wizard_registry as _wr
    _wr.schedule_persist_op(operator_id, None)


# ──────────────────────────────────────────────────────────────────────
# Хелперы
# ──────────────────────────────────────────────────────────────────────


async def _safe_get_chat_members(bot) -> list:
    """Безопасная обёртка над get_chat_members: на любой ошибке
    возвращает пустой список, чтобы UI откатился к ручному вводу ID
    без падения сценария."""
    try:
        result = await bot.get_chat_members(chat_id=cfg.admin_group_id)
        if hasattr(result, "members"):
            return list(result.members or [])
        return []
    except Exception as exc:
        log.warning("get_chat_members failed: %s", exc)
        return []


def _full_name_from_member(member) -> str:
    """Сборка ФИО из first_name + last_name. Если в MAX-профиле есть
    только first_name, возвращаем только его."""
    first = (getattr(member, "first_name", None) or "").strip()
    last = (getattr(member, "last_name", None) or "").strip()
    if first and last:
        return f"{first} {last}"
    return first or last or f"User {getattr(member, 'user_id', '?')}"


# ──────────────────────────────────────────────────────────────────────
# Точка входа в меню «👥 Операторы»
# ──────────────────────────────────────────────────────────────────────


async def run_operators_menu(event) -> None:
    """Меню «👥 Операторы» в админ-панели для роли it. Точка входа."""
    from aemr_bot import keyboards as kbds

    if not await ensure_role(event, OperatorRole.IT):
        return
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            "👥 Управление операторами\n"
            "──────────\n"
            "📋 Список — все операторы с возможностью смены роли\n"
            "    и деактивации через карточку.\n\n"
            "➕ Из участников группы — подобрать из тех, кто уже\n"
            "    в служебном чате (одним тапом, без /whoami).\n\n"
            "🔢 По ID вручную — если человека ещё нет в группе."
        ),
        attachments=[kbds.op_operators_menu_keyboard()],
    )


async def run_operators_action(event, payload: str) -> None:
    """Главный диспетчер callback'ов с префиксом `op:opadd:*`,
    `op:opcard:*`, `op:oprole:*`, `op:opchrole:*`, `op:opdeact*`,
    `op:opreact:*`."""
    from aemr_bot import keyboards as kbds

    if not await ensure_role(event, OperatorRole.IT):
        return
    operator_id = get_user_id(event)
    if operator_id is None:
        await ack_callback(event)
        return

    # Карточка оператора
    if payload.startswith("op:opcard:"):
        await _show_operator_card(event, payload, operator_id)
        return

    # Смена роли — открыть picker
    if payload.startswith("op:oprole:"):
        await _show_role_change(event, payload, operator_id)
        return

    # Применить смену роли
    if payload.startswith("op:opchrole:"):
        await _apply_role_change(event, payload, operator_id)
        return

    # Деактивация — подтверждение
    if payload.startswith("op:opdeact_ok:"):
        await _apply_deactivate(event, payload, operator_id)
        return
    if payload.startswith("op:opdeact:"):
        await _show_deactivate_confirm(event, payload, operator_id)
        return

    # Реактивация
    if payload.startswith("op:opreact:"):
        await _apply_reactivate(event, payload, operator_id)
        return

    # Wizard добавления оператора — старая семья callback'ов «op:opadd:*»
    suffix = payload.removeprefix("op:opadd:")
    await ack_callback(event)

    if suffix == "list":
        await _show_operators_list(event)
        return
    if suffix == "from_group":
        await _show_from_group(event, operator_id)
        return
    if suffix.startswith("pick:"):
        try:
            picked_user_id = int(suffix.removeprefix("pick:"))
        except ValueError:
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text="Некорректный выбор.",
                attachments=[kbds.op_back_to_operators_keyboard()],
            )
            return
        await _start_add_with_picked(event, operator_id, picked_user_id)
        return
    if suffix == "start":
        await _start_manual_add(event, operator_id)
        return
    if suffix == "cancel":
        _op_wizard_drop(operator_id)
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Регистрация оператора отменена.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    if suffix.startswith("role:"):
        await _apply_role_choice(event, suffix, operator_id)
        return
    if suffix == "name_keep":
        await _apply_name_keep(event, operator_id)
        return
    if suffix == "name_edit":
        await _start_name_edit(event, operator_id)
        return
    if suffix == "edit_role":
        await _back_to_role_pick(event, operator_id)
        return
    if suffix == "confirm":
        await _confirm_save(event, operator_id)
        return


# ──────────────────────────────────────────────────────────────────────
# Список и карточка оператора
# ──────────────────────────────────────────────────────────────────────


async def _show_operators_list(event) -> None:
    from aemr_bot import keyboards as kbds

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
    from aemr_bot import keyboards as kbds

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
        "──────────",
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


# ──────────────────────────────────────────────────────────────────────
# Смена роли
# ──────────────────────────────────────────────────────────────────────


async def _show_role_change(event, payload: str, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    try:
        target_id = int(payload.removeprefix("op:oprole:"))
    except ValueError:
        await ack_callback(event)
        return
    await ack_callback(event)
    if target_id == operator_id:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Изменить свою роль через меню нельзя.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    async with session_scope() as session:
        op = await operators_service.get_any(session, target_id)
    if op is None or not op.is_active:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Оператор не найден или деактивирован.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            f"✏️ Смена роли: {op.full_name}\n"
            f"Текущая роль: {op.role}\n\n"
            f"Выберите новую:"
        ),
        attachments=[kbds.op_operator_role_change_keyboard(op.max_user_id, op.role)],
    )


async def _apply_role_change(event, payload: str, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

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
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=f"Роль «{new_role_value}» неизвестна.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    if target_id == operator_id:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Изменить свою роль нельзя.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    async with session_scope() as session:
        op = await operators_service.get_any(session, target_id)
        if op is None or not op.is_active:
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text="Оператор не найден или деактивирован.",
                attachments=[kbds.op_back_to_operators_keyboard()],
            )
            return
        # Если меняем IT на не-IT, нужно убедиться, что есть ещё хотя бы
        # один активный IT — иначе организация останется без IT-управления.
        if op.role == OperatorRole.IT.value and new_role_value != OperatorRole.IT.value:
            active_it = await operators_service.count_active_by_role(
                session, OperatorRole.IT
            )
            if active_it <= 1:
                await send_or_edit_screen(
                    event, chat_id=cfg.admin_group_id,
                    text=(
                        "❌ Нельзя забрать IT-роль у единственного активного "
                        "IT-оператора. Сначала добавьте второго IT, потом "
                        "повторите смену роли."
                    ),
                    attachments=[kbds.op_back_to_operators_keyboard()],
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
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"✅ Роль изменена: {op.full_name}\n"
            f"{old_role} → {new_role_value}"
        ),
        attachments=[kbds.op_back_to_operators_keyboard()],
    )


# ──────────────────────────────────────────────────────────────────────
# Деактивация / Реактивация
# ──────────────────────────────────────────────────────────────────────


async def _show_deactivate_confirm(event, payload: str, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    try:
        target_id = int(payload.removeprefix("op:opdeact:"))
    except ValueError:
        await ack_callback(event)
        return
    await ack_callback(event)
    if target_id == operator_id:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Себя деактивировать нельзя.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    async with session_scope() as session:
        op = await operators_service.get(session, target_id)
        if op is None:
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text="Активный оператор не найден.",
                attachments=[kbds.op_back_to_operators_keyboard()],
            )
            return
        active_it = await operators_service.count_active_by_role(
            session, OperatorRole.IT
        )
    if op.role == OperatorRole.IT.value and active_it <= 1:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "❌ Нельзя деактивировать единственного активного IT.\n"
                "Сначала добавьте второго IT-оператора."
            ),
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"⚠️ Деактивировать оператора?\n"
            f"──────────\n"
            f"{op.full_name} ({op.role})\n\n"
            f"Сотрудник потеряет доступ к командам бота.\n"
            f"Данные сохранятся — при необходимости его\n"
            f"можно будет восстановить через карточку."
        ),
        attachments=[kbds.op_operator_deactivate_confirm_keyboard(op.max_user_id)],
    )


async def _apply_deactivate(event, payload: str, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    try:
        target_id = int(payload.removeprefix("op:opdeact_ok:"))
    except ValueError:
        await ack_callback(event)
        return
    await ack_callback(event)
    if target_id == operator_id:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Себя деактивировать нельзя.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    async with session_scope() as session:
        op = await operators_service.get(session, target_id)
        if op is None:
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text="Активный оператор не найден.",
                attachments=[kbds.op_back_to_operators_keyboard()],
            )
            return
        if op.role == OperatorRole.IT.value:
            active_it = await operators_service.count_active_by_role(
                session, OperatorRole.IT
            )
            if active_it <= 1:
                await send_or_edit_screen(
                    event, chat_id=cfg.admin_group_id,
                    text="❌ Нельзя деактивировать единственного активного IT.",
                    attachments=[kbds.op_back_to_operators_keyboard()],
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
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=f"🚫 Деактивирован: {saved_name} ({saved_role})",
        attachments=[kbds.op_back_to_operators_keyboard()],
    )


async def _apply_reactivate(event, payload: str, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    try:
        target_id = int(payload.removeprefix("op:opreact:"))
    except ValueError:
        await ack_callback(event)
        return
    await ack_callback(event)
    async with session_scope() as session:
        op = await operators_service.get_any(session, target_id)
        if op is None:
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text="Оператор не найден.",
                attachments=[kbds.op_back_to_operators_keyboard()],
            )
            return
        if op.is_active:
            await send_or_edit_screen(
                event, chat_id=cfg.admin_group_id,
                text="Оператор уже активен.",
                attachments=[kbds.op_back_to_operators_keyboard()],
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
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=f"🔄 Реактивирован: {saved_name} ({saved_role})",
        attachments=[kbds.op_back_to_operators_keyboard()],
    )


# ──────────────────────────────────────────────────────────────────────
# Добавление из участников группы
# ──────────────────────────────────────────────────────────────────────


async def _show_from_group(event, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    members = await _safe_get_chat_members(event.bot)
    if not members:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                "Не удалось получить список участников группы.\n"
                "Используйте «🔢 По ID вручную»."
            ),
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return

    # Мапа уже зарегистрированных
    async with session_scope() as session:
        existing = await operators_service.list_all(session)
    by_max_id = {op.max_user_id: op for op in existing}

    candidates: list[tuple[int, str, str | None]] = []
    bot_self_id = getattr(getattr(event.bot, "me", None), "user_id", None)
    for m in members:
        user_id = getattr(m, "user_id", None)
        if user_id is None:
            continue
        if getattr(m, "is_bot", False) or user_id == bot_self_id:
            continue
        full_name = _full_name_from_member(m)
        existing_op = by_max_id.get(user_id)
        if existing_op is not None and existing_op.is_active:
            label = f"{full_name} · уже оператор ({existing_op.role})"
            candidates.append((user_id, label, existing_op.role))
        elif user_id == operator_id:
            label = f"{full_name} (вы) — уже оператор"
            candidates.append((user_id, label, "self"))
        else:
            candidates.append((user_id, full_name, None))

    if not candidates:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="В группе нет участников, кроме бота.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return

    addable = sum(1 for _, _, hint in candidates if hint is None)
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"👥 Кого зарегистрировать?\n"
            f"──────────\n"
            f"Участников группы: {len(candidates)}\n"
            f"Доступно для добавления: {addable}\n\n"
            f"Тапните по человеку для добавления, или\n"
            f"по уже-оператору — чтобы открыть карточку."
        ),
        attachments=[kbds.op_from_group_keyboard(candidates)],
    )


async def _start_add_with_picked(
    event, operator_id: int, picked_user_id: int
) -> None:
    """Пользователь выбрал участника из группы. Подтягиваем профиль
    из MAX и переходим к выбору роли."""
    from aemr_bot import keyboards as kbds

    if picked_user_id == operator_id:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Себя через меню добавить/изменить нельзя.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return

    # Тянем профиль из группы для имени
    suggested_name: str | None = None
    try:
        member = await event.bot.get_chat_member(
            chat_id=cfg.admin_group_id, user_id=picked_user_id
        )
        if member is not None:
            suggested_name = _full_name_from_member(member)
    except Exception as exc:
        log.warning("get_chat_member failed for %s: %s", picked_user_id, exc)

    _op_wizard_set(
        operator_id,
        step="awaiting_role",
        target_id=picked_user_id,
        suggested_name=suggested_name,
        source="group",
    )
    extra = (
        f"Имя из MAX: {suggested_name}\n"
        if suggested_name else
        "Имя из MAX недоступно — введёте вручную позже.\n"
    )
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"👥 Шаг 2 — выбор роли\n"
            f"──────────\n"
            f"ID:  {picked_user_id}\n"
            f"{extra}\n"
            f"Выберите роль:"
        ),
        attachments=[kbds.op_role_picker_keyboard()],
    )


# ──────────────────────────────────────────────────────────────────────
# Добавление по ID вручную (старый wizard)
# ──────────────────────────────────────────────────────────────────────


async def _start_manual_add(event, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    # Сбрасываем чужие wizard'ы и reply-intent этого оператора
    from aemr_bot.handlers import broadcast as broadcast_handler
    from aemr_bot.handlers import operator_reply as op_reply

    broadcast_handler._wizards.pop(operator_id, None)
    op_reply.drop_reply_intent(operator_id)

    _op_wizard_set(operator_id, step="awaiting_id", source="manual")
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            "👥 Шаг 1 — ID оператора\n"
            "──────────\n"
            "Введите max_user_id будущего оператора.\n\n"
            "Узнать ID можно несколькими способами:\n"
            "• попросите человека добавиться в служебную\n"
            "  группу и выберите «➕ Из участников»;\n"
            "• попросите написать боту в личке /whoami\n"
            "  и прислать вам число из ответа."
        ),
        attachments=[kbds.op_add_cancel_keyboard()],
    )


async def _apply_role_choice(event, suffix: str, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    role_value = suffix.removeprefix("role:")
    valid = {r.value for r in OperatorRole}
    if role_value not in valid:
        await send_or_edit_screen(
            event,
            chat_id=cfg.admin_group_id,
            text=f"Роль «{role_value}» неизвестна.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    state = _op_wizard_get(operator_id)
    if state is None or state.get("step") != "awaiting_role":
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Мастер закрыт. Откройте «👥 Операторы → ➕» заново.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    _op_wizard_set(operator_id, role=role_value, step="picked_role")
    suggested = state.get("suggested_name")
    if suggested:
        # Есть имя из MAX — предложить «как есть» или ввести вручную
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                f"👥 Шаг 3 — ФИО для журнала\n"
                f"──────────\n"
                f"Роль: {role_value} ✅\n"
                f"Имя из MAX: {suggested}\n\n"
                f"Сохранить как есть или указать полное\n"
                f"ФИО с отчеством?"
            ),
            attachments=[kbds.op_add_name_choice_keyboard()],
        )
    else:
        # Имени нет — сразу запрашиваем текстом
        _op_wizard_set(operator_id, step="awaiting_name")
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text=(
                f"👥 Шаг 3 — ФИО\n"
                f"──────────\n"
                f"Роль {role_value} выбрана.\n\n"
                f"Введите ФИО оператора одним сообщением.\n"
                f"Пример: «Иванова Анна Петровна»"
            ),
            attachments=[kbds.op_add_cancel_keyboard()],
        )


async def _apply_name_keep(event, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    state = _op_wizard_get(operator_id)
    if state is None or state.get("step") != "picked_role":
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Мастер закрыт.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    suggested = state.get("suggested_name")
    if not suggested:
        await _start_name_edit(event, operator_id)
        return
    _op_wizard_set(operator_id, full_name=suggested, step="ready_to_confirm")
    await _show_add_confirm(event, operator_id)


async def _start_name_edit(event, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    state = _op_wizard_get(operator_id)
    if state is None:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Мастер закрыт.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    _op_wizard_set(operator_id, step="awaiting_name")
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            "👥 Шаг 3 — ФИО полностью\n"
            "──────────\n"
            "Введите ФИО оператора одним сообщением.\n"
            "Пример: «Иванова Анна Петровна»"
        ),
        attachments=[kbds.op_add_cancel_keyboard()],
    )


async def _back_to_role_pick(event, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    state = _op_wizard_get(operator_id)
    if state is None:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Мастер закрыт.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    _op_wizard_set(operator_id, step="awaiting_role")
    target_id = state.get("target_id")
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"👥 Шаг 2 — выбор роли\n"
            f"──────────\n"
            f"ID: {target_id}\n\n"
            f"Выберите роль:"
        ),
        attachments=[kbds.op_role_picker_keyboard()],
    )


async def _show_add_confirm(event, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    state = _op_wizard_get(operator_id)
    if state is None:
        return
    target_id = state.get("target_id")
    role = state.get("role")
    full_name = state.get("full_name")
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"👥 Подтверждение\n"
            f"──────────\n"
            f"ID:   {target_id}\n"
            f"Роль: {role}\n"
            f"ФИО:  {full_name}\n\n"
            f"Добавить оператора?"
        ),
        attachments=[kbds.op_add_confirm_keyboard()],
    )


async def _confirm_save(event, operator_id: int) -> None:
    from aemr_bot import keyboards as kbds

    state = _op_wizard_get(operator_id)
    if state is None:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Мастер закрыт.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    try:
        target_id = int(state["target_id"])
    except (KeyError, ValueError, TypeError):
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="ID не задан, начните заново.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    role = state.get("role")
    full_name = state.get("full_name")
    if not role or not full_name:
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Не хватает данных, начните заново.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
    if target_id == operator_id:
        _op_wizard_drop(operator_id)
        await send_or_edit_screen(
            event, chat_id=cfg.admin_group_id,
            text="Изменить свою роль через мастера нельзя.",
            attachments=[kbds.op_back_to_operators_keyboard()],
        )
        return
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
            details={"role": role, "full_name": full_name, "source": state.get("source", "?")},
        )
    _op_wizard_drop(operator_id)
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            f"✅ {'Обновлено' if existed else 'Добавлено'}:\n"
            f"{full_name} · {role} · #{target_id}"
        ),
        attachments=[kbds.op_add_done_keyboard()],
    )


# ──────────────────────────────────────────────────────────────────────
# Перехватчик текстовых сообщений wizard'а
# ──────────────────────────────────────────────────────────────────────


async def handle_operators_wizard_text(event, text: str) -> bool:
    """Перехватчик текстовых сообщений в админ-группе на стороне wizard'а.
    Возвращает True, если сообщение поглощено."""
    from aemr_bot import keyboards as kbds

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
                attachments=[kbds.op_add_cancel_keyboard()],
            )
            return True
        _op_wizard_set(operator_id, target_id=target_id, step="awaiting_role")
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=(
                f"👥 Шаг 2 — выбор роли\n"
                f"──────────\n"
                f"ID:  {target_id}\n\n"
                f"Выберите роль:"
            ),
            attachments=[kbds.op_role_picker_keyboard()],
        )
        return True
    if step == "awaiting_name":
        full_name = text.strip()
        if len(full_name) < 2:
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text="ФИО слишком короткое. Введите полностью.",
                attachments=[kbds.op_add_cancel_keyboard()],
            )
            return True
        _op_wizard_set(operator_id, full_name=full_name, step="ready_to_confirm")
        await _show_add_confirm(event, operator_id)
        return True
    return False
