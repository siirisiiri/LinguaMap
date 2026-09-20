#!/usr/bin/env python3
"""Prebuild subdivision-2 outlines into data/admin2/.

Subdivision 1 is already on disk from Natural Earth. Subdivision 2 used to
hit live OSM/Overpass for every county, which was slow and, for Quebec,
fell back to 17 huge administrative regions. This bakes the next level from
geoBoundaries (open license, already simplified): raions in Ukraine, local
authorities in the UK, US counties. Canada uses Statistics Canada census
divisions — counties, MRCs, regional districts — instead of the 5,000
municipalities (too fine) or 76 economic regions (too coarse).

  python3 tools/fetch_admin2.py
  python3 tools/fetch_admin2.py --iso major
  python3 tools/fetch_admin2.py --iso CA,UA,GB
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import requests

from fetch_admin1 import (
    HEADERS,
    fetch_overpass,
    iso_code,
    normalize_name,
    simplify_geometry,
)

ROOT = Path(__file__).resolve().parent.parent
COUNTRIES_PATH = ROOT / "data" / "countries.geojson"
ADMIN1_DIR = ROOT / "data" / "admin1"
OUT_DIR = ROOT / "data" / "admin2"
SIMPLIFY_TOLERANCE = 0.003
GB_API = "https://www.geoboundaries.org/api/current/gbOpen/{iso3}/{level}/"

ISO3 = {
    "AE": "ARE", "AF": "AFG", "AL": "ALB", "AM": "ARM", "AO": "AGO", "AR": "ARG",
    "AT": "AUT", "AU": "AUS", "AZ": "AZE", "BA": "BIH", "BD": "BGD", "BE": "BEL",
    "BF": "BFA", "BG": "BGR", "BH": "BHR", "BI": "BDI", "BJ": "BEN", "BN": "BRN",
    "BO": "BOL", "BR": "BRA", "BS": "BHS", "BT": "BTN", "BW": "BWA", "BY": "BLR",
    "BZ": "BLZ", "CA": "CAN", "CD": "COD", "CF": "CAF", "CG": "COG", "CH": "CHE",
    "CI": "CIV", "CL": "CHL", "CM": "CMR", "CN": "CHN", "CO": "COL", "CR": "CRI",
    "CU": "CUB", "CY": "CYP", "CZ": "CZE", "DE": "DEU", "DJ": "DJI", "DK": "DNK",
    "DO": "DOM", "DZ": "DZA", "EC": "ECU", "EE": "EST", "EG": "EGY", "ER": "ERI",
    "ES": "ESP", "ET": "ETH", "FI": "FIN", "FJ": "FJI", "FR": "FRA", "GA": "GAB",
    "GB": "GBR", "GE": "GEO", "GH": "GHA", "GL": "GRL", "GM": "GMB", "GN": "GIN",
    "GQ": "GNQ", "GR": "GRC", "GT": "GTM", "GW": "GNB", "GY": "GUY", "HN": "HND",
    "HR": "HRV", "HT": "HTI", "HU": "HUN", "ID": "IDN", "IE": "IRL", "IL": "ISR",
    "IN": "IND", "IQ": "IRQ", "IR": "IRN", "IS": "ISL", "IT": "ITA", "JM": "JAM",
    "JO": "JOR", "JP": "JPN", "KE": "KEN", "KG": "KGZ", "KH": "KHM", "KP": "PRK",
    "KR": "KOR", "KW": "KWT", "KZ": "KAZ", "LA": "LAO", "LB": "LBN", "LK": "LKA",
    "LR": "LBR", "LS": "LSO", "LT": "LTU", "LU": "LUX", "LV": "LVA", "LY": "LBY",
    "MA": "MAR", "MD": "MDA", "ME": "MNE", "MG": "MDG", "MK": "MKD", "ML": "MLI",
    "MM": "MMR", "MN": "MNG", "MR": "MRT", "MW": "MWI", "MX": "MEX", "MY": "MYS",
    "MZ": "MOZ", "NA": "NAM", "NE": "NER", "NG": "NGA", "NI": "NIC", "NL": "NLD",
    "NO": "NOR", "NP": "NPL", "NZ": "NZL", "OM": "OMN", "PA": "PAN", "PE": "PER",
    "PG": "PNG", "PH": "PHL", "PK": "PAK", "PL": "POL", "PT": "PRT", "PY": "PRY",
    "QA": "QAT", "RO": "ROU", "RS": "SRB", "RU": "RUS", "RW": "RWA", "SA": "SAU",
    "SD": "SDN", "SE": "SWE", "SI": "SVN", "SK": "SVK", "SL": "SLE", "SN": "SEN",
    "SO": "SOM", "SR": "SUR", "SS": "SSD", "SV": "SLV", "SY": "SYR", "SZ": "SWZ",
    "TD": "TCD", "TG": "TGO", "TH": "THA", "TJ": "TJK", "TL": "TLS", "TM": "TKM",
    "TN": "TUN", "TR": "TUR", "TT": "TTO", "TZ": "TZA", "UA": "UKR", "UG": "UGA",
    "US": "USA", "UY": "URY", "UZ": "UZB", "VE": "VEN", "VN": "VNM", "VU": "VUT",
    "XK": "XKK", "YE": "YEM", "ZA": "ZAF", "ZM": "ZMB", "ZW": "ZWE",
    "GF": "GUF",
}

# Overseas regions that belong on a sovereign country's subdivision-1 map
# (French Guiana is part of France on the 110m overview). Each entry is an
# extra geoBoundaries download assigned to that parent.
OVERSEAS_ADM2 = {
    "FR": [
        {
            "iso3": "GUF",
            "osm_id": 1260551,
            "name": "French Guiana",
            "iso": "FR-GF",
            "names": {"frenchguiana", "guyane"},
        },
    ],
}

# Size, population, or diplomatic weight — the countries people actually open.
MAJOR_ISO = [
    "US", "CA", "MX", "BR", "AR", "CO", "CL", "PE", "VE",
    "GB", "FR", "DE", "IT", "ES", "PT", "NL", "BE", "AT", "CH",
    "SE", "NO", "FI", "PL", "RO", "CZ", "HU", "IE", "GR",
    "UA", "RU", "TR",
    "SA", "AE", "IL", "IR", "IQ", "EG", "DZ", "MA", "NG", "ZA", "KE", "ET", "GH",
    "IN", "PK", "BD", "CN", "JP", "KR", "ID", "TH", "VN", "PH", "MY", "AU", "NZ",
    "KZ",
]

# Natural Earth admin1 is counties/departments here. Use geoBoundaries instead.
# Italy's ADM1 is 5 macro-areas; the 20 regions are ADM2.
PROMOTE_ADMIN1 = {
    "GB": "ADM1",
    "FR": "ADM1",
    "ES": "ADM1",
    "IT": "ADM2",
    "BE": "ADM1",
    "IE": "ADM1",
    "GR": "ADM1",
}

# After promoting Italy's regions to subdivision 1, try provinces as level 2.
ADMIN2_LEVEL = {"IT": "ADM3", "CA": "STATCAN"}

# Natural Earth admin1 for the UK is counties, so the viewer uses OSM nations
# as subdivision 1. Pin those relation ids so admin2 files key the same way.
OSM_NATIONS = {
    "Wales": 58437,
    "Cymru": 58437,
    "Scotland": 58446,
    "England": 58447,
    "Northern Ireland": 192732,
}

STATCAN_CD_URL = (
    "https://geo.statcan.gc.ca/geo_wa/rest/services/2021/"
    "Cartographic_boundary_files/MapServer/4/query"
)
# StatCan province codes -> Natural Earth / OSM ISO3166-2 on admin1.
PRUID_TO_ISO = {
    "10": "CA-NL",
    "11": "CA-PE",
    "12": "CA-NS",
    "13": "CA-NB",
    "24": "CA-QC",
    "35": "CA-ON",
    "46": "CA-MB",
    "47": "CA-SK",
    "48": "CA-AB",
    "59": "CA-BC",
    "60": "CA-YT",
    "61": "CA-NT",
    "62": "CA-NU",
}


def centroid(geom: dict):
    coords = []

    def walk(coord):
        if not coord:
            return
        if isinstance(coord[0], (int, float)):
            coords.append(coord)
            return
        for item in coord:
            walk(item)

    walk((geom or {}).get("coordinates"))
    if not coords:
        return None
    return (
        sum(p[0] for p in coords) / len(coords),
        sum(p[1] for p in coords) / len(coords),
    )


def point_in_ring(lon: float, lat: float, ring: list) -> bool:
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def contains(geom: dict, lon: float, lat: float) -> bool:
    if not geom:
        return False
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    for poly in polys:
        if not poly:
            continue
        if point_in_ring(lon, lat, poly[0]) and not any(
            point_in_ring(lon, lat, hole) for hole in poly[1:]
        ):
            return True
    return False


def admin1_too_deep(features: list[dict]) -> bool:
    if len(features) < 2:
        return True
    deeper = 0
    for feat in features:
        raw = str((feat.get("properties") or {}).get("admin_level") or "4")
        if raw.isdigit() and int(raw) > 4:
            deeper += 1
    return deeper > len(features) / 2


def country_osm_ids() -> dict[str, dict]:
    if not COUNTRIES_PATH.exists():
        sys.exit(f"Missing {COUNTRIES_PATH}. Run tools/fetch_countries.py first.")
    with COUNTRIES_PATH.open(encoding="utf-8") as f:
        countries = json.load(f)
    out = {}
    for feat in countries.get("features") or []:
        props = feat.get("properties") or {}
        iso = (props.get("iso") or "").upper()
        if iso and props.get("osm_id"):
            out[iso] = {"osm_id": int(props["osm_id"]), "name": props.get("name") or iso}
    return out


def fill_admin1_ids() -> int:
    """Give every subdivision-1 polygon a stable id so drill-down never stalls."""
    filled = 0
    for path in sorted(ADMIN1_DIR.glob("*.geojson")):
        try:
            payload = json.loads(path.read_bytes())
        except json.JSONDecodeError:
            continue
        changed = False
        for feat in payload.get("features") or []:
            props = feat.setdefault("properties", {})
            if props.get("osm_id"):
                continue
            props["osm_id"] = stable_id(str(props.get("iso") or ""), props.get("name") or path.stem)
            changed = True
            filled += 1
        if changed:
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return filled


def osm_codes_for_country(iso: str) -> tuple[dict, dict]:
    """ISO3166-2 and name indexes for one country's OSM subdivision relations."""
    query = f"""
    [out:json][timeout:90];
    rel["boundary"="administrative"]["ISO3166-2"~"^{iso}-"]["name"];
    out tags;
    """
    try:
        osm = fetch_overpass(query)
    except Exception as e:
        print(f"    overpass failed: {e}", flush=True)
        return {}, {}
    by_iso: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for el in osm.get("elements") or []:
        tags = el.get("tags") or {}
        if el.get("type") != "relation":
            continue
        rec = {
            "osm_id": el["id"],
            "name": tags.get("name:en") or tags.get("name") or "",
            "admin_level": tags.get("admin_level") or "4",
            "iso": iso_code(tags.get("ISO3166-2")),
        }
        if rec["iso"]:
            by_iso[rec["iso"]] = rec
        for label in (tags.get("name"), tags.get("name:en"), tags.get("official_name")):
            key = normalize_name(label)
            if key and key not in by_name:
                by_name[key] = rec
    return by_iso, by_name


