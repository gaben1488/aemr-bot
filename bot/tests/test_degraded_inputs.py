"""Бот не должен падать (и не должен «тихо разрешать») на битых входах.

Спасено из удалённых cov2-файлов: сами файлы писались по отчёту
покрытия, но четыре проверки в них про последствия, а не про строки —
праздничный календарь, кэш тихих часов, список вредоносных доменов и
вложения на релей. Все четыре источника внешние (файл в образе, строки
в БД, чужие feed'ы, payload из MAX), то есть могут прийти битыми в
проде.

Формат докстрингов: «если <поломка>, то <что случится с жителем/
оператором/данными>».
"""
from __future__ import annotations

import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aemr_bot.services import calendar_ru
from aemr_bot.services import threat_intel as ti
from aemr_bot.utils import attachments as A


@pytest.fixture
def patch_holidays(monkeypatch, tmp_path):
    """Направить HOLIDAYS_PATH на временный файл и сбросить lru_cache.

    None → путь на несуществующий файл (имитация потерянного seed'а).
    """

    def _apply(content: str | None) -> None:
        if content is None:
            target = tmp_path / "does_not_exist.json"
        else:
            target = tmp_path / "holidays.json"
            target.write_text(content, encoding="utf-8")
        monkeypatch.setattr(calendar_ru, "HOLIDAYS_PATH", Path(target))
        calendar_ru._load_holidays.cache_clear()

    yield _apply
    calendar_ru._load_holidays.cache_clear()


class TestHolidayCalendarSurvivesBadFile:
    """seed/holidays.json — обычный файл в образе: его можно потерять при
    сборке или испортить правкой руками."""

    def test_missing_holidays_file_does_not_break_sla(self, patch_holidays) -> None:
        """Если потерянный файл праздников не глушить, упадёт расчёт SLA
        (рабочие часы) — то есть перестанут ставиться сроки по всем
        обращениям жителей, а не только по праздничным дням."""
        patch_holidays(None)
        assert calendar_ru._load_holidays() == frozenset()
        # Дальше SLA считает 1 января как обычный день — это хуже, чем
        # правильный календарь, но лучше, чем отказ приёма обращений.
        assert calendar_ru.is_holiday(date(2026, 1, 1)) is False

    def test_corrupt_holidays_json_does_not_break_sla(self, patch_holidays) -> None:
        """То же для битого JSON: одна лишняя скобка в файле не должна
        останавливать приём обращений."""
        patch_holidays("{ this is not json ]")
        assert calendar_ru._load_holidays() == frozenset()

    def test_bad_dates_skipped_but_valid_ones_kept(self, patch_holidays) -> None:
        """Если на первой кривой дате парсер сдастся целиком, из-за одной
        опечатки в файле бот посчитает рабочими ВСЕ праздники и разошлёт
        просроченные SLA-напоминания операторам в выходные."""
        patch_holidays('["2026-01-01", "31-12-2026", "garbage", ""]')
        assert calendar_ru._load_holidays() == frozenset({date(2026, 1, 1)})


class TestQuietHoursFallback:
    @pytest.fixture(autouse=True)
    def _restore_cache(self):
        from aemr_bot.services.quiet_hours import _cache

        snapshot = dict(_cache)
        yield
        _cache.clear()
        _cache.update(snapshot)

    @pytest.mark.asyncio
    async def test_corrupt_quiet_hours_settings_fall_back_to_safe_window(
        self,
    ) -> None:
        """Если не нормализовать значения из настроек, час «not-an-int»
        (свежая БД или правка руками) уронит обновление кэша, и бот
        останется с прежним окном молчания — вплоть до ночных уведомлений
        жителям."""
        from aemr_bot.services.quiet_hours import _cache, refresh_cache_from_db

        with patch(
            "aemr_bot.services.quiet_hours.settings_store.get",
            AsyncMock(side_effect=lambda s, key: {
                "admin_quiet_hours_enabled": True,
                "admin_quiet_hours_start": "not-an-int",
                "admin_quiet_hours_end": None,
            }[key]),
        ):
            await refresh_cache_from_db(SimpleNamespace())

        assert _cache["enabled"] is True
        # Безопасные дефолты: тишина с 18:00 до 09:00.
        assert _cache["start"] == 18
        assert _cache["end"] == 9


class TestThreatFeedsFailOpen:
    @pytest.fixture(autouse=True)
    def _isolated_store(self, monkeypatch):
        monkeypatch.setattr(ti, "_STORE", None)
        monkeypatch.delenv("PHISHTANK_APP_KEY", raising=False)

    @pytest.mark.asyncio
    async def test_all_feeds_down_keeps_previous_blocklist(self, monkeypatch) -> None:
        """Если при недоступных feed'ах затирать список вредоносных
        доменов пустым, любой обрыв связи с urlhaus/threatfox снимет
        защиту: ссылки, которые вчера блокировались, сегодня уйдут
        жителю."""
        store = ti.get_store()
        store.hosts = {"old.evil"}
        store.last_refresh_at = time.monotonic() - 100

        async def fake_fetch(session, url):  # noqa: ARG001
            return None  # все feed'ы лежат

        monkeypatch.setattr(ti, "_fetch_text", fake_fetch)
        counts = await ti.refresh_all()

        assert counts == {}
        assert ti.get_store().hosts == {"old.evil"}


class TestAttachmentRelayIsolatesBadPayload:
    def test_malformed_attachment_does_not_block_relay_of_valid_ones(self) -> None:
        """Если одно вложение неожиданной формы уронит десериализацию,
        оператор не увидит НИ ОДНОЙ фотографии из обращения — при том
        что фото часто и есть всё содержание жалобы."""
        bad = {"type": "image", "payload": "should-be-object-not-string"}
        good = {"type": "image", "payload": {"token": "ok", "url": "http://x/z.jpg"}}

        out = A.deserialize_for_relay([bad, good])

        assert len(out) == 1
        assert type(out[0]).__name__ == "Image"

    def test_contact_attachment_is_not_relayed(self) -> None:
        """Если снять фильтр типов, визитка жителя (телефон, ФИО) уйдёт
        в служебную группу целиком — вместо маскированных полей карточки
        обращения."""
        out = A.deserialize_for_relay([{"type": "contact", "payload": {}}])
        assert out == []
