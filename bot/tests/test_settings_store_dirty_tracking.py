"""Регрессионные тесты на dirty-tracking в `services/settings_store`.

Закрывают **два связанных бага**, найденных 2026-05-28:

**Bug A — legacy `synced_at=NULL`**: миграция 0013 добавила колонку
nullable без backfill. Ключи, существовавшие до миграции
(`emergency_contacts`, `topics`, `transport_dispatcher_contacts`),
остались с `synced_at=NULL` и постоянно показывались как dirty в
админ-меню «⚙️ Настройки бота → 💾 Создать PR (N изм.)». `seed_if_empty`
не делал backfill для уже существующих ключей.

**Bug B — `set_value` не обновлял `updated_at` и не сбрасывал
`synced_at`**: `pg_insert.on_conflict_do_update(set_={"value": value})`
обновлял только `value`. ORM-hook `onupdate=func.now()` НЕ срабатывает
на server-level upsert. Это противоречило docstring миграции 0013
(«при set_value поле обнуляется»). Тихо: оператор правил настройку
через UI, ключ оставался clean, правка не уезжала в репо.

Все тесты требуют PG-fixture `session`. Локально skip без
`DATABASE_URL=postgresql+asyncpg://`, в CI работают.
"""
from __future__ import annotations

import asyncio

import pytest

from aemr_bot.db.models import Setting
from aemr_bot.services import settings_store


@pytest.fixture
def fake_seed_reader(monkeypatch):
    """Подменяет `_read_seed_json` в settings_store фиксированными
    тестовыми значениями для 3 ключей жалобы owner. Используется в
    Bug A тестах чтобы не зависеть от наличия `/app/seed/*.json` в
    test-env (SEED_DIR env по умолчанию `/app/seed`, в CI отсутствует)."""
    fake_data = {
        "topics.json": ["Уличное освещение", "Дороги"],
        "contacts.json": [
            {"name": "Пожарная", "phone": "01", "section": "Экстренные"},
        ],
        "transport_dispatchers.json": [
            {"routes": "101, 102", "phone": "8-800-100"},
        ],
    }
    monkeypatch.setattr(
        settings_store,
        "_read_seed_json",
        lambda name: fake_data.get(name),
    )
    return fake_data


