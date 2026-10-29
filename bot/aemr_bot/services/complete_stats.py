"""Readable XLSX export, built from bounded database batches."""
from __future__ import annotations

import asyncio
from datetime import UTC
from io import BytesIO

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font
from sqlalchemy import select, text
from sqlalchemy.orm import selectinload

from aemr_bot.config import settings
from aemr_bot.db.models import Appeal, Message
from aemr_bot.services import appeals as appeals_service
from aemr_bot.services import privacy_epoch, sla, stats
from aemr_bot.utils.pii_mask import mask_phone
from aemr_bot.utils.text_pages import split_text_units

_BATCH_SIZE = 100
_SHEET_LIMIT = 1_048_576


def cell_chunks(value: str) -> list[str]:
    """Keep all text, explicitly represent XML-incompatible control characters."""
    safe = "".join(c if c in "\t\n\r" or 0x20 <= ord(c) <= 0xD7FF
                   or 0xE000 <= ord(c) <= 0xFFFD
                   or 0x10000 <= ord(c) <= 0x10FFFF
                   else f"⟦U+{ord(c):04X}⟧" for c in value)
    # Conservative for Excel's cell limit even if non-BMP characters count
    # as two UTF-16 units; allow room for a formula-protection apostrophe.
    return split_text_units(safe, 32000)


def _cell(value):
    if not isinstance(value, str):
        return value
    return "'" + value if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


def _date(value) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(stats.TZ).strftime("%d.%m.%Y %H:%M")


class _Sheet:
    def __init__(self, workbook, title: str, headers: list[str], widths: list[int]):
        self.workbook, self.title, self.headers, self.widths = workbook, title, headers, widths
        self.number = 0
        self.rows = 0
        self.ws = None
        self.completed = []
        self._new()

    def _new(self) -> None:
        from openpyxl.utils import get_column_letter

        if self.ws is not None:
            self.completed.append((self.ws, self.rows))
        self.number += 1
        name = self.title if self.number == 1 else f"{self.title} {self.number}"
        self.ws = self.workbook.create_sheet(name[:31])
        self.ws.freeze_panes = "A2"
        for index, width in enumerate(self.widths, 1):
            self.ws.column_dimensions[get_column_letter(index)].width = width
        cells = []
        for title in self.headers:
            cell = WriteOnlyCell(self.ws, value=title)
            cell.font = Font(bold=True)
            cells.append(cell)
        self.ws.append(cells)
        self.rows = 1

    def append(self, values: list) -> None:
        columns = [cell_chunks(value) if isinstance(value, str) else [value] for value in values]
        for part in range(max(len(column) for column in columns)):
            if self.rows >= _SHEET_LIMIT:
                self._new()
            cells = []
            for column in columns:
                value = column[part] if part < len(column) else column[0] if len(column) == 1 else ""
                cell = WriteOnlyCell(self.ws, value=_cell(value))
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cells.append(cell)
            self.ws.append(cells)
            self.rows += 1

    def finish(self) -> None:
        from openpyxl.utils import get_column_letter

        for sheet, rows in [*self.completed, (self.ws, self.rows)]:
            sheet.auto_filter.ref = f"A1:{get_column_letter(len(self.headers))}{rows}"


def _attachment_description(item) -> str:
    if not isinstance(item, dict):
        return "Вложение: сведения недоступны"
    label = {"image": "Фото", "video": "Видео", "file": "Файл", "audio": "Аудио"}.get(
        str(item.get("type", "")).lower(), "Вложение"
    )
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    name = item.get("filename") or payload.get("filename") or item.get("name") or ""
    return f"{label}: {name}" if name else label


