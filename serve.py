#!/usr/bin/env python3
"""Serve the map viewer on localhost with every dataset in data/ preloaded.

Usage: python3 serve.py [--port 8000] [--no-browser]

The viewer asks /api/datasets which files to load, so dropping a new
data/<Region>.json in place is enough to make it show up on the next refresh.
Country and subdivision-1 boundaries are served from data/, prebuilt by
tools/fetch_countries.py and tools/fetch_admin1.py. Subdivision 2 uses
data/admin2 when present (tools/fetch_admin2.py); otherwise it lists OSM
child areas and draws cached OSM geometries.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import http.server
import json
import re
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:  # python.org builds on macOS ship without a usable system CA bundle
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache" / "overpass"
RELATION_CACHE_DIR = ROOT / ".cache" / "osm_relations"
VIEWER = "map_viewer.html"

# Boundary outlines the viewer fetches by name; they are not language datasets.
NON_DATASET_FILES = {"countries.geojson", "authorities.geojson"}

OVERPASS_URLS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
OVERPASS_TIMEOUT_S = 90
SUBDIV_CACHE_DIR = ROOT / ".cache" / "subdivisions"
ADMIN2_DIR = DATA_DIR / "admin2"
USER_AGENT = "linguamap-viewer/1.0"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
FULL_CACHE_DIR = ROOT / ".cache" / "osm_full"
GEOJSON_CACHE_DIR = ROOT / ".cache" / "relation_geojson"
KIND_LABELS = {
    "country": "Country",
    "subdivision_1": "Subdivision 1",
    "subdivision_2": "Subdivision 2",
}

_AREA_INDEX = None
_OSM_GATE = threading.Semaphore(4)
_CHILD_MEMO: dict[int, tuple[dict, list[dict]]] = {}
_CHILD_LOCKS: dict[int, threading.Lock] = {}
_CHILD_LOCKS_GUARD = threading.Lock()


def norm_name(value: str) -> str:
    stripped = unicodedata.normalize("NFKD", value or "")
    ascii_only = "".join(c for c in stripped if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", "", ascii_only.lower())


def geometry_bbox(geom: dict | None) -> list[float] | None:
    """Return [west, south, east, north] or None."""
    if not geom:
        return None
    west, south, east, north = 180.0, 90.0, -180.0, -90.0
    found = False

    def walk(coord):
        nonlocal west, south, east, north, found
        if not coord:
            return
        if isinstance(coord[0], (int, float)):
            west = min(west, float(coord[0]))
            east = max(east, float(coord[0]))
            south = min(south, float(coord[1]))
            north = max(north, float(coord[1]))
            found = True
            return
        for item in coord:
            walk(item)

    walk(geom.get("coordinates"))
    if not found or west > east:
        return None
    return [west, south, east, north]


def area_kind(admin_level, default: str = "subdivision_2") -> str:
    try:
        level = int(admin_level)
    except (TypeError, ValueError):
        return default
    if level <= 2:
        return "country"
    if level <= 4:
        return "subdivision_1"
    return "subdivision_2"


def preferred_child_level(parent_level: int) -> int:
    """Countries -> 4, provinces/states/oblasts -> 6, else 8.

    Matches the original viewer: Quebec should open MRCs (level 6), not the
    17 administrative regions (level 5).
    """
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


def select_child_records(records: list[dict], parent_level: int) -> list[dict]:
    by_level: dict[str, list[dict]] = {}
    unlabeled: list[dict] = []
    for rec in records:
        level = str(rec.get("admin_level") or "")
        if level.isdigit() and parent_level and int(level) <= parent_level:
            continue
        if level.isdigit():
            by_level.setdefault(level, []).append(rec)
        else:
            unlabeled.append(rec)
    for level in child_levels_to_try(parent_level):
        recs = by_level.get(str(level)) or []
        if len(recs) >= 3:
            return recs
    ranked = sorted(by_level.values(), key=len, reverse=True)
    for recs in ranked:
        if 3 <= len(recs) <= 500:
            return recs
    if len(unlabeled) >= 2:
        return unlabeled
    if ranked:
        return ranked[0]
    return unlabeled


def _hit_from_row(row: dict) -> dict:
    kind = row.get("kind") or area_kind(row.get("admin_level"))
    hit = {
        "name": row["name"],
        "osm_id": row["osm_id"],
        "admin_level": row.get("admin_level"),
        "kind": kind,
        "kind_label": KIND_LABELS[kind],
    }
    for key in ("bbox", "iso", "parent_name", "parent_osm_id", "parent_iso"):
        if row.get(key) not in (None, ""):
            hit[key] = row[key]
    return hit


def area_index() -> list[dict]:
    """Names and OSM ids from the prebuilt country and subdivision-1 files."""
    global _AREA_INDEX
    if _AREA_INDEX is not None:
        return _AREA_INDEX
    rows: list[dict] = []
    country_by_iso: dict[str, str] = {}
    country_by_osm: dict[int, str] = {}

    countries = DATA_DIR / "countries.geojson"
    if countries.exists():
        for feature in json.loads(countries.read_bytes()).get("features") or []:
            props = feature.get("properties") or {}
            name = props.get("name")
            osm_id = props.get("osm_id")
            if not name or osm_id is None:
                continue
            osm_id = int(osm_id)
            iso = (props.get("iso") or "").upper()
            country_by_osm[osm_id] = name
            if iso:
                country_by_iso[iso] = name
            row = {
                "name": name,
                "osm_id": osm_id,
                "admin_level": str(props.get("admin_level") or "2"),
                "kind": "country",
                "iso": iso,
                "key": norm_name(name),
                "iso_key": norm_name(iso),
            }
            bbox = geometry_bbox(feature.get("geometry"))
            if bbox:
                row["bbox"] = bbox
            rows.append(row)

    admin1 = DATA_DIR / "admin1"
    if admin1.exists():
        for path in admin1.glob("*.geojson"):
            try:
                payload = json.loads(path.read_bytes())
            except (OSError, json.JSONDecodeError):
                continue
            for feature in payload.get("features") or []:
                props = feature.get("properties") or {}
                name = props.get("name")
                osm_id = props.get("osm_id")
                if not name or osm_id is None:
                    continue
                parent_iso = (props.get("parent_iso") or "").upper()
                parent_osm_id = props.get("parent_osm_id")
                parent_name = (
                    country_by_iso.get(parent_iso)
                    or country_by_osm.get(int(parent_osm_id) if parent_osm_id else 0)
                    or ""
                )
                row = {
                    "name": name,
                    "osm_id": int(osm_id),
                    "admin_level": str(props.get("admin_level") or "4"),
                    "kind": "subdivision_1",
                    "iso": props.get("iso") or "",
                    "parent_iso": parent_iso,
                    "parent_osm_id": int(parent_osm_id) if parent_osm_id else None,
                    "parent_name": parent_name,
                    "key": norm_name(name),
                    "iso_key": norm_name(props.get("iso") or ""),
                }
                bbox = geometry_bbox(feature.get("geometry"))
                if bbox:
                    row["bbox"] = bbox
                rows.append(row)
    _AREA_INDEX = rows
    return rows


def search_local_areas(query: str) -> list[dict]:
    key = norm_name(query)
    if not key:
        return []
    scored = []
    for row in area_index():
        name_key = row.get("key") or ""
        iso_key = row.get("iso_key") or ""
        parent_key = norm_name(row.get("parent_name") or "")
        if name_key == key or iso_key == key:
            rank = 0
        elif name_key.startswith(key) or iso_key.startswith(key):
            rank = 1
        elif len(key) >= 4 and name_key.endswith(key):
            rank = 2
        elif len(key) >= 5 and key in name_key:
            rank = 3
        elif parent_key and f"{name_key},{parent_key}".startswith(key):
            rank = 4
        else:
            continue
        kind_rank = {"country": 0, "subdivision_1": 1, "subdivision_2": 2}.get(row.get("kind"), 9)
        scored.append((rank, kind_rank, len(name_key), row["name"], row["osm_id"], row))
    scored.sort(key=lambda item: item[:5])
    hits = []
    seen = set()
    for _, _, _, _, _, row in scored:
        if row["osm_id"] in seen:
            continue
        seen.add(row["osm_id"])
        hits.append(_hit_from_row(row))
        if len(hits) >= 10:
            break
    return hits


def search_nominatim(query: str) -> list[dict]:
    params = urllib.parse.urlencode(
        {
            "q": query,
            "format": "json",
            "limit": 8,
            "extratags": 1,
            "addressdetails": 1,
            "class": "boundary",
        }
    )
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}", headers={"User-Agent": USER_AGENT}
    )
    try:
        with urllib.request.urlopen(req, timeout=12, context=SSL_CONTEXT) as resp:
            results = json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return []
    hits = []
    seen = set()
    for item in results:
        if item.get("osm_type") != "relation":
            continue
        osm_id = int(item["osm_id"])
        if osm_id in seen:
            continue
        seen.add(osm_id)
        extras = item.get("extratags") or {}
        address = item.get("address") or {}
        kind = area_kind(extras.get("admin_level"))
        country_name = address.get("country") or ""
        region_name = (
            address.get("state")
            or address.get("region")
            or address.get("county")
            or ""
        )
        if kind == "country":
            parent_name = ""
        elif kind == "subdivision_1":
            parent_name = country_name
        else:
            parent_name = region_name or country_name
        hit = {
            "name": (item.get("display_name") or query).split(",")[0],
            "osm_id": osm_id,
            "admin_level": extras.get("admin_level"),
            "kind": kind,
            "kind_label": KIND_LABELS[kind],
        }
        if parent_name:
            hit["parent_name"] = parent_name
        if country_name and kind != "country":
            hit["country_name"] = country_name
        cc = (address.get("country_code") or "").upper()
        if cc and kind != "country":
            hit["country_iso"] = cc
        bbox = item.get("boundingbox")
        if bbox and len(bbox) == 4:
            try:
                south, north, west, east = (float(v) for v in bbox)
                hit["bbox"] = [west, south, east, north]
            except (TypeError, ValueError):
                pass
        hits.append(hit)
    return hits


def _score_hit(hit: dict, key: str) -> tuple:
    name_key = norm_name(hit.get("name") or "")
    iso_key = norm_name(str(hit.get("iso") or ""))
    if name_key == key or iso_key == key:
        rank = 0
    elif name_key.startswith(key) or iso_key.startswith(key):
        rank = 1
    elif len(key) >= 4 and name_key.endswith(key):
        rank = 2
    elif len(key) >= 5 and key in name_key:
        rank = 3
    else:
        rank = 4
    kind_rank = {"country": 0, "subdivision_1": 1, "subdivision_2": 2}.get(hit.get("kind"), 9)
    return (rank, kind_rank, len(name_key), hit.get("name") or "")


def search_areas(query: str) -> list[dict]:
    """Local files first (instant); Nominatim fills gaps and exact names like Wales."""
    key = norm_name(query)
    if not key:
        return []
    local = search_local_areas(query)
    if local and _score_hit(local[0], key)[0] <= 1:
        return local
    remote = search_nominatim(query) if len(key) >= 3 else []
    merged: list[dict] = []
    seen = set()
    for hit in local + remote:
        if hit["osm_id"] in seen:
            continue
        seen.add(hit["osm_id"])
        merged.append(hit)
    merged.sort(key=lambda hit: _score_hit(hit, key))
    return merged[:10]


def discover_datasets() -> list[dict]:
    """List data/*.json region files, largest last so the log reads naturally."""
    found = []
    for path in sorted(DATA_DIR.glob("*.json")):
        if path.name in NON_DATASET_FILES:
            continue
        found.append(
            {
                "name": path.stem,
                "url": f"data/{path.name}",
                "bytes": path.stat().st_size,
            }
        )
    return found


def run_overpass(query: str, wait_s: float | None = None) -> tuple[bytes, bool]:
    """Return (response bytes, cache_hit). Race mirrors; first good answer wins."""
    cached = CACHE_DIR / (hashlib.sha1(query.encode("utf-8")).hexdigest() + ".json")
    if cached.exists():
        return cached.read_bytes(), True

    wait = OVERPASS_TIMEOUT_S + 5 if wait_s is None else wait_s
    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    problems: list[str] = []
    winner: dict[str, bytes] = {}
    lock = threading.Lock()
    done = threading.Event()

    def worker(url: str) -> None:
        if done.is_set():
            return
        req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=wait, context=SSL_CONTEXT) as resp:
                payload = resp.read()
            parsed = json.loads(payload)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            with lock:
                problems.append(f"{url}: {e}")
            return
        if "error" in str(parsed.get("remark", "")).lower():
            with lock:
                problems.append(f"{url}: {str(parsed.get('remark', ''))[:120]}")
            return
        with lock:
            if "payload" not in winner:
                winner["payload"] = payload
                done.set()

    threads = [
        threading.Thread(target=worker, args=(url,), daemon=True) for url in OVERPASS_URLS
    ]
    for thread in threads:
        thread.start()
    done.wait(timeout=wait + 2)
    payload = winner.get("payload")
    if not payload:
        raise RuntimeError("; ".join(problems) or "all Overpass endpoints failed")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(payload)
    return payload, False


def osm_get_json(url: str) -> dict:
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with _OSM_GATE:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                with urllib.request.urlopen(req, timeout=60, context=SSL_CONTEXT) as resp:
                    return json.loads(resp.read())
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            last_error = e
            time.sleep(0.8 * (attempt + 1))
    raise last_error or RuntimeError(f"OSM request failed: {url}")


def _write_relation_cache(osm_id: int, element: dict) -> None:
    RELATION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (RELATION_CACHE_DIR / f"{osm_id}.json").write_text(
        json.dumps({"elements": [element]}), encoding="utf-8"
    )


def cached_osm_relation(osm_id: int) -> dict | None:
    path = RELATION_CACHE_DIR / f"{osm_id}.json"
    if path.exists():
        try:
            data = json.loads(path.read_bytes())
        except json.JSONDecodeError:
            return None
        els = data.get("elements") or []
        return next((el for el in els if el.get("type") == "relation"), None)
    try:
        data = osm_get_json(f"https://api.openstreetmap.org/api/0.6/relation/{osm_id}.json")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None
    el = next((e for e in (data.get("elements") or []) if e.get("type") == "relation"), None)
    if el:
        _write_relation_cache(osm_id, el)
    return el


def cached_osm_relations(ids: list[int]) -> list[dict]:
    found: dict[int, dict] = {}
    missing: list[int] = []
    for osm_id in ids:
        path = RELATION_CACHE_DIR / f"{osm_id}.json"
        el = None
        if path.exists():
            try:
                data = json.loads(path.read_bytes())
                el = next(
                    (e for e in (data.get("elements") or []) if e.get("type") == "relation"),
                    None,
                )
            except json.JSONDecodeError:
                el = None
        if el:
            found[osm_id] = el
        else:
            missing.append(osm_id)
    if missing:
        workers = min(4, len(missing))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for el in pool.map(cached_osm_relation, missing):
                if el:
                    found[int(el["id"])] = el
    return [found[i] for i in ids if i in found]


def relation_member_records(parent_id: int) -> list[dict]:
    """Child admin areas listed as members of an OSM relation — no geometry."""
    parent = cached_osm_relation(parent_id)
    if not parent:
        return []
    ordered: list[int] = []
    roles: dict[int, str] = {}
    seen: set[int] = set()
    for member in parent.get("members") or []:
        if member.get("type") != "relation":
            continue
        if member.get("role") in ("inner", "outer"):
            continue
        child_id = int(member["ref"])
        if child_id == parent_id or child_id in seen:
            continue
        seen.add(child_id)
        ordered.append(child_id)
        roles[child_id] = member.get("role") or ""
        if len(ordered) >= 500:
            break
    recs = overpass_tags_for_ids(ordered)
    if len(recs) >= 2:
        return recs
    records = []
    for child in cached_osm_relations(ordered):
        tags = child.get("tags") or {}
        if tags.get("end_date") or tags.get("historic"):
            continue
        name = tags.get("name:en") or tags.get("name")
        level = tags.get("admin_level")
        if not name:
            continue
        if tags.get("boundary") and tags.get("boundary") != "administrative":
            continue
        records.append(
            {
                "osm_id": child["id"],
                "name": name,
                "admin_level": str(level) if level else "",
                "role": roles.get(child["id"], ""),
            }
        )
    return records


def overpass_tags_for_ids(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    query = f"""
    [out:json][timeout:40];
    rel(id:{",".join(str(i) for i in ids)});
    out tags;
    """
    try:
        payload, _ = run_overpass(query)
        data = json.loads(payload)
    except (RuntimeError, json.JSONDecodeError, TypeError, ValueError):
        return []
    recs = []
    by_id = {}
    for el in data.get("elements") or []:
        if el.get("type") != "relation":
            continue
        tags = el.get("tags") or {}
        if tags.get("end_date") or tags.get("historic"):
            continue
        name = tags.get("name:en") or tags.get("name")
        if not name:
            continue
        if tags.get("boundary") and tags.get("boundary") != "administrative":
            continue
        rec = {
            "osm_id": int(el["id"]),
            "name": name,
            "admin_level": str(tags.get("admin_level") or ""),
        }
        by_id[rec["osm_id"]] = rec
    for osm_id in ids:
        if osm_id in by_id:
            recs.append(by_id[osm_id])
    return recs


def wikidata_entities(ids: list[str]) -> dict:
    out: dict = {}
    for offset in range(0, len(ids), 50):
        chunk = ids[offset : offset + 50]
        qs = urllib.parse.urlencode(
            {
                "action": "wbgetentities",
                "ids": "|".join(chunk),
                "props": "claims|labels",
                "languages": "en",
                "format": "json",
            }
        )
        try:
            data = osm_get_json(f"https://www.wikidata.org/w/api.php?{qs}")
        except (urllib.error.URLError, OSError, json.JSONDecodeError, RuntimeError, ValueError):
            continue
        out.update(data.get("entities") or {})
    return out


def wikidata_child_records(parent_tags: dict | None) -> list[dict]:
    """Admin children via Wikidata P150, used when OSM lists no subareas."""
    qid = (parent_tags or {}).get("wikidata")
    if not qid or not re.fullmatch(r"Q[0-9]+", qid):
        return []
    parent = wikidata_entities([qid]).get(qid) or {}
    child_ids = []
    for claim in (parent.get("claims") or {}).get("P150") or []:
        datavalue = ((claim.get("mainsnak") or {}).get("datavalue") or {}).get("value") or {}
        child_id = datavalue.get("id")
        if child_id:
            child_ids.append(child_id)
    if len(child_ids) < 2:
        return []
    children = wikidata_entities(child_ids)
    recs = []
    seen: set[int] = set()
    for child_id in child_ids:
        ent = children.get(child_id) or {}
        p402 = (ent.get("claims") or {}).get("P402") or []
        osm_raw = (
            ((p402[0].get("mainsnak") or {}).get("datavalue") or {}).get("value") if p402 else None
        )
        if not osm_raw or not str(osm_raw).isdigit():
            continue
        osm_id = int(osm_raw)
        if osm_id in seen:
            continue
        name = ((ent.get("labels") or {}).get("en") or {}).get("value")
        if not name:
            continue
        seen.add(osm_id)
        recs.append({"osm_id": osm_id, "name": name, "admin_level": ""})
    return recs


def overpass_child_records(parent_id: int, parent_level: int, parent_tags: dict | None = None) -> list[dict]:
    """Tags-only Overpass fallback when the OSM relation lists no subareas."""
    levels = child_levels_to_try(parent_level)
    if not levels:
        return []
    pattern = "|".join(str(level) for level in levels)
    iso = (parent_tags or {}).get("ISO3166-2") or (parent_tags or {}).get("ISO3166-1")
    if iso and re.fullmatch(r"[A-Z]{2}(-[A-Z0-9]{1,3})?", iso):
        key = "ISO3166-2" if "-" in iso else "ISO3166-1"
        query = f"""
        [out:json][timeout:25];
        area["{key}"="{iso}"]["admin_level"="{parent_level or 4}"];
        rel(area)["type"="boundary"]["boundary"="administrative"]["admin_level"~"^({pattern})$"]["name"];
        out tags;
        """
    else:
        query = f"""
        [out:json][timeout:25];
        rel({parent_id});
        map_to_area -> .parent;
        rel(area.parent)["type"="boundary"]["boundary"="administrative"]["admin_level"~"^({pattern})$"]["name"];
        out tags;
        """
    try:
        payload, _ = run_overpass(query, wait_s=28)
        data = json.loads(payload)
    except (RuntimeError, json.JSONDecodeError, TypeError, ValueError):
        return []
    recs = []
    seen = set()
    for el in data.get("elements") or []:
        if el.get("type") != "relation" or el.get("id") == parent_id:
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
        recs.append(
            {
                "osm_id": osm_id,
                "name": name,
                "admin_level": str(tags.get("admin_level") or ""),
            }
        )
    return select_child_records(recs, parent_level)


def _child_lock(parent_id: int) -> threading.Lock:
    with _CHILD_LOCKS_GUARD:
        return _CHILD_LOCKS.setdefault(parent_id, threading.Lock())


def _cached_children_ok(members) -> bool:
    if not isinstance(members, list) or len(members) < 2:
        return False
    labeled = sum(1 for rec in members if str(rec.get("admin_level") or "").isdigit())
    # Wikidata P150 lists have no admin_level and are often a coarse 10–20
    # regions. Reject those so Overpass can supply county-level children.
    if labeled < max(2, len(members) // 2) and len(members) < 20:
        return False
    return True


def choose_child_records(parent_id: int) -> tuple[dict, list[dict]]:
    """Immediate admin children of a relation, grouped as the next subdivision."""
    cached = _CHILD_MEMO.get(parent_id)
    if cached and _cached_children_ok(cached[1]):
        return cached
    path = SUBDIV_CACHE_DIR / f"{parent_id}.json"
    if path.exists():
        try:
            payload = json.loads(path.read_bytes())
            result = (payload["parent"], payload["members"])
            if _cached_children_ok(result[1]):
                _CHILD_MEMO[parent_id] = result
                return result
        except (json.JSONDecodeError, KeyError, TypeError):
            pass
        path.unlink(missing_ok=True)
    with _child_lock(parent_id):
        cached = _CHILD_MEMO.get(parent_id)
        if cached and _cached_children_ok(cached[1]):
            return cached
        if path.exists():
            try:
                payload = json.loads(path.read_bytes())
                result = (payload["parent"], payload["members"])
                if _cached_children_ok(result[1]):
                    _CHILD_MEMO[parent_id] = result
                    return result
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
            path.unlink(missing_ok=True)
        parent = cached_osm_relation(parent_id) or {}
        tags = parent.get("tags") or {}
        parent_info = {
            "osm_id": parent_id,
            "name": tags.get("name:en") or tags.get("name") or f"relation/{parent_id}",
            "admin_level": tags.get("admin_level"),
            "kind": area_kind(tags.get("admin_level"), "subdivision_1"),
        }
        records = relation_member_records(parent_id)
        parent_level = int(tags["admin_level"]) if str(tags.get("admin_level") or "").isdigit() else 0
        chosen = select_child_records(records, parent_level)
        # Wikidata P150 is often a coarse "contains" list (Quebec's 17 regions).
        # Prefer OSM/Overpass county-level children when those exist.
        if len(chosen) < 20:
            denser = overpass_child_records(parent_id, parent_level, tags)
            if len(denser) > max(len(chosen), 2):
                chosen = denser
        if len(chosen) < 2:
            chosen = wikidata_child_records(tags)
        child_kind = "subdivision_1" if parent_info["kind"] == "country" else "subdivision_2"
        for rec in chosen:
            rec["kind"] = child_kind
            rec["kind_label"] = KIND_LABELS[child_kind]
            rec["parent_osm_id"] = parent_id
            rec["parent_name"] = parent_info["name"]
        parent_info["child_kind"] = child_kind
        parent_info["kind_label"] = KIND_LABELS[parent_info["kind"]]
        result = (parent_info, chosen)
        if chosen:
            SUBDIV_CACHE_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"parent": parent_info, "members": chosen}), encoding="utf-8")
            _CHILD_MEMO[parent_id] = result
        return result


def _coords_equal(a, b) -> bool:
    return abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9


def _merge_ways_to_rings(ways: list[list[list[float]]]) -> list[list[list[float]]]:
    segs = [way[:] for way in ways if len(way) >= 2]
    rings: list[list[list[float]]] = []
    while segs:
        ring = segs.pop()
        closed = _coords_equal(ring[0], ring[-1])
        grew = True
        while not closed and grew:
            grew = False
            for i in range(len(segs) - 1, -1, -1):
                seg = segs[i]
                if _coords_equal(ring[-1], seg[0]):
                    ring.extend(seg[1:])
                elif _coords_equal(ring[-1], seg[-1]):
                    ring.extend(reversed(seg[:-1]))
                elif _coords_equal(ring[0], seg[-1]):
                    ring[0:0] = seg[:-1]
                elif _coords_equal(ring[0], seg[0]):
                    ring[0:0] = list(reversed(seg[1:]))
                else:
                    continue
                segs.pop(i)
                grew = True
                closed = _coords_equal(ring[0], ring[-1])
                break
        if len(ring) >= 4:
            if not _coords_equal(ring[0], ring[-1]):
                ring.append(ring[0])
            rings.append(ring)
    return rings


def _point_in_ring(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


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


def _simplify_ring(ring: list[list[float]], max_pts: int = 180, tol: float = 0.003) -> list[list[float]]:
    pts = rdp(ring, tol) if len(ring) > 4 else list(ring)
    if len(pts) > max_pts:
        stride = (len(pts) - 1) / (max_pts - 1)
        pts = [pts[int(i * stride)] for i in range(max_pts - 1)]
        pts.append(pts[0] if pts else ring[0])
    pts = [[round(p[0], 4), round(p[1], 4)] for p in pts]
    deduped = [p for i, p in enumerate(pts) if i == 0 or p != pts[i - 1]]
    if len(deduped) < 4:
        return [[round(p[0], 4), round(p[1], 4)] for p in ring[:4]]
    if deduped[0] != deduped[-1]:
        deduped.append(deduped[0])
    return deduped


def _feature_from_rings(outers, inners, osm_id: int, tags: dict) -> dict | None:
    outer_rings = _merge_ways_to_rings(outers)
    inner_rings = _merge_ways_to_rings(inners)
    if not outer_rings:
        return None
    leftover = inner_rings[:]
    polygons = []
    for outer in outer_rings:
        holes = [hole for hole in leftover if hole and _point_in_ring(hole[0][0], hole[0][1], outer)]
        for hole in holes:
            leftover.remove(hole)
        polygons.append(
            [_simplify_ring(outer, 180), *(_simplify_ring(hole, 48) for hole in holes)]
        )
    geometry = (
        {"type": "Polygon", "coordinates": polygons[0]}
        if len(polygons) == 1
        else {"type": "MultiPolygon", "coordinates": polygons}
    )
    kind = area_kind(tags.get("admin_level"))
    return {
        "type": "Feature",
        "properties": {
            "osm_type": "relation",
            "osm_id": int(osm_id),
            "name": tags.get("name:en") or tags.get("name") or f"relation/{osm_id}",
            "admin_level": tags.get("admin_level"),
            "kind": kind,
        },
        "geometry": geometry,
    }


def overpass_relation_to_feature(el: dict) -> dict | None:
    if el.get("type") != "relation":
        return None
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
    return _feature_from_rings(outers, inners, int(el["id"]), el.get("tags") or {})


def osm_full_to_feature(data: dict, osm_id: int) -> dict | None:
    nodes: dict[int, list[float]] = {}
    ways: dict[int, list[list[float]]] = {}
    rels: dict[int, dict] = {}
    for el in data.get("elements") or []:
        kind = el.get("type")
        if kind == "node" and "lat" in el and "lon" in el:
            nodes[int(el["id"])] = [float(el["lon"]), float(el["lat"])]
        elif kind == "way":
            coords = [nodes[int(nid)] for nid in el.get("nodes") or [] if int(nid) in nodes]
            if len(coords) >= 2:
                ways[int(el["id"])] = coords
        elif kind == "relation":
            rels[int(el["id"])] = el
    rel = rels.get(int(osm_id))
    if not rel:
        return None
    visited: set[int] = set()

    def collect(rel_el: dict, outers: list, inners: list) -> None:
        rid = int(rel_el.get("id") or 0)
        if rid in visited:
            return
        visited.add(rid)
        for member in rel_el.get("members") or []:
            role = member.get("role") or ""
            ref = int(member.get("ref") or 0)
            typ = member.get("type")
            if typ == "way" and ref in ways:
                (inners if role == "inner" else outers).append(ways[ref])
            elif typ == "relation" and ref in rels and role in ("inner", "outer", ""):
                sub = rels[ref]
                sub_tags = sub.get("tags") or {}
                if sub_tags.get("boundary") == "administrative" and role not in ("inner", "outer"):
                    continue
                collect(sub, outers, inners)

    outers: list[list[list[float]]] = []
    inners: list[list[list[float]]] = []
    collect(rel, outers, inners)
    outer_rings = _merge_ways_to_rings(outers)
    inner_rings = _merge_ways_to_rings(inners)
    if not outer_rings:
        return None
    leftover = inner_rings[:]
    polygons = []
    for outer in outer_rings:
        holes = [hole for hole in leftover if hole and _point_in_ring(hole[0][0], hole[0][1], outer)]
        for hole in holes:
            leftover.remove(hole)
        polygons.append(
            [_simplify_ring(outer, 180), *(_simplify_ring(hole, 48) for hole in holes)]
        )
    geometry = (
        {"type": "Polygon", "coordinates": polygons[0]}
        if len(polygons) == 1
        else {"type": "MultiPolygon", "coordinates": polygons}
    )
    tags = rel.get("tags") or {}
    kind = area_kind(tags.get("admin_level"))
    return {
        "type": "Feature",
        "properties": {
            "osm_type": "relation",
            "osm_id": int(osm_id),
            "name": tags.get("name:en") or tags.get("name") or f"relation/{osm_id}",
            "admin_level": tags.get("admin_level"),
            "kind": kind,
        },
        "geometry": geometry,
    }


def cached_relation_feature(osm_id: int) -> dict | None:
    GEOJSON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = GEOJSON_CACHE_DIR / f"{osm_id}.json"
    if path.exists():
        try:
            return json.loads(path.read_bytes())
        except json.JSONDecodeError:
            path.unlink(missing_ok=True)
    try:
        payload = json.loads(fetch_osm_relation_full(osm_id))
        feature = osm_full_to_feature(payload, osm_id)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, RuntimeError, ValueError):
        return None
    if not feature:
        return None
    path.write_text(json.dumps(feature), encoding="utf-8")
    return feature


OVERPASS_GEOM_BATCH = 8
_OVERPASS_GEOM_FAILS = 0


def relation_features(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    global _OVERPASS_GEOM_FAILS
    found: dict[int, dict] = {}
    missing: list[int] = []
    GEOJSON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for osm_id in ids:
        path = GEOJSON_CACHE_DIR / f"{osm_id}.json"
        if path.exists():
            try:
                found[osm_id] = json.loads(path.read_bytes())
                continue
            except json.JSONDecodeError:
                path.unlink(missing_ok=True)
        missing.append(osm_id)
    if missing:
        workers = min(8, len(missing))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for osm_id, feature in zip(missing, pool.map(cached_relation_feature, missing)):
                if feature:
                    found[osm_id] = feature
    still = [i for i in missing if i not in found]
    if still and _OVERPASS_GEOM_FAILS < 2:
        query = f"""
        [out:json][timeout:12];
        rel(id:{",".join(str(i) for i in still[:OVERPASS_GEOM_BATCH])});
        out geom qt;
        """
        try:
            payload, _ = run_overpass(query, wait_s=14)
            data = json.loads(payload)
            got_any = False
            for el in data.get("elements") or []:
                feature = overpass_relation_to_feature(el)
                if not feature:
                    continue
                osm_id = int(el["id"])
                found[osm_id] = feature
                got_any = True
                (GEOJSON_CACHE_DIR / f"{osm_id}.json").write_text(
                    json.dumps(feature), encoding="utf-8"
                )
            _OVERPASS_GEOM_FAILS = 0 if got_any else _OVERPASS_GEOM_FAILS + 1
        except (RuntimeError, json.JSONDecodeError, TypeError, ValueError):
            _OVERPASS_GEOM_FAILS += 1
    return [found[i] for i in ids if i in found]


def prebuilt_admin2(osm_id: int) -> dict | None:
    path = ADMIN2_DIR / f"{osm_id}.geojson"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return None
    features = payload.get("features") or []
    if len(features) < 2:
        return None
    parent = payload.get("parent") or {
        "osm_id": osm_id,
        "kind": "subdivision_1",
        "child_kind": "subdivision_2",
    }
    parent["child_kind"] = parent.get("child_kind") or "subdivision_2"
    members = []
    for feat in features:
        props = feat.get("properties") or {}
        if props.get("osm_id") is None:
            continue
        members.append(
            {
                "osm_id": int(props["osm_id"]),
                "name": props.get("name") or "",
                "admin_level": str(props.get("admin_level") or ""),
                "kind": "subdivision_2",
                "kind_label": KIND_LABELS["subdivision_2"],
                "parent_osm_id": osm_id,
                "parent_name": parent.get("name") or props.get("parent_name") or "",
            }
        )
        feat.setdefault("properties", {})["kind"] = "subdivision_2"
    if len(members) < 2:
        return None
    return {
        "parent": parent,
        "kind": "subdivision_2",
        "kind_label": KIND_LABELS["subdivision_2"],
        "members": members,
        "features": features,
    }


def save_admin2(osm_id: int, parent: dict, members: list[dict], features: list[dict]) -> None:
    if len(features) < 2 or len(features) < len(members) * 0.8:
        return
    ADMIN2_DIR.mkdir(parents=True, exist_ok=True)
    path = ADMIN2_DIR / f"{osm_id}.geojson"
    payload = {
        "type": "FeatureCollection",
        "parent": parent,
        "features": features,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    index_path = ADMIN2_DIR / "index.json"
    try:
        index = json.loads(index_path.read_bytes()) if index_path.exists() else {}
    except json.JSONDecodeError:
        index = {}
    index[str(osm_id)] = {
        "url": f"data/admin2/{path.name}",
        "count": len(features),
        "name": parent.get("name") or "",
        "kind": "subdivision_2",
    }
    index_path.write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")


def subdivisions_payload(osm_id: int, with_geom: bool = False) -> dict:
    prebuilt = prebuilt_admin2(osm_id)
    if prebuilt:
        return prebuilt
    # No live OSM/Overpass for subdivision 2. Country → subdivision 1 is
    # served from data/admin1 by the viewer; anything else without a baked
    # file is a leaf.
    return {
        "parent": {"osm_id": osm_id, "kind": "subdivision_1", "child_kind": "subdivision_2"},
        "kind": "subdivision_2",
        "kind_label": KIND_LABELS["subdivision_2"],
        "members": [],
        "features": [],
    }


def fetch_osm_relation_full(osm_id: int) -> bytes:
    """OSM API full relation (ways+nodes), cached. Used instead of Overpass out geom."""
    path = FULL_CACHE_DIR / f"{osm_id}.json"
    if path.exists():
        return path.read_bytes()
    data = osm_get_json(f"https://api.openstreetmap.org/api/0.6/relation/{osm_id}/full.json")
    payload = json.dumps(data).encode("utf-8")
    FULL_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        route = self.path.split("?")[0]
        if route == "/api/datasets":
            self._send_json(discover_datasets())
            return
        if route == "/api/search":
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("q", [""])[0]
            self._send_json(search_areas(query.strip()))
            return
        if route == "/api/subdivisions":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            raw = (qs.get("id") or [""])[0]
            try:
                osm_id = int(raw)
            except ValueError:
                self._send_json({"error": "id must be an integer"}, status=400)
                return
            if osm_id <= 0:
                self._send_json({"error": "id must be a positive OSM relation id"}, status=400)
                return
            with_geom = (qs.get("geom") or ["0"])[0] in ("1", "true", "yes")
            try:
                self._send_json(subdivisions_payload(osm_id, with_geom=with_geom))
            except RuntimeError as e:
                self._send_json({"error": str(e)}, status=502)
            return
        if route == "/api/geojson":
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            raw_ids = (qs.get("ids") or qs.get("id") or [""])[0]
            try:
                ids = [int(part) for part in raw_ids.split(",") if part.strip()]
            except ValueError:
                self._send_json({"error": "ids must be integers"}, status=400)
                return
            ids = [i for i in ids if i > 0][:250]
            if not ids:
                self._send_json({"type": "FeatureCollection", "features": []})
                return
            try:
                features = relation_features(ids)
            except RuntimeError as e:
                self._send_json({"error": str(e)}, status=502)
                return
            self._send_json({"type": "FeatureCollection", "features": features})
            return
        if route == "/api/relation-members":
            raw = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
            try:
                osm_id = int(raw)
            except ValueError:
                self._send_json({"error": "id must be an integer"}, status=400)
                return
            if osm_id <= 0:
                self._send_json({"error": "id must be a positive OSM relation id"}, status=400)
                return
            try:
                _, members = choose_child_records(osm_id)
                self._send_json(members)
            except RuntimeError as e:
                self._send_json({"error": str(e)}, status=502)
            return
        if route == "/api/relation-full":
            raw = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("id", [""])[0]
            try:
                osm_id = int(raw)
            except ValueError:
                self._send_json({"error": "id must be an integer"}, status=400)
                return
            if osm_id <= 0:
                self._send_json({"error": "id must be a positive OSM relation id"}, status=400)
                return
            try:
                self._send_raw(fetch_osm_relation_full(osm_id), "application/json")
            except (urllib.error.URLError, OSError, json.JSONDecodeError, RuntimeError) as e:
                self._send_json({"error": str(e)}, status=502)
            return
        if route in ("/", "/index.html"):
            self.path = "/" + VIEWER
        super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] != "/api/overpass":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        query = self.rfile.read(length).decode("utf-8")
        try:
            payload, hit = run_overpass(query)
        except RuntimeError as e:
            self._send_json({"error": str(e)}, status=502)
            return
        self._send_raw(payload, "application/json", {"X-Overpass-Cache": "hit" if hit else "miss"})

    def _send_json(self, payload, status: int = 200) -> None:
        self._send_raw(json.dumps(payload).encode("utf-8"), "application/json", status=status)

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _send_raw(self, body: bytes, content_type: str, extra=None, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # the per-tile and per-dataset request log drowns out the startup banner


def bind(port: int, tries: int = 20) -> http.server.ThreadingHTTPServer:
    """Take the requested port, or the next free one if something else has it."""
    for offset in range(tries):
        try:
            return http.server.ThreadingHTTPServer(("127.0.0.1", port + offset), Handler)
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
    raise SystemExit(f"No free port in {port}-{port + tries - 1}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not (ROOT / VIEWER).exists():
        raise SystemExit(f"Missing {VIEWER} next to serve.py.")

    datasets = discover_datasets()
    if not datasets:
        print(f"Warning: no datasets found in {DATA_DIR}")
    for d in datasets:
        print(f"  {d['name']:<16} {d['bytes'] / 1e6:6.1f} MB  {d['url']}")

    threading.Thread(target=area_index, daemon=True).start()

    httpd = bind(args.port)
    url = "http://{}:{}/".format(*httpd.socket.getsockname())
    print(f"\nLinguaMap viewer: {url}\nPress Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
