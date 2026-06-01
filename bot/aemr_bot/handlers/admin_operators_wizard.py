"""Wizard добавления оператора + общий wizard-state.

Выделено из ``admin_operators.py`` декомпозицией god-объекта (DDD
tactical: связная ответственность «добавление оператора» — отдельный
подмодуль). Сюда же снесён общий wizard-state (``_op_wizards`` и
примитивы ``_op_wizard_get/set/drop``) и два MAX-хелпера
(``_safe_get_chat_members``, ``_full_name_from_member``), потому что ими
пользуется именно add-флоу. Остальные подмодули (list, roles) и фасад
``admin_operators`` импортируют их отсюда — единый источник правды для
состояния мастера.

Сценарии добавления:

1. ИЗ УЧАСТНИКОВ ГРУППЫ — ``_show_from_group`` →
   ``_start_add_with_picked`` → выбор роли → ФИО → подтверждение.
2. ПО ID ВРУЧНУЮ — ``_start_manual_add`` → ввод ID (текстом) → роль →
   ФИО → подтверждение.

Шаги стейта:
    idle → awaiting_id → picked_id → awaiting_role → picked_role
        → awaiting_name (если ручная правка имени)
        → ready_to_confirm → done
Допускается «откат» (Изменить роль) — возврат на awaiting_role.
"""
from __future__ import annotations

import logging
import re
import time as _time_op

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as operators_service
from aemr_bot.utils.event import get_user_id, send_or_edit_screen

log = logging.getLogger(__name__)

# Имя оператора должно содержать хотя бы одну букву/цифру — та же защита,
# что у имени жителя (appeal_runtime._HAS_ALNUM). Без неё «77», «...»,
# «👍👍» проходили как ФИО оператора в журнал 152-ФЗ. Локальная копия
# паттерна, чтобы не тянуть appeal_runtime (citizen-воронка) в
# операторский модуль и не плодить импорт-цикл.
_HAS_ALNUM = re.compile(r"[A-Za-zА-Яа-яЁё0-9]")

# Wizard state, in-memory + persist через services/wizard_persist.
# ВАЖНО: этот dict — единственный экземпляр; фасад admin_operators и
# подмодули list/roles импортируют ИМЕННО его (re-export сохраняет
# идентичность объекта). main.py и admin_appeal_ops.py мутируют его
# через admin_operators._op_wizards — это тот же объект.
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


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Хелперы
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


async def _safe_get_chat_members(bot) -> list:
    """Безопасная обёртка над get_chat_members: возвращает **полный**
    список через пагинацию, на любой ошибке — пустой.

    MAXAPI_DEEP_DIVE §3 fix (P2): раньше делал один вызов
    `bot.get_chat_members(chat_id=…)` без пагинации — для группы >100
    членов MAX возвращал только первую страницу, остальные молча
    терялись. Теперь используем `ChatMembersManager.iter_all()` из
    maxapi 1.1.0 — async-итератор с защитой от циклов marker.

    Это правильно решает F11 (раньше там был эвристик «если получили
    меньше членов, чем активных операторов — пропускаем» — теперь
    эвристик не нужен, но оставляем как defence-in-depth, см.
    `_job_stale_operators_cleanup` в cron.py).
    """
    if cfg.admin_group_id is None:
        # Без admin_group_id (например в dev-окружении без MAX-чата)
        # делать нечего — вернём пусто, downstream увидит no-op.
        return []
    try:
        from maxapi.types.chats import ChatMembersManager
        manager = ChatMembersManager(bot=bot, chat_id=int(cfg.admin_group_id))
        return await manager.list_all()
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


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Добавление из участников группы
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


async def _show_from_group(event, operator_id: int) -> None:

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
            f"· · · · · · · ·\n"
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
            f"· · · · · · · ·\n"
            f"ID:  {picked_user_id}\n"
            f"{extra}\n"
            f"Выберите роль:"
        ),
        attachments=[kbds.op_role_picker_keyboard()],
    )


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Добавление по ID вручную (старый wizard)
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


async def _start_manual_add(event, operator_id: int) -> None:

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
            "· · · · · · · ·\n"
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
                f"· · · · · · · ·\n"
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
                f"· · · · · · · ·\n"
                f"Роль {role_value} выбрана.\n\n"
                f"Введите ФИО оператора одним сообщением.\n"
                f"Пример: «Иванова Анна Петровна»"
            ),
            attachments=[kbds.op_add_cancel_keyboard()],
        )


async def _apply_name_keep(event, operator_id: int) -> None:

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
            "· · · · · · · ·\n"
            "Введите ФИО оператора одним сообщением.\n"
            "Пример: «Иванова Анна Петровна»"
        ),
        attachments=[kbds.op_add_cancel_keyboard()],
    )


async def _back_to_role_pick(event, operator_id: int) -> None:

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
            f"· · · · · · · ·\n"
            f"ID: {target_id}\n\n"
            f"Выберите роль:"
        ),
        attachments=[kbds.op_role_picker_keyboard()],
    )


async def _show_add_confirm(event, operator_id: int) -> None:

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
            f"· · · · · · · ·\n"
            f"ID:   {target_id}\n"
            f"Роль: {role}\n"
            f"ФИО:  {full_name}\n\n"
            f"Добавить оператора?"
        ),
        attachments=[kbds.op_add_confirm_keyboard()],
    )


async def _confirm_save(event, operator_id: int) -> None:

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


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Перехватчик текстовых сообщений wizard'а
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


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
                attachments=[kbds.op_add_cancel_keyboard()],
            )
            return True
        _op_wizard_set(operator_id, target_id=target_id, step="awaiting_role")
        await event.bot.send_message(
            chat_id=cfg.admin_group_id,
            text=(
                f"👥 Шаг 2 — выбор роли\n"
                f"· · · · · · · ·\n"
                f"ID:  {target_id}\n\n"
                f"Выберите роль:"
            ),
            attachments=[kbds.op_role_picker_keyboard()],
        )
        return True
    if step == "awaiting_name":
        # Лимит длины — как у имени жителя (cfg.name_max_chars), чтобы
        # сверхдлинная строка не попадала в журнал/карточки сырьём.
        full_name = text.strip()[: cfg.name_max_chars]
        if len(full_name) < 2 or not _HAS_ALNUM.search(full_name):
            # Пусто/короткое ИЛИ только пунктуация/эмодзи («77» проходит
            # длину, но это не ФИО — режем по alnum, как у жителя).
            await event.bot.send_message(
                chat_id=cfg.admin_group_id,
                text="ФИО слишком короткое или без букв. Введите полностью.",
                attachments=[kbds.op_add_cancel_keyboard()],
            )
            return True
        _op_wizard_set(operator_id, full_name=full_name, step="ready_to_confirm")
        await _show_add_confirm(event, operator_id)
        return True
    return False
