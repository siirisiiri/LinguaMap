#!/usr/bin/env python3
"""Prebuild every country's first-level subdivisions into data/admin1/.

Drilling a country used to cost a minute or two of live Overpass geometry
queries. This bakes that first level in ahead of time: Natural Earth 10m
outlines supply the shapes, simplified to roughly half a kilometre, and a
tags-only Overpass pass supplies the OSM relation ids the viewer needs to keep
drilling deeper. One file per country, so the viewer downloads only the
subdivisions it is about to draw.
"""

from __future__ import annotations

import json
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests

OVERPASS_URLS = [
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
HEADERS = {"User-Agent": "linguamap-admin1-fetch/1.0 (contact: YOUR_EMAIL@example.com)"}
NE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_10m_admin_1_states_provinces.geojson"
)
COUNTRIES_PATH = Path("data/countries.geojson")
OUT_DIR = Path("data/admin1")
# The raw 10m outlines are 40 MB of coastline detail nobody can see at the zoom
# levels a province choropleth is read at. ~0.005 deg is roughly 550 m.
SIMPLIFY_TOLERANCE = 0.005
COORD_PRECISION = 4


def fetch_overpass(query):
    last_error = None
    for url in OVERPASS_URLS:
        try:
            print(f"  {url}", flush=True)
            resp = requests.post(
                url, data={"data": query}, headers=HEADERS, timeout=(15, 300)
            )
            if resp.status_code in {429, 502, 503, 504}:
                last_error = RuntimeError(f"{resp.status_code} from {url}")
                time.sleep(2)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            last_error = e
            time.sleep(2)
    raise last_error or RuntimeError("All Overpass endpoints failed")


def normalize_name(name):
    stripped = unicodedata.normalize("NFKD", name or "")
    ascii_only = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", ascii_only.lower())


def relation_rank(tags):
    """Lower is better: prefer live, shallow boundaries."""
    level = tags.get("admin_level") or ""
    return (100 if (tags.get("end_date") or tags.get("historic")) else 0) + (
        int(level) if level.isdigit() else 99
    )


def osm_subdivisions():
    """Index OSM subdivision relations by ISO3166-2 code and by bare name."""
    query = """
    [out:json][timeout:300];
    (
      rel["boundary"="administrative"]["ISO3166-2"];
      rel["boundary"="administrative"]["admin_level"~"^[34]$"]["name"];
    );
    out tags;
    """
    print("Fetching OSM subdivision relations (tags only)...")
    osm = fetch_overpass(query)
    by_iso = {}
    by_name = {}
    for el in osm.get("elements") or []:
        tags = el.get("tags") or {}
        if el.get("type") != "relation":
            continue
        rec = {
            "osm_id": el["id"],
            "name": tags.get("name:en") or tags.get("name") or "",
            "admin_level": tags.get("admin_level") or "4",
            "rank": relation_rank(tags),
        }
        code = iso_code(tags.get("ISO3166-2"))
        if code and (code not in by_iso or rec["rank"] < by_iso[code]["rank"]):
            by_iso[code] = rec
        for label in (tags.get("name"), tags.get("name:en")):
            key = normalize_name(label)
            if key:
                by_name.setdefault(key, []).append(rec)
    print(f"  {len(by_iso)} ISO3166-2 codes, {len(by_name)} distinct names")
    return by_iso, by_name


def natural_earth():
    print("Downloading Natural Earth 10m state/province outlines (~40 MB)...")
    resp = requests.get(NE_URL, headers=HEADERS, timeout=(15, 300))
    resp.raise_for_status()
    return resp.json()


def country_osm_ids():
    """iso2 -> country relation id, so each subdivision knows its parent."""
    if not COUNTRIES_PATH.exists():
        sys.exit(f"Missing {COUNTRIES_PATH}. Run fetch_countries.py first.")
    with COUNTRIES_PATH.open(encoding="utf-8") as f:
        countries = json.load(f)
    ids = {}
    for feat in countries.get("features") or []:
        props = feat.get("properties") or {}
        if props.get("iso") and props.get("osm_id"):
            ids[props["iso"].upper()] = props["osm_id"]
    return ids


def iso_code(value):
    code = (value or "").upper().replace("_", "-").strip()
    return code if re.fullmatch(r"[A-Z]{2}-[A-Z0-9]{1,3}", code) else ""


def feature_codes(props):
    """(ISO3166-2 code, country iso2) for a Natural Earth admin-1 row."""
    code = iso_code(props.get("iso_3166_2"))
    # NE codes dependencies against their own ISO prefix but sometimes carries
    # the sovereign state in iso_a2, so the prefix wins when both disagree.
    country = code[:2] if code else (props.get("iso_a2") or "").upper()
    return code, country if len(country) == 2 else ""


# ------------------------------------------------------------- simplification


