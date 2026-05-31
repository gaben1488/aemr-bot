"""Карточка автора коммитов от бота.

Выделено из god-объекта `admin_settings.py`. `commit_author_name` /
`commit_author_email` подставляются в коммиты, которые бот создаёт при
синхронизации настроек с репозиторием (PR-flow).

Карточка только отображает значения; сама правка идёт через общий
intent-flow текстовых ключей (`_apply_single_edit` в
`admin_settings_text`, который для commit_author_* перерисовывает
именно эту карточку).
"""
from __future__ import annotations

from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.services import settings_store
from aemr_bot.utils.event import send_or_edit_screen


async def _show_author_card(event) -> None:

    async with session_scope() as session:
        name = await settings_store.get(session, "commit_author_name")
        email = await settings_store.get(session, "commit_author_email")
    name_line = name or "(не задано)"
    email_line = email or "(не задано)"
    await send_or_edit_screen(
        event, chat_id=cfg.admin_group_id,
        text=(
            "👤 Автор коммитов от бота\n"
            "· · · · · · · ·\n"
            f"ФИО:   {name_line}\n"
            f"Email: {email_line}\n\n"
            "Это значения подставляются в коммиты,\n"
            "которые бот создаёт при синхронизации\n"
            "настроек с репозиторием."
        ),
        attachments=[kbds.op_settings_author_keyboard()],
    )
