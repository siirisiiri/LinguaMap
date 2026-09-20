#!/usr/bin/env python3
"""
osm_business_websites.py

Given an area of interest (place name), this script:
  1. Geocodes the place name to a bounding box using Nominatim.
  2. Downloads the matching Geofabrik OSM PBF (daily extract) if needed.
  3. Reads every node/way with a `website` or `contact:website` tag.
  4. Writes the results (name, website, category, lat/lon) to a JSON file.

PBF extracts skip OSM relations (routes, some schools/museums mapped as
multipolygons). Use --overpass for a live tiled Overpass fetch that includes
them. --by-area only applies to --overpass.

Usage:
    python osm_business_websites.py "Switzerland" -o data/Switzerland.json
    python osm_business_websites.py "Cambridge, MA"
    python osm_business_websites.py "Ukraine" --overpass --by-area -o data/ukraine.json
    python osm_business_websites.py --from-pbf .geofabrik/switzerland-latest.osm.pbf -o data/Switzerland.json
"""

import argparse
import json
import math
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOFABRIK_INDEX_URL = "https://download.geofabrik.de/index-v1.json"
GEOFABRIK_DIR = ".geofabrik"
CONTINENT_IDS = {
    "africa", "antarctica", "asia", "australia-oceania",
    "central-america", "europe", "north-america", "south-america",
}
OVERPASS_URL = "https://overpass-api.de/api/interpreter"
# Rotated through on failure; the first two tolerate heavy queries best.
OVERPASS_URLS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
TILE_TIMEOUT = 180
MIN_TILE_DEG = 0.25
TILE_PAUSE_S = 2.0
CONNECT_TIMEOUT = 30
OVERPASS_ROUNDS = 3
GIVE_UP_RETRIES = 4
# Tiles are cached so a country-sized run can be interrupted and resumed.
TILE_CACHE_DIR = ".osm_tile_cache"

_print_lock = threading.Lock()


def log(message: str) -> None:
    with _print_lock:
        print(message, flush=True)

# Required by Nominatim's usage policy: identify your app with a real
# contact so they can reach you if something goes wrong.
HEADERS = {"User-Agent": "osm-business-website-finder/1.0 (contact: YOUR_EMAIL@example.com)"}

# Tag keys checked (in priority order) to label what an element *is*.
# This is just for a human-readable "category" field in the output — it no
# longer filters anything out.
CATEGORY_TAG_KEYS = [
    "shop", "amenity", "office", "craft", "tourism", "leisure", "healthcare",
    "government", "religion", "historic", "natural", "man_made", "landuse",
    "building",
]


def record_from_tags(osm_type, osm_id, tags, lat, lon):
    """Build the output dict from OSM tags + a point. Shared by Overpass and PBF."""
    website = tags.get("website") or tags.get("contact:website")
    if not website or lat is None or lon is None:
        return None
    matched_key = next((k for k in CATEGORY_TAG_KEYS if k in tags), None)
    category = f"{matched_key}={tags[matched_key]}" if matched_key else "unclassified"
    return {
        "name": tags.get("name", "(unnamed)"),
        "website": website,
        "category": category,
        "lat": lat,
        "lon": lon,
        "osm_type": osm_type,
        "osm_id": osm_id,
    }


def _norm_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def in_bbox(lat, lon, bbox) -> bool:
    if bbox is None or lat is None or lon is None:
        return True
    south, west, north, east = bbox
    return south <= lat <= north and west <= lon <= east


def extract_from_pbf(path: str, bbox=None):
    """Read website-tagged nodes/ways from a Geofabrik (or other) OSM PBF."""
    try:
        import osmium
    except ImportError:
        sys.exit("Reading a PBF needs pyosmium. Install with: pip install osmium")

    class WebsiteHandler(osmium.SimpleHandler):
        def __init__(self):
            super().__init__()
            self.records = []

        def node(self, n):
            if not n.location.valid():
                return
            if "website" not in n.tags and "contact:website" not in n.tags:
                return
            lat, lon = n.location.lat, n.location.lon
            if not in_bbox(lat, lon, bbox):
                return
            tags = {t.k: t.v for t in n.tags}
            rec = record_from_tags("node", n.id, tags, lat, lon)
            if rec:
                self.records.append(rec)

        def way(self, w):
            if "website" not in w.tags and "contact:website" not in w.tags:
                return
            lats = []
            lons = []
            try:
                for node in w.nodes:
                    if node.location.valid():
                        lats.append(node.location.lat)
                        lons.append(node.location.lon)
            except Exception:
                return
            if not lats:
                return
            lat, lon = sum(lats) / len(lats), sum(lons) / len(lons)
            if not in_bbox(lat, lon, bbox):
                return
            tags = {t.k: t.v for t in w.tags}
            rec = record_from_tags("way", w.id, tags, lat, lon)
            if rec:
                self.records.append(rec)

    handler = WebsiteHandler()
    print(f"Reading {path} (this walks the whole extract once)...", flush=True)
    handler.apply_file(path, locations=True, idx="flex_mem")
    return handler.records


