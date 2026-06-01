"""Управление операторами через кнопочные wizard'ы — ФАСАД.

Этот модуль — точка входа и диспетчер. Реализация разнесена по
подмодулям (декомпозиция god-объекта, DDD tactical):

- ``admin_operators_wizard``  — добавление оператора (из участников
  группы и по ID вручную) + общий wizard-state (``_op_wizards`` и
  примитивы) + MAX-хелперы.
- ``admin_operators_list``    — список операторов и карточка оператора.
- ``admin_operators_roles``   — смена роли, деактивация, реактивация.

Здесь остаются только два публичных хендлера (``run_operators_menu`` и
``run_operators_action``) и re-export перенесённого, чтобы внешние
импорты (``admin_commands``, ``cron``, ``main``, ``admin_appeal_ops``)
и тесты продолжали работать по старому пути
``aemr_bot.handlers.admin_operators.*``.

Сценарии (полное описание — в docstring'ах подмодулей):

1. ДОБАВЛЕНИЕ ИЗ УЧАСТНИКОВ ГРУППЫ (основной путь).
2. ДОБАВЛЕНИЕ ПО ID ВРУЧНУЮ (fallback).
3. КАРТОЧКА ОПЕРАТОРА (смена роли / деактивация / реактивация).
4. УДАЛЕНИЕ — мягкое (is_active=false), физического DELETE нет:
   история ответов жителю сохраняется по 152-ФЗ.

Защиты:
- Самомодификация (деактивировать/сменить роль себе) блокируется.
- Деактивация / снятие IT-роли у единственного активного IT блокируется.
- Назначение IT-роли «себе» через wizard блокируется.
"""
from __future__ import annotations

import logging

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.models import OperatorRole
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.utils.event import ack_callback, get_user_id, send_or_edit_screen

# --- Подмодули реализации (импортируем как пространства имён, чтобы
# диспетчер дёргал хендлеры через них) ------------------------------------
from aemr_bot.handlers import admin_operators_list as _list
from aemr_bot.handlers import admin_operators_roles as _roles
from aemr_bot.handlers import admin_operators_wizard as _wizard

# --- Re-export перенесённого (фасад: старый путь импорта сохраняется) -----
# Wizard-state и примитивы (используют main.py, admin_appeal_ops.py,
# admin_commands.py и тесты через aemr_bot.handlers.admin_operators.*).
# _op_wizards — ТОТ ЖЕ объект, что в admin_operators_wizard (re-export
# сохраняет идентичность), поэтому .clear()/.pop() из внешних модулей
# работают на едином dict'е.
from aemr_bot.handlers.admin_operators_wizard import (  # noqa: F401
    _OP_WIZARD_TTL_SEC,
    _full_name_from_member,
    _op_wizard_drop,
    _op_wizard_get,
    _op_wizard_set,
    _op_wizards,
    _safe_get_chat_members,
    _time_op,
    handle_operators_wizard_text,
)

log = logging.getLogger(__name__)


# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────
# Точка входа в меню «👥 Операторы»
# · · · · · · · ·· · · · · · · ·· · · · · · · ·· · · · · · · ·──────


async def run_operators_menu(event) -> None:
    """Меню «👥 Операторы» в админ-панели для роли it. Точка входа."""

    if not await ensure_role(event, OperatorRole.IT):
        return
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text=(
            "👥 Управление операторами\n"
            "· · · · · · · ·\n"
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

    if not await ensure_role(event, OperatorRole.IT):
        return
    operator_id = get_user_id(event)
    if operator_id is None:
        await ack_callback(event)
        return

    # Карточка оператора
    if payload.startswith("op:opcard:"):
        await _list._show_operator_card(event, payload, operator_id)
        return

    # Смена роли — открыть picker
    if payload.startswith("op:oprole:"):
        await _roles._show_role_change(event, payload, operator_id)
        return

    # Применить смену роли
    if payload.startswith("op:opchrole:"):
        await _roles._apply_role_change(event, payload, operator_id)
        return

    # Деактивация — подтверждение
    if payload.startswith("op:opdeact_ok:"):
        await _roles._apply_deactivate(event, payload, operator_id)
        return
    if payload.startswith("op:opdeact:"):
        await _roles._show_deactivate_confirm(event, payload, operator_id)
        return

    # Реактивация
    if payload.startswith("op:opreact:"):
        await _roles._apply_reactivate(event, payload, operator_id)
        return

    # Wizard добавления оператора — старая семья callback'ов «op:opadd:*»
    suffix = payload.removeprefix("op:opadd:")
    await ack_callback(event)

    if suffix == "list":
        await _list._show_operators_list(event)
        return
    if suffix == "from_group":
        await _wizard._show_from_group(event, operator_id)
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
        await _wizard._start_add_with_picked(event, operator_id, picked_user_id)
        return
    if suffix == "start":
        await _wizard._start_manual_add(event, operator_id)
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
        await _wizard._apply_role_choice(event, suffix, operator_id)
        return
    if suffix == "name_keep":
        await _wizard._apply_name_keep(event, operator_id)
        return
    if suffix == "name_edit":
        await _wizard._start_name_edit(event, operator_id)
        return
    if suffix == "edit_role":
        await _wizard._back_to_role_pick(event, operator_id)
        return
    if suffix == "confirm":
        await _wizard._confirm_save(event, operator_id)
        return
