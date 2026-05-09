"""Локальный reverse geocoding для Елизовского муниципального округа.

Источники данных (`seed/geo/`):
- `localities.geojson` — 10 полигонов поселений ЕМО из OpenStreetMap,
  верифицированы через Wikidata (см. `scripts/verify_geo.py`)
- `streets.geojson` — 955 линий улиц
- `buildings.geojson` — 3018 точек зданий с addr:housenumber

Архитектура каскада:
1. find_locality(lat, lon) — point-in-polygon, всегда работает локально
2. find_address(lat, lon) — ближайшее здание в радиусе 100м, иначе
   ближайшая улица в радиусе 200м, иначе None

Никаких внешних API не используется. Полная отказоустойчивость.

Lazy-load: GeoJSON файлы читаются один раз при первом обращении.
Spatial index (rtree) строится поверх — O(log n) для запросов вместо
O(n) full-scan по 3018 зданиям.
"""
from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

from shapely.geometry import Point, shape
from shapely.strtree import STRtree

log = logging.getLogger(__name__)


def _resolve_geo_dir() -> Path:
    """Найти seed/geo/. В контейнере — через SEED_DIR=/app/seed env;
    локально — relative от этого файла (4 parent — bot/aemr_bot/services/geo.py
    → корень проекта)."""
    seed_env = os.environ.get("SEED_DIR")
    if seed_env:
        return Path(seed_env) / "geo"
    return Path(__file__).parent.parent.parent.parent / "seed" / "geo"


_GEO_DIR = _resolve_geo_dir()

# OSM-имена → наши короткие (settings.localities)
_OSM_TO_SHORT = {
    "Елизовское городское поселение": "Елизовское ГП",
    "Вулканное городское поселение": "Вулканное ГП",
    "Корякское сельское поселение": "Корякское СП",
    "Начикинское сельское поселение": "Начикинское СП",
    "Николаевское сельское поселение": "Николаевское СП",
    "Новоавачинское сельское поселение": "Новоавачинское СП",
    "Новолесновское сельское поселение": "Новолесновское СП",
    "Паратунское сельское поселение": "Паратунское СП",
    "Пионерское сельское поселение": "Пионерское СП",
    "Раздольненское сельское поселение": "Раздольненское СП",
}


# Конфигурация поиска (метры). Один градус по широте ≈ 111 км
# повсеместно; по долготе на ~53° (Камчатка) ≈ 67 км. Buffer считаем
# по широте — он чуть избыточен по долготе, но это безопасно
# (захватит больше кандидатов, реальная фильтрация — _haversine_m ниже).
_BUILDING_RADIUS_M = 100  # ближе чем 100м — считаем «житель в этом доме»
_STREET_RADIUS_M = 200    # ближе чем 200м — считаем «житель на этой улице»
_DEG_PER_METER_LAT = 1 / 111_000


@dataclass(frozen=True)
class GeoResult:
    """Результат reverse geocoding: что определили из координат."""

    locality: Optional[str]
    """Короткое имя поселения как в settings.localities, либо None."""

    street: Optional[str]
    """Название улицы (без типа «ул.»), либо None."""

    house_number: Optional[str]
    """Номер дома, либо None."""

    confidence: str
    """Уверенность: 'high' (нашли здание), 'medium' (только улица),
    'low' (только поселение), 'none' (точка вне ЕМО)."""


# ---- внутренние индексы (lazy) ------------------------------------------------


@lru_cache(maxsize=1)
def _load_localities() -> list[tuple[str, object]]:
    """[(short_name, polygon)] — 10 поселений.

    Если файл отсутствует или повреждён — возвращаем [] и логируем
    предупреждение. Бот продолжит работать в режиме «geocoding не
    доступен» вместо падения с FileNotFoundError при первом тапе на
    «Поделиться геолокацией».
    """
    path = _GEO_DIR / "localities.geojson"
    if not path.exists():
        log.error("geo: localities.geojson отсутствует в %s", _GEO_DIR)
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error("geo: не могу прочитать localities.geojson: %s", e)
        return []
    out = []
    for f in data.get("features", []):
        full_name = f["properties"].get("name", "")
        short = _OSM_TO_SHORT.get(full_name)
        if not short:
            log.warning("locality unmapped: %s", full_name)
            continue
        out.append((short, shape(f["geometry"])))
    return out