async def build_xlsx_with_epoch(session, period: str) -> tuple[bytes, str, int, int]:
    if not session.in_transaction() and session.bind.dialect.name == "postgresql":
        # Keep the selected appeals and their histories at one consistent
        # point in time while other operators continue their work.
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"))
    snapshot_epoch = await privacy_epoch.read(session)
    start, end, title = stats.period_window(period)
    workbook = Workbook(write_only=True)
    overview = _Sheet(workbook, "Обращения", ["№", "Подано", "Житель", "Телефон", "Населённый пункт",
                        "Адрес", "Тема", "Статус", "Первый ответ периода", "Итоговый ответ", "Закрыто",
                        "Время до первого ответа, ч", "Первый ответ в срок", "Согласие дано",
                        "Согласие отозвано", "Подписка", "Блокировка"],
                      [9, 19, 22, 17, 25, 34, 25, 20, 19, 19, 19, 18, 20, 19, 19, 15, 15])
    history = _Sheet(workbook, "История", ["Обращение №", "Запись", "Дата", "Автор", "Часть", "Текст"],
                     [16, 12, 19, 24, 12, 75])
    files = _Sheet(workbook, "Вложения", ["Обращение №", "Запись", "Дата", "Вложение"], [16, 12, 19, 60])
    consent = _Sheet(workbook, "Согласия", ["Обращение №", "Подтверждено", "Часть", "Текст согласия"],
                     [16, 19, 12, 75])
    periods = _Sheet(workbook, "Сроки", ["Обращение №", "Начало периода", "Первый ответ",
                     "Окончание оценки", "Норматив, рабочих часов", "Просрочено", "Пояснение"],
                     [16, 20, 20, 20, 24, 15, 45])

    def add_entry(appeal_id, record_no, date, author, text, attachments):
        parts = cell_chunks(text or "")
        for i, part in enumerate(parts, 1):
            history.append([appeal_id, record_no, _date(date), author, f"{i}/{len(parts)}", part])
        for item in attachments or []:
            files.append([appeal_id, record_no, _date(date), _attachment_description(item)])

    count, after_id = 0, 0
    while True:
        query = (select(Appeal).options(selectinload(Appeal.user))
                 .where(Appeal.created_at <= end, Appeal.id > after_id)
                 .order_by(Appeal.id).limit(_BATCH_SIZE))
        if start is not None:
            query = query.where(Appeal.created_at >= start)
        appeals = list(await session.scalars(query))
        if not appeals:
            break
        after_id = appeals[-1].id
        first_replies = await appeals_service.first_operator_reply_times(session, [a.id for a in appeals])
        for appeal in appeals:
            user = appeal.user
            elapsed = ""
            on_time = ""
            first_reply = first_replies.get(appeal.id)
            anchor = appeal.sla_anchor_at or appeal.created_at
            if first_reply and anchor:
                elapsed = round((first_reply - anchor).total_seconds() / 3600, 2)
                on_time = "нет" if sla.is_overdue(anchor, first_reply,
                                                settings.sla_response_hours) else "да"
            overview.append([appeal.id, _date(appeal.created_at), getattr(user, "first_name", "") or "",
                             mask_phone(getattr(user, "phone", None)), appeal.locality or "",
                             appeal.address or "", appeal.topic or "", stats._status_label(appeal.status),
                             _date(first_reply), _date(appeal.answered_at), _date(appeal.closed_at), elapsed, on_time,
                             _date(getattr(user, "consent_pdn_at", None)),
                             _date(getattr(user, "consent_revoked_at", None)),
                             "да" if getattr(user, "subscribed_broadcast", False) else "нет",
                             "да" if getattr(user, "is_blocked", False) else "нет"])
            for period_record in appeal.sla_history or []:
                if not isinstance(period_record, dict):
                    continue
                def period_date(field):
                    try:
                        from datetime import datetime
                        return _date(datetime.fromisoformat(period_record[field]))
                    except (ValueError, KeyError, TypeError):
                        return ""
                periods.append([appeal.id, period_date("start"), period_date("first_response"),
                                period_date("end"), period_record.get("sla_hours", ""),
                                "да" if period_record.get("overdue") else "нет",
                                "Оценка рассчитана при возобновлении обращения"])
            records = getattr(user, "consent_records", None) or []
            if not records:
                consent.append([appeal.id, "", "", "Текст ранее данного согласия не сохранён"])
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("text"), str):
                    continue
                try:
                    from datetime import datetime
                    accepted = _date(datetime.fromisoformat(record["accepted_at"]))
                except (ValueError, KeyError, TypeError):
                    accepted = "дата не сохранилась"
                parts = cell_chunks(record["text"])
                for i, part in enumerate(parts, 1):
                    consent.append([appeal.id, accepted, f"{i}/{len(parts)}", part])
        messages = await session.stream_scalars(
            select(Message).where(Message.appeal_id.in_([a.id for a in appeals]))
            .order_by(Message.appeal_id, Message.created_at, Message.id)
            .execution_options(yield_per=500)
        )
        index = 0
        record_no = 0
        async for message in messages:
            while index < len(appeals) and appeals[index].id < message.appeal_id:
                a = appeals[index]
                if record_no == 0:
                    add_entry(a.id, 1, a.created_at, "Обращение жителя", a.summary, a.attachments)
                index += 1
                record_no = 0
            if index >= len(appeals):
                break
            appeal = appeals[index]
            if record_no == 0:
                add_entry(appeal.id, 1, appeal.created_at, "Обращение жителя", appeal.summary, appeal.attachments)
                record_no = 1
            record_no += 1
            author = {"from_user": "Житель", "from_operator": "Специалист", "system": "Служебная запись"}.get(
                message.direction, "Запись"
            )
            add_entry(appeal.id, record_no, message.created_at, author, message.text, message.attachments)
        while index < len(appeals):
            appeal = appeals[index]
            if record_no == 0:
                add_entry(appeal.id, 1, appeal.created_at, "Обращение жителя", appeal.summary, appeal.attachments)
            index += 1
            record_no = 0
        count += len(appeals)

    for sheet in (overview, history, files, consent, periods):
        sheet.finish()
    output = BytesIO()
    await asyncio.to_thread(workbook.save, output)
    return output.getvalue(), title, count, snapshot_epoch


async def build_xlsx(session, period: str) -> tuple[bytes, str, int]:
    content, title, count, _ = await build_xlsx_with_epoch(session, period)
    return content, title, count
