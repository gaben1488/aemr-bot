from datetime import datetime, timedelta, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from zoneinfo import ZoneInfo

from aemr_bot.config import settings
from aemr_bot.db.models import Appeal, AppealStatus

TZ = ZoneInfo(settings.timezone)


def period_window(period: str) -> tuple[datetime, datetime, str]:
    now = datetime.now(TZ)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        title = f"за сегодня {start:%d.%m.%Y}"
    elif period == "week":
        start = (now - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        title = f"за 7 дней с {start:%d.%m.%Y}"
    elif period == "month":
        start = (now - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        title = f"за 30 дней с {start:%d.%m.%Y}"
    else:
        raise ValueError(f"Unknown period: {period}")
    return start.astimezone(timezone.utc), now.astimezone(timezone.utc), title


async def build_xlsx(session: AsyncSession, period: str) -> tuple[bytes, str, int]:
    start, end, title = period_window(period)
    res = await session.scalars(
        select(Appeal)
        .options(selectinload(Appeal.user), selectinload(Appeal.messages))
        .where(Appeal.created_at >= start, Appeal.created_at <= end)
        .order_by(Appeal.created_at)
    )
    appeals = list(res)

    wb = Workbook()
    ws = wb.active
    ws.title = "Обращения"

    headers = [
        "№",
        "Создано",
        "Имя",
        "Телефон",
        "Населённый пункт",
        "Адрес",
        "Тематика",
        "Суть",
        "Статус",
        "Ответ оператора",
        "Время ответа, ч",
        "В SLA (4ч)",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top", wrap_text=True)

    sla_seconds = settings.sla_response_hours * 3600
    for a in appeals:
        operator_reply = next(
            (m.text for m in reversed(a.messages) if m.direction == "from_operator"),
            None,
        )
        if a.answered_at and a.created_at:
            elapsed = (a.answered_at - a.created_at).total_seconds()
            elapsed_hours = round(elapsed / 3600, 2)
            in_sla = "да" if elapsed <= sla_seconds else "нет"
        else:
            elapsed_hours = None
            in_sla = ""
        ws.append([
            a.id,
            a.created_at.astimezone(TZ).strftime("%d.%m.%Y %H:%M"),
            a.user.first_name if a.user else "",
            a.user.phone if a.user else "",
            a.locality or "",
            a.address or "",
            a.topic or "",
            a.summary or "",
            _status_label(a.status),
            operator_reply or "",
            elapsed_hours if elapsed_hours is not None else "",
            in_sla,
        ])

    widths = [6, 18, 16, 18, 28, 36, 24, 60, 18, 60, 14, 12]
    for col_idx, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = width
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=True)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.getvalue(), title, len(appeals)


def _status_label(status: str) -> str:
    return {
        AppealStatus.NEW.value: "Новое",
        AppealStatus.IN_PROGRESS.value: "В работе",
        AppealStatus.ANSWERED.value: "Завершено",
        AppealStatus.CLOSED.value: "Закрыто",
    }.get(status, status)
