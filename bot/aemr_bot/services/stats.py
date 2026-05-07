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


VALID_PERIODS = ("today", "week", "month", "quarter", "half_year", "year", "all")


def period_window(period: str) -> tuple[datetime | None, datetime, str]:
    """Окно периода для /stats. Возвращает (start_utc, end_utc, title).

    Для `all` start_utc=None — выгрузка идёт без нижнего фильтра по дате,
    то есть «за всё время существования бота». Все остальные значения
    дают конкретный start.
    """
    now = datetime.now(TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "today":
        start = midnight
        title = f"за сегодня {start:%d.%m.%Y}"
    elif period == "week":
        start = midnight - timedelta(days=7)
        title = f"за 7 дней с {start:%d.%m.%Y}"
    elif period == "month":
        start = midnight - timedelta(days=30)
        title = f"за 30 дней с {start:%d.%m.%Y}"
    elif period == "quarter":
        start = midnight - timedelta(days=90)
        title = f"за квартал с {start:%d.%m.%Y}"
    elif period == "half_year":
        start = midnight - timedelta(days=183)
        title = f"за полгода с {start:%d.%m.%Y}"
    elif period == "year":
        start = midnight - timedelta(days=365)
        title = f"за год с {start:%d.%m.%Y}"
    elif period == "all":
        return None, now.astimezone(timezone.utc), "за всё время"
    else:
        raise ValueError(f"Unknown period: {period}")
    return start.astimezone(timezone.utc), now.astimezone(timezone.utc), title


async def build_xlsx(session: AsyncSession, period: str) -> tuple[bytes, str, int]:
    start, end, title = period_window(period)
    query = (
        select(Appeal)
        .options(selectinload(Appeal.user), selectinload(Appeal.messages))
        .where(Appeal.created_at <= end)
        .order_by(Appeal.created_at)
    )
    if start is not None:
        query = query.where(Appeal.created_at >= start)
    res = await session.scalars(query)
    appeals = list(res)

    wb = Workbook()
    ws = wb.active
    ws.title = "Обращения"

    headers = [
        "№",
        "Создано",
        "Имя",
        "Телефон",
        "max_user_id",
        "Населённый пункт",
        "Адрес",
        "Тематика",
        "Суть",
        "Статус",
        "Ответ оператора",
        "Время ответа, ч",
        "В SLA (4ч)",
        "Согласие на ПДн",
        "Согласие отозвано",
        "Подписан на рассылку",
        "Заблокирован",
    ]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True)
        cell.alignment = Alignment(vertical="top", wrap_text=True)

    sla_seconds = settings.sla_response_hours * 3600

    def _fmt_dt(dt) -> str:
        if dt is None:
            return ""
        return dt.astimezone(TZ).strftime("%d.%m.%Y %H:%M")

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
        u = a.user
        ws.append([
            a.id,
            _fmt_dt(a.created_at),
            u.first_name if u else "",
            u.phone if u else "",
            u.max_user_id if u else "",
            a.locality or "",
            a.address or "",
            a.topic or "",
            a.summary or "",
            _status_label(a.status),
            operator_reply or "",
            elapsed_hours if elapsed_hours is not None else "",
            in_sla,
            _fmt_dt(u.consent_pdn_at) if u else "",
            _fmt_dt(u.consent_revoked_at) if u else "",
            "да" if (u and u.subscribed_broadcast) else "нет",
            "да" if (u and u.is_blocked) else "нет",
        ])

    widths = [6, 18, 16, 18, 14, 28, 36, 24, 60, 18, 60, 14, 12, 18, 18, 14, 14]
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
