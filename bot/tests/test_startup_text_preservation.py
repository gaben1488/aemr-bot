"""Restart must not replace a valid operator-authored welcome by a substring."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from aemr_bot.services import settings_store


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [
    "Добро пожаловать! Мы НИКОГДА не запрашиваем пароль. Приём: кабинет 17.",
    "Добро пожаловать! Приём: кабинет 17.",
])
async def test_restart_preserves_authored_welcome(monkeypatch, existing):
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(
        all=lambda: [("welcome_text", existing)])))
    monkeypatch.setattr(settings_store, "_read_seed_json", lambda _: None)
    monkeypatch.setattr(settings_store, "_read_seed_text", lambda n: "Новый шаблон" if n == "welcome.md" else None)
    write = AsyncMock()
    monkeypatch.setattr(settings_store, "set_value", write)
    assert settings_store.validate("welcome_text", existing)[0]
    await settings_store.seed_if_empty(session)
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_welcome_still_bootstraps(monkeypatch):
    session = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(all=list)))
    monkeypatch.setattr(settings_store, "_read_seed_json", lambda _: None)
    monkeypatch.setattr(settings_store, "_read_seed_text", lambda n: "Новый шаблон" if n == "welcome.md" else None)
    write = AsyncMock()
    monkeypatch.setattr(settings_store, "set_value", write)
    await settings_store.seed_if_empty(session)
    write.assert_awaited_once_with(session, "welcome_text", "Новый шаблон")


@pytest.mark.asyncio
@pytest.mark.parametrize("authored", [True, False])
async def test_postgresql_restart_preserves_value_revision_and_timestamp(session, monkeypatch, authored):
    from sqlalchemy import select

    from aemr_bot.db.models import Setting

    monkeypatch.setattr(settings_store, "_read_seed_json", lambda _: None)
    seed = {"value": "Первый шаблон"}
    monkeypatch.setattr(settings_store, "_read_seed_text",
                        lambda n: seed["value"] if n == "welcome.md" else None)
    expected = "Мы НИКОГДА не запрашиваем пароль. Приём: кабинет 17."
    if authored:
        await settings_store.set_value(session, "welcome_text", expected)
    else:
        await settings_store.seed_if_empty(session)
        expected = seed["value"]
    await session.commit()

    async def persisted():
        session.expire_all()
        row = await session.scalar(select(Setting).where(Setting.key == "welcome_text"))
        return row.value, row.revision, row.updated_at

    original = await persisted()
    assert original[0] == expected
    for value in ("Другой шаблон после обновления", "Третий шаблон"):
        seed["value"] = value
        await settings_store.seed_if_empty(session)
        await session.commit()
        assert await persisted() == original
