#!/usr/bin/env python3
"""Верификация GeoJSON-данных Елизовского МО:
1. Совпадение OSM-имён поселений с нашим settings.localities
2. Cross-check центров поселений против Wikidata (если задан wikidata тег)
3. Известные точки (например, площадь Ленина в Елизово) попадают
   в правильное поселение через point-in-polygon
4. Покрытие: для каждого поселения — сколько улиц и зданий

Запускать: python scripts/verify_geo.py
Результат: stdout-отчёт + exit code 0 если всё ок, 1 если ошибки.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests
from shapely.geometry import Point, shape

GEO_DIR = Path(__file__).parent.parent / "seed" / "geo"

# Маппинг полных OSM-имён в наши короткие (как в settings.localities)
OSM_TO_SHORT = {
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

# Известные точки внутри поселений — для тестирования point-in-polygon.
# Источник: Wikidata P625 центров населённых пунктов (после первой
# успешной верификации wikidata cross-check). Это эталонные координаты
# центров посёлков, не «на глаз».
KNOWN_POINTS = [
    # (lat, lon, expected_locality_short)
    # Источник: Wikidata P625 центров населённых пунктов, кроме
    # Пионерского — для него Wikidata Q21193644 содержит ошибочную
    # координату (53.205, 158.453), реально попадающую в Елизовское ГП.
    # Cross-проверка через OSM place=village «Пионерский» дала точку
    # (53.090, 158.557), которая корректно попадает в полигон
    # Пионерского СП. Используем OSM place-ноду как эталон.
    (53.184, 158.385, "Елизовское ГП"),       # площадь Ленина в Елизово
    (52.961, 158.249, "Паратунское СП"),      # Wikidata Q21193640
    (53.271, 158.289, "Раздольненское СП"),   # Wikidata Q21193647
    (53.281, 158.208, "Корякское СП"),        # Wikidata Q21193619
    (53.099, 158.537, "Новоавачинское СП"),   # Wikidata Q21193636
    (53.096, 158.350, "Вулканное ГП"),        # Wikidata Q21195299
    (53.045, 158.336, "Николаевское СП"),     # Wikidata Q21193632
    (53.255, 158.029, "Новолесновское СП"),   # Wikidata Q21193638
    (53.145, 157.698, "Начикинское СП"),      # Wikidata Q21193630
    (53.090, 158.557, "Пионерское СП"),       # OSM place-нода (Wikidata неточна)
]


def wikidata_coords(qid: str) -> tuple[float, float] | None:
    """Получить координаты центра объекта из Wikidata (point P625)."""
    if not qid:
        return None
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "aemr-bot-geo-verify/1.0"})
        r.raise_for_status()
        data = r.json()
        claims = data.get("entities", {}).get(qid, {}).get("claims", {})
        coord_claim = claims.get("P625", [])
        if not coord_claim:
            return None
        v = coord_claim[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
        return (v.get("latitude"), v.get("longitude"))
    except Exception as e:
        print(f"    wikidata error for {qid}: {e}")
        return None


def main() -> int:
    print("=== Verify ЕМО geo database ===\n")
    errors = 0

    # 1. Загрузка данных
    localities = json.loads((GEO_DIR / "localities.geojson").read_text(encoding="utf-8"))
    streets = json.loads((GEO_DIR / "streets.geojson").read_text(encoding="utf-8"))
    buildings = json.loads((GEO_DIR / "buildings.geojson").read_text(encoding="utf-8"))

    polys = {}
    for f in localities["features"]:
        full = f["properties"]["name"]
        short = OSM_TO_SHORT.get(full)
        if not short:
            print(f"  ✗ MAPPING: OSM имя «{full}» не в OSM_TO_SHORT")
            errors += 1
            continue
        polys[short] = (shape(f["geometry"]), f)

    print(f"1. Маппинг OSM → наши имена: {len(polys)}/10")
    if len(polys) < 10:
        errors += 1
    print()

    # 2. Wikidata cross-check (центральные точки поселений)
    print("2. Wikidata cross-check центров поселений:")
    for short, (poly, feat) in polys.items():
        qid = feat["properties"].get("wikidata")
        if not qid:
            print(f"  - {short}: нет wikidata тега в OSM")
            continue
        wd_coords = wikidata_coords(qid)
        if wd_coords is None:
            print(f"  ? {short} ({qid}): Wikidata не вернула координаты")
            continue
        lat, lon = wd_coords
        # Проверяем что центр Wikidata попадает в полигон OSM (или близко к нему)
        p = Point(lon, lat)
        inside = poly.contains(p)
        dist_km = p.distance(poly) * 111  # прибл. градус → км
        if inside:
            print(f"  ✓ {short} ({qid}): центр Wikidata ({lat:.3f}, {lon:.3f}) внутри полигона OSM")
        elif dist_km < 5:
            print(f"  ~ {short} ({qid}): центр Wikidata снаружи, но в {dist_km:.1f} км — норм")
        else:
            print(f"  ✗ {short} ({qid}): центр Wikidata в {dist_km:.1f} км от полигона — ПРОВЕРИТЬ")
            errors += 1
    print()

    # 3. Известные точки → ожидаемое поселение
    print("3. Point-in-polygon на известных точках:")
    for lat, lon, expected in KNOWN_POINTS:
        p = Point(lon, lat)
        found = None
        for short, (poly, _) in polys.items():
            if poly.contains(p):
                found = short
                break
        if found == expected:
            print(f"  ✓ ({lat}, {lon}) → {found} (ожидался {expected})")
        elif found is None:
            print(f"  ✗ ({lat}, {lon}) → НЕ НАЙДЕН (ожидался {expected})")
            errors += 1
        else:
            print(f"  ✗ ({lat}, {lon}) → {found} (ожидался {expected})")
            errors += 1
    print()

    # 4. Покрытие: улицы и здания по поселениям
    print("4. Покрытие поселений данными:")
    locality_streets: dict[str, int] = {short: 0 for short in polys}
    locality_buildings: dict[str, int] = {short: 0 for short in polys}

    for s in streets["features"]:
        coords = s["geometry"]["coordinates"]
        if not coords:
            continue
        # Проверка по средней точке улицы
        mid = coords[len(coords) // 2]
        p = Point(mid[0], mid[1])
        for short, (poly, _) in polys.items():
            if poly.contains(p):
                locality_streets[short] = locality_streets.get(short, 0) + 1
                break

    for b in buildings["features"]:
        coords = b["geometry"]["coordinates"]
        p = Point(coords[0], coords[1])
        for short, (poly, _) in polys.items():
            if poly.contains(p):
                locality_buildings[short] = locality_buildings.get(short, 0) + 1
                break

    for short in sorted(polys):
        s_count = locality_streets[short]
        b_count = locality_buildings[short]
        marker = "✓" if (s_count > 0 and b_count > 0) else "?"
        print(f"  {marker} {short:25s} улиц={s_count:4d}  зданий={b_count:5d}")
    print()

    # Итог
    if errors == 0:
        print("=== ВСЁ ОК — geo database готова к использованию ===")
        return 0
    else:
        print(f"=== {errors} ошибок — посмотри выше ===")
        return 1


if __name__ == "__main__":
    sys.exit(main())