def match_osm_parent(name: str, shape_iso: str, by_iso: dict, by_name: dict) -> dict | None:
    if name in OSM_NATIONS:
        return {"osm_id": OSM_NATIONS[name], "name": name, "admin_level": "4", "iso": ""}
    key = normalize_name(name)
    if key in OSM_NATIONS:
        return {"osm_id": OSM_NATIONS[key], "name": name, "admin_level": "4", "iso": ""}
    for raw in (shape_iso, shape_iso.replace(".", "-"), shape_iso.replace("_", "-")):
        code = iso_code(raw)
        if code and code in by_iso:
            return by_iso[code]
        parts = (raw or "").upper().split("-")
        if len(parts) >= 2 and len(parts[0]) == 3:
            guess = iso_code(f"{parts[0][:2]}-{parts[1]}")
            if guess and guess in by_iso:
                return by_iso[guess]
    key = normalize_name(name)
    if key in by_name:
        return by_name[key]
    if key in OSM_NATIONS:
        return {"osm_id": OSM_NATIONS[key], "name": name, "admin_level": "4", "iso": ""}
    for other, rec in by_name.items():
        if key and other and (key in other or other in key) and min(len(key), len(other)) >= 5:
            return rec
    if name in OSM_NATIONS:
        return {"osm_id": OSM_NATIONS[name], "name": name, "admin_level": "4", "iso": ""}
    return None


