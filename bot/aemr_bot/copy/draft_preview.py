"""Compact resident preview; the full draft remains available separately."""

from collections.abc import Mapping
from typing import Any


def render(data: Mapping[str, Any], *, followup: bool,
           max_chars: int, max_attachments: int) -> str:
    chunks = data.get("summary_chunks") or []
    files = data.get("attachments") or []
    full = "\n".join(chunks)
    preview = full[:400] + ("…" if len(full) > 400 else "")
    title = f"Черновик дополнения к № {data.get('appeal_id')}" if followup else "Черновик обращения"
    context = ""
    if not followup:
        context = "\n".join(
            f"{label}: {data.get(key) or 'не указано'}"
            for key, label in (("locality", "Населённый пункт"),
                               ("address", "Адрес проблемы"), ("topic", "Тема"))
        ) + "\n\n"
    return (f"📝 {title}\n\n{context}Частей текста: {len(chunks)} · "
            f"Символов: {len(full)} из {max_chars}\n"
            f"Вложений: {len(files)} из {max_attachments}\n\n"
            + (f"{preview}\n\n" if preview else "")
            + "Можно прислать ещё текст или файлы. Когда всё готово, нажмите кнопку отправки.")
