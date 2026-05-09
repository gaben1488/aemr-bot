#!/usr/bin/env python3
"""Скачать из OpenStreetMap всё что нужно для локального reverse geocoding
Елизовского муниципального округа: границы 10 поселений + улицы +
здания с адресами. Сохраняет 3 GeoJSON файла в seed/geo/.

Источники:
- OSM relation 1783592 (Елизовский район) — корневой контейнер
- Overpass API — публичный, без регистрации
- Формат: GeoJSON

Файлы на выходе (в seed/geo/):
- localities.geojson — полигоны поселений ЕМО
- streets.geojson — линии улиц
- buildings.geojson — точки зданий с addr:housenumber

После сборки запустить scripts/verify_geo.py для cross-check.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
EMO_RELATION_ID = 1783592  # Елизовский район в OSM
OUT_DIR = Path(__file__).parent.parent / "seed" / "geo"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def overpass(query: str, retries: int = 3) -> dict:
    """Запрос к Overpass API с retry'ями."""
    headers = {
        "User-Agent": "aemr-bot-geo-builder/1.0 (https://github.com/gaben1488/aemr-bot)",
        "Accept": "application/json",
    }
    for attempt in range(retries):
        try:
            r = requests.post(OVERPASS_URL, data={"data": query}, headers=headers, timeout=300)
            r.raise_for_status()
            return r.json()
        except (requests.HTTPError, requests.Timeout, requests.ConnectionError) as e:
            if attempt == retries - 1:
                raise
            print(f"  retry {attempt + 1}/{retries}: {e}")
            time.sleep(2 ** attempt * 5)
    raise RuntimeError("unreachable")


def way_to_polygon(way_geom: list[dict]) -> list[list[float]]:
    """Список точек way → массив [lon, lat] для GeoJSON."""
    return [[p["lon"], p["lat"]] for p in way_geom]


def assemble_polygon(rel_members: list, ways_by_id: dict) -> list[list[list[float]]] | None:
    """Собрать полигон из member'ов relation типа boundary.

    OSM хранит boundary как relation с outer/inner ways. Outer-ways
    могут идти в произвольном порядке и направлении — собираем
    последовательно по точкам соприкосновения.
    """
    outer_ways = [
        ways_by_id[m["ref"]]
        for m in rel_members
        if m.get("type") == "way" and m.get("role") in ("outer", "")
        and m["ref"] in ways_by_id
    ]
    if not outer_ways:
        return None

    rings: list[list[list[float]]] = []
    remaining = [way_to_polygon(w["geometry"]) for w in outer_ways if w.get("geometry")]

    while remaining:
        current = list(remaining.pop(0))
        while current[0] != current[-1] and remaining:
            last = current[-1]
            for i, w in enumerate(remaining):
                if w[0] == last:
                    current.extend(w[1:])
                    remaining.pop(i)
                    break
                if w[-1] == last:
                    current.extend(reversed(w[:-1]))
                    remaining.pop(i)
                    break
            else:
                break
        if current[0] != current[-1]:
            current.append(current[0])
        if len(current) >= 4:
            rings.append(current)
    return rings if rings else None


def fetch_localities() -> dict:
    """Поселения ЕМО (admin_level 8 и 9 — городские и сельские).

    Используем `out;` (не `out tags;`) для relation — иначе members
    приходят пустыми и невозможно собрать полигон по ways.
    """
    print("→ Скачиваю границы поселений ЕМО (admin_level=8,9)…")
    query = f"""
    [out:json][timeout:300];
    relation({EMO_RELATION_ID});
    map_to_area->.emo;
    relation(area.emo)["boundary"="administrative"]["admin_level"~"^(8|9)$"];
    out;
    >;
    out geom;
    """
    data = overpass(query)
    relations = [e for e in data["elements"] if e["type"] == "relation"]
    ways = {e["id"]: e for e in data["elements"] if e["type"] == "way"}

    features = []
    for rel in relations:
        tags = rel.get("tags", {})
        name = tags.get("name", "")
        if not name:
            continue
        polygon = assemble_polygon(rel.get("members", []), ways)
        if not polygon:
            print(f"  пропустил {name}: не собирается полигон")
            continue
        if len(polygon) == 1:
            geom = {"type": "Polygon", "coordinates": [polygon[0]]}
        else:
            geom = {"type": "MultiPolygon", "coordinates": [[p] for p in polygon]}
        features.append({
            "type": "Feature",
            "properties": {
                "name": name,
                "name_ru": tags.get("name:ru", name),
                "admin_level": tags.get("admin_level"),
                "place": tags.get("place"),
                "osm_id": rel["id"],
                "wikidata": tags.get("wikidata"),
            },
            "geometry": geom,
        })
        print(f"  + {name} (osm_id={rel['id']})")

    return {"type": "FeatureCollection", "features": features}


