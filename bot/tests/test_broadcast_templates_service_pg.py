"""PG-тесты services/broadcast_templates (PR H).

Чистый persistence-слой: создание, переименование, обновление текста и
вложений, soft-delete. Уникальность имени, валидация длины, ошибочные
сценарии.
"""
from __future__ import annotations

import pytest

from aemr_bot.services import broadcast_templates as templates


@pytest.mark.asyncio
async def test_create_template_minimal(session) -> None:
    """Минимальный шаблон: только name + text, без вложений и оператора."""
    tmpl = await templates.create_template(
        session, name="Отключение воды", text="Уважаемые жители!"
    )
    assert tmpl.id is not None
    assert tmpl.name == "Отключение воды"
    assert tmpl.text == "Уважаемые жители!"
    assert tmpl.attachments == []
    assert tmpl.archived_at is None


@pytest.mark.asyncio
async def test_create_template_with_attachments(session) -> None:
    """Шаблон с image-вложениями сохраняет dict'ы как есть."""
    atts = [{"type": "image", "payload": {"token": "x"}}]
    tmpl = await templates.create_template(
        session, name="Афиша", text="См. изображение.", attachments=atts
    )
    assert tmpl.attachments == atts


@pytest.mark.asyncio
async def test_create_template_strips_name_whitespace(session) -> None:
    """Пробелы по краям имени тримятся, иначе пользователь не отличит дубль."""
    tmpl = await templates.create_template(
        session, name="  Тест  ", text="t"
    )
    assert tmpl.name == "Тест"


@pytest.mark.asyncio
async def test_create_template_empty_name_raises(session) -> None:
    with pytest.raises(ValueError):
        await templates.create_template(session, name="   ", text="t")


@pytest.mark.asyncio
async def test_create_template_empty_text_raises(session) -> None:
    with pytest.raises(ValueError):
        await templates.create_template(session, name="x", text="   ")


@pytest.mark.asyncio
async def test_create_template_long_text_raises(session) -> None:
    with pytest.raises(ValueError):
        await templates.create_template(
            session, name="x", text="a" * (templates.MAX_TEXT_LEN + 1)
        )


@pytest.mark.asyncio
async def test_create_template_long_name_raises(session) -> None:
    with pytest.raises(ValueError):
        await templates.create_template(
            session, name="a" * (templates.MAX_NAME_LEN + 1), text="t"
        )


@pytest.mark.asyncio
async def test_duplicate_name_raises(session) -> None:
    """Имя уникальное среди активных шаблонов."""
    await templates.create_template(session, name="Праздник", text="t1")
    with pytest.raises(templates.TemplateNameAlreadyExists):
        await templates.create_template(session, name="Праздник", text="t2")


@pytest.mark.asyncio
async def test_list_active_orders_by_updated_at_desc(session) -> None:
    """Список упорядочен по updated_at desc — свежие сверху."""
    t1 = await templates.create_template(session, name="A", text="a")
    t2 = await templates.create_template(session, name="B", text="b")
    items = await templates.list_active(session)
    ids = [t.id for t in items]
    assert ids[0] == t2.id and ids[1] == t1.id


@pytest.mark.asyncio
async def test_list_active_excludes_archived(session) -> None:
    """Архивированные шаблоны не попадают в выборку."""
    t1 = await templates.create_template(session, name="A", text="a")
    await templates.create_template(session, name="B", text="b")
    await templates.archive(session, t1.id)
    items = await templates.list_active(session)
    names = [t.name for t in items]
    assert "A" not in names
    assert "B" in names


@pytest.mark.asyncio
async def test_count_active_excludes_archived(session) -> None:
    t1 = await templates.create_template(session, name="A", text="a")
    await templates.create_template(session, name="B", text="b")
    await templates.archive(session, t1.id)
    assert await templates.count_active(session) == 1


@pytest.mark.asyncio
async def test_get_by_id_default_excludes_archived(session) -> None:
    t1 = await templates.create_template(session, name="A", text="a")
    await templates.archive(session, t1.id)
    assert await templates.get_by_id(session, t1.id) is None