def write_admin1(iso: str, parent_osm_id: int, features: list[dict]) -> None:
    ADMIN1_DIR.mkdir(parents=True, exist_ok=True)
    path = ADMIN1_DIR / f"{iso}.geojson"
    path.write_text(
        json.dumps({"type": "FeatureCollection", "features": features}, ensure_ascii=False),
        encoding="utf-8",
    )
    index_path = ADMIN1_DIR / "index.json"
    try:
        index = json.loads(index_path.read_bytes()) if index_path.exists() else {}
    except json.JSONDecodeError:
        index = {}
    index[str(parent_osm_id)] = {
        "iso": iso,
        "url": f"data/admin1/{path.name}",
        "count": len(features),
    }
    index_path.write_text(json.dumps(index, indent=1, sort_keys=True), encoding="utf-8")
    print(f"  wrote admin1 {iso}: {len(features)} areas -> {path}", flush=True)


def promote_admin1(iso: str, iso3: str, level: str, country_osm_id: int) -> list[dict]:
    """Replace too-local Natural Earth admin1 with geoBoundaries regions/nations."""
    print(f"  promoting {iso} subdivision 1 from geoBoundaries {level}…", flush=True)
    raw = download_geoboundaries(iso3, level)
    by_iso, by_name = osm_codes_for_country(iso)
    parents = []
    features = []
    for feat in raw:
        props = feat.get("properties") or {}
        name = fix_mojibake(props.get("shapeName") or props.get("name") or "")
        if not name:
            continue
        geom = simplify_geometry(feat.get("geometry") or {}, 0.005)
        if not geom:
            continue
        rec = match_osm_parent(name, props.get("shapeISO") or "", by_iso, by_name)
        osm_id = int(rec["osm_id"]) if rec else stable_id(str(props.get("shapeID") or ""), name)
        parent = {
            "osm_id": osm_id,
            "name": (rec.get("name") if rec and rec.get("name") else name),
            "admin_level": str((rec or {}).get("admin_level") or "4"),
            "iso": (rec or {}).get("iso") or "",
            "geometry": geom,
        }
        parents.append(parent)
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "osm_type": "relation",
                    "osm_id": osm_id,
                    "name": parent["name"],
                    "admin_level": parent["admin_level"],
                    "iso": parent["iso"],
                    "parent_iso": iso,
                    "parent_osm_id": country_osm_id,
                    "kind": "subdivision_1",
                },
                "geometry": geom,
            }
        )
    if len(features) < 2:
        print(f"  promote {iso}: only {len(features)} areas, keeping existing admin1", flush=True)
        return []
    write_admin1(iso, country_osm_id, features)
    return parents