def fetch_streets() -> dict:
    """Все улицы (highway=*) с name внутри ЕМО."""
    print("→ Скачиваю улицы ЕМО (highway, name)…")
    query = f"""
    [out:json][timeout:300];
    relation({EMO_RELATION_ID});
    map_to_area->.emo;
    way(area.emo)["highway"]["name"];
    out tags geom;
    """
    data = overpass(query)

    features = []
    for el in data["elements"]:
        if el["type"] != "way" or not el.get("geometry"):
            continue
        tags = el.get("tags", {})
        coords = [[p["lon"], p["lat"]] for p in el["geometry"]]
        features.append({
            "type": "Feature",
            "properties": {
                "name": tags.get("name", ""),
                "name_ru": tags.get("name:ru", tags.get("name", "")),
                "highway": tags.get("highway"),
                "place_addr": tags.get("addr:place"),
                "city_addr": tags.get("addr:city"),
                "osm_id": el["id"],
            },
            "geometry": {"type": "LineString", "coordinates": coords},
        })
    print(f"  + {len(features)} сегментов улиц")
    return {"type": "FeatureCollection", "features": features}


def fetch_buildings() -> dict:
    """Здания с addr:housenumber в ЕМО."""
    print("→ Скачиваю здания с адресами (addr:housenumber)…")
    query = f"""
    [out:json][timeout:300];
    relation({EMO_RELATION_ID});
    map_to_area->.emo;
    (
      way(area.emo)["addr:housenumber"];
      node(area.emo)["addr:housenumber"];
    );
    out tags geom center;
    """
    data = overpass(query)

    features = []
    for el in data["elements"]:
        tags = el.get("tags", {})
        housenumber = tags.get("addr:housenumber")
        if not housenumber:
            continue
        if el["type"] == "way":
            center = el.get("center")
            if not center:
                continue
            lon, lat = center["lon"], center["lat"]
        elif el["type"] == "node":
            lon, lat = el["lon"], el["lat"]
        else:
            continue
        features.append({
            "type": "Feature",
            "properties": {
                "housenumber": housenumber,
                "street": tags.get("addr:street", ""),
                "place": tags.get("addr:place", ""),
                "city": tags.get("addr:city", ""),
                "postcode": tags.get("addr:postcode", ""),
                "osm_id": el["id"],
                "osm_type": el["type"],
            },
            "geometry": {"type": "Point", "coordinates": [lon, lat]},
        })
    print(f"  + {len(features)} зданий с адресами")
    return {"type": "FeatureCollection", "features": features}


def main():
    print(f"=== build_geo_database — Елизовский МО (OSM rel={EMO_RELATION_ID}) ===")
    print(f"Output: {OUT_DIR}")
    print()

    localities = fetch_localities()
    (OUT_DIR / "localities.geojson").write_text(
        json.dumps(localities, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"  → localities.geojson saved ({len(localities['features'])} features)\n")

    streets = fetch_streets()
    (OUT_DIR / "streets.geojson").write_text(
        json.dumps(streets, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"  → streets.geojson saved ({len(streets['features'])} features)\n")

    buildings = fetch_buildings()
    (OUT_DIR / "buildings.geojson").write_text(
        json.dumps(buildings, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    print(f"  → buildings.geojson saved ({len(buildings['features'])} features)\n")

    print("=== Done ===")


if __name__ == "__main__":
    main()
