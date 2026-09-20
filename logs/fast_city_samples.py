#!/usr/bin/env python3
"""Tight downtown Overpass samples so AU/NZ/NG files exist quickly."""
from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path("/Users/aryandaga/Documents/LinguaMap")
sys.path.insert(0, str(ROOT))

from osm_business_websites import (  # noqa: E402
    OVERPASS_URLS,
    extract_business,
    write_businesses,
)
from run_world_pipeline import log  # noqa: E402
import requests

HEADERS = {"User-Agent": "osm-business-website-finder/1.0 (contact: YOUR_EMAIL@example.com)"}
CAP = 1200

# south, west, north, east — small downtown boxes, not metro sprawl
JOBS = [
    (
        "Australia",
        ROOT / "data" / "Australia.json",
        (-33.895, 151.175, -33.845, 151.235),  # Sydney CBD
    ),
    (
        "New Zealand",
        ROOT / "data" / "New Zealand.json",
        (-36.868, 174.745, -36.835, 174.785),  # Auckland CBD
    ),
    (
        "Nigeria",
        ROOT / "data" / "Nigeria.json",
        (6.430, 3.380, 6.500, 3.450),  # Lagos Island / Marina
    ),
]


def query(bbox: tuple[float, float, float, float]) -> list[dict]:
    south, west, north, east = bbox
    bbox_str = f"{south},{west},{north},{east}"
    q = f"""
    [out:json][timeout:25][maxsize:33554432];
    (
      nwr["website"]({bbox_str});
      nwr["contact:website"]({bbox_str});
    );
    out center {CAP};
    """
    last = None
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(
                url, data={"data": q}, headers=HEADERS, timeout=(10, 35)
            )
            if resp.status_code >= 400:
                last = RuntimeError(f"{resp.status_code} {url}")
                continue
            data = resp.json()
            remark = str(data.get("remark") or "").lower()
            if "timed out" in remark or "error" in remark:
                last = RuntimeError(data.get("remark"))
                continue
            seen = set()
            records = []
            for element in data.get("elements") or []:
                key = (element.get("type"), element.get("id"))
                if key in seen:
                    continue
                seen.add(key)
                rec = extract_business(element)
                if rec is not None:
                    records.append(rec)
                if len(records) >= CAP:
                    break
            return records
        except Exception as exc:
            last = exc
            continue
    raise last or RuntimeError("all Overpass endpoints failed")


def fetch_job(job: tuple) -> None:
    name, path, bbox = job
    if path.exists() and path.stat().st_size > 2000:
        log(f"{name}: sample already on disk")
        return
    log(f"{name}: tight Overpass {bbox}")
    records = query(bbox)
    if not records:
        log(f"{name}: empty tight sample")
        return
    write_businesses(str(path), records)
    log(f"{name}: wrote {len(records)} tight-sample records")


def main() -> None:
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(fetch_job, JOBS))


if __name__ == "__main__":
    main()