def admin2_mostly_ready(parents: list[dict]) -> bool:
    if len(parents) < 2:
        return False
    have = sum(1 for p in parents if (OUT_DIR / f"{p['osm_id']}.geojson").exists())
    return have >= max(2, int(len(parents) * 0.9))


def download_statcan_census_divisions() -> list[dict]:
    """Counties, MRCs, and regional districts (~293), not towns."""
    print("  Statistics Canada 2021 census divisions…", flush=True)
    meta = requests.get(
        STATCAN_CD_URL,
        params={
            "where": "1=1",
            "outFields": "OBJECTID,CDUID,CDNAME,CDTYPE,PRUID",
            "returnGeometry": "false",
            "f": "json",
            "resultRecordCount": 6000,
        },
        headers=HEADERS,
        timeout=(20, 60),
    )
    meta.raise_for_status()
    rows = meta.json().get("features") or []
    oids = [row["attributes"]["OBJECTID"] for row in rows]
    print(f"  {len(oids)} divisions", flush=True)
    features: list[dict] = []
    batch_size = 5
    for i in range(0, len(oids), batch_size):
        batch = oids[i : i + batch_size]
        oid_list = ",".join(str(oid) for oid in batch)
        last_error: Exception | None = None
        for attempt in range(5):
            try:
                resp = requests.get(
                    STATCAN_CD_URL,
                    params={
                        "where": f"OBJECTID IN ({oid_list})",
                        "outFields": "CDUID,CDNAME,CDTYPE,PRUID",
                        "outSR": "4326",
                        "f": "geojson",
                        "returnGeometry": "true",
                        "maxAllowableOffset": "0.005",
                        "geometryPrecision": "4",
                    },
                    headers=HEADERS,
                    timeout=(20, 90),
                )
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                chunk = resp.json().get("features") or []
                if len(chunk) < len(batch):
                    raise RuntimeError(f"got {len(chunk)} of {len(batch)}")
                features.extend(chunk)
                print(f"    {len(features)}/{len(oids)}", flush=True)
                last_error = None
                break
            except Exception as e:
                last_error = e
                time.sleep(1.5 * (attempt + 1))
        if last_error:
            raise RuntimeError(f"census division batch {batch[0]}-{batch[-1]}: {last_error}")
    return features


