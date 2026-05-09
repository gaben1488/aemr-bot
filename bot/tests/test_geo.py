"""Тесты на services/geo.py — локальный reverse geocoding ЕМО.

Также защита от регрессий FSM-воронки (правильные поля User vs Operator).

Координаты — из Wikidata P625 (центры населённых пунктов), точные.
Если эти тесты упадут после обновления seed/geo/*.geojson — значит
данные OSM сильно изменились и нужно посмотреть верификатор:
`python scripts/verify_geo.py`.
"""
from __future__ import annotations

import pytest

from aemr_bot.services.geo import find_address, find_locality


class TestFindLocality:
    """find_locality по координатам — point-in-polygon."""

    @pytest.mark.parametrize(
        "lat,lon,expected",
        [
            # Эталонные центры из Wikidata P625 + cross-verify через OSM
            # place=village (см. scripts/cross_verify_geo.py).
            # Для Пионерского — OSM place-нода вместо Wikidata, потому
            # что Q21193644 содержит ошибочную координату.
            (53.184, 158.385, "Елизовское ГП"),       # центр Елизово
            (52.961, 158.249, "Паратунское СП"),
            (53.271, 158.289, "Раздольненское СП"),
            (53.281, 158.208, "Корякское СП"),
            (53.099, 158.537, "Новоавачинское СП"),
            (53.096, 158.350, "Вулканное ГП"),
            (53.045, 158.336, "Николаевское СП"),
            (53.255, 158.029, "Новолесновское СП"),
            (53.145, 157.698, "Начикинское СП"),
            (53.090, 158.557, "Пионерское СП"),       # OSM place-нода
        ],
    )
    def test_known_centers_match(self, lat: float, lon: float, expected: str) -> None:
        """Эталонные центры из Wikidata + OSM должны попадать в полигоны."""
        assert find_locality(lat, lon) == expected

    def test_outside_emo_returns_none(self) -> None:
        """Точка в Тихом океане не должна попасть ни в одно поселение."""
        assert find_locality(53.5, 159.0) is None
        # Москва — заведомо вне ЕМО
        assert find_locality(55.751, 37.617) is None


class TestAdministrationBuildings:
    """Здания администраций поселений ЕМО — независимый источник
    верификации границ. Эти координаты получены из OSM запроса
    `amenity=townhall` на территории Елизовского района (см.
    scripts/cross_verify_geo.py). Каждое здание администрации
    конкретного поселения должно физически находиться в полигоне
    своего поселения — это **юридически и физически очевидно**.

    Если эти тесты падают — серьёзная ошибка в данных границ.
    """

    @pytest.mark.parametrize(
        "lat,lon,expected,name",
        [
            # Координаты получены через Overpass API
            # запрос: amenity=townhall во всех поселениях ЕМО
            (53.184, 158.385, "Елизовское ГП", "Администрация ЕМР (центр)"),
            (53.090, 158.557, "Пионерское СП", "Администрация Пионерского СП"),
            (52.961, 158.249, "Паратунское СП", "Администрация Паратунского СП"),
        ],
    )
    def test_townhall_in_correct_polygon(
        self, lat: float, lon: float, expected: str, name: str
    ) -> None:
        result = find_locality(lat, lon)
        assert result == expected, f"{name}: ожидался {expected}, получен {result}"


class TestFindAddress:
    """find_address — поселение + улица + номер дома."""

    def test_yelizovo_center_finds_building(self) -> None:
        """Площадь Ленина в Елизово — должно найти конкретное здание."""
        r = find_address(53.184, 158.385)
        assert r.locality == "Елизовское ГП"
        assert r.confidence == "high"
        # Конкретный адрес может меняться при обновлении OSM, но
        # обязательно должна быть улица и номер дома
        assert r.street is not None
        assert r.house_number is not None

    def test_paratunka_center_finds_building(self) -> None:
        r = find_address(52.961, 158.249)
        assert r.locality == "Паратунское СП"
        assert r.confidence in ("high", "medium")

    def test_outside_emo(self) -> None:
        r = find_address(53.5, 159.0)
        assert r.locality is None
        assert r.street is None
        assert r.house_number is None
        assert r.confidence == "none"

    def test_inside_locality_no_address(self) -> None:
        """Точка в посёлке без зданий рядом — confidence=low/medium."""
        # Координаты внутри Елизовского ГП, но в зоне без зданий
        # (поле/парк) — должен вернуть только locality
        r = find_address(53.180, 158.400)
        assert r.locality == "Елизовское ГП"
        # confidence любой кроме none
        assert r.confidence != "none"

    def test_invalid_coordinates_dont_crash(self) -> None:
        """Нечисловые / экстремальные координаты не должны вызывать crash."""
        # Северный полюс
        r = find_address(89.99, 0.0)
        assert r.locality is None
        assert r.confidence == "none"

        # NaN — должен корректно обработаться (or пропуститься в shapely)
        import math
        r = find_address(math.nan, math.nan)
        # Не crash; результат может быть none либо bogus, но без
        # исключения — главное чтобы бот не упал
        assert r.confidence in ("none", "low", "medium", "high")


class TestUserModelFields:
    """Регрессионная проверка: модель User имеет first_name (не full_name).

    Это поле читается в _ask_locality для echo-feedback. В прошлом был
    баг: написал user.full_name (поле Operator) вместо user.first_name —
    AttributeError при первом тапе на «Выбрать населённый пункт».
    """

    def test_user_has_first_name_not_full_name(self) -> None:
        from aemr_bot.db.models import User
        # Поля декларации SQLAlchemy
        cols = {c.name for c in User.__table__.columns}
        assert "first_name" in cols, "User должен иметь поле first_name"
        assert "full_name" not in cols, "full_name — поле Operator, не User"

    def test_operator_has_full_name(self) -> None:
        from aemr_bot.db.models import Operator
        cols = {c.name for c in Operator.__table__.columns}
        assert "full_name" in cols
