#!/usr/bin/env python3
"""Fetch + classify the priority list, overlapping crawl(n) with fetch(n+1)."""
from __future__ import annotations

import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path("/Users/aryandaga/Documents/LinguaMap")
sys.path.insert(0, str(ROOT))

from run_world_pipeline import (  # noqa: E402
    PYTHON,
    dataset_path,
    fetch_queries,
    load_records,
    log,
    maybe_delete_large_pbf,
    run_step,
    unlabeled_count,
    write_status,
)

STATUS_PATH = ROOT / "logs" / "priority_status.json"
STATUS_LOCK = threading.Lock()

COUNTRIES = [
    {
        "id": "russia",
        "name": "Russian Federation",
        "display": "Russia",
        "query": "Russia",
        "parent": "russia",
    },
    {
        "id": "chile",
        "name": "Chile",
        "display": "Chile",
        "query": "Chile",
        "parent": "south-america",
    },
    {
        "id": "peru",
        "name": "Peru",
        "display": "Peru",
        "query": "Peru",
        "parent": "south-america",
    },
    {
        "id": "colombia",
        "name": "Colombia",
        "display": "Colombia",
        "query": "Colombia",
        "parent": "south-america",
    },
    {
        "id": "argentina",
        "name": "Argentina",
        "display": "Argentina",
        "query": "Argentina",
        "parent": "south-america",
    },
    {
        "id": "mexico",
        "name": "Mexico",
        "display": "Mexico",
        "query": "Mexico",
        "parent": "north-america",
    },
    {
        "id": "brazil",
        "name": "Brazil",
        "display": "Brazil",
        "query": "Brazil",
        "parent": "south-america",
    },
    {
        "id": "canada",
        "name": "Canada",
        "display": "Canada",
        "query": "Canada",
        "parent": "north-america",
    },
]


def _set_current(status: dict, extract: dict | None, phase: str | None = None) -> None:
    with STATUS_LOCK:
        if extract is None:
            status["current"] = None
            status["current_id"] = None
            status["phase"] = None
        else:
            status["current"] = extract["display"]
            status["current_id"] = extract["id"]
            status["phase"] = phase
        write_status(status, STATUS_PATH)


def fetch_country(extract: dict) -> str:
    path = dataset_path(extract)
    records = load_records(path)
    if records:
        log(
            f"{extract['display']}: OSM file already present "
            f"({len(records)} records, {unlabeled_count(records)} unlabeled)"
        )
        maybe_delete_large_pbf(extract)
        return "kept"

    last_error: Exception | None = None
    for query in fetch_queries(extract):
        log(f"{extract['display']}: fetching OSM websites as {query!r}")
        try:
            run_step(
                f"OSM fetch {extract['display']}",
                [
                    PYTHON,
                    str(ROOT / "osm_business_websites.py"),
                    query,
                    "-o",
                    str(path),
                    "--no-clip",
                ],
            )
            last_error = None
            break
        except Exception as exc:
            last_error = exc
            log(f"{extract['display']}: fetch with {query!r} failed: {exc!r}")
    if last_error:
        raise last_error
    records = load_records(path)
    if records:
        maybe_delete_large_pbf(extract)
    if not records:
        return "empty"
    return "fetched"


def crawl_country(extract: dict) -> str:
    path = dataset_path(extract)
    records = load_records(path)
    if not records:
        return "empty"
    unlabeled = unlabeled_count(records)
    if unlabeled == 0:
        log(f"{extract['display']}: language crawl already complete")
        return "complete"
    log(f"{extract['display']}: classifying {unlabeled} unlabeled URLs")
    run_step(
        f"language crawl {extract['display']}",
        [
            PYTHON,
            str(ROOT / "crawl_languages.py"),
            "--data",
            str(path),
        ],
    )
    return "crawled"


