"""Тесты на services/geo.py — локальный reverse geocoding ЕМО.

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
            (53.184, 158.385, "Елизовское ГП"),       # центр Елизово
            (52.961, 158.249, "Паратунское СП"),
            (53.271, 158.289, "Раздольненское СП"),
            (53.281, 158.208, "Корякское СП"),
            (53.099, 158.537, "Новоавачинское СП"),
            (53.096, 158.350, "Вулканное ГП"),
            (53.045, 158.336, "Николаевское СП"),
            (53.255, 158.029, "Новолесновское СП"),
            (53.145, 157.698, "Начикинское СП"),
        ],
    )
    def test_known_centers_match(self, lat: float, lon: float, expected: str) -> None:
        """Эталонные центры из Wikidata должны попадать в свои полигоны."""
        assert find_locality(lat, lon) == expected

    def test_outside_emo_returns_none(self) -> None:
        """Точка в Тихом океане не должна попасть ни в одно поселение."""
        assert find_locality(53.5, 159.0) is None
        # Москва — заведомо вне ЕМО
        assert find_locality(55.751, 37.617) is None


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
