#!/usr/bin/env python3
"""Download OSM administrative boundaries and write a GeoJSON file.

Default is admin_level=6 (Welsh principal areas / UK local authorities,
and the usual 'county / unitary' tier elsewhere). Override with --admin-level
(8 is typically municipalities / districts).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent

OVERPASS_URLS = [
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
HEADERS = {"User-Agent": "linguamap-authority-fetch/1.0 (contact: YOUR_EMAIL@example.com)"}


def same(a, b, eps=1e-9):
    return abs(a[0] - b[0]) < eps and abs(a[1] - b[1]) < eps


def merge_ways_to_rings(ways):
    segs = [list(w) for w in ways if len(w) >= 2]
    rings = []
    while segs:
        ring = segs.pop()
        closed = same(ring[0], ring[-1])
        grew = True
        while not closed and grew:
            grew = False
            for i in range(len(segs) - 1, -1, -1):
                s = segs[i]
                if same(ring[-1], s[0]):
                    ring.extend(s[1:])
                elif same(ring[-1], s[-1]):
                    ring.extend(reversed(s[:-1]))
                elif same(ring[0], s[-1]):
                    ring[0:0] = s[:-1]
                elif same(ring[0], s[0]):
                    ring[0:0] = list(reversed(s[1:]))
                else:
                    continue
                segs.pop(i)
                grew = True
                if same(ring[0], ring[-1]):
                    closed = True
                break
        if len(ring) >= 4:
            if not same(ring[0], ring[-1]):
                ring.append(ring[0])
            rings.append(ring)
    return rings


def point_in_ring(lon, lat, ring):
    inside = False
    n = len(ring)
    for i in range(n - 1):
        x1, y1 = ring[i]
        x2, y2 = ring[i + 1]
        if (y1 > lat) != (y2 > lat):
            xinters = (x2 - x1) * (lat - y1) / (y2 - y1 + 0.0) + x1
            if lon < xinters:
                inside = not inside
    return inside


def relation_geometry(element):
    outers, inners = [], []
    for member in element.get("members") or []:
        geom = member.get("geometry")
        if not geom or len(geom) < 2:
            continue
        line = [(p["lon"], p["lat"]) for p in geom]
        (inners if member.get("role") == "inner" else outers).append(line)
    outer_rings = merge_ways_to_rings(outers)
    inner_rings = merge_ways_to_rings(inners)
    if not outer_rings:
        return None
    polygons = []
    remaining_holes = list(inner_rings)
    for outer in outer_rings:
        holes = [h for h in remaining_holes if point_in_ring(h[0][0], h[0][1], outer)]
        remaining_holes = [h for h in remaining_holes if h not in holes]
        polygons.append([outer, *holes])
    if len(polygons) == 1:
        return {"type": "Polygon", "coordinates": polygons[0]}
    return {"type": "MultiPolygon", "coordinates": polygons}


def way_geometry(element):
    geom = element.get("geometry")
    if not geom or len(geom) < 3:
        return None
    ring = [(p["lon"], p["lat"]) for p in geom]
    if not same(ring[0], ring[-1]):
        ring.append(ring[0])
    return {"type": "Polygon", "coordinates": [ring]}


def _dist(point, start, end):
    px, py = point
    x1, y1 = start
    x2, y2 = end
    dx, dy = x2 - x1, y2 - y1
    if dx == 0 and dy == 0:
        return ((px - x1) ** 2 + (py - y1) ** 2) ** 0.5
    t = max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy)))
    return ((px - (x1 + t * dx)) ** 2 + (py - (y1 + t * dy)) ** 2) ** 0.5


def _rdp(points, epsilon):
    if len(points) < 3:
        return list(points)
    start, end = points[0], points[-1]
    max_dist, max_idx = -1.0, 0
    for i, point in enumerate(points[1:-1], start=1):
        dist = _dist(point, start, end)
        if dist > max_dist:
            max_dist, max_idx = dist, i
    if max_dist > epsilon:
        return _rdp(points[: max_idx + 1], epsilon)[:-1] + _rdp(points[max_idx:], epsilon)
    return [start, end]


def _simplify_ring(ring, epsilon=0.003):
    body = ring[:-1] if len(ring) >= 2 and same(ring[0], ring[-1]) else list(ring)
    simplified = _rdp(body, epsilon)
    if len(simplified) < 3:
        step = max(1, len(body) // 20) if body else 1
        simplified = body[::step]
    if simplified and not same(simplified[0], simplified[-1]):
        simplified.append(simplified[0])
    return simplified


def simplify_geometry(geometry, epsilon=0.003):
    if geometry["type"] == "Polygon":
        return {
            "type": "Polygon",
            "coordinates": [_simplify_ring(r, epsilon) for r in geometry["coordinates"]],
        }
    if geometry["type"] == "MultiPolygon":
        return {
            "type": "MultiPolygon",
            "coordinates": [
                [_simplify_ring(r, epsilon) for r in poly] for poly in geometry["coordinates"]
            ],
        }
    return geometry


def osm_to_geojson(osm):
    features = []
    for el in osm.get("elements") or []:
        tags = el.get("tags") or {}
        if el.get("type") != "relation":
            continue
        geometry = relation_geometry(el)
        if not geometry:
            continue
        geometry = simplify_geometry(geometry)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "name": tags.get("name") or tags.get("name:en") or f"{el['type']}/{el['id']}",
                    "admin_level": tags.get("admin_level"),
                    "designation": tags.get("designation"),
                    "osm_type": el["type"],
                    "osm_id": el["id"],
                },
                "geometry": geometry,
            }
        )
    return {"type": "FeatureCollection", "features": features}


def overpass_query(south, west, north, east, admin_level):
    return f"""
    [out:json][timeout:120];
    relation["type"="boundary"]["boundary"="administrative"]["admin_level"="{admin_level}"]["name"]({south},{west},{north},{east});
    out geom;
    """


def fetch_overpass(query):
    last_error = None
    for url in OVERPASS_URLS:
        try:
            print(f"  {url}", flush=True)
            resp = requests.post(
                url, data={"data": query}, headers=HEADERS, timeout=(15, 120)
            )
            if resp.status_code in {429, 502, 503, 504}:
                print(f"  {resp.status_code}; trying next...", flush=True)
                last_error = RuntimeError(f"{resp.status_code} from {url}")
                time.sleep(3)
                continue
            resp.raise_for_status()
            return resp.json()
        except (requests.Timeout, requests.ConnectionError) as e:
            print(f"  {e}; trying next...", flush=True)
            last_error = e
            time.sleep(3)
    raise last_error or RuntimeError("All Overpass endpoints failed")


def bbox_from_businesses(path):
    with open(path, encoding="utf-8") as f:
        records = json.load(f)
    lats, lons = [], []
    for rec in records:
        if isinstance(rec.get("lat"), (int, float)) and isinstance(rec.get("lon"), (int, float)):
            lats.append(rec["lat"])
            lons.append(rec["lon"])
    if not lats:
        raise ValueError(f"No coordinates in {path}")
    pad = 0.02
    return (min(lats) - pad, min(lons) - pad, max(lats) + pad, max(lons) + pad)


def main():
    parser = argparse.ArgumentParser(description="Fetch OSM local-authority polygons.")
    parser.add_argument("--businesses", default=str(ROOT / "data" / "businesses.json"))
    parser.add_argument("--admin-level", default="6")
    parser.add_argument("-o", "--output", default=str(ROOT / "data" / "authorities.geojson"))
    args = parser.parse_args()

    south, west, north, east = bbox_from_businesses(args.businesses)
    print(f"BBox {south:.4f},{west:.4f},{north:.4f},{east:.4f}")
    print(f"Querying OSM admin_level={args.admin_level}...")
    osm = fetch_overpass(overpass_query(south, west, north, east, args.admin_level))
    geojson = osm_to_geojson(osm)
    print(f"Converted {len(geojson['features'])} boundaries")
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(geojson, f)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(f"Failed: {e}")
