"""IT diagnostics for local cleanup and manual external-copy handling."""
from __future__ import annotations

from aemr_bot.db.models import OperatorRole
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.handlers._common import op_screen
from aemr_bot.services import data_erasure


async def show_status(event) -> None:
    if not await ensure_role(event, OperatorRole.IT):
        return
    status = await data_erasure.diagnostics()
    manual = sum(status.get(key, 0) for key in ("manual", "pending", "deleting", "uncertain", "reserved"))
    lines = [
        "🧹 Удаление из рабочей базы и локальных копий",
        ("Сообщения MAX и ранее выданные выгрузки автоматически не удаляются. "
         "Внешние копии сотрудники обрабатывают вручную."),
        f"Ручная проверка учтённых копий: {manual}",
        f"Локальные резервные копии требуют проверки: {status.get('backups_unresolved', 0)}",
        ("Метаданные ниже ограничивают новый вывод удалённых данных; "
         "они не подтверждают очистку внешних копий."),
    ]
    for item in await data_erasure.unresolved_copies(limit=10):
        lines.append(f"Копия {item['id']}: {item['kind']} / {item['state']}; MAX mid={item['message_id'] or 'неизвестен'}")
    for item in await data_erasure.unresolved_backups(limit=5):
        lines.append(f"Backup {item['digest']}: {item['state']} / {item['erased_before']}")
    await op_screen(event, text="\n".join(lines))