def rdp(points, tol):
    """Ramer-Douglas-Peucker, iterative so long coastlines cannot blow the stack."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        ax, ay = points[start]
        bx, by = points[end]
        dx, dy = bx - ax, by - ay
        norm = (dx * dx + dy * dy) ** 0.5
        worst, worst_i = -1.0, -1
        for i in range(start + 1, end):
            px, py = points[i]
            if norm:
                dist = abs(dx * (py - ay) - dy * (px - ax)) / norm
            else:
                dist = ((px - ax) ** 2 + (py - ay) ** 2) ** 0.5
            if dist > worst:
                worst, worst_i = dist, i
        if worst > tol:
            keep[worst_i] = True
            stack.append((start, worst_i))
            stack.append((worst_i, end))
    return [p for p, k in zip(points, keep) if k]


def simplify_ring(ring, tol):
    """A closed ring of rounded coordinates, or None if nothing survives."""
    for attempt in (tol, tol / 4, 0):
        pts = rdp(ring, attempt) if attempt else list(ring)
        pts = [[round(x, COORD_PRECISION), round(y, COORD_PRECISION)] for x, y in pts]
        deduped = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
        if len(deduped) >= 3:
            if deduped[0] != deduped[-1]:
                deduped.append(deduped[0])
            return deduped
    return None


def simplify_geometry(geom, tol):
    polygons = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    kept = []
    for poly in polygons:
        rings = []
        for index, ring in enumerate(poly):
            simple = simplify_ring(ring, tol)
            if simple is None:
                if index == 0:  # outer ring gone, so the whole polygon goes
                    rings = []
                    break
                continue  # a hole too small to matter
            rings.append(simple)
        if rings:
            kept.append(rings)
    if not kept:
        return None
    if len(kept) == 1:
        return {"type": "Polygon", "coordinates": kept[0]}
    return {"type": "MultiPolygon", "coordinates": kept}


# --------------------------------------------------------------------- build


def main():
    by_iso, by_name = osm_subdivisions()
    parents = country_osm_ids()
    ne = natural_earth()

    print("Simplifying outlines and joining OSM ids...")
    by_country = {}
    matched_iso = matched_name = unmatched = 0
    skipped_country = skipped_geom = 0
    for feat in ne.get("features") or []:
        props = feat.get("properties") or {}
        code, country_iso = feature_codes(props)
        parent_id = parents.get(country_iso)
        if not parent_id:
            skipped_country += 1
            continue
        geometry = simplify_geometry(feat["geometry"], SIMPLIFY_TOLERANCE)
        if not geometry:
            skipped_geom += 1
            continue

        name = props.get("name_en") or props.get("name") or code or "(unnamed)"
        rec = by_iso.get(code) if code else None
        if rec:
            matched_iso += 1
        else:
            # Names collide across countries ("Santa Cruz"), so a name match is
            # only trusted when the whole planet offers exactly one candidate.
            candidates = by_name.get(normalize_name(name)) or []
            rec = candidates[0] if len(candidates) == 1 else None
            if rec:
                matched_name += 1
            else:
                unmatched += 1

        by_country.setdefault(country_iso, {"parent_osm_id": parent_id, "features": []})
        by_country[country_iso]["features"].append(
            {
                "type": "Feature",
                "properties": {
                    "osm_type": "relation",
                    "osm_id": rec["osm_id"] if rec else None,
                    "name": rec["name"] if rec and rec["name"] else name,
                    "admin_level": rec["admin_level"] if rec else "4",
                    "iso": code,
                    "parent_iso": country_iso,
                    "parent_osm_id": parent_id,
                },
                "geometry": geometry,
            }
        )

    if OUT_DIR.exists():
        for stale in OUT_DIR.glob("*.geojson"):
            stale.unlink()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    index = {}
    total_bytes = 0
    for country_iso, bundle in sorted(by_country.items()):
        path = OUT_DIR / f"{country_iso}.geojson"
        with path.open("w", encoding="utf-8") as f:
            json.dump({"type": "FeatureCollection", "features": bundle["features"]}, f)
        total_bytes += path.stat().st_size
        index[str(bundle["parent_osm_id"])] = {
            "iso": country_iso,
            "url": f"data/admin1/{path.name}",
            "count": len(bundle["features"]),
        }
    with (OUT_DIR / "index.json").open("w", encoding="utf-8") as f:
        json.dump(index, f, indent=1, sort_keys=True)

    total = sum(len(b["features"]) for b in by_country.values())
    print(
        f"{total} subdivisions in {len(by_country)} countries: {matched_iso} matched "
        f"by ISO3166-2, {matched_name} by name, {unmatched} without an OSM id "
        f"(those fall back to a live lookup only if drilled further)."
    )
    if skipped_country or skipped_geom:
        print(
            f"Skipped {skipped_country} rows with no country outline and "
            f"{skipped_geom} with unusable geometry."
        )
    biggest = max(index.values(), key=lambda v: v["count"])
    print(
        f"Wrote {len(index) + 1} files to {OUT_DIR} ({total_bytes / 1e6:.1f} MB total; "
        f"largest is {biggest['iso']} with {biggest['count']} areas)"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(f"Failed: {e}")