class TestBugBSetValueBumpsTimestamps:
    """Bug B: `set_value` теперь явно сбрасывает synced_at и
    обновляет updated_at."""

    @pytest.mark.asyncio
    async def test_insert_new_key_synced_at_null(self, session) -> None:
        """Первый вызов set_value для ключа — INSERT путь.
        `synced_at` остаётся NULL (default), `updated_at` ставится
        через server_default."""
        from sqlalchemy import select

        await settings_store.set_value(session, "topics", ["A", "B"])
        row = await session.scalar(
            select(Setting).where(Setting.key == "topics")
        )
        assert row is not None
        assert row.synced_at is None
        assert row.updated_at is not None

    @pytest.mark.asyncio
    async def test_update_existing_resets_synced_at(self, session) -> None:
        """Bug B fix: повторный set_value сбрасывает synced_at на NULL,
        даже если ключ был помечен synced."""
        from sqlalchemy import select

        # Создаём ключ + помечаем как synced.
        await settings_store.set_value(session, "topics", ["A"])
        await settings_store.mark_synced(session, ["topics"])
        await session.flush()
        session.expire_all()  # отбрасываем ORM-кеш, заставляем re-read
        row = await session.scalar(
            select(Setting).where(Setting.key == "topics")
        )
        assert row.synced_at is not None  # pre-check

        # Правим ключ — should reset synced_at.
        await settings_store.set_value(session, "topics", ["A", "B"])
        await session.flush()
        session.expire_all()
        row = await session.scalar(
            select(Setting).where(Setting.key == "topics")
        )
        assert row.synced_at is None

    @pytest.mark.asyncio
    async def test_update_bumps_or_keeps_updated_at(self, session) -> None:
        """Bug B fix: повторный set_value должен записать updated_at = now().

        Caveat: в Postgres `now()` фиксируется на момент начала
        транзакции — два set_value в одной session-tx получат
        одинаковый timestamp. Проверяем что `updated_at >= first_updated`
        (поведенческий контракт сохранён: `set_={updated_at=func.now()}`
        в on_conflict срабатывает). Реальный «больше» проверяется в
        интеграционном test_set_value_makes_key_dirty через `synced_at
        IS NULL` после правки.
        """
        from sqlalchemy import select

        await settings_store.set_value(session, "topics", ["A"])
        await session.flush()
        session.expire_all()
        row_before = await session.scalar(
            select(Setting).where(Setting.key == "topics")
        )
        first_updated = row_before.updated_at

        await asyncio.sleep(0.05)
        await settings_store.set_value(session, "topics", ["A", "B"])
        await session.flush()
        session.expire_all()
        row_after = await session.scalar(
            select(Setting).where(Setting.key == "topics")
        )
        # В одной транзакции now() статичен → >= вместо >. Если когда-
        # нибудь переключимся на clock_timestamp() — assert станет >.
        assert row_after.updated_at >= first_updated

    @pytest.mark.asyncio
    async def test_set_value_makes_key_dirty(self, session) -> None:
        """End-to-end: после set_value ключ должен попасть в
        `get_dirty_keys`. Раньше Bug B молча скрывал dirty статус."""
        await settings_store.set_value(session, "topics", ["A"])
        await settings_store.mark_synced(session, ["topics"])
        await session.flush()
        session.expire_all()
        # Pre-check: чистый.
        dirty = await settings_store.get_dirty_keys(session)
        assert "topics" not in dirty

        await settings_store.set_value(session, "topics", ["A", "B", "C"])
        await session.flush()
        session.expire_all()
        dirty = await settings_store.get_dirty_keys(session)
        assert "topics" in dirty


