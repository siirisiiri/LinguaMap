#!/usr/bin/env python3
"""
osm_business_websites.py

Given an area of interest (place name), this script:
  1. Geocodes the place name to a bounding box using Nominatim.
  2. Queries the Overpass API for every OSM element in that bounding box
     that has a `website` or `contact:website` tag (no filtering by type —
     businesses, government buildings, places of worship, attractions,
     etc. are all included).
  3. Writes the results (name, website, category, lat/lon) to a JSON file.

Usage:
    python osm_business_websites.py "Cambridge, MA"
    python osm_business_websites.py            # will prompt interactively
"""

import argparse
import json
import sys
import time
import requests

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

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


def extract_business(element):
    """Return a clean dict for this element (every element with a website tag is kept)."""
    tags = element.get("tags", {})

    website = tags.get("website") or tags.get("contact:website")
    if not website:
        return None  # shouldn't happen given the query, but guard anyway

    matched_key = next((k for k in CATEGORY_TAG_KEYS if k in tags), None)
    category = f"{matched_key}={tags[matched_key]}" if matched_key else "unclassified"

    # Nodes have lat/lon directly; ways/relations have a "center" object
    # (present because we used "out center").
    if element["type"] == "node":
        lat, lon = element.get("lat"), element.get("lon")
    else:
        center = element.get("center", {})
        lat, lon = center.get("lat"), center.get("lon")

    return {
        "name": tags.get("name", "(unnamed)"),
        "website": website,
        "category": category,
        "lat": lat,
        "lon": lon,
        "osm_type": element["type"],
        "osm_id": element["id"],
    }


def geocode_area(place_name: str):
    """Turn a place name into a (south, west, north, east) bounding box."""
    params = {"q": place_name, "format": "json", "limit": 1}
    resp = requests.get(NOMINATIM_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    results = resp.json()
    if not results:
        raise ValueError(f"Could not find a location matching '{place_name}'.")

    # Nominatim returns boundingbox as [min_lat, max_lat, min_lon, max_lon]
    min_lat, max_lat, min_lon, max_lon = (float(x) for x in results[0]["boundingbox"])
    display_name = results[0]["display_name"]
    return display_name, (min_lat, min_lon, max_lat, max_lon)  # south, west, north, east


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
    args = parser.parse_args()

    area = args.area or input("Enter an area of interest (e.g. 'Cambridge, MA'): ").strip()
    if not area:
        sys.exit("No area provided.")

    print(f"Geocoding '{area}'...")
    try:
        display_name, bbox = geocode_area(area)
    except (requests.RequestException, ValueError) as e:
        sys.exit(f"Geocoding failed: {e}")
    print(f"Resolved to: {display_name}")
    print(f"Bounding box (south, west, north, east): {bbox}")

    time.sleep(1)  # be polite to Nominatim before hitting Overpass

    query = build_overpass_query(bbox)
    print("Querying Overpass API (this can take a while for large areas)...")
    try:
        data = query_overpass(query)
    except requests.RequestException as e:
        sys.exit(f"Overpass query failed: {e}")

    elements = data.get("elements", [])
    print(f"Retrieved {len(elements)} raw elements with a website tag.")

    businesses = [b for e in elements if (b := extract_business(e)) is not None]
    print(f"Kept {len(businesses)} elements with a usable website.")

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(businesses, f, indent=2, ensure_ascii=False)

    print(f"Wrote results to {args.output}")


if __name__ == "__main__":
    main()