def download_geoboundaries(iso3: str, level: str) -> list[dict]:
    meta_url = GB_API.format(iso3=iso3, level=level)
    print(f"  {meta_url}", flush=True)
    meta = requests.get(meta_url, headers=HEADERS, timeout=(20, 60))
    meta.raise_for_status()
    info = meta.json()
    if isinstance(info, list):
        info = info[0] if info else {}
    url = info.get("simplifiedGeometryGeoJSON") or info.get("gjDownloadURL")
    if not url:
        raise RuntimeError(f"No geoBoundaries download for {iso3} {level}")
    print(f"  {url} ({info.get('admUnitCount', '?')} units)", flush=True)
    resp = requests.get(url, headers=HEADERS, timeout=(20, 180))
    resp.raise_for_status()
    return (json.loads(resp.content.decode("utf-8")).get("features") or [])


def parents_from_admin1(iso: str) -> list[dict]:
    path = ADMIN1_DIR / f"{iso}.geojson"
    if not path.exists():
        return []
    features = json.loads(path.read_bytes()).get("features") or []
    parents = []
    for feat in features:
        props = feat.get("properties") or {}
        osm_id = props.get("osm_id")
        if not osm_id:
            continue
        raw = str(props.get("admin_level") or "4")
        level = int(raw) if raw.isdigit() else 4
        parents.append(
            {
                "osm_id": int(osm_id),
                "name": props.get("name") or f"relation/{osm_id}",
                "admin_level": str(level),
                "iso": props.get("iso") or "",
                "geometry": feat.get("geometry"),
            }
        )
    return parents


def parents_from_geoboundaries_adm1(iso: str, iso3: str) -> list[dict]:
    """UK-style: use geoBoundaries ADM1 (nations) as the subdivision-1 parents."""
    features = download_geoboundaries(iso3, "ADM1")
    parents = []
    for feat in features:
        name = fix_mojibake((feat.get("properties") or {}).get("shapeName") or "")
        osm_id = OSM_NATIONS.get(name)
        if not osm_id:
            print(f"    skip ADM1 {name}: no OSM id")
            continue
        parents.append(
            {
                "osm_id": int(osm_id),
                "name": name,
                "admin_level": "4",
                "iso": "",
                "geometry": feat.get("geometry"),
            }
        )
    return parents