def extract_business(element):
    """Return a clean dict for this element (every element with a website tag is kept)."""
    tags = element.get("tags", {})
    if element["type"] == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:
        center = element.get("center", {})
        lat, lon = center.get("lat"), center.get("lon")
    return record_from_tags(element["type"], element["id"], tags, lat, lon)


def geocode_area(place_name: str):
    """Turn a place name into a bounding box plus the raw Nominatim hit."""
    params = {"q": place_name, "format": "json", "limit": 1, "addressdetails": 1}
    resp = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Could not find a location matching '{place_name}'.")

    # Nominatim returns boundingbox as [min_lat, max_lat, min_lon, max_lon]
    min_lat, max_lat, min_lon, max_lon = (float(x) for x in results[0]["boundingbox"])
    hit = results[0]
    return (
        hit.get("display_name"),
        (min_lat, min_lon, max_lat, max_lon),  # south, west, north, east
        (hit.get("osm_type"), hit.get("osm_id")),
        hit,
    )


def load_geofabrik_index() -> list[dict]:
    """Geofabrik region list; cached next to the PBF extracts."""
    os.makedirs(GEOFABRIK_DIR, exist_ok=True)
    cache_path = os.path.join(GEOFABRIK_DIR, "index-v1.json")
    try:
        stale = (not os.path.exists(cache_path)
                 or time.time() - os.path.getmtime(cache_path) > 86400)
        if stale:
            raise FileNotFoundError
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        print("Fetching Geofabrik extract index...", flush=True)
        resp = requests.get(GEOFABRIK_INDEX_URL, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    return data.get("features") or []


def resolve_geofabrik_extract(place_name: str, hit: dict) -> dict:
    """Pick the smallest Geofabrik extract that covers this Nominatim result."""
    address = hit.get("address") or {}
    country = (address.get("country_code") or "").upper()
    iso2_codes = {
        str(value).upper()
        for key, value in address.items()
        if key.upper().startswith("ISO3166-2") and value
    }
    query_norm = _norm_name(place_name)
    name_norm = _norm_name(hit.get("name") or "")
    features = load_geofabrik_index()

    best = None
    best_score = -1
    for feat in features:
        props = feat.get("properties") or {}
        pbf_url = (props.get("urls") or {}).get("pbf")
        if not pbf_url:
            continue
        extract_id = props.get("id") or ""
        if extract_id in CONTINENT_IDS:
            continue
        extract_name = props.get("name") or ""
        iso1 = [str(x).upper() for x in (props.get("iso3166-1:alpha2") or [])]
        iso2 = [str(x).upper() for x in (props.get("iso3166-2") or [])]
        id_leaf = _norm_name(extract_id.split("/")[-1])
        named = _norm_name(extract_name)
        depth = extract_id.count("/") + (1 if props.get("parent") else 0)

        score = 0
        if iso2 and iso2_codes.intersection(iso2):
            score = 400 + depth
        elif query_norm and query_norm in {id_leaf, named}:
            score = 350 + depth
        elif name_norm and name_norm in {id_leaf, named}:
            score = 320 + depth
        elif country and country in iso1:
            score = 200 + depth
        if score > best_score:
            best = props
            best_score = score

    if not best:
        raise ValueError(
            f"No Geofabrik extract matched '{place_name}'. "
            "Pass --from-pbf PATH or --overpass."
        )
    return best


def download_geofabrik_pbf(url: str) -> str:
    """Download url into .geofabrik/, reusing a complete cached file."""
    os.makedirs(GEOFABRIK_DIR, exist_ok=True)
    filename = url.rstrip("/").rsplit("/", 1)[-1]
    dest = os.path.join(GEOFABRIK_DIR, filename)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        print(f"Using cached {dest}", flush=True)
        return dest

    tmp = dest + ".part"
    print(f"Downloading {url} ...", flush=True)
    with requests.get(url, headers=HEADERS, stream=True, timeout=60) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0)
        got = 0
        last_report = 0
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(1024 * 1024):
                if not chunk:
                    continue
                f.write(chunk)
                got += len(chunk)
                if total and got - last_report >= 8 * 1024 * 1024:
                    print(f"  {got / 1e6:.0f}/{total / 1e6:.0f} MB", flush=True)
                    last_report = got
    os.replace(tmp, dest)
    print(f"Saved {dest} ({os.path.getsize(dest) / 1e6:.0f} MB)", flush=True)
    return dest


