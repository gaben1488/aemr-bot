"""Shared operator-authorization helpers.

Used by handlers/admin_commands.py and handlers/broadcast.py to gate access
to operator-only flows. Centralized here so that role-set checks and the
refusal message stay consistent across all callers.
"""

from __future__ import annotations

from aemr_bot.db.models import Operator, OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as operators_service
from aemr_bot.utils.event import get_user_id, is_admin_chat


async def get_operator(event) -> Operator | None:
    """Return the active Operator row for the message author when the event
    came from the admin group; otherwise None. Used as the building block
    for both ensure_operator and ensure_role.
    """
    if not is_admin_chat(event):
        return None
    author_id = get_user_id(event)
    if author_id is None:
        return None
    async with session_scope() as session:
        return await operators_service.get(session, author_id)


async def ensure_operator(event) -> bool:
    """True if the event author is a registered active operator in the
    admin group."""
    return (await get_operator(event)) is not None


async def ensure_role(event, *allowed: OperatorRole) -> bool:
    """True if the event author has one of `allowed` roles. Sends a
    Russian refusal message into the chat on failure (so the operator
    sees why their command was ignored)."""
    op = await get_operator(event)
    if op is None:
        return False
    if op.role not in {r.value for r in allowed}:
        await event.message.answer(
            f"Команда доступна только ролям: {', '.join(r.value for r in allowed)}"
        )
        return False
    return True