def stable_id(shape_id: str, name: str) -> int:
    raw = (shape_id or name or "").encode("utf-8")
    return int(hashlib.sha1(raw).hexdigest()[:8], 16) % 900_000_000 + 100_000_000


def fix_mojibake(value: str) -> str:
    """geoBoundaries simplified files often store UTF-8 names as Latin-1."""
    if not value or ("Ã" not in value and "Â" not in value):
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


def child_feature(feat: dict, parent: dict) -> dict | None:
    props = feat.get("properties") or {}
    name = fix_mojibake(
        props.get("shapeName") or props.get("CDNAME") or props.get("name") or ""
    )
    if not name:
        return None
    geom = simplify_geometry(feat.get("geometry") or {}, SIMPLIFY_TOLERANCE)
    if not geom:
        return None
    return {
        "type": "Feature",
        "properties": {
            "osm_type": "relation",
            "osm_id": stable_id(str(props.get("shapeID") or props.get("CDUID") or ""), name),
            "name": name,
            "admin_level": "6",
            "kind": "subdivision_2",
            "parent_osm_id": parent["osm_id"],
            "parent_name": parent.get("name") or "",
            "parent_iso": parent.get("iso") or "",
        },
        "geometry": geom,
    }


def assign_canada(children: list[dict], parents: list[dict]) -> dict[int, list[dict]]:
    by_iso = {p.get("iso"): p for p in parents if p.get("iso")}
    by_parent: dict[int, list[dict]] = {p["osm_id"]: [] for p in parents}
    for feat in children:
        pruid = str((feat.get("properties") or {}).get("PRUID") or "")
        parent = by_iso.get(PRUID_TO_ISO.get(pruid, ""))
        if not parent:
            continue
        child = child_feature(feat, parent)
        if child:
            by_parent[parent["osm_id"]].append(child)
    return by_parent


def assign_children(children: list[dict], parents: list[dict]) -> dict[int, list[dict]]:
    by_parent: dict[int, list[dict]] = {p["osm_id"]: [] for p in parents}
    for feat in children:
        point = centroid(feat.get("geometry") or {})
        if not point:
            continue
        lon, lat = point
        hit = None
        for parent in parents:
            if contains(parent.get("geometry") or {}, lon, lat):
                hit = parent
                break
        if not hit:
            best, best_d = None, 25.0  # degrees²; ~5° is still the same country
            for parent in parents:
                pc = centroid(parent.get("geometry") or {})
                if not pc:
                    continue
                dist = (pc[0] - lon) ** 2 + (pc[1] - lat) ** 2
                if dist < best_d:
                    best, best_d = parent, dist
            hit = best
        if not hit:
            continue
        child = child_feature(feat, hit)
        if child:
            by_parent[hit["osm_id"]].append(child)
    return by_parent


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
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    index[str(osm_id)] = {
        "url": f"data/admin2/{path.name}",
        "count": len(features),
        "name": parent.get("name") or "",
        "iso": parent.get("iso") or "",
        "kind": "subdivision_2",
    }
    kb = path.stat().st_size / 1000
    print(f"  {parent.get('name')}: {len(features)} areas, {kb:.0f} KB -> {path}", flush=True)


def rebuild_index() -> dict:
    index = {}
    for path in sorted(OUT_DIR.glob("*.geojson")):
        try:
            payload = json.loads(path.read_bytes())
        except json.JSONDecodeError:
            continue
        parent = payload.get("parent") or {}
        features = payload.get("features") or []
        osm_id = parent.get("osm_id") or path.stem
        index[str(osm_id)] = {
            "url": f"data/admin2/{path.name}",
            "count": len(features),
            "name": parent.get("name") or "",
            "iso": parent.get("iso") or "",
            "kind": "subdivision_2",
        }
    return index


