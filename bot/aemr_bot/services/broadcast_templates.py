"""Пул шаблонов рассылок (PR H).

CRUD над таблицей `broadcast_templates`. Сервис ничего не знает про
maxapi и handlers — это чистый persistence-слой. UI и применение
шаблона как drafft'а рассылки — в handlers/broadcast_templates.py.

Семантика soft-delete: archive_template переводит archived_at в now,
get/list по умолчанию исключают archived. Старые Broadcast'ы,
созданные на основе шаблона, не задеваются — они хранят собственные
text/attachments (template копируется в момент применения, не
ссылочно).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from aemr_bot.db.models import BroadcastTemplate


class TemplateNameAlreadyExists(Exception):
    """Имя шаблона уже занято среди активных (не archived) шаблонов."""


class TemplateNotFound(Exception):
    """Шаблон с заданным id отсутствует или архивирован."""


MAX_NAME_LEN = 64
MAX_TEXT_LEN = 1000


def _normalize_name(name: str) -> str:
    return name.strip()


def _validate_name(name: str) -> str:
    n = _normalize_name(name)
    if not n:
        raise ValueError("Имя шаблона не может быть пустым.")
    if len(n) > MAX_NAME_LEN:
        raise ValueError(
            f"Имя шаблона не длиннее {MAX_NAME_LEN} символов "
            f"(получено {len(n)})."
        )
    return n


def _validate_text(text: str) -> str:
    t = text.strip()
    if not t:
        raise ValueError("Текст шаблона не может быть пустым.")
    if len(t) > MAX_TEXT_LEN:
        raise ValueError(
            f"Текст шаблона не длиннее {MAX_TEXT_LEN} символов "
            f"(получено {len(t)})."
        )
    return t


async def create_template(
    session: AsyncSession,
    *,
    name: str,
    text: str,
    attachments: Sequence[dict] | None = None,
    created_by_operator_id: int | None = None,
) -> BroadcastTemplate:
    """Создать шаблон. `name` уникальное среди активных шаблонов."""
    n = _validate_name(name)
    t = _validate_text(text)
    tmpl = BroadcastTemplate(
        name=n,
        text=t,
        attachments=list(attachments or []),
        created_by_operator_id=created_by_operator_id,
    )
    session.add(tmpl)
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise TemplateNameAlreadyExists(n) from exc
    return tmpl


async def list_active(
    session: AsyncSession, *, limit: int = 50
) -> list[BroadcastTemplate]:
    """Активные (не archived) шаблоны, новые сверху."""
    result = await session.execute(
        select(BroadcastTemplate)
        .where(BroadcastTemplate.archived_at.is_(None))
        .order_by(desc(BroadcastTemplate.updated_at))
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_active(session: AsyncSession) -> int:
    """Количество активных шаблонов."""
    return (
        await session.scalar(
            select(func.count())
            .select_from(BroadcastTemplate)
            .where(BroadcastTemplate.archived_at.is_(None))
        )
    ) or 0


async def get_by_id(
    session: AsyncSession, template_id: int, *, include_archived: bool = False
) -> BroadcastTemplate | None:
    stmt = select(BroadcastTemplate).where(BroadcastTemplate.id == template_id)
    if not include_archived:
        stmt = stmt.where(BroadcastTemplate.archived_at.is_(None))
    return await session.scalar(stmt)


async def rename(
    session: AsyncSession, template_id: int, new_name: str
) -> BroadcastTemplate:
    """Переименовать шаблон. Имя должно быть уникальным."""
    n = _validate_name(new_name)
    tmpl = await get_by_id(session, template_id)
    if tmpl is None:
        raise TemplateNotFound(template_id)
    if tmpl.name == n:
        return tmpl  # noop, не дёргаем updated_at зря
    tmpl.name = n
    try:
        await session.flush()
    except IntegrityError as exc:
        await session.rollback()
        raise TemplateNameAlreadyExists(n) from exc
    return tmpl


async def update_text(
    session: AsyncSession,
    template_id: int,
    new_text: str,
    *,
    attachments: Sequence[dict] | None = None,
) -> BroadcastTemplate:
    """Обновить текст и (опционально) приложения шаблона."""
    t = _validate_text(new_text)
    tmpl = await get_by_id(session, template_id)
    if tmpl is None:
        raise TemplateNotFound(template_id)
    tmpl.text = t
    if attachments is not None:
        tmpl.attachments = list(attachments)
    await session.flush()
    return tmpl


async def archive(session: AsyncSession, template_id: int) -> BroadcastTemplate:
    """Soft-delete: проставить archived_at = now."""
    tmpl = await get_by_id(session, template_id)
    if tmpl is None:
        raise TemplateNotFound(template_id)
    tmpl.archived_at = datetime.now(timezone.utc)
    await session.flush()
    return tmpl