@lru_cache(maxsize=1)
def _load_buildings_index() -> tuple[Optional[STRtree], list[dict]]:
    """STRtree spatial index по точкам зданий.

    При отсутствии файла — возвращает (None, []), find_address уйдёт в
    fallback на улицы либо только locality.
    """
    path = _GEO_DIR / "buildings.geojson"
    if not path.exists():
        log.error("geo: buildings.geojson отсутствует в %s", _GEO_DIR)
        return None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error("geo: не могу прочитать buildings.geojson: %s", e)
        return None, []
    geoms = []
    props = []
    for f in data.get("features", []):
        coords = f["geometry"]["coordinates"]
        geoms.append(Point(coords[0], coords[1]))
        props.append(f["properties"])
    tree = STRtree(geoms) if geoms else None
    return tree, props


@lru_cache(maxsize=1)
def _load_streets_index() -> tuple[Optional[STRtree], list[dict]]:
    """STRtree spatial index по сегментам улиц."""
    path = _GEO_DIR / "streets.geojson"
    if not path.exists():
        log.error("geo: streets.geojson отсутствует в %s", _GEO_DIR)
        return None, []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        log.error("geo: не могу прочитать streets.geojson: %s", e)
        return None, []
    geoms = []
    props = []
    for f in data.get("features", []):
        line = shape(f["geometry"])
        geoms.append(line)
        props.append(f["properties"])
    tree = STRtree(geoms) if geoms else None
    return tree, props


# ---- расстояния ---------------------------------------------------------------


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Расстояние между двумя точками в метрах. Учитывает кривизну Земли."""
    R = 6_371_000  # радиус Земли в метрах
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


# ---- public API ---------------------------------------------------------------


def find_locality(lat: float, lon: float) -> Optional[str]:
    """Определить поселение ЕМО по координатам через point-in-polygon.

    Возвращает короткое имя как в settings.localities либо None если
    точка находится вне Елизовского МО или geo-данные не загружаются.
    Не выбрасывает исключений — fallback всегда None.
    """
    try:
        p = Point(lon, lat)
        for short, poly in _load_localities():
            if poly.contains(p):
                return short
    except Exception:
        log.exception("geo.find_locality crashed")
    return None


def find_address(lat: float, lon: float, search_radius_m: int = _STREET_RADIUS_M) -> GeoResult:
    """Полный reverse geocoding: поселение + улица + номер дома.

    Алгоритм:
    1. Поселение через point-in-polygon (надёжно)
    2. Ближайшее здание с addr:housenumber в радиусе _BUILDING_RADIUS_M
       — confidence=high
    3. Иначе ближайшая улица в радиусе search_radius_m — confidence=medium
    4. Иначе только поселение — confidence=low

    Если точка вне ЕМО либо geo-индексы недоступны — confidence=none,
    все поля кроме locality=None. Никогда не падает с исключением.
    """
    try:
        locality = find_locality(lat, lon)
        if not locality:
            return GeoResult(None, None, None, "none")

        target = Point(lon, lat)

        # Шаг 1: ближайшее здание
        bld_tree, bld_props = _load_buildings_index()
        if bld_tree is not None:
            # Buffer считаем по широте — для 53°N это 1.5× избыточно по
            # долготе. Безопасно: реальное расстояние фильтрует
            # _haversine_m. Захват кандидатов через bbox-индекс O(log n).
            radius_deg = _BUILDING_RADIUS_M * _DEG_PER_METER_LAT
            candidates = bld_tree.query(target.buffer(radius_deg))
            best_dist = float("inf")
            best_idx: Optional[int] = None
            for idx in candidates:
                geom = bld_tree.geometries[idx]
                dist_m = _haversine_m(lat, lon, geom.y, geom.x)
                if dist_m < best_dist and dist_m <= _BUILDING_RADIUS_M:
                    best_dist = dist_m
                    best_idx = int(idx)
            if best_idx is not None:
                p = bld_props[best_idx]
                street = p.get("street") or None
                housenum = p.get("housenumber") or None
                return GeoResult(locality, street, housenum, "high")

        # Шаг 2: ближайшая улица
        str_tree, str_props = _load_streets_index()
        if str_tree is not None:
            radius_deg = search_radius_m * _DEG_PER_METER_LAT
            candidates = str_tree.query(target.buffer(radius_deg))
            best_dist = float("inf")
            best_idx = None
            for idx in candidates:
                geom = str_tree.geometries[idx]
                nearest = geom.interpolate(geom.project(target))
                dist_m = _haversine_m(lat, lon, nearest.y, nearest.x)
                if dist_m < best_dist and dist_m <= search_radius_m:
                    best_dist = dist_m
                    best_idx = int(idx)
            if best_idx is not None:
                p = str_props[best_idx]
                return GeoResult(locality, p.get("name") or None, None, "medium")

        return GeoResult(locality, None, None, "low")
    except Exception:
        log.exception("geo.find_address crashed")
        return GeoResult(None, None, None, "none")
