"""Покрытие реального парсера services/calendar_ru._load_holidays.

Базовый test_calendar_ru_full.py монкипатчит _load_holidays целиком,
поэтому фактическая загрузка/парсинг seed/holidays.json не тестируется:
- файл отсутствует → frozenset() + warning (строки 35-41);
- битый JSON → frozenset() (44-46);
- dict с переносами + служебными _comment/не-list полями (48-53);
- list-форма верхнего уровня (54-55);
- невалидная дата в списке пропускается (60-61).

_load_holidays кэширована lru_cache(maxsize=1), а путь HOLIDAYS_PATH —
module-level. Поэтому в каждом тесте монкипатчим HOLIDAYS_PATH на
временный файл и сбрасываем кэш через cache_clear().
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from aemr_bot.services import calendar_ru


@pytest.fixture
def patch_holidays(monkeypatch, tmp_path):
    """Вернуть функцию, которая пишет содержимое в tmp-файл, направляет
    туда HOLIDAYS_PATH и сбрасывает lru_cache. None → путь несуществующий."""

    def _apply(content: str | None) -> None:
        if content is None:
            target = tmp_path / "does_not_exist.json"
        else:
            target = tmp_path / "holidays.json"
            target.write_text(content, encoding="utf-8")
        monkeypatch.setattr(calendar_ru, "HOLIDAYS_PATH", Path(target))
        calendar_ru._load_holidays.cache_clear()

    yield _apply
    # Сбросить кэш и для следующих тестов (фикстура восстановит путь).
    calendar_ru._load_holidays.cache_clear()


class TestLoadHolidays:
    def test_missing_file_returns_empty(self, patch_holidays) -> None:
        patch_holidays(None)
        assert calendar_ru._load_holidays() == frozenset()

    def test_invalid_json_returns_empty(self, patch_holidays) -> None:
        patch_holidays("{ this is not json ]")
        assert calendar_ru._load_holidays() == frozenset()

    def test_dict_form_with_comment_and_nonlist_fields(self, patch_holidays) -> None:
        # _comment и числовое поле пропускаются; собираются только list-значения.
        patch_holidays(
            '{"_comment": "2026", "_source": 5, "january": '
            '["2026-01-01", "2026-01-02"], "may": ["2026-05-09"]}'
        )
        result = calendar_ru._load_holidays()
        assert result == frozenset(
            {date(2026, 1, 1), date(2026, 1, 2), date(2026, 5, 9)}
        )

    def test_list_form_top_level(self, patch_holidays) -> None:
        patch_holidays('["2026-06-12", "2026-11-04"]')
        assert calendar_ru._load_holidays() == frozenset(
            {date(2026, 6, 12), date(2026, 11, 4)}
        )

    def test_empty_list_form(self, patch_holidays) -> None:
        # Пустой список верхнего уровня — цикл парсинга не выполняется ни разу.
        patch_holidays("[]")
        assert calendar_ru._load_holidays() == frozenset()

    def test_unexpected_top_level_type_yields_empty(self, patch_holidays) -> None:
        # Верхний уровень — не dict и не list (число): items остаётся [].
        patch_holidays("42")
        assert calendar_ru._load_holidays() == frozenset()

    def test_bad_date_entries_skipped(self, patch_holidays) -> None:
        # "31-12-2026" (неверный формат) и "garbage" пропускаются,
        # валидная дата остаётся.
        patch_holidays('["2026-01-01", "31-12-2026", "garbage", ""]')
        assert calendar_ru._load_holidays() == frozenset({date(2026, 1, 1)})

    def test_is_holiday_uses_loaded_file(self, patch_holidays) -> None:
        # Сквозная проверка: is_holiday читает результат реального парсера.
        patch_holidays('{"days": ["2026-05-09"]}')
        assert calendar_ru.is_holiday(date(2026, 5, 9)) is True
        assert calendar_ru.is_holiday(date(2026, 5, 8)) is False
