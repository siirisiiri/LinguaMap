#!/usr/bin/env python3
"""Prebuild subdivision-2 outlines into data/admin2/.

Subdivision 1 (states, provinces, oblasts) is already on disk. Clicking into
one of those used to fan out live OSM geometry requests, which is why Quebec
felt slow and why its 17 administrative regions showed up as huge blocky
shapes. This bakes the next OSM admin level — counties, MRCs, raions — with
the same simplification used for admin1.

One GeoJSON per parent area, keyed by OSM relation id. Default is the
countries that have language datasets (CA, GB, UA); pass --iso to add more.

  python3 fetch_admin2.py
  python3 fetch_admin2.py --iso CA,UA
  python3 fetch_admin2.py --osm-id 61549
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests

from fetch_admin1 import HEADERS, simplify_geometry

COUNTRIES_PATH = Path("data/countries.geojson")
ADMIN1_DIR = Path("data/admin1")
OUT_DIR = Path("data/admin2")
# Counties are smaller than provinces; keep a bit more coastline detail.
SIMPLIFY_TOLERANCE = 0.003
OVERPASS_MIRRORS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]


def fetch_overpass(query):
    last_error = None
    for url in OVERPASS_MIRRORS:
        try:
            print(f"    {url}", flush=True)
            resp = requests.post(
                url, data={"data": query}, headers=HEADERS, timeout=(12, 180)
            )
            if resp.status_code in {429, 502, 503, 504}:
                last_error = RuntimeError(f"{resp.status_code} from {url}")
                print(f"    {last_error}", flush=True)
                time.sleep(2)
                continue
            resp.raise_for_status()
            data = resp.json()
            remark = str(data.get("remark") or "")
            if "error" in remark.lower():
                last_error = RuntimeError(remark[:160])
                print(f"    {last_error}", flush=True)
                continue
            return data
        except (requests.Timeout, requests.ConnectionError, ValueError) as e:
            last_error = e
            print(f"    {e}", flush=True)
            time.sleep(2)
    raise last_error or RuntimeError("All Overpass endpoints failed")


def preferred_child_level(parent_level: int) -> int:
    if not parent_level or parent_level <= 2:
        return 4
    if parent_level <= 4:
        return 6
    return 8


def child_levels_to_try(parent_level: int) -> list[int]:
    preferred = preferred_child_level(parent_level)
    ordered = [preferred, preferred + 2, preferred - 1, preferred + 1]
    out: list[int] = []
    for level in ordered:
        if parent_level and level <= parent_level:
            continue
        if level < 3 or level > 10:
            continue
        if level not in out:
            out.append(level)
    return out


def coords_equal(a, b) -> bool:
    return abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9


def merge_ways(ways: list[list[list[float]]]) -> list[list[list[float]]]:
    segs = [way[:] for way in ways if len(way) >= 2]
    rings: list[list[list[float]]] = []
    while segs:
        ring = segs.pop()
        closed = coords_equal(ring[0], ring[-1])
        grew = True
        while not closed and grew:
            grew = False
            for i in range(len(segs) - 1, -1, -1):
                seg = segs[i]
                if coords_equal(ring[-1], seg[0]):
                    ring.extend(seg[1:])
                elif coords_equal(ring[-1], seg[-1]):
                    ring.extend(reversed(seg[:-1]))
                elif coords_equal(ring[0], seg[-1]):
                    ring[0:0] = seg[:-1]
                elif coords_equal(ring[0], seg[0]):
                    ring[0:0] = list(reversed(seg[1:]))
                else:
                    continue
                segs.pop(i)
                grew = True
                closed = coords_equal(ring[0], ring[-1])
                break
        if len(ring) >= 4:
            if not coords_equal(ring[0], ring[-1]):
                ring.append(ring[0])
            rings.append(ring)
    return rings


def point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def overpass_element_geometry(el: dict):
    outers: list[list[list[float]]] = []
    inners: list[list[list[float]]] = []
    for member in el.get("members") or []:
        geom = member.get("geometry") or []
        if len(geom) < 2:
            continue
        line = [[float(pt["lon"]), float(pt["lat"])] for pt in geom if "lon" in pt and "lat" in pt]
        if len(line) < 2:
            continue
        (inners if member.get("role") == "inner" else outers).append(line)
    outer_rings = merge_ways(outers)
    if not outer_rings:
        return None
    leftover = merge_ways(inners)
    polygons = []
    for outer in outer_rings:
        holes = [hole for hole in leftover if hole and point_in_ring(hole[0][0], hole[0][1], outer)]
        for hole in holes:
            leftover.remove(hole)
        polygons.append([outer, *holes])
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def element_to_feature(el: dict, parent: dict, level: int) -> dict | None:
    tags = el.get("tags") or {}
    if tags.get("end_date") or tags.get("historic"):
        return None
    name = tags.get("name:en") or tags.get("name")
    if not name:
        return None
    geom = overpass_element_geometry(el)
    if not geom:
        return None
    simple = simplify_geometry(geom, SIMPLIFY_TOLERANCE)
    if not simple:
        return None
    return {
        "type": "Feature",
        "properties": {
            "osm_type": "relation",
            "osm_id": int(el["id"]),
            "name": name,
            "admin_level": str(tags.get("admin_level") or level),
            "kind": "subdivision_2",
            "parent_osm_id": int(parent["osm_id"]),
            "parent_name": parent.get("name") or "",
            "parent_iso": parent.get("iso") or "",
        },
        "geometry": simple,
    }


def overpass_child_ids(parent: dict, level: int) -> list[dict]:
    parent_id = int(parent["osm_id"])
    iso = parent.get("iso") or ""
    parent_level = int(str(parent.get("admin_level") or "4"))
    if iso and re.fullmatch(r"[A-Z]{2}-[A-Z0-9]{1,3}", iso):
        query = f"""
        [out:json][timeout:60];
        area["ISO3166-2"="{iso}"]["admin_level"="{parent_level}"];
        rel(area)["type"="boundary"]["boundary"="administrative"]["admin_level"="{level}"]["name"];
        out tags;
        """
    else:
        query = f"""
        [out:json][timeout:60];
        rel({parent_id});
        map_to_area -> .parent;
        rel(area.parent)["type"="boundary"]["boundary"="administrative"]["admin_level"="{level}"]["name"];
        out tags;
        """
    data = fetch_overpass(query)
    recs = []
    seen = set()
    for el in data.get("elements") or []:
        if el.get("type") != "relation" or int(el.get("id") or 0) == parent_id:
            continue
        tags = el.get("tags") or {}
        if tags.get("end_date") or tags.get("historic"):
            continue
        name = tags.get("name:en") or tags.get("name")
        if not name:
            continue
        osm_id = int(el["id"])
        if osm_id in seen:
            continue
        seen.add(osm_id)
        recs.append({"osm_id": osm_id, "name": name, "admin_level": str(tags.get("admin_level") or level)})
    return recs


def overpass_children_geom(parent: dict, level: int) -> list[dict]:
    recs = overpass_child_ids(parent, level)
    print(f"    {len(recs)} tagged relations", flush=True)
    if not recs:
        return []
    recs = recs[:400]
    from concurrent.futures import ThreadPoolExecutor

    from serve import cached_relation_feature

    by_id = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for feat in pool.map(cached_relation_feature, [rec["osm_id"] for rec in recs]):
            if not feat or not feat.get("properties"):
                continue
            osm_id = int(feat["properties"]["osm_id"])
            geom = simplify_geometry(feat.get("geometry") or {}, SIMPLIFY_TOLERANCE)
            if not geom:
                continue
            feat = dict(feat)
            feat["geometry"] = geom
            feat["properties"] = dict(feat["properties"])
            feat["properties"]["kind"] = "subdivision_2"
            feat["properties"]["parent_osm_id"] = int(parent["osm_id"])
            feat["properties"]["parent_name"] = parent.get("name") or ""
            feat["properties"]["parent_iso"] = parent.get("iso") or ""
            by_id[osm_id] = feat
            if len(by_id) % 20 == 0:
                print(f"    geom {len(by_id)}/{len(recs)}", flush=True)
    features = []
    for rec in recs:
        feat = by_id.get(rec["osm_id"])
        if not feat:
            continue
        feat["properties"]["name"] = rec["name"] or feat["properties"].get("name")
        feat["properties"]["admin_level"] = rec.get("admin_level") or feat["properties"].get("admin_level")
        features.append(feat)
    print(f"    geom {len(features)}/{len(recs)}", flush=True)
    return features


def admin1_too_deep(features: list[dict]) -> bool:
    if len(features) < 2:
        return True
    deeper = 0
    for feat in features:
        raw = str((feat.get("properties") or {}).get("admin_level") or "4")
        if raw.isdigit() and int(raw) > 4:
            deeper += 1
    return deeper > len(features) / 2


def osm_nations(country_osm_id: int, iso: str) -> list[dict]:
    """UK-style: Natural Earth admin1 is counties, so take OSM nations instead."""
    query = f"""
    [out:json][timeout:90];
    rel({country_osm_id});
    map_to_area -> .parent;
    rel(area.parent)["type"="boundary"]["boundary"="administrative"]["admin_level"="4"]["name"];
    out tags;
    """
    data = fetch_overpass(query)
    out = []
    seen = set()
    for el in data.get("elements") or []:
        if el.get("type") != "relation" or int(el.get("id") or 0) == country_osm_id:
            continue
        tags = el.get("tags") or {}
        if tags.get("end_date") or tags.get("historic"):
            continue
        name = tags.get("name:en") or tags.get("name")
        if not name:
            continue
        osm_id = int(el["id"])
        if osm_id in seen:
            continue
        seen.add(osm_id)
        code = (tags.get("ISO3166-2") or "").upper()
        out.append(
            {
                "osm_id": osm_id,
                "name": name,
                "admin_level": tags.get("admin_level") or "4",
                "iso": code if re.fullmatch(r"[A-Z]{2}-[A-Z0-9]{1,3}", code) else "",
                "parent_iso": iso,
            }
        )
    return out


def country_osm_ids() -> dict[str, dict]:
    if not COUNTRIES_PATH.exists():
        sys.exit(f"Missing {COUNTRIES_PATH}. Run fetch_countries.py first.")
    with COUNTRIES_PATH.open(encoding="utf-8") as f:
        countries = json.load(f)
    out = {}
    for feat in countries.get("features") or []:
        props = feat.get("properties") or {}
        iso = (props.get("iso") or "").upper()
        if iso and props.get("osm_id"):
            out[iso] = {"osm_id": int(props["osm_id"]), "name": props.get("name") or iso}
    return out


def parents_for_iso(iso: str, country: dict) -> list[dict]:
    path = ADMIN1_DIR / f"{iso}.geojson"
    features = []
    if path.exists():
        features = json.loads(path.read_bytes()).get("features") or []
    if features and not admin1_too_deep(features):
        parents = []
        for feat in features:
            props = feat.get("properties") or {}
            osm_id = props.get("osm_id")
            if not osm_id:
                continue
            raw = str(props.get("admin_level") or "4")
            level = int(raw) if raw.isdigit() else 4
            if level > 4:
                continue
            parents.append(
                {
                    "osm_id": int(osm_id),
                    "name": props.get("name") or f"relation/{osm_id}",
                    "admin_level": str(level),
                    "iso": props.get("iso") or "",
                    "parent_iso": iso,
                }
            )
        if parents:
            return parents
    print(f"  {iso}: admin1 is too local; using OSM nation/region relations")
    return osm_nations(country["osm_id"], iso)


def lookup_parent(osm_id: int) -> dict:
    """Fill name/iso from the admin1 files when only an OSM id was given."""
    parent = {
        "osm_id": osm_id,
        "name": f"relation/{osm_id}",
        "admin_level": "4",
        "iso": "",
        "parent_iso": "",
    }
    if not ADMIN1_DIR.exists():
        return parent
    for path in ADMIN1_DIR.glob("*.geojson"):
        try:
            payload = json.loads(path.read_bytes())
        except (OSError, json.JSONDecodeError):
            continue
        for feat in payload.get("features") or []:
            props = feat.get("properties") or {}
            if props.get("osm_id") == osm_id:
                parent.update(
                    {
                        "name": props.get("name") or parent["name"],
                        "admin_level": str(props.get("admin_level") or "4"),
                        "iso": props.get("iso") or "",
                        "parent_iso": props.get("parent_iso") or path.stem,
                    }
                )
                return parent
    return parent


def write_parent(parent: dict, features: list[dict], index: dict) -> None:
    osm_id = int(parent["osm_id"])
    path = OUT_DIR / f"{osm_id}.geojson"
    payload = {
        "type": "FeatureCollection",
        "parent": {
            "osm_id": osm_id,
            "name": parent.get("name") or "",
            "admin_level": parent.get("admin_level") or "4",
            "kind": "subdivision_1",
            "child_kind": "subdivision_2",
            "iso": parent.get("iso") or "",
        },
        "features": features,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    index[str(osm_id)] = {
        "url": f"data/admin2/{path.name}",
        "count": len(features),
        "name": parent.get("name") or "",
        "iso": parent.get("iso") or "",
        "kind": "subdivision_2",
    }
    print(f"  {parent.get('name')}: {len(features)} areas -> {path}", flush=True)


def fetch_parent(parent: dict) -> list[dict]:
    parent_level = int(str(parent.get("admin_level") or "4"))
    last: list[dict] = []
    for level in child_levels_to_try(parent_level):
        print(f"  {parent.get('name')} admin_level={level}…", flush=True)
        features: list[dict] = []
        for attempt in range(2):
            try:
                features = overpass_children_geom(parent, level)
                break
            except Exception as e:
                print(f"    failed ({attempt + 1}/2): {e}", flush=True)
                time.sleep(3)
        print(f"    {len(features)} relations", flush=True)
        # County/MRC/raion scale is typically tens to low hundreds. A thousand
        # municipalities is a last resort — it paints slowly and looks noisy.
        if 3 <= len(features) <= 400:
            return features
        if len(features) > len(last):
            last = features
    return last


def load_index() -> dict:
    path = OUT_DIR / "index.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_bytes())
    except json.JSONDecodeError:
        return {}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--iso",
        default="CA,GB,UA",
        help="Comma-separated ISO2 countries to prebuild (default: CA,GB,UA).",
    )
    parser.add_argument(
        "--osm-id",
        default="",
        help="Comma-separated parent OSM relation ids to fetch instead of --iso.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-fetch parents that already have a file.",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    index = load_index()
    countries = country_osm_ids()
    parents: list[dict] = []

    if args.osm_id:
        for raw in args.osm_id.split(","):
            raw = raw.strip()
            if not raw:
                continue
            parents.append(lookup_parent(int(raw)))
    else:
        for iso in [part.strip().upper() for part in args.iso.split(",") if part.strip()]:
            country = countries.get(iso)
            if not country:
                print(f"Skipping {iso}: no country outline")
                continue
            print(f"Parents in {iso} ({country['name']})…", flush=True)
            found = parents_for_iso(iso, country)
            print(f"  {len(found)} subdivision 1 areas", flush=True)
            parents.extend(found)

    fetched = skipped = failed = 0
    for parent in parents:
        osm_id = str(parent["osm_id"])
        if not args.force and (OUT_DIR / f"{osm_id}.geojson").exists():
            skipped += 1
            if osm_id not in index:
                existing = json.loads((OUT_DIR / f"{osm_id}.geojson").read_bytes())
                index[osm_id] = {
                    "url": f"data/admin2/{osm_id}.geojson",
                    "count": len(existing.get("features") or []),
                    "name": parent.get("name") or "",
                    "iso": parent.get("iso") or "",
                    "kind": "subdivision_2",
                }
            continue
        try:
            features = fetch_parent(parent)
        except Exception as e:
            print(f"  {parent.get('name')} failed: {e}", flush=True)
            failed += 1
            time.sleep(2)
            continue
        if len(features) < 2:
            print(f"  {parent.get('name')}: only {len(features)} areas, skipping")
            failed += 1
            continue
        write_parent(parent, features, index)
        fetched += 1
        (OUT_DIR / "index.json").write_text(
            json.dumps(index, indent=1, sort_keys=True), encoding="utf-8"
        )

    (OUT_DIR / "index.json").write_text(
        json.dumps(index, indent=1, sort_keys=True), encoding="utf-8"
    )
    total = sum(v.get("count", 0) for v in index.values())
    print(
        f"Admin2 index: {len(index)} parents, {total} areas "
        f"(fetched {fetched}, skipped {skipped}, failed {failed})"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(f"Failed: {e}")