def process_one(extract: dict, nxt: dict | None, status: dict) -> str:
    """Crawl this country while prefetching the next OSM extract."""
    _set_current(status, extract, "crawl+prefetch" if nxt else "crawl")
    crawl_result = "pending"
    prefetch_error: Exception | None = None

    def do_crawl() -> str:
        return crawl_country(extract)

    def do_prefetch() -> None:
        if nxt is None:
            return
        log(f"{nxt['display']}: prefetching OSM extract during {extract['display']} crawl")
        fetch_country(nxt)

    with ThreadPoolExecutor(max_workers=2) as pool:
        crawl_fut = pool.submit(do_crawl)
        prefetch_fut = pool.submit(do_prefetch) if nxt else None
        crawl_result = crawl_fut.result()
        if prefetch_fut is not None:
            try:
                prefetch_fut.result()
            except Exception as exc:
                prefetch_error = exc
                log(
                    f"{nxt['display']}: prefetch failed ({exc!r}); "
                    "will retry on its own turn"
                )

    if crawl_result == "empty" and not load_records(dataset_path(extract)):
        raise RuntimeError(f"no records for {extract['display']}")
    if prefetch_error and nxt is not None:
        status.setdefault("prefetch_warnings", []).append(
            {"id": nxt["id"], "name": nxt["display"], "error": repr(prefetch_error)}
        )
        write_status(status, STATUS_PATH)
    return crawl_result


def main() -> None:
    import run_world_pipeline as pipeline

    pipeline.STATUS_PATH = STATUS_PATH
    status = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "overlap": True,
        "queue": [c["display"] for c in COUNTRIES],
        "completed": [],
        "failed": [],
        "prefetch_warnings": [],
        "current": None,
    }
    write_status(status, STATUS_PATH)
    log(
        f"Priority run starting with overlap: {len(COUNTRIES)} countries "
        "(crawl N || fetch N+1)"
    )

    first = COUNTRIES[0]
    log(f"--- 1/{len(COUNTRIES)} {first['display']} fetch ---")
    _set_current(status, first, "fetch")
    try:
        fetch_country(first)
    except Exception as exc:
        log(f"{first['display']}: FAILED {exc!r}")
        status["failed"].append(
            {"id": first["id"], "name": first["display"], "error": repr(exc)}
        )
        write_status(status, STATUS_PATH)
    else:
        for i, extract in enumerate(COUNTRIES):
            nxt = COUNTRIES[i + 1] if i + 1 < len(COUNTRIES) else None
            log(
                f"--- {i + 1}/{len(COUNTRIES)} {extract['display']} "
                f"({extract['id']})"
                + (f" || prefetch {nxt['display']}" if nxt else "")
                + " ---"
            )
            try:
                if i > 0:
                    fetch_country(extract)
                result = process_one(extract, nxt, status)
                status["completed"].append(
                    {
                        "id": extract["id"],
                        "name": extract["display"],
                        "result": result,
                    }
                )
            except Exception as exc:
                log(f"{extract['display']}: FAILED {exc!r}")
                status["failed"].append(
                    {
                        "id": extract["id"],
                        "name": extract["display"],
                        "error": repr(exc),
                    }
                )
            _set_current(status, None)
            write_status(status, STATUS_PATH)
            time.sleep(1)

    if status["failed"]:
        retry = list(status["failed"])
        status["failed"] = []
        log(f"Retrying {len(retry)} failed countries once more")
        write_status(status, STATUS_PATH)
        for item in retry:
            extract = next(c for c in COUNTRIES if c["id"] == item["id"])
            log(f"--- retry {extract['display']} ({extract['id']}) ---")
            try:
                fetch_country(extract)
                result = crawl_country(extract)
                status["completed"].append(
                    {
                        "id": extract["id"],
                        "name": extract["display"],
                        "result": result,
                    }
                )
            except Exception as exc:
                log(f"{extract['display']}: FAILED again {exc!r}")
                status["failed"].append(
                    {
                        "id": extract["id"],
                        "name": extract["display"],
                        "error": repr(exc),
                    }
                )
            _set_current(status, None)
            write_status(status, STATUS_PATH)
            time.sleep(1)

    status["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_status(status, STATUS_PATH)
    log(
        f"Priority run finished. completed={len(status['completed'])} "
        f"failed={len(status['failed'])}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
