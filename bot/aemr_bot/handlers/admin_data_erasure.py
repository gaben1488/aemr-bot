"""IT-only reconciliation view; no automatic retry of unknown MAX mutations."""
from __future__ import annotations

from aemr_bot.db.models import OperatorRole
from aemr_bot.handlers._auth import ensure_role
from aemr_bot.handlers._common import op_screen
from aemr_bot.services import data_erasure


async def show_status(event) -> None:
    if not await ensure_role(event, OperatorRole.IT):
        return
    status = await data_erasure.diagnostics()
    lines = ["🧹 Удаление данных: учтённые копии",
             "Неучтённые старые сообщения, внешние выгрузки и версии хранилищ сверяются отдельно.",
             f"В очереди: {status.get('pending', 0)}",
             f"Удаление начато, результат неизвестен: {status.get('deleting', 0)}",
             f"Нужна проверка в MAX: {status.get('uncertain', 0) + status.get('reserved', 0)}",
             f"Удаление подтверждено: {status.get('deleted', 0)}",
             f"Резервные копии требуют проверки: {status.get('backups_unresolved', 0)}",
             ("Неизвестный результат не повторяется автоматически. "
             "Специалист ИТ проверяет историю MAX, версии S3 и журнал хоста по регламенту удаления.")]
    for item in await data_erasure.unresolved_copies(limit=10):
        lines.append(f"Копия {item['id']}: {item['kind']} / {item['state']}; MAX mid={item['message_id'] or 'неизвестен'}")
    for item in await data_erasure.unresolved_backups(limit=5):
        lines.append(f"Backup {item['digest']}: {item['state']} / {item['erased_before']}")
    await op_screen(event, text="\n".join(lines))