def load_index() -> dict:
    path = OUT_DIR / "index.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_bytes())
    except json.JSONDecodeError:
        return {}


def parse_iso_list(raw: str) -> list[str]:
    if raw.strip().lower() in {"major", "majors"}:
        return list(MAJOR_ISO)
    return [part.strip().upper() for part in raw.split(",") if part.strip()]


def overseas_geometry(iso3: str) -> dict | None:
    """Whole-territory outline for an overseas region (ADM1, else ADM0)."""
    for level in ("ADM1", "ADM0"):
        try:
            raw = download_geoboundaries(iso3, level)
        except Exception as e:
            print(f"    {iso3} {level}: {e}", flush=True)
            continue
        best = None
        best_n = 0
        for feat in raw:
            geom = simplify_geometry(feat.get("geometry") or {}, 0.005)
            if not geom:
                continue
            n = len(json.dumps(geom, separators=(",", ":")))
            if n > best_n:
                best, best_n = geom, n
        if best:
            return best
    return None


def ensure_overseas_admin1(iso: str, country_osm_id: int, parents: list[dict]) -> list[dict]:
    """Keep overseas regions on the sovereign country's subdivision-1 map."""
    extras = OVERSEAS_ADM2.get(iso) or []
    if not extras:
        return parents
    have_ids = {int(p["osm_id"]) for p in parents}
    have_names = {normalize_name(p.get("name") or "") for p in parents}
    added = []
    for extra in extras:
        names = extra.get("names") or {normalize_name(extra["name"])}
        if int(extra["osm_id"]) in have_ids or have_names & names:
            continue
        print(f"  adding overseas admin1 {extra['name']}…", flush=True)
        geom = overseas_geometry(extra["iso3"])
        if not geom:
            print(f"    no geometry for {extra['iso3']}", flush=True)
            continue
        parent = {
            "osm_id": int(extra["osm_id"]),
            "name": extra["name"],
            "admin_level": "4",
            "iso": extra.get("iso") or "",
            "geometry": geom,
        }
        parents.append(parent)
        added.append(parent)
    if not added:
        return parents
    path = ADMIN1_DIR / f"{iso}.geojson"
    try:
        payload = json.loads(path.read_bytes()) if path.exists() else {"features": []}
    except json.JSONDecodeError:
        payload = {"features": []}
    features = list(payload.get("features") or [])
    for parent in added:
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "osm_type": "relation",
                    "osm_id": parent["osm_id"],
                    "name": parent["name"],
                    "admin_level": parent["admin_level"],
                    "iso": parent.get("iso") or "",
                    "parent_iso": iso,
                    "parent_osm_id": country_osm_id,
                    "kind": "subdivision_1",
                },
                "geometry": parent["geometry"],
            }
        )
    write_admin1(iso, country_osm_id, features)
    return parents


def fill_overseas_admin2(iso: str, parents: list[dict], index: dict) -> None:
    extras = OVERSEAS_ADM2.get(iso) or []
    if not extras:
        return
    by_id = {int(p["osm_id"]): p for p in parents}
    by_name = {normalize_name(p.get("name") or ""): p for p in parents}
    for extra in extras:
        parent = by_id.get(int(extra["osm_id"]))
        if not parent:
            for name in extra.get("names") or []:
                parent = by_name.get(name)
                if parent:
                    break
        if not parent:
            print(f"  overseas {extra['name']}: no subdivision-1 parent", flush=True)
            continue
        path = OUT_DIR / f"{parent['osm_id']}.geojson"
        if path.exists():
            try:
                n = len(json.loads(path.read_bytes()).get("features") or [])
            except json.JSONDecodeError:
                n = 0
            if n >= 2:
                print(f"  {parent['name']}: subdivision 2 already on disk", flush=True)
                continue
        children = []
        for level in ("ADM2", "ADM3"):
            try:
                raw = download_geoboundaries(extra["iso3"], level)
            except Exception as e:
                print(f"    {extra['iso3']} {level}: {e}", flush=True)
                continue
            if len(raw) < 2:
                continue
            if len(raw) > 8000:
                print(f"    {extra['iso3']} {level} has {len(raw)} units — too local")
                continue
            grouped = assign_children(raw, [parent])
            children = grouped.get(parent["osm_id"]) or []
            if len(children) >= 2:
                break
        if len(children) < 2:
            print(f"  {parent['name']}: only {len(children)} areas, skipping", flush=True)
            continue
        write_parent(parent, children, index)