def write_businesses(path: str, businesses: list[dict]) -> None:
    out_dir = os.path.dirname(path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(businesses, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"Wrote results to {path}")


def overpass_area_id(osm_type: str, osm_id) -> int:
    """Overpass area ids are the OSM id plus a per-type offset."""
    if osm_type == "relation":
        return 3600000000 + int(osm_id)
    if osm_type == "way":
        return 2400000000 + int(osm_id)
    raise ValueError(f"Cannot clip to a {osm_type}; need a way or relation boundary.")


def tiles(bbox, step_deg):
    """Split a bbox into a grid of (south, west, north, east) tiles."""
    south, west, north, east = bbox
    rows = math.ceil((north - south) / step_deg)
    cols = math.ceil((east - west) / step_deg)
    for r in range(rows):
        for col in range(cols):
            yield (
                south + r * step_deg,
                west + col * step_deg,
                min(south + (r + 1) * step_deg, north),
                min(west + (col + 1) * step_deg, east),
            )


def build_area_query(area_id: int, tile) -> str:
    south, west, north, east = tile
    bbox_str = f"{south:.4f},{west:.4f},{north:.4f},{east:.4f}"
    return f"""
    [out:json][timeout:{TILE_TIMEOUT}];
    area({area_id})->.a;
    (
      nwr["website"](area.a)({bbox_str});
      nwr["contact:website"](area.a)({bbox_str});
    );
    out center tags;
    """


def query_overpass_resilient(query: str):
    """POST a query, rotating endpoints and backing off on rate limits."""
    last_error = None
    attempt = 0
    for _round in range(OVERPASS_ROUNDS):
        for url in OVERPASS_URLS:
            attempt += 1
            try:
                resp = requests.post(
                    url,
                    data={"data": query},
                    headers=HEADERS,
                    timeout=(CONNECT_TIMEOUT, TILE_TIMEOUT + 30),
                )
                if resp.status_code in {429, 502, 503, 504}:
                    last_error = RuntimeError(f"{resp.status_code} from {url}")
                    time.sleep(5 * attempt)
                    continue
                resp.raise_for_status()
                data = resp.json()
                remark = str(data.get("remark") or "").lower()
                if "timed out" in remark or "error" in remark:
                    last_error = RuntimeError(data.get("remark") or "Overpass runtime error")
                    time.sleep(5 * attempt)
                    continue
                return data
            except (requests.Timeout, requests.ConnectionError, ValueError) as e:
                last_error = e
                time.sleep(3 * attempt)
    raise last_error or RuntimeError("All Overpass endpoints failed")


def tile_cache_path(area_id: int, tile) -> str:
    south, west, north, east = tile
    name = f"{area_id}_{south:.4f}_{west:.4f}_{north:.4f}_{east:.4f}.json"
    return os.path.join(TILE_CACHE_DIR, name)


def _has_child_cache(area_id: int, tile) -> bool:
    """True if any smaller cached tile sits strictly inside this tile."""
    south, west, north, east = tile
    prefix = f"{area_id}_"
    try:
        names = os.listdir(TILE_CACHE_DIR)
    except FileNotFoundError:
        return False
    for name in names:
        if not name.startswith(prefix) or not name.endswith(".json"):
            continue
        try:
            _id, s, w, n, e = name[:-5].split("_")
            s, w, n, e = float(s), float(w), float(n), float(e)
        except ValueError:
            continue
        if s >= south - 1e-6 and w >= west - 1e-6 and n <= north + 1e-6 and e <= east + 1e-6:
            if abs(s - south) + abs(w - west) + abs(n - north) + abs(e - east) > 1e-4:
                return True
    return False


def fetch_tile(area_id: int, tile, depth: int = 0):
    """Fetch one tile, splitting it into quarters if Overpass cannot finish."""
    south, west, north, east = tile
    label = f"{south:.2f},{west:.2f} .. {north:.2f},{east:.2f}"

    cached = tile_cache_path(area_id, tile)
    span = min(north - south, east - west)
    if os.path.exists(cached):
        with open(cached, encoding="utf-8") as f:
            elements = json.load(f)
        # Empty results on large tiles are usually timed-out Overpass
        # replies; ignore them and split so child caches can be used.
        if elements or span <= MIN_TILE_DEG * 2 or not _has_child_cache(area_id, tile):
            log(f"    {label}  {len(elements)} elements (cached)")
            return elements
        log(f"    {label}  ignoring empty large cache, splitting")
    else:
        try:
            data = query_overpass_resilient(build_area_query(area_id, tile))
            elements = data.get("elements", [])
            if not elements and span > MIN_TILE_DEG * 2:
                raise RuntimeError("empty result on large tile")
            os.makedirs(TILE_CACHE_DIR, exist_ok=True)
            with open(cached, "w", encoding="utf-8") as f:
                json.dump(elements, f)
            log(f"    {label}  {len(elements)} elements")
            return elements
        except Exception as e:
            if span / 2 < MIN_TILE_DEG:
                for retry in range(1, GIVE_UP_RETRIES + 1):
                    time.sleep(8 * retry)
                    log(f"    {label}  retry {retry}/{GIVE_UP_RETRIES} after: {e}")
                    try:
                        data = query_overpass_resilient(build_area_query(area_id, tile))
                        elements = data.get("elements", [])
                        os.makedirs(TILE_CACHE_DIR, exist_ok=True)
                        with open(cached, "w", encoding="utf-8") as f:
                            json.dump(elements, f)
                        log(f"    {label}  {len(elements)} elements")
                        return elements
                    except Exception as retry_err:
                        e = retry_err
                log(f"    {label}  GIVING UP ({e})")
                return []
            log(f"    {label}  splitting after: {e}")

    mid_lat = (south + north) / 2
    mid_lon = (west + east) / 2
    quarters = [
        (south, west, mid_lat, mid_lon),
        (south, mid_lon, mid_lat, east),
        (mid_lat, west, north, mid_lon),
        (mid_lat, mid_lon, north, east),
    ]
    out = []
    for quarter in quarters:
        out.extend(fetch_tile(area_id, quarter, depth + 1))
        time.sleep(TILE_PAUSE_S)
    return out


def build_overpass_query(bbox):
    south, west, north, east = bbox
    bbox_str = f"{south},{west},{north},{east}"
    return f"""
    [out:json][timeout:180];
    (
      nwr["website"]({bbox_str});
      nwr["contact:website"]({bbox_str});
    );
    out center tags;
    """


def query_overpass(query: str):
    # overpass-api.de now rejects requests that look like default library
    # traffic (e.g. python-requests' default User-Agent) with 406 Not
    # Acceptable. Sending the same descriptive headers we use for Nominatim
    # keeps the server happy.
    resp = requests.post(OVERPASS_URL, data={"data": query}, headers=HEADERS, timeout=200)
    resp.raise_for_status()
    return resp.json()


def main():
    parser = argparse.ArgumentParser(description="Fetch business websites from OSM for an area.")
    parser.add_argument("area", nargs="?", help="Area of interest, e.g. 'Cambridge, MA'")
    parser.add_argument("-o", "--output", default="businesses.json", help="Output JSON file path")
    parser.add_argument(
        "--from-pbf",
        help="Read website tags from this local OSM PBF instead of downloading Geofabrik",
    )
    parser.add_argument(
        "--overpass",
        action="store_true",
        help="Fetch live via Overpass instead of a Geofabrik PBF",
    )
    parser.add_argument(
        "--by-area",
        action="store_true",
        help="With --overpass, clip to the OSM boundary and fetch in tiles (use for countries)",
    )
    parser.add_argument("--tile-deg", type=float, default=1.0, help="Tile size in degrees")
    parser.add_argument(
        "--workers", type=int, default=3, help="Tiles to fetch in parallel (--by-area)"
    )
    args = parser.parse_args()
    if args.by_area and not args.overpass:
        print("--by-area only applies to Overpass; implying --overpass.", flush=True)
        args.overpass = True

    bbox = None
    if args.from_pbf:
        if args.area:
            print(f"Geocoding '{args.area}' to clip the PBF...", flush=True)
            try:
                display_name, bbox, _osm, _hit = geocode_area(args.area)
            except (requests.RequestException, ValueError) as e:
                sys.exit(f"Geocoding failed: {e}")
            print(f"Resolved to: {display_name}")
            print(f"Bounding box (south, west, north, east): {bbox}")
        businesses = extract_from_pbf(args.from_pbf, bbox=bbox)
        print(f"Kept {len(businesses)} unique elements with a usable website.")
        write_businesses(args.output, businesses)
        return

    area = args.area or input("Enter an area of interest (e.g. 'Cambridge, MA'): ").strip()
    if not area:
        sys.exit("No area provided.")

    print(f"Geocoding '{area}'...")
    try:
        display_name, bbox, (osm_type, osm_id), hit = geocode_area(area)
    except (requests.RequestException, ValueError) as e:
        sys.exit(f"Geocoding failed: {e}")
    print(f"Resolved to: {display_name}")
    print(f"Bounding box (south, west, north, east): {bbox}")

    if not args.overpass:
        try:
            extract = resolve_geofabrik_extract(area, hit)
        except ValueError as e:
            sys.exit(str(e))
        pbf_url = extract["urls"]["pbf"]
        print(
            f"Using Geofabrik extract {extract.get('id')} "
            f"({extract.get('name')}) — daily PBF, nodes/ways only.",
            flush=True,
        )
        try:
            pbf_path = download_geofabrik_pbf(pbf_url)
        except requests.RequestException as e:
            sys.exit(f"Geofabrik download failed: {e}")
        businesses = extract_from_pbf(pbf_path, bbox=bbox)
        print(f"Kept {len(businesses)} unique elements with a usable website.")
        write_businesses(args.output, businesses)
        return

    time.sleep(1)  # be polite to Nominatim before hitting Overpass

    if args.by_area:
        try:
            area_id = overpass_area_id(osm_type, osm_id)
        except ValueError as e:
            sys.exit(str(e))
        grid = list(tiles(bbox, args.tile_deg))
        print(
            f"Clipping to {osm_type} {osm_id} (area {area_id}) over {len(grid)} tiles "
            f"with {args.workers} workers...",
            flush=True,
        )
        elements = []
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for i, tile_elements in enumerate(
                pool.map(lambda t: fetch_tile(area_id, t), grid), 1
            ):
                elements.extend(tile_elements)
                log(f"  tile {i}/{len(grid)} done, {len(elements)} elements so far")
    else:
        print("Querying Overpass API (this can take a while for large areas)...")
        try:
            data = query_overpass(build_overpass_query(bbox))
        except requests.RequestException as e:
            sys.exit(f"Overpass query failed: {e}")
        elements = data.get("elements", [])

    print(f"Retrieved {len(elements)} raw elements with a website tag.")

    # Tiles overlap on their shared edges, and an element can carry both
    # website and contact:website, so the same element can arrive twice.
    seen = set()
    businesses = []
    for element in elements:
        key = (element.get("type"), element.get("id"))
        if key in seen:
            continue
        seen.add(key)
        business = extract_business(element)
        if business is not None:
            businesses.append(business)
    print(f"Kept {len(businesses)} unique elements with a usable website.")

    if args.by_area:
        # A cross-border route or boundary relation only has to touch the area
        # to be returned, and its center can land in another country.
        south, west, north, east = bbox
        inside = [
            b for b in businesses
            if b["lat"] is not None and b["lon"] is not None
            and south <= b["lat"] <= north and west <= b["lon"] <= east
        ]
        dropped = len(businesses) - len(inside)
        if dropped:
            print(f"Dropped {dropped} elements centred outside the bounding box.")
        businesses = inside

    write_businesses(args.output, businesses)


if __name__ == "__main__":
    main()
