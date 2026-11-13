"""Show the remaining attachments of an operator reply attempt."""
from aemr_bot import keyboards as kbds
from aemr_bot.config import settings as cfg
from aemr_bot.db.session import session_scope
from aemr_bot.handlers._auth import ensure_operator
from aemr_bot.services import admin_relay, data_erasure
from aemr_bot.services import appeals as appeals_service
from aemr_bot.utils.event import ack_callback, send_or_edit_screen


async def _op_reply_attempt_atts(event, attempt_id: int) -> None:
    """Explicitly show the attachment references saved with an attempt."""
    from aemr_bot.db.models import OperatorReplyAttempt

    await ack_callback(event)
    if not await ensure_operator(event):
        return
    try:
        async with data_erasure.personal_view() as view:
            async with session_scope() as session:
                attempt = await session.get(OperatorReplyAttempt, attempt_id)
                appeal = (await appeals_service.get_by_id_with_messages(session, attempt.appeal_id)
                          if attempt else None)
            if not attempt or not appeal or attempt.state not in {"sending", "unknown", "confirmed", "not_delivered"}:
                await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text="Вложения недоступны.",
                                          attachments=[kbds.op_back_to_menu_keyboard()])
                return
            await view.include(appeal_id=appeal.id)
            await admin_relay.render_appeal_attachments(
                event.bot, chat_id=cfg.admin_group_id, user_id=None, appeal=appeal,
                header_template="Вложения ответа по обращению #{appeal_id}",
                attachments_override=attempt.attachments,
            )
    except data_erasure.PersonalDataErasedError:
        await send_or_edit_screen(event, chat_id=cfg.admin_group_id, text="Вложения недоступны.",
                                  attachments=[kbds.op_back_to_menu_keyboard()])
