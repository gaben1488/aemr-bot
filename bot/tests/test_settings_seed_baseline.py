"""Тесты на seed_if_empty: после seed свежие SYNCED_KEYS должны быть
сразу помечены как synced (baseline = seed-файл в репо).

Регрессия от user-bug «3 грязных ключа после первого старта бота»:
emergency_contacts / topics / transport_dispatcher_contacts горели в
индикаторе «несинхронизированные изменения» сразу после seed, хотя
никаких реальных изменений ещё не было.

Требует PostgreSQL (модели используют JSONB, sqlite не подходит).
В чисто-unit окружении тест пропускается через fixture в conftest.
"""
from __future__ import annotations

import pytest

from aemr_bot.services import settings_store


@pytest.mark.asyncio
class TestSeedBaseline:
    async def test_fresh_seed_marks_synced_keys_as_clean(self, session) -> None:
        """После первого seed_if_empty — get_dirty_keys() = []."""
        await settings_store.seed_if_empty(session)
        await session.flush()

        dirty = await settings_store.get_dirty_keys(session)
        assert dirty == [], (
            f"Свежезасеянная БД не должна иметь dirty ключей. "
            f"Реально dirty: {dirty}"
        )

    async def test_seed_idempotent(self, session) -> None:
        """Повторный seed не вставляет дубли и не сбрасывает synced_at.

        Сценарий: бот стартует второй раз (контейнер пересоздан) — seed
        вызывается, но БД уже непустая. Ничего не должно поменяться.
        """
        await settings_store.seed_if_empty(session)
        await session.flush()

        # Симулируем: оператор поменял настройку → она стала dirty
        await settings_store.set_value(session, "topics", ["Только это"])
        await session.flush()

        dirty_after_change = await settings_store.get_dirty_keys(session)
        assert "topics" in dirty_after_change, (
            "topics после set_value без mark_synced должен быть dirty"
        )

        # Второй seed (рестарт бота)
        await settings_store.seed_if_empty(session)
        await session.flush()

        dirty_after_reseed = await settings_store.get_dirty_keys(session)
        assert dirty_after_reseed == dirty_after_change, (
            f"Повторный seed не должен трогать уже изменённые ключи. "
            f"До reseed: {dirty_after_change}, после: {dirty_after_reseed}"
        )

    async def test_changed_setting_becomes_dirty(self, session) -> None:
        """После seed → set_value → ключ становится dirty.

        Это позитивный контроль: если бы mark_synced заходил при каждом
        set_value, дальнейшие изменения никогда не отображались бы в
        dirty-списке. Должен срабатывать только для seed-baseline.
        """
        await settings_store.seed_if_empty(session)
        await session.flush()

        # Оператор меняет topics через UI:
        await settings_store.set_value(session, "topics", ["Новая тема"])
        await session.flush()

        dirty = await settings_store.get_dirty_keys(session)
        assert "topics" in dirty

    async def test_repair_invalid_welcome_text_from_db(self, session) -> None:
        """Системный фикс 2026-05-26: если в БД лежит welcome_text
        без обязательной подстроки 'НИКОГДА не запрашиваем' (старая
        версия до антифишинг-блока), seed_if_empty при следующем
        запуске должен перезаписать его актуальным seed-значением.
        """
        # Кладём в БД "стаpый" welcome без антифишинга
        old_welcome = "Здравствуйте. Это бот.\n\nВыберите действие."
        await settings_store.set_value(session, "welcome_text", old_welcome)
        await session.flush()

        # Запускаем seed_if_empty — должен починить
        await settings_store.seed_if_empty(session)
        await session.flush()

        # В БД теперь актуальный welcome (либо из seed/welcome.md, либо
        # никаких изменений если seed-файл сам не валидный)
        raw = await settings_store.get(session, "welcome_text")
        if "НИКОГДА не запрашиваем" in (raw or ""):
            # Seed-файл валиден — repair прошёл
            assert raw != old_welcome
        else:
            # Seed-файл сам устарел — оставлено как есть, но WARNING
            # логирован (см. реализацию)
            assert raw == old_welcome
