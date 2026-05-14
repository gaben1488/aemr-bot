"""Статистика для оператора — XLSX за период.

Выделено из handlers/admin_commands.py (рефакторинг 2026-05-10).
"""
from __future__ import annotations

from datetime import datetime

from aemr_bot import keyboards as kbds
from aemr_bot import texts
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator
from aemr_bot.services import stats as stats_service
from aemr_bot.utils.event import get_chat_id, send_or_edit_screen


async def _send_stats_xlsx(
    event, period: str, *, target_chat_id: int | None = None
) -> bool:
    """Сформировать XLSX за период и опубликовать в админ-группе."""
    from aemr_bot.services import uploads

    chat_id = target_chat_id if target_chat_id is not None else get_chat_id(event)
    async with session_scope() as session:
        content, title, count = await stats_service.build_xlsx(session, period)
    if count == 0:
        await send_or_edit_screen(
            event,
            chat_id=chat_id,
            text=texts.OP_STATS_EMPTY,
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return False
    filename = f"appeals_{period}_{datetime.now():%Y-%m-%d}.xlsx"
    token = await uploads.upload_bytes(event.bot, content, suffix=".xlsx")
    if token is None:
        await send_or_edit_screen(
            event,
            chat_id=chat_id,
            text=(
                f"Сформирован XLSX за {title} ({count} обращений), "
                "но загрузить файл не удалось. См. логи бота."
            ),
            attachments=[kbds.op_back_to_menu_keyboard()],
        )
        return False
    await event.bot.send_message(
        chat_id=chat_id,
        text=f"📊 Статистика {title} ({count} обращений). Файл: {filename}",
        attachments=[uploads.file_attachment(token)],
    )
    return True


async def run_stats_today(event) -> bool:
    """То же действие, что и /stats today, вызывается по кнопке."""
    if not await ensure_operator(event):
        return False
    return await _send_stats_xlsx(event, "today", target_chat_id=cfg.admin_group_id)


async def run_stats(event, period: str) -> None:
    """Универсальный обработчик кнопок «📊 За …»."""
    from aemr_bot.handlers.admin_panel import show_op_menu
    from aemr_bot.services.stats import VALID_PERIODS

    if period not in VALID_PERIODS:
        return
    if not await ensure_operator(event):
        return
    if await _send_stats_xlsx(event, period, target_chat_id=cfg.admin_group_id):
        await show_op_menu(event, pin=False)


async def run_stats_menu(event) -> None:
    """Открыть подменю «📊 Статистика» — выбор периода."""
    if not await ensure_operator(event):
        return
    await send_or_edit_screen(
        event,
        chat_id=cfg.admin_group_id,
        text="Выгрузка XLSX. Выберите период:",
        attachments=[kbds.op_stats_menu_keyboard()],
    )
