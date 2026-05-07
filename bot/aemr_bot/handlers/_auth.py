"""Общие хелперы авторизации операторов.

Используются handlers/admin_commands.py и handlers/broadcast.py для
ограничения доступа к операторским сценариям. Собраны здесь, чтобы
проверки набора ролей и сообщение об отказе оставались одинаковыми у
всех вызывающих.
"""

from __future__ import annotations

from aemr_bot.db.models import Operator, OperatorRole
from aemr_bot.db.session import session_scope
from aemr_bot.services import operators as operators_service
from aemr_bot.utils.event import get_user_id, is_admin_chat


async def get_operator(event) -> Operator | None:
    """Вернуть активную запись Operator для автора сообщения, если событие
    пришло из админ-группы. Иначе None. Используется как кирпичик для
    ensure_operator и ensure_role.
    """
    if not is_admin_chat(event):
        return None
    author_id = get_user_id(event)
    if author_id is None:
        return None
    async with session_scope() as session:
        return await operators_service.get(session, author_id)


async def ensure_operator(event) -> bool:
    """True, если автор события зарегистрирован как активный оператор в
    админ-группе."""
    return (await get_operator(event)) is not None


async def ensure_role(event, *allowed: OperatorRole) -> bool:
    """True, если автор события имеет одну из ролей `allowed`. При отказе
    отправляет в чат русский текст-отказ, чтобы оператор видел, почему
    команда проигнорирована."""
    op = await get_operator(event)
    if op is None:
        return False
    if op.role not in {r.value for r in allowed}:
        await event.message.answer(
            f"Команда доступна только ролям: {', '.join(r.value for r in allowed)}"
        )
        return False
    return True
