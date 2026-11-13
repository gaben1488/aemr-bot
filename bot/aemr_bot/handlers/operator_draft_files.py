"""Explicit, owner-scoped inspection of durable operator draft attachments."""
from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from aemr_bot.config import settings
from aemr_bot.db.session import session_scope
from aemr_bot.services import data_erasure, operator_drafts
from aemr_bot.utils import menu_tracker
from aemr_bot.utils.attachments import _attachment_filename, deserialize_for_relay
from aemr_bot.utils.event import ack_callback, extract_message_id, get_callback_message_id
from aemr_bot.utils.url_defang import defang_for_admin


async def _screen(event, text, markup):
    """Edit a live navigation screen, never blindly resend an uncertain edit."""
    mid = get_callback_message_id(event)
    try:
        if menu_tracker.can_edit(settings.admin_group_id, mid, kind="menu"):
            await event.bot.edit_message(message_id=mid, text=text, attachments=[markup], notify=False)
        else:
            sent = await event.bot.send_message(chat_id=settings.admin_group_id, text=text,
                                                attachments=[markup])
            new_mid = extract_message_id(sent)
            if new_mid:
                menu_tracker.note_editable_send(settings.admin_group_id, new_mid, kind="menu")
    except Exception:
        await ack_callback(event, "Не удалось подтвердить показ списка. Черновик сохранён.")


async def handle(event, operator_id: int, parts: list[str]) -> None:
    """Called only after operator_draft.callback verifies author and admin chat."""
    action = parts[2]
    try:
        token = parts[3]
        if len(token) != 32:
            raise ValueError
        if action == "filelist" and len(parts) == 5:
            page = int(parts[4])
            revision = index = None
        elif action in {"fileopen", "filedel"} and len(parts) == 7:
            revision, index, page = map(int, parts[4:])
            if revision < 0 or index < 0:
                raise ValueError
        else:
            raise ValueError
        if page < 0:
            raise ValueError
    except (ValueError, IndexError):
        await ack_callback(event, "Кнопка устарела.")
        return
    try:
        # Begin before reading filenames/content, and keep through output. The
        # exclusive eraser cannot interleave between validation and publication.
        async with data_erasure.personal_view() as view:
            async with session_scope() as session:
                draft = await operator_drafts.get(session, operator_id, locked=True)
                if draft is None or draft.token != token or draft.state not in {"editing", "paused"}:
                    await ack_callback(event, "Черновик недоступен или отправка уже началась.")
                    return
                await view.include(appeal_id=draft.appeal_id)
                files = list((draft.data or {}).get("attachments") or [])
                current = operator_drafts.attachment_revision(draft.data or {})
                if action != "filelist":
                    if revision != current or index is None or index >= len(files):
                        await ack_callback(event, "Состав изменился. Откройте список заново.")
                        return
                    if action == "filedel":
                        if draft.state != "editing":
                            await ack_callback(event, "Сначала продолжите черновик.")
                            return
                        files.pop(index)
                        operator_drafts.replace_attachments(draft, files)
                        current = operator_drafts.attachment_revision(draft.data)
                        await session.flush()
                editable = draft.state == "editing"
            # Row transaction committed before output; no MAX call while holding
            # the row lock needed by append/claim/cancel.
            if action == "fileopen":
                assert index is not None  # validated file action above
                attachment = deserialize_for_relay([files[index]])
                if not attachment:
                    await ack_callback(event, "Файл недоступен. Он сохранён в черновике.")
                    return
                try:
                    sent = await event.bot.send_message(chat_id=settings.admin_group_id,
                        text=f"Вложение № {index + 1} черновика ответа", attachments=attachment)
                    if not extract_message_id(sent):
                        raise RuntimeError("unconfirmed attachment view")
                except Exception:
                    await ack_callback(event, "Результат показа неизвестен. Проверьте чат; файл сохранён.")
                    return
                await ack_callback(event)
                return
            page = min(page, max(0, (len(files) - 1) // 5))
            start = page * 5
            lines = ["📎 Вложения черновика ответа", "Доступность файлов при отправке пока не проверена."]
            keyboard = InlineKeyboardBuilder()
            labels = {"image": "Фото", "video": "Видео", "file": "Файл"}
            for i, item in enumerate(files[start:start + 5], start):
                name = _attachment_filename(item)
                if name:
                    name = (defang_for_admin(" ".join(name.split())[:160]) or "").replace("🆔 №", "🆔 \u200b№")
                label = labels.get(str(item.get("type")) if isinstance(item, dict) else "", "Вложение")
                lines.append(f"{i + 1}. {label}" + (" — " + name if name else ""))
                buttons = [CallbackButton(text=f"Открыть № {i + 1}",
                    payload=f"op:replydraft:fileopen:{token}:{current}:{i}:{page}")]
                if editable:
                    buttons.append(CallbackButton(text=f"Удалить № {i + 1}",
                        payload=f"op:replydraft:filedel:{token}:{current}:{i}:{page}"))
                keyboard.row(*buttons)
            if not files:
                lines.append("Вложений пока нет.")
            nav = []
            if page:
                nav.append(CallbackButton(text="◀️ Назад", payload=f"op:replydraft:filelist:{token}:{page - 1}"))
            if start + 5 < len(files):
                nav.append(CallbackButton(text="Далее ▶️", payload=f"op:replydraft:filelist:{token}:{page + 1}"))
            if nav:
                keyboard.row(*nav)
            keyboard.row(CallbackButton(text="↩️ К черновику", payload=f"op:replydraft:open:{token}"))
            await ack_callback(event, "Вложение удалено из черновика." if action == "filedel" else "")
            await _screen(event, "\n".join(lines), keyboard.as_markup())
    except (data_erasure.PersonalDataErasedError, operator_drafts.DraftConflict):
        await ack_callback(event, "Черновик сейчас недоступен. Данные не отправлены.")