def build_country_admin2(iso: str, parents: list[dict], index: dict) -> None:
    level = ADMIN2_LEVEL.get(iso, "ADM2")
    try:
        if level == "STATCAN":
            raw_children = download_statcan_census_divisions()
            grouped = assign_canada(raw_children, parents)
            label = "census divisions"
        else:
            iso3 = ISO3[iso]
            raw_children = download_geoboundaries(iso3, level)
            if len(raw_children) > 8000:
                print(f"  {level} has {len(raw_children)} units — too local, skipping")
                return
            print(
                f"  assigning {len(raw_children)} {level} areas to {len(parents)} parents…",
                flush=True,
            )
            grouped = assign_children(raw_children, parents)
            label = level
    except Exception as e:
        print(f"  download failed: {e}")
        return
    if level == "STATCAN":
        print(f"  assigning {len(raw_children)} {label} to {len(parents)} parents…", flush=True)
    wrote = 0
    for parent in parents:
        features = grouped.get(parent["osm_id"]) or []
        path = OUT_DIR / f"{parent['osm_id']}.geojson"
        if len(features) < 2:
            if path.exists() and iso not in {"US", "CA", "UA", "GB"}:
                path.unlink()
                print(
                    f"  {parent.get('name')}: only {len(features)} areas, removed old file",
                    flush=True,
                )
            else:
                print(f"  {parent.get('name')}: only {len(features)} areas, skipping", flush=True)
            continue
        write_parent(parent, features, index)
        wrote += 1
    print(f"  {iso}: wrote {wrote}/{len(parents)} subdivision-2 files", flush=True)
    index.update(rebuild_index())
    (OUT_DIR / "index.json").write_text(
        json.dumps(index, indent=1, sort_keys=True), encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--iso",
        default="major",
        help="Comma-separated ISO2 countries, or 'major' (default).",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    filled = fill_admin1_ids()
    if filled:
        print(f"Filled {filled} missing subdivision-1 ids", flush=True)
    index = load_index()
    countries = country_osm_ids()

    for iso in parse_iso_list(args.iso):
        iso3 = ISO3.get(iso)
        if not iso3:
            print(f"Skipping {iso}: no ISO3 mapping")
            continue
        if iso not in countries:
            print(f"Skipping {iso}: no country outline")
            continue
        print(f"{iso} ({countries[iso]['name']})…", flush=True)
        country_osm_id = int(countries[iso]["osm_id"])
        promote_level = PROMOTE_ADMIN1.get(iso)
        parents = []
        if promote_level:
            try:
                parents = promote_admin1(iso, iso3, promote_level, country_osm_id)
            except Exception as e:
                print(f"  promote failed: {e}", flush=True)
                parents = []
        if not parents:
            parents = parents_from_admin1(iso)
        if not parents:
            print(f"  no subdivision-1 parents for {iso}")
            if iso not in OVERSEAS_ADM2:
                continue
        parents = ensure_overseas_admin1(iso, country_osm_id, parents)
        if not parents:
            continue
        if admin2_mostly_ready(parents):
            print(f"  subdivision 2 already on disk ({len(parents)} parents), skipping", flush=True)
        else:
            build_country_admin2(iso, parents, index)
        fill_overseas_admin2(iso, parents, index)

    index = rebuild_index()
    (OUT_DIR / "index.json").write_text(
        json.dumps(index, indent=1, sort_keys=True), encoding="utf-8"
    )
    total = sum(v.get("count", 0) for v in index.values())
    print(f"Admin2 index: {len(index)} parents, {total} areas")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(f"Failed: {e}")
