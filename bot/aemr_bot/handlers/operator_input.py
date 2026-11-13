"""Explicit ownership of the operator's next free-text message."""
from aemr_bot.db.session import session_scope
from aemr_bot.services import operator_drafts, operators, wizard_registry


def clear_other_consumers(actor_id: int) -> None:
    # These are the actual stores consulted by appeal.on_message, not mirrors.
    from aemr_bot.handlers import (
        admin_audience,
        admin_operators,
        admin_settings,
        broadcast,
        broadcast_templates,
    )
    broadcast._wizards.pop(actor_id, None)
    admin_operators._op_wizard_drop(actor_id)
    admin_settings._intent_drop(actor_id)
    broadcast_templates._wizards.pop(actor_id, None)
    admin_audience._search_intents.pop(actor_id, None)
    wizard_registry.drop_reply_intent(actor_id)


async def pause_reply(actor_id: int) -> None:
    """Preserve an existing reply when another composer takes input."""
    async with session_scope() as session:
        operator = await operators.get(session, actor_id)
        if operator is not None:
            draft = await operator_drafts.get(session, operator.id, locked=True)
            if draft is not None and draft.state == "editing":
                draft.state = "paused"


async def claim_composer(actor_id: int) -> None:
    """Switch input explicitly; a saved reply requires an explicit resume."""
    await pause_reply(actor_id)
    clear_other_consumers(actor_id)
