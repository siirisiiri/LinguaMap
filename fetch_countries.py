#!/usr/bin/env python3
"""Build a lightweight world-country GeoJSON with OSM relation ids.

Fetches admin_level=2 tags from Overpass, then joins ISO3166-1 codes to
Natural Earth 110m outlines so the file stays small enough for the browser.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.request

import requests

OVERPASS_URLS = [
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
HEADERS = {"User-Agent": "linguamap-country-fetch/1.0 (contact: YOUR_EMAIL@example.com)"}
NE_URL = (
    "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
    "master/geojson/ne_110m_admin_0_countries.geojson"
)


def fetch_overpass(query):
    last_error = None
    for url in OVERPASS_URLS:
        try:
            print(f"  {url}", flush=True)
            resp = requests.post(
                url, data={"data": query}, headers=HEADERS, timeout=(15, 90)
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


def osm_countries():
    query = """
    [out:json][timeout:60];
    rel["type"="boundary"]["boundary"="administrative"]["admin_level"="2"]["ISO3166-1"];
    out tags;
    """
    print("Fetching OSM country relations...")
    osm = fetch_overpass(query)
    by_iso = {}
    for el in osm.get("elements") or []:
        tags = el.get("tags") or {}
        iso = (tags.get("ISO3166-1") or tags.get("ISO3166-1:alpha2") or "").upper()
        if el.get("type") != "relation" or len(iso) != 2:
            continue
        by_iso[iso] = {
            "osm_id": el["id"],
            "osm_type": "relation",
            "name": tags.get("name:en") or tags.get("name") or iso,
            "admin_level": "2",
            "iso": iso,
        }
    print(f"  {len(by_iso)} OSM countries with ISO3166-1")
    return by_iso


def natural_earth():
    print("Downloading Natural Earth 110m country outlines...")
    req = urllib.request.Request(NE_URL, headers={"User-Agent": HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    by_iso = osm_countries()
    ne = natural_earth()
    features = []
    unmatched = []
    for feat in ne.get("features") or []:
        props = feat.get("properties") or {}
        iso = (props.get("ISO_A2") or props.get("ISO_A2_EH") or "").upper()
        if iso == "-99":
            iso = (props.get("ISO_A2_EH") or "").upper()
        osm = by_iso.get(iso)
        if not osm:
            unmatched.append(iso or props.get("ADMIN"))
            continue
        features.append(
            {
                "type": "Feature",
                "properties": osm,
                "geometry": feat["geometry"],
            }
        )
    print(f"Joined {len(features)} countries; unmatched NE rows: {unmatched[:12]}")
    out = {"type": "FeatureCollection", "features": features}
    path = "data/countries.geojson"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f)
    print(f"Wrote {path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(f"Failed: {e}")