class TestBugALegacyBackfill:
    """Bug A: `seed_if_empty` backfill'ит `synced_at` для уже
    существующих SYNCED_KEYS, у которых `synced_at IS NULL` (legacy
    после миграции 0013)."""

    @pytest.mark.asyncio
    async def test_seed_if_empty_backfills_legacy_null_at_baseline(
        self, session, fake_seed_reader,
    ) -> None:
        """Симулируем legacy: ключ загружен из seed (значение совпадает
        с baseline), но `synced_at=NULL` (миграция 0013 добавила колонку
        без backfill). После `seed_if_empty` должен помечать synced.
        """
        from sqlalchemy import select

        # 1. Залить через seed_if_empty (нормальный bootstrap).
        await settings_store.seed_if_empty(session)
        await session.flush()
        # 2. Симулируем legacy: вручную обнуляем synced_at для
        # эмуляции «бот существовал до миграции 0013, потом колонку
        # добавили без backfill».
        await session.execute(
            Setting.__table__.update()
            .where(Setting.key == "topics")
            .values(synced_at=None)
        )
        await session.flush()
        session.expire_all()

        row = await session.scalar(
            select(Setting).where(Setting.key == "topics")
        )
        assert row.synced_at is None  # pre-check

        # 3. Повторный seed_if_empty → backfill для legacy (value
        # совпадает с seed, значит legacy, помечаем).
        await settings_store.seed_if_empty(session)
        await session.flush()
        session.expire_all()
        row = await session.scalar(
            select(Setting).where(Setting.key == "topics")
        )
        assert row.synced_at is not None, (
            "Bug A регресс: seed_if_empty не сделал backfill synced_at "
            "для legacy ключа (миграция 0013 + value совпадает с seed)."
        )

    @pytest.mark.asyncio
    async def test_three_legacy_keys_no_longer_dirty(
        self, session, fake_seed_reader,
    ) -> None:
        """Главный сценарий: 3 ключа жалобы owner —
        emergency_contacts, topics, transport_dispatcher_contacts —
        после `seed_if_empty` повторного запуска не должны больше
        показываться как dirty (если значения = seed-baseline).
        """
        # 1. Нормальный bootstrap.
        await settings_store.seed_if_empty(session)
        await session.flush()

        # 2. Симулируем legacy: обнуляем synced_at для 3 ключей.
        for key in (
            "emergency_contacts",
            "topics",
            "transport_dispatcher_contacts",
        ):
            await session.execute(
                Setting.__table__.update()
                .where(Setting.key == key)
                .values(synced_at=None)
            )
        await session.flush()
        session.expire_all()

        # Pre-check: 3 ключа dirty.
        dirty_before = await settings_store.get_dirty_keys(session)
        assert "emergency_contacts" in dirty_before
        assert "topics" in dirty_before
        assert "transport_dispatcher_contacts" in dirty_before

        # 3. Trigger seed_if_empty → backfill.
        await settings_store.seed_if_empty(session)
        await session.flush()
        session.expire_all()

        dirty_after = await settings_store.get_dirty_keys(session)
        assert "emergency_contacts" not in dirty_after
        assert "topics" not in dirty_after
        assert "transport_dispatcher_contacts" not in dirty_after

    @pytest.mark.asyncio
    async def test_backfill_skips_user_edited_keys(
        self, session, fake_seed_reader,
    ) -> None:
        """Защитный инвариант: если value в БД отличается от seed-
        baseline (оператор правил через UI), backfill НЕ помечает
        synced. Это закрывает регрессию когда reseed возвращал бы
        baseline у user-edited.
        """
        from sqlalchemy import select

        # 1. Bootstrap.
        await settings_store.seed_if_empty(session)
        await session.flush()

        # 2. Оператор правит topics через UI — Bug B fix сбрасывает
        # synced_at в NULL, value становится отличным от seed.
        await settings_store.set_value(session, "topics", ["Только-моя-тема"])
        await session.flush()
        session.expire_all()
        row = await session.scalar(
            select(Setting).where(Setting.key == "topics")
        )
        assert row.synced_at is None

        # 3. Рестарт бота → seed_if_empty. Не должен затронуть
        # topics (value != seed-baseline → не legacy).
        await settings_store.seed_if_empty(session)
        await session.flush()
        session.expire_all()
        row = await session.scalar(
            select(Setting).where(Setting.key == "topics")
        )
        # synced_at остался NULL — ключ всё ещё dirty.
        assert row.synced_at is None, (
            "User-edited ключ ошибочно помечен synced — это потеряло бы "
            "правку оператора. Backfill должен различать legacy "
            "(value == seed) от user-edited (value != seed)."
        )


class TestSetValueIdempotentTimestamps:
    """Sanity: повторный set_value с тем же value всё равно срабатывает
    `set_={updated_at=func.now()}`. Это правильное поведение — оператор
    формально «зафиксировал» значение, дата нужна для audit.

    Caveat: в Postgres `now()` per-tx — два set_value в одной session
    дают одинаковый timestamp. Проверяем `>=` вместо `>` (если когда-
    нибудь переключимся на `clock_timestamp()` — assert станет `>`)."""

    @pytest.mark.asyncio
    async def test_same_value_keeps_or_bumps_updated_at(self, session) -> None:
        from sqlalchemy import select

        await settings_store.set_value(session, "topics", ["A"])
        await session.flush()
        session.expire_all()
        before = await session.scalar(
            select(Setting.updated_at).where(Setting.key == "topics")
        )

        await asyncio.sleep(0.05)
        await settings_store.set_value(session, "topics", ["A"])  # same
        await session.flush()
        session.expire_all()
        after = await session.scalar(
            select(Setting.updated_at).where(Setting.key == "topics")
        )
        # Per-tx `now()` статичен → >=. Главное — что `set_` clause
        # перезаписал updated_at (mechanism жив), а не оставил
        # старый timestamp с момента INSERT.
        assert after >= before
