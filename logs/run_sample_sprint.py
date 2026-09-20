#!/usr/bin/env python3
"""20-minute city-sample fetch + crawl for Canada, UK, Australia, NZ, Nigeria."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path("/Users/aryandaga/Documents/LinguaMap")
sys.path.insert(0, str(ROOT))

from osm_business_websites import (  # noqa: E402
    extract_business,
    geocode_area,
    query_overpass_resilient,
    write_businesses,
)
from run_world_pipeline import PYTHON, log  # noqa: E402

LOG_PATH = ROOT / "logs" / "priority_pipeline.log"
STATUS_PATH = ROOT / "logs" / "priority_status.json"
DEADLINE_S = 20 * 60
MAX_CRAWLS = 3
SAMPLE_CAP = 2500

COUNTRIES = [
    {
        "display": "Canada",
        "path": ROOT / "data" / "Canada.json",
        "cities": ["Toronto, Canada", "Vancouver, Canada", "Calgary, Canada"],
    },
    {
        "display": "United Kingdom",
        "path": ROOT / "data" / "United Kingdom.json",
        "cities": ["London, United Kingdom", "Manchester, United Kingdom"],
    },
    {
        "display": "Australia",
        "path": ROOT / "data" / "Australia.json",
        "cities": ["Sydney, Australia", "Melbourne, Australia"],
    },
    {
        "display": "New Zealand",
        "path": ROOT / "data" / "New Zealand.json",
        "cities": ["Auckland, New Zealand", "Wellington, New Zealand"],
    },
    {
        "display": "Nigeria",
        "path": ROOT / "data" / "Nigeria.json",
        "cities": ["Lagos, Nigeria", "Abuja, Nigeria"],
    },
]


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "state="],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False
    state = (result.stdout or "").strip()
    return bool(state) and not state.startswith("Z")


def _spawn(cmd: list[str]) -> int:
    log_fd = os.open(LOG_PATH, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        start_new_session=True,
        stdout=log_fd,
        stderr=log_fd,
    )
    os.close(log_fd)
    return proc.pid


def sample_query(bbox: tuple[float, float, float, float]) -> str:
    south, west, north, east = bbox
    bbox_str = f"{south},{west},{north},{east}"
    return f"""
    [out:json][timeout:75][maxsize:67108864];
    (
      nwr["website"]({bbox_str});
      nwr["contact:website"]({bbox_str});
    );
    out center {SAMPLE_CAP};
    """


def fetch_city_sample(city: str) -> list[dict]:
    log(f"sample geocode {city}")
    display, bbox, _osm, _hit = geocode_area(city)
    log(f"sample Overpass {city} ({display})")
    data = query_overpass_resilient(sample_query(bbox))
    seen: set[tuple] = set()
    records: list[dict] = []
    for element in data.get("elements") or []:
        key = (element.get("type"), element.get("id"))
        if key in seen:
            continue
        seen.add(key)
        rec = extract_business(element)
        if rec is not None:
            records.append(rec)
        if len(records) >= SAMPLE_CAP:
            break
    log(f"sample kept {len(records)} sites from {city}")
    return records


def fetch_country_sample(job: dict) -> int:
    merged: dict[tuple, dict] = {}
    if job["path"].exists():
        import json

        try:
            existing = json.loads(job["path"].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = []
        if isinstance(existing, list):
            for rec in existing:
                merged[(rec.get("osm_type"), rec.get("osm_id"))] = rec
    for city in job["cities"]:
        try:
            for rec in fetch_city_sample(city):
                merged[(rec["osm_type"], rec["osm_id"])] = rec
        except Exception as exc:
            log(f"{job['display']}: sample {city} failed ({exc!r})")
            continue
        if len(merged) >= SAMPLE_CAP:
            break
        time.sleep(1)
    records = list(merged.values())
    if not records:
        return 0
    write_businesses(str(job["path"]), records)
    log(f"{job['display']}: wrote {len(records)} sample records")
    return len(records)


def crawl_cmd(path: Path) -> list[str]:
    return [PYTHON, str(ROOT / "crawl_languages.py"), "--data", str(path)]


def main() -> None:
    started = datetime.now(timezone.utc)
    deadline = started + timedelta(seconds=DEADLINE_S)
    log(
        f"Sample sprint: {', '.join(c['display'] for c in COUNTRIES)} "
        f"until {deadline.isoformat()} ({DEADLINE_S // 60} min)"
    )
    crawls: dict[str, int] = {}

    for job in COUNTRIES:
        if datetime.now(timezone.utc) >= deadline:
            log("sample sprint: fetch deadline reached")
            break
        name = job["display"]
        n = fetch_country_sample(job)
        if n <= 0:
            log(f"{name}: no sample records, skipping crawl")
            continue
        while sum(_alive(pid) for pid in crawls.values()) >= MAX_CRAWLS:
            if datetime.now(timezone.utc) >= deadline + timedelta(minutes=8):
                break
            time.sleep(2)
            crawls = {k: p for k, p in crawls.items() if _alive(p)}
        crawls[name] = _spawn(crawl_cmd(job["path"]))
        log(f"{name}: starting language crawl pid={crawls[name]} ({n} records)")

    while any(_alive(pid) for pid in crawls.values()):
        if datetime.now(timezone.utc) >= deadline + timedelta(minutes=10):
            log("sample sprint: stopping leftover crawls after grace period")
            for pid in crawls.values():
                if _alive(pid):
                    try:
                        os.kill(pid, 15)
                    except OSError:
                        pass
            break
        time.sleep(3)
        crawls = {k: p for k, p in crawls.items() if _alive(p)}

    log("sample sprint finished")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
