"""Миграция 0025: текст приёма граждан должен доехать до жителя.

Дефолт исправлен в коде, но на работающем боте значение живёт строкой в
`settings` — дефолт применяется только при её отсутствии. Без миграции
житель продолжал бы читать «района» после преобразования округа. При
этом миграция обязана пройти мимо текста, который Администрация уже
правила под себя.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "aemr_bot" / "db" / "alembic" / "versions" / "0025_appointment_text_okrug.py"
)


def _load():
    spec = importlib.util.spec_from_file_location("m0025", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_old_string_matches_shipped_default_verbatim() -> None:
    """Строка «до» совпадает с прежним дефолтом слово в слово.

    Миграция обновляет запись сравнением на равенство. Разойдись здесь
    хоть один пробел — UPDATE не заденет ни строки, причём МОЛЧА: ни
    ошибки, ни предупреждения, а житель так и останется с «районом».
    """
    m = _load()
    assert m._OLD == (
        "Приём граждан временно исполняющим полномочия Главы Елизовского "
        "муниципального района А.С. Гончаровым осуществляется два раза в месяц "
        "(1 и 3 среда каждого месяца) по предварительной записи. "
        "Запись на приём ведётся по номеру телефона 8 (415-31) 7-25-29."
    )


def test_new_string_equals_current_default() -> None:
    """Строка «после» совпадает с действующим дефолтом в коде.

    Иначе на новой установке и на обновлённой окажутся разные тексты —
    расхождение, которое всплывёт только жалобой жителя.
    """
    from aemr_bot.services.settings_store import DEFAULTS

    assert _load()._NEW == DEFAULTS["appointment_text"]


def test_only_the_word_district_changes() -> None:
    """Меняется ровно одно слово: фамилия, график и телефон не тронуты."""
    m = _load()
    assert m._NEW == m._OLD.replace("муниципального района", "муниципального округа")
    assert "Гончаровым" in m._NEW
    assert "8 (415-31) 7-25-29" in m._NEW
    assert "района" not in m._NEW
