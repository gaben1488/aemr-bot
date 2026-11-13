"""Inspect and remove individual attachments from a resident's draft."""
from __future__ import annotations

from maxapi.types import CallbackButton
from maxapi.utils.inline_keyboard import InlineKeyboardBuilder

from aemr_bot import keyboards
from aemr_bot.config import settings as cfg
from aemr_bot.copy.draft_preview import render as render_draft_preview
from aemr_bot.db.models import Appeal, AppealStatus, DialogState
from aemr_bot.db.session import session_scope
from aemr_bot.services import data_erasure, users
from aemr_bot.utils.attachments import _attachment_filename, deserialize_for_relay
from aemr_bot.utils.event import ack_callback
from aemr_bot.utils.resident_lifecycle import guard


async def handle(event, user_id: int, payload: str) -> None:
    parts = payload.split(":")
    action = parts[1] if len(parts) > 1 else ""
    try:
        if action in {"files", "filesback"} and len(parts) in ((3, 4) if action == "files" else (3,)):
            token = parts[2]
            page = int(parts[3]) if len(parts) == 4 else 0
            revision = index = None
        elif action in {"filedel", "fileopen"} and len(parts) == 6:
            token = parts[2]
            revision, index, page = map(int, parts[3:])
        else:
            raise ValueError
        if page < 0 or (revision is not None and (revision < 0 or index is None or index < 0)):
            raise ValueError
    except (ValueError, IndexError):
        await ack_callback(event, "Эта кнопка устарела.")
        return

    try:
        async with guard(user_id), data_erasure.personal_view(max_user_ids=[user_id]):
            async with session_scope() as session:
                user = await users.find_by_max_id(session, user_id, for_update=True)
                if (user is None or user.is_blocked or user.consent_pdn_at is None
                        or user.dialog_state not in {DialogState.AWAITING_SUMMARY.value,
                                                     DialogState.AWAITING_FOLLOWUP_TEXT.value}
                        or (user.dialog_data or {}).get("draft_token") != token):
                    await ack_callback(event, "Черновик недоступен или кнопка устарела.")
                    return
                data = dict(user.dialog_data or {})
                followup = user.dialog_state == DialogState.AWAITING_FOLLOWUP_TEXT.value
                source = None
                if action == "filesback" and followup:
                    try:
                        source_id = int(data.get("appeal_id") or 0)
                    except (TypeError, ValueError):
                        source_id = 0
                    if source_id > 0:
                        source = await session.get(Appeal, source_id)
                    if source is not None and source.user_id != user.id:
                        source = None
                files = list(data.get("attachments") or [])
                current_revision = data.get("attachments_revision", 0)
                if not isinstance(current_revision, int) or current_revision < 0:
                    await ack_callback(event, "Не удалось открыть вложения. Черновик сохранён.")
                    return
                if action in {"filedel", "fileopen"}:
                    assert index is not None  # validated file action above
                    if revision != current_revision or index >= len(files):
                        await ack_callback(event, "Состав изменился. Откройте список заново.")
                        return
                    if action == "filedel":
                        files.pop(index)
                        current_revision += 1
                        data.update(attachments=files, attachments_revision=current_revision)
                        user.dialog_data = data
                        await session.flush()
            if action == "filesback":
                if data.get("replace_text"):
                    text = "Пришлите новый текст одним сообщением. Прежний текст сохранится, пока вы не пришлёте замену; вложения останутся."
                    markup = keyboards.draft_edit_cancel_keyboard(token)
                elif source is not None and (source.content_erased_at is not None or source.status in {
                    AppealStatus.ANSWERED.value, AppealStatus.CLOSED.value
                }):
                    text = ("Обращение завершено или его содержание очищено. "
                            "Можно создать новое обращение: текст и вложения черновика сохранятся.")
                    markup = keyboards.closed_followup_draft_keyboard(token)
                else:
                    text = render_draft_preview(data, followup=followup, max_chars=cfg.summary_max_chars,
                                                max_attachments=cfg.attachments_max_per_appeal)
                    has_content = bool("\n".join(data.get("summary_chunks") or []).strip() or files)
                    markup = keyboards.appeal_draft_keyboard(token, has_content=has_content, followup=followup)
                await ack_callback(event)
                await data_erasure.tracked_send(event.bot, kind="personal_view", max_user_ids=[user_id],
                                                user_id=user_id, text=text, attachments=[markup])
                return
            if action == "fileopen":
                assert index is not None  # validated file action above
                attachment = deserialize_for_relay([files[index]])
                if not attachment:
                    await ack_callback(event, "Не удалось открыть файл. Он сохранён в черновике.")
                    return
                try:
                    await data_erasure.tracked_send(event.bot, kind="personal_view", max_user_ids=[user_id],
                                                    user_id=user_id, text=f"Вложение № {index + 1}",
                                                    attachments=attachment)
                except Exception:
                    await ack_callback(event, "Не удалось подтвердить показ файла. Черновик сохранён.")
                    return
                await ack_callback(event)
                return
            # Render only after the mutation is committed, while the privacy barrier is held.
            page = min(page, max(0, (len(files) - 1) // 5))
            start = page * 5
            lines = ["📎 Вложения черновика", "Доступность файлов при отправке пока не проверена."]
            keyboard = InlineKeyboardBuilder()
            labels = {"image": "Фото", "video": "Видео", "file": "Файл"}
            for i, item in enumerate(files[start:start + 5], start):
                kind = labels.get(str(item.get("type")) if isinstance(item, dict) else "", "Вложение")
                name = _attachment_filename(item)
                if name:
                    name = " ".join(name.split())[:160]
                lines.append(f"{i + 1}. {kind}" + (f" — {name}" if name else ""))
                keyboard.row(CallbackButton(text=f"Открыть № {i + 1}",
                                            payload=f"draft:fileopen:{token}:{current_revision}:{i}:{page}"),
                             CallbackButton(text=f"Удалить № {i + 1}",
                                            payload=f"draft:filedel:{token}:{current_revision}:{i}:{page}"))
            if not files:
                lines.append("Вложений пока нет.")
            nav = []
            if page:
                nav.append(CallbackButton(text="◀️ Назад", payload=f"draft:files:{token}:{page - 1}"))
            if start + 5 < len(files):
                nav.append(CallbackButton(text="Далее ▶️", payload=f"draft:files:{token}:{page + 1}"))
            if nav:
                keyboard.row(*nav)
            keyboard.row(CallbackButton(text="↩️ К черновику", payload=f"draft:filesback:{token}"))
            await ack_callback(event, "Вложение удалено из черновика." if action == "filedel" else "")
            await data_erasure.tracked_send(event.bot, kind="personal_view", max_user_ids=[user_id],
                                            user_id=user_id, text="\n".join(lines),
                                            attachments=[keyboard.as_markup()])
    except data_erasure.PersonalDataErasedError:
        await ack_callback(event, "Черновик сейчас недоступен.")
