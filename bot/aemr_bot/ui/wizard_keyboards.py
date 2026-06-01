"""Клавиатуры wizard'а «👥 Операторы» (admin-only, IT role).

Список операторов, карточка конкретного оператора, смена роли,
деактивация, реактивация. Wizard добавления нового оператора:
выбор из участников группы, выбор роли, выбор имени, подтверждение.
"""
from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from aemr_bot.handlers import callback_payloads as cp


def op_add_cancel_keyboard():
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="❌ Отменить добавление", payload=cp.op_opadd("cancel")))
    return kb.as_markup()


def op_operators_menu_keyboard():
    """Меню «👥 Операторы» в админ-панели для роли it."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="📋 Список операторов", payload=cp.op_opadd("list")))
    kb.row(CallbackButton(text="➕ Добавить из участников группы", payload=cp.op_opadd("from_group")))
    kb.row(CallbackButton(text="🔢 Добавить по ID вручную", payload=cp.op_opadd("start")))
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_MENU))
    return kb.as_markup()


def op_operators_list_keyboard(rows: list[tuple[int, str, str, bool]]):
    """Список операторов как кнопки. rows: (max_user_id, full_name, role,
    is_active). Тап — открывает карточку конкретного оператора. После
    списка — кнопка «Назад в меню операторов».
    Длина подписи ограничена ~50 символами для узких экранов MAX."""
    kb = InlineKeyboardBuilder()
    for max_user_id, full_name, role, is_active in rows:
        marker = "👤" if is_active else "💤"
        suffix = f" · {role}" if is_active else f" · {role} · деактивирован"
        # 40 символов на ФИО — компромисс между «видно полностью» и
        # «помещается на узких экранах MAX»
        name_short = full_name if len(full_name) <= 40 else full_name[:37] + "…"
        kb.row(
            CallbackButton(
                text=f"{marker} {name_short}{suffix}",
                payload=cp.op_opcard(max_user_id),
            )
        )
    kb.row(CallbackButton(text="↩️ Назад", payload=cp.OP_OPERATORS))
    return kb.as_markup()


def op_operator_card_keyboard(
    max_user_id: int,
    *,
    is_active: bool,
    is_self: bool,
    can_deactivate: bool,
):
    """Карточка оператора — действия зависят от состояния:
    - active + не self + can_deactivate → «Сменить роль», «Деактивировать»
    - active + self → «Сменить роль» нельзя, «Деактивировать» нельзя
    - active + единственный IT → «Сменить роль» можно (на любую другую только если есть другие IT — проверка в обработчике), «Деактивировать» нельзя
    - inactive → «Реактивировать»
    """
    kb = InlineKeyboardBuilder()
    if is_active:
        if not is_self:
            kb.row(CallbackButton(text="✏️ Сменить роль", payload=cp.op_oprole(max_user_id)))
        if can_deactivate and not is_self:
            kb.row(
                CallbackButton(
                    text="🚫 Деактивировать", payload=cp.op_opdeact(max_user_id)
                )
            )
    else:
        kb.row(
            CallbackButton(
                text="🔄 Реактивировать", payload=cp.op_opreact(max_user_id)
            )
        )
    kb.row(CallbackButton(text="↩️ К списку", payload=cp.op_opadd("list")))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload=cp.OP_MENU))
    return kb.as_markup()


def op_operator_role_change_keyboard(max_user_id: int, current_role: str):
    """Смена роли существующему оператору. Текущую роль показываем
    как заблокированную (без callback'а)."""
    from aemr_bot.db.models import OperatorRole

    kb = InlineKeyboardBuilder()
    roles = [
        (OperatorRole.IT.value, "🛠 it — ИТ, полный доступ"),
        (OperatorRole.COORDINATOR.value, "👤 coordinator — ответы + рассылки"),
        (OperatorRole.AEMR.value, "👤 aemr — рядовой специалист"),
        (OperatorRole.EGP.value, "👤 egp — специалист ЕГП"),
    ]
    for role_value, label in roles:
        if role_value == current_role:
            # Текущая роль — пометка, без активного callback'а
            kb.row(
                CallbackButton(
                    text=f"✓ {label} (текущая)",
                    payload=cp.op_opcard(max_user_id),
                )
            )
        else:
            kb.row(
                CallbackButton(
                    text=label,
                    payload=cp.op_opchrole(max_user_id, role_value),
                )
            )
    kb.row(CallbackButton(text="❌ Отмена", payload=cp.op_opcard(max_user_id)))
    return kb.as_markup()


def op_operator_deactivate_confirm_keyboard(max_user_id: int):
    """Подтверждение деактивации — две кнопки в ряд."""
    kb = InlineKeyboardBuilder()
    kb.row(
        CallbackButton(text="✅ Да, деактивировать", payload=cp.op_opdeact_ok(max_user_id)),
        CallbackButton(text="❌ Отмена", payload=cp.op_opcard(max_user_id)),
    )
    return kb.as_markup()


def op_from_group_keyboard(
    candidates: list[tuple[int, str, str | None]],  # (user_id, label, role_hint)
):
    """Кнопки добавления оператора из участников группы. label —
    готовая строка вида «Иванова А.П.» с пометкой [уже оператор: aemr]
    если есть. role_hint=None для добавления, role_hint=<role> для
    уже зарегистрированных (тап открывает их карточку)."""
    kb = InlineKeyboardBuilder()
    for user_id, label, role_hint in candidates:
        # 50 символов на label — место для имени + пометки
        text = label if len(label) <= 50 else label[:47] + "…"
        if role_hint is None:
            kb.row(CallbackButton(text=f"➕ {text}", payload=cp.op_opadd(f"pick:{user_id}")))
        else:
            kb.row(CallbackButton(text=f"👤 {text}", payload=cp.op_opcard(user_id)))
    kb.row(CallbackButton(text="🔢 Ввести ID вручную", payload=cp.op_opadd("start")))
    kb.row(CallbackButton(text="❌ Отмена", payload=cp.OP_OPERATORS))
    return kb.as_markup()


def op_role_picker_keyboard():
    """Шаг 2 wizard'а добавления оператора — выбор роли. По одной
    кнопке в строку с пояснением что значит каждая роль. Самомодификация
    (попытка выдать it самому себе) ловится в обработчике."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="🛠 it — ИТ, полный доступ", payload="op:opadd:role:it"))
    kb.row(CallbackButton(text="👤 coordinator — ответы + рассылки", payload="op:opadd:role:coordinator"))
    kb.row(CallbackButton(text="👤 aemr — рядовой специалист", payload="op:opadd:role:aemr"))
    kb.row(CallbackButton(text="👤 egp — специалист ЕГП", payload="op:opadd:role:egp"))
    kb.row(CallbackButton(text="❌ Отменить добавление", payload="op:opadd:cancel"))
    return kb.as_markup()


def op_add_name_choice_keyboard():
    """Шаг 4 wizard'а добавления — выбор: «сохранить имя из MAX» или
    «указать ФИО полностью текстом»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Сохранить как есть", payload="op:opadd:name_keep"))
    kb.row(CallbackButton(text="✏️ Указать ФИО полностью", payload="op:opadd:name_edit"))
    kb.row(CallbackButton(text="❌ Отменить добавление", payload="op:opadd:cancel"))
    return kb.as_markup()


def op_add_confirm_keyboard():
    """Финальное подтверждение перед сохранением — три кнопки."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="✅ Сохранить", payload="op:opadd:confirm"))
    kb.row(CallbackButton(text="✏️ Изменить роль", payload="op:opadd:edit_role"))
    kb.row(CallbackButton(text="❌ Отменить добавление", payload="op:opadd:cancel"))
    return kb.as_markup()


def op_add_done_keyboard():
    """После успешного добавления — «Добавить ещё» / «К списку» / «В меню»."""
    kb = InlineKeyboardBuilder()
    kb.row(CallbackButton(text="➕ Добавить ещё", payload="op:operators"))
    kb.row(CallbackButton(text="📋 К списку операторов", payload="op:opadd:list"))
    kb.row(CallbackButton(text="↩️ В админ-меню", payload="op:menu"))
    return kb.as_markup()


__all__ = [
    "op_add_cancel_keyboard",
    "op_operators_menu_keyboard",
    "op_operators_list_keyboard",
    "op_operator_card_keyboard",
    "op_operator_role_change_keyboard",
    "op_operator_deactivate_confirm_keyboard",
    "op_from_group_keyboard",
    "op_role_picker_keyboard",
    "op_add_name_choice_keyboard",
    "op_add_confirm_keyboard",
    "op_add_done_keyboard",
]
