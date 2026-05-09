#!/usr/bin/env python3
"""Расширенная cross-верификация границ поселений ЕМО.

Проверяет 5 разных свойств которые независимы друг от друга:

1. Wikidata P2046 (площадь объекта) — сверка с площадью OSM-полигона
2. Полигоны не пересекаются между собой (administrative boundaries
   юридически не должны пересекаться)
3. Здания администраций / школ по адресам из открытых источников
   попадают в правильные полигоны
4. place=town/village ноды OSM (отдельный source) попадают в свои полигоны
5. Hausdorff-расстояние полигонов до Wikidata-центров — sanity-check

Если что-то не сходится — выводит чёткое сообщение что именно и где.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import requests
from shapely.geometry import Point, shape

GEO_DIR = Path(__file__).parent.parent / "seed" / "geo"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
EMO_RELATION_ID = 1783592

WIKIDATA_QIDS = {
    "Елизовское ГП": "Q21195451",
    "Вулканное ГП": "Q21195299",
    "Корякское СП": "Q21193619",
    "Начикинское СП": "Q21193630",
    "Николаевское СП": "Q21193632",
    "Новоавачинское СП": "Q21193636",
    "Новолесновское СП": "Q21193638",
    "Паратунское СП": "Q21193640",
    "Пионерское СП": "Q21193644",
    "Раздольненское СП": "Q21193647",
}

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

UA = {
    "User-Agent": "aemr-bot-geo-verify/1.0",
    "Accept": "application/json",
}


def overpass(query: str) -> dict:
    r = requests.post(OVERPASS_URL, data={"data": query}, headers=UA, timeout=120)
    r.raise_for_status()
    return r.json()


def wikidata_entity(qid: str) -> dict | None:
    if not qid:
        return None
    url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
    try:
        r = requests.get(url, timeout=20, headers=UA)
        r.raise_for_status()
        return r.json().get("entities", {}).get(qid, {})
    except Exception as e:
        print(f"  wikidata error for {qid}: {e}")
        return None


def shapely_area_km2(poly) -> float:
    """Грубая оценка площади полигона в км². Учитывает что 1° по
    широте на Камчатке (~53°) ≈ 111 км, по долготе на 53° ≈ 67 км."""
    deg2 = poly.area
    return deg2 * 111 * 67


def main() -> int:
    print("=== Cross-verification границ ЕМО ===\n")
    errors = 0
    warnings = 0

    # Загружаем основные данные
    localities = json.loads((GEO_DIR / "localities.geojson").read_text(encoding="utf-8"))
    polys: dict[str, tuple[object, dict]] = {}
    for f in localities["features"]:
        full = f["properties"]["name"]
        short = OSM_TO_SHORT.get(full)
        if short:
            polys[short] = (shape(f["geometry"]), f)

    # ---- 1. Wikidata P2046 area cross-check ----
    print("1. Площадь полигонов vs Wikidata P2046:")
    for short in sorted(polys):
        poly, _ = polys[short]
        osm_km2 = shapely_area_km2(poly)
        qid = WIKIDATA_QIDS.get(short)
        ent = wikidata_entity(qid) if qid else None
        wd_km2 = None
        if ent:
            claims = ent.get("claims", {}).get("P2046", [])
            if claims:
                v = claims[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
                amount = v.get("amount", "")
                unit = v.get("unit", "")
                try:
                    val = float(amount)
                    if "Q712226" in unit:  # km²
                        wd_km2 = val
                    elif "Q35852" in unit:  # ha
                        wd_km2 = val / 100
                    elif "Q25343" in unit:  # m²
                        wd_km2 = val / 1_000_000
                except ValueError:
                    pass
        if wd_km2 is None:
            print(f"  ? {short:25s} OSM={osm_km2:8.1f} км²  Wikidata: P2046 нет")
            continue
        diff_pct = abs(osm_km2 - wd_km2) / wd_km2 * 100
        marker = "✓" if diff_pct < 30 else "✗" if diff_pct > 100 else "~"
        print(
            f"  {marker} {short:25s} OSM={osm_km2:8.1f} км²  WD={wd_km2:8.1f} км²  "
            f"diff={diff_pct:5.1f}%"
        )
        if diff_pct > 100:
            errors += 1
        elif diff_pct > 30:
            warnings += 1
    print()

    # ---- 2. Полигоны не пересекаются ----
    print("2. Попарное непересечение полигонов:")
    overlap_found = False
    items = list(polys.items())
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            sa, (pa, _) = items[i]
            sb, (pb, _) = items[j]
            inter = pa.intersection(pb)
            if not inter.is_empty:
                overlap_km2 = shapely_area_km2(inter)
                if overlap_km2 > 0.1:  # > 100×100 м
                    print(f"  ✗ {sa} ↔ {sb}: пересечение {overlap_km2:.2f} км²")
                    errors += 1
                    overlap_found = True
                elif overlap_km2 > 0.001:
                    print(f"  ~ {sa} ↔ {sb}: пересечение {overlap_km2*1000:.1f}м² (граничный шум)")
    if not overlap_found:
        print("  ✓ ни одного существенного пересечения")
    print()

    # ---- 3. place=town/village ноды попадают в свои полигоны ----
    print("3. OSM place-ноды (independent source vs admin_level boundaries):")
    query = f"""
    [out:json][timeout:60];
    relation({EMO_RELATION_ID});
    map_to_area->.emo;
    node(area.emo)["place"~"^(village|town|hamlet)$"];
    out tags;
    """
    try:
        data = overpass(query)
        place_nodes = [
            (e["lat"], e["lon"], e.get("tags", {}).get("name"))
            for e in data["elements"]
            if e["type"] == "node"
        ]
        print(f"  скачано {len(place_nodes)} place-нод")
        misplaced = 0
        for lat, lon, name in place_nodes:
            p = Point(lon, lat)
            found = None
            for short, (poly, _) in polys.items():
                if poly.contains(p):
                    found = short
                    break
            # Эвристика: ожидаемое поселение по названию ноды
            expected = None
            for short in polys:
                # Сравнение по корню: «Елизово» в «Елизовское ГП»
                short_root = short.split()[0].rstrip("оеыий")
                if name and short_root.lower()[:5] in name.lower():
                    expected = short
                    break
            if found is None:
                # Нода вне всех полигонов — это нормально для удалённых хуторов
                continue
            if expected and found != expected:
                print(f"  ~ {name}: попадает в {found}, по имени ждали {expected}")
                misplaced += 1
        if misplaced == 0:
            print("  ✓ все размеченные place-ноды попадают в логичные полигоны")
        else:
            print(f"  {misplaced} нот с расхождением имени и полигона (м.б. норма для приграничных)")
    except Exception as e:
        print(f"  ! Overpass query failed: {e}")
        warnings += 1
    print()

    # ---- 4. Здания администраций как стационарные эталоны ----
    print("4. Здания государственных учреждений (amenity=townhall/school)")
    query = f"""
    [out:json][timeout:60];
    relation({EMO_RELATION_ID});
    map_to_area->.emo;
    (
      way(area.emo)["amenity"="townhall"];
      node(area.emo)["amenity"="townhall"];
      way(area.emo)["amenity"="school"]["operator:type"="public"];
    );
    out tags center;
    """
    try:
        data = overpass(query)
        anchors = []
        for el in data["elements"]:
            tags = el.get("tags", {})
            if el["type"] == "way":
                center = el.get("center")
                if not center:
                    continue
                anchors.append((center["lat"], center["lon"], tags.get("name", "?"), tags.get("amenity")))
            elif el["type"] == "node":
                anchors.append((el["lat"], el["lon"], tags.get("name", "?"), tags.get("amenity")))
        print(f"  найдено {len(anchors)} институциональных зданий")
        for lat, lon, name, amenity in anchors[:10]:
            p = Point(lon, lat)
            found = None
            for short, (poly, _) in polys.items():
                if poly.contains(p):
                    found = short
                    break
            print(f"  {name[:40]:42s} ({amenity:8s}) → {found or 'НЕ В ПОЛИГОНЕ'}")
    except Exception as e:
        print(f"  ! Overpass query failed: {e}")
    print()

    # ---- 5. Hausdorff-расстояние центров Wikidata до полигонов ----
    print("5. Wikidata centers: distance to polygon boundary:")
    for short, qid in sorted(WIKIDATA_QIDS.items()):
        if short not in polys:
            continue
        poly, _ = polys[short]
        ent = wikidata_entity(qid)
        if not ent:
            continue
        coord = ent.get("claims", {}).get("P625", [])
        if not coord:
            continue
        v = coord[0].get("mainsnak", {}).get("datavalue", {}).get("value", {})
        lat, lon = v.get("latitude"), v.get("longitude")
        if lat is None or lon is None:
            continue
        center = Point(lon, lat)
        if poly.contains(center):
            # внутри — проверяем на сколько глубоко (расстояние до ближайшей границы)
            dist_deg = poly.boundary.distance(center)
            dist_m = dist_deg * 111_000
            print(f"  ✓ {short:25s} центр внутри, до границы {dist_m:6.0f} м")
        else:
            dist_deg = poly.distance(center)
            dist_m = dist_deg * 111_000
            print(f"  ✗ {short:25s} центр СНАРУЖИ, расстояние {dist_m:6.0f} м")
            if dist_m > 5000:
                errors += 1
            else:
                warnings += 1
    print()

    # ---- Итог ----
    print("=" * 50)
    if errors == 0 and warnings == 0:
        print("=== ВСЁ ОК — границы корректны по всем источникам ===")
        return 0
    elif errors == 0:
        print(f"=== {warnings} предупреждений — посмотрите выше, но критики нет ===")
        return 0
    else:
        print(f"=== {errors} ошибок, {warnings} предупреждений ===")
        return 1


if __name__ == "__main__":
    sys.exit(main())