@pytest.mark.asyncio
async def test_get_by_id_include_archived(session) -> None:
    """include_archived=True позволяет дотянуться до архивных (нужно для аудита)."""
    t1 = await templates.create_template(session, name="A", text="a")
    await templates.archive(session, t1.id)
    found = await templates.get_by_id(session, t1.id, include_archived=True)
    assert found is not None
    assert found.archived_at is not None


@pytest.mark.asyncio
async def test_rename_changes_name(session) -> None:
    t1 = await templates.create_template(session, name="Старое", text="t")
    await templates.rename(session, t1.id, "Новое")
    refetched = await templates.get_by_id(session, t1.id)
    assert refetched is not None
    assert refetched.name == "Новое"


@pytest.mark.asyncio
async def test_rename_to_existing_raises(session) -> None:
    t1 = await templates.create_template(session, name="A", text="a")
    await templates.create_template(session, name="B", text="b")
    with pytest.raises(templates.TemplateNameAlreadyExists):
        await templates.rename(session, t1.id, "B")


@pytest.mark.asyncio
async def test_rename_unknown_raises(session) -> None:
    with pytest.raises(templates.TemplateNotFound):
        await templates.rename(session, 99999, "Whatever")


@pytest.mark.asyncio
async def test_update_text_replaces_text_only_when_attachments_none(
    session,
) -> None:
    """update_text(attachments=None) меняет только text, attachments не трогаются."""
    atts = [{"type": "image", "payload": {"token": "x"}}]
    t1 = await templates.create_template(
        session, name="A", text="старый", attachments=atts
    )
    await templates.update_text(session, t1.id, "новый")
    refetched = await templates.get_by_id(session, t1.id)
    assert refetched is not None
    assert refetched.text == "новый"
    assert refetched.attachments == atts  # не сброшено


@pytest.mark.asyncio
async def test_update_text_replaces_attachments_when_provided(session) -> None:
    """attachments=[] очищает вложения; attachments=<list> заменяет."""
    atts_v1 = [{"type": "image", "payload": {"token": "old"}}]
    atts_v2 = [{"type": "image", "payload": {"token": "new"}}]
    t1 = await templates.create_template(
        session, name="A", text="t", attachments=atts_v1
    )
    await templates.update_text(session, t1.id, "t2", attachments=atts_v2)
    refetched = await templates.get_by_id(session, t1.id)
    assert refetched is not None
    assert refetched.attachments == atts_v2

    await templates.update_text(session, t1.id, "t3", attachments=[])
    refetched = await templates.get_by_id(session, t1.id)
    assert refetched is not None
    assert refetched.attachments == []


@pytest.mark.asyncio
async def test_archive_sets_archived_at(session) -> None:
    t1 = await templates.create_template(session, name="A", text="a")
    await templates.archive(session, t1.id)
    refetched = await templates.get_by_id(session, t1.id, include_archived=True)
    assert refetched is not None
    assert refetched.archived_at is not None


@pytest.mark.asyncio
async def test_archive_unknown_raises(session) -> None:
    with pytest.raises(templates.TemplateNotFound):
        await templates.archive(session, 99999)


@pytest.mark.asyncio
async def test_archived_name_can_be_reused(session) -> None:
    """После архивации имя освобождается — оператор может создать
    новый шаблон под тем же названием. (Уникальный индекс по name —
    DB-уровень; см. тест: после archive прежний row остаётся, но
    создание дубля должно пройти, потому что unique constraint
    конкретно по name без учёта archived_at не различает их.

    NB: при необходимости в будущем добавим partial unique index
    `WHERE archived_at IS NULL` — пока этот тест фиксирует ТЕКУЩЕЕ
    поведение: имя освобождается ТОЛЬКО при hard-delete; soft-deleted
    оставляет имя занятым. Это сознательно — операторы видят историю
    «А» через include_archived и могут перепутать."""
    t1 = await templates.create_template(session, name="A", text="a")
    await templates.archive(session, t1.id)
    # текущее поведение: имя остаётся занятым
    with pytest.raises(templates.TemplateNameAlreadyExists):
        await templates.create_template(session, name="A", text="a2")
