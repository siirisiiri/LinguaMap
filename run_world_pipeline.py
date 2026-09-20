#!/usr/bin/env python3
"""Fetch OSM websites and classify languages, country by country.

Default coverage is Europe, Asia, Australia/Oceania, then Africa, Central
America, South America, the United States, and Canada. Existing data/*.json
files are never overwritten; language crawls resume with --skip-good.

Usage:
    python3 run_world_pipeline.py
    python3 run_world_pipeline.py --continents europe,asia,australia-oceania
    python3 run_world_pipeline.py --continents africa,central-america,south-america,north-america
    python3 run_world_pipeline.py --start denmark
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
GEOFABRIK_INDEX = ROOT / ".geofabrik" / "index-v1.json"
LOG_DIR = ROOT / "logs"
STATUS_PATH = LOG_DIR / "world_pipeline_status.json"
PYTHON = sys.executable

# Skip continent-scale blobs and regional composites. Country extracts only.
SKIP_IDS = {
    "africa",
    "antarctica",
    "asia",
    "australia-oceania",
    "central-america",
    "europe",
    "north-america",
    "south-america",
    "russia",
    "alps",
    "azores",
    "britain-and-ireland",
    "dach",
    "great-britain",  # covered by united-kingdom
    "guernsey-jersey",
    "isle-of-man",
}

# Smaller European extracts first so the global map starts showing coverage.
EUROPE_FIRST = [
    "monaco",
    "liechtenstein",
    "andorra",
    "malta",
    "faroe-islands",
    "luxembourg",
    "iceland",
    "estonia",
    "latvia",
    "lithuania",
    "slovenia",
    "montenegro",
    "macedonia",
    "albania",
    "kosovo",
    "moldova",
    "cyprus",
    "bosnia-herzegovina",
    "croatia",
    "slovakia",
    "denmark",
    "ireland-and-northern-ireland",
    "finland",
    "norway",
    "sweden",
    "hungary",
    "bulgaria",
    "serbia",
    "greece",
    "czech-republic",
    "austria",
    "belgium",
    "netherlands",
    "portugal",
    "romania",
    "belarus",
    "georgia",
    "switzerland",
    "poland",
    "spain",
    "italy",
    "united-kingdom",
    "france",
    "germany",
    "ukraine",
    "turkey",
]

CONTINENT_ORDER = [
    "europe",
    "asia",
    "australia-oceania",
    "africa",
    "north-america",
    "central-america",
    "south-america",
]
DEFAULT_CONTINENTS = (
    "europe",
    "asia",
    "australia-oceania",
    "africa",
    "central-america",
    "south-america",
    "north-america",
)
# Large extracts last so smaller countries show up on the map sooner.
ASIA_LAST = ["pakistan", "iran", "indonesia", "japan", "india", "china"]
OCEANIA_LAST = ["new-zealand", "australia"]
AFRICA_LAST = ["egypt", "nigeria", "south-africa"]
SOUTH_AMERICA_LAST = ["argentina", "brazil"]
# User asked for USA and Canada, not Mexico/Greenland.
NORTH_AMERICA_KEEP = {"canada", "us"}

DISPLAY_NAMES = {
    "bosnia-herzegovina": "Bosnia and Herzegovina",
    "congo-brazzaville": "Republic of the Congo",
    "congo-democratic-republic": "Democratic Republic of the Congo",
    "czech-republic": "Czechia",
    "haiti-and-domrep": "Haiti and Dominican Republic",
    "ireland-and-northern-ireland": "Ireland",
    "macedonia": "North Macedonia",
    "swaziland": "Eswatini",
    "ukraine": "Ukraine",
    "united-kingdom": "United Kingdom",
    "us": "United States",
    "us/puerto-rico": "Puerto Rico",
    "us/us-virgin-islands": "US Virgin Islands",
}

# Huge extracts last so a US/Canada/Brazil stall cannot leave Africa empty.
GLOBAL_LAST = [
    "tanzania",
    "kenya",
    "morocco",
    "algeria",
    "egypt",
    "south-africa",
    "nigeria",
    "congo-democratic-republic",
    "chile",
    "peru",
    "colombia",
    "argentina",
    "mexico",
    "brazil",
    "canada",
    "us",
]

STEP_ATTEMPTS = 3
LARGE_PBF_DELETE_BYTES = 800 * 1024 * 1024

# Reuse datasets that were saved under a different filename.
EXISTING_ALIASES = {
    "ukraine": "ukraine.json",
    "switzerland": "Switzerland.json",
}


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp} UTC] {message}", flush=True)


def load_index() -> list[dict]:
    if not GEOFABRIK_INDEX.exists():
        raise SystemExit(
            f"Missing {GEOFABRIK_INDEX}. Run osm_business_websites.py once "
            "to cache the Geofabrik index, or fetch index-v1.json into .geofabrik/."
        )
    data = json.loads(GEOFABRIK_INDEX.read_text(encoding="utf-8"))
    return data.get("features") or []


def country_extracts(features: list[dict]) -> list[dict]:
    out = []
    for feat in features:
        props = feat.get("properties") or {}
        extract_id = props.get("id") or ""
        iso = props.get("iso3166-1:alpha2") or []
        parent = props.get("parent")
        if extract_id in SKIP_IDS:
            continue
        if not iso and extract_id != "kosovo":
            continue
        if parent not in CONTINENT_ORDER:
            continue
        name = (props.get("name") or extract_id).split("(")[0].strip()
        out.append(
            {
                "id": extract_id,
                "name": name,
                "display": DISPLAY_NAMES.get(extract_id, name),
                "parent": parent,
                "iso": iso[0] if iso else None,
                "query": DISPLAY_NAMES.get(extract_id, name),
            }
        )
    return out


def ordered_countries(extracts: list[dict], continents: list[str]) -> list[dict]:
    by_id = {item["id"]: item for item in extracts}
    seen: set[str] = set()
    ordered: list[dict] = []

    def add(item: dict) -> None:
        if item["id"] in seen:
            return
        seen.add(item["id"])
        ordered.append(item)

    if "europe" in continents:
        for extract_id in EUROPE_FIRST:
            if extract_id in by_id:
                add(by_id[extract_id])
    for continent in continents:
        group = [item for item in extracts if item["parent"] == continent]
        if continent == "europe":
            group.sort(key=lambda item: (item["id"] not in EUROPE_FIRST, item["display"]))
        elif continent == "asia":
            group.sort(key=lambda item: (item["id"] in ASIA_LAST, item["display"]))
        elif continent == "australia-oceania":
            group.sort(key=lambda item: (item["id"] in OCEANIA_LAST, item["display"]))
        elif continent == "africa":
            group.sort(key=lambda item: (item["id"] in AFRICA_LAST, item["display"]))
        elif continent == "south-america":
            group.sort(key=lambda item: (item["id"] in SOUTH_AMERICA_LAST, item["display"]))
        elif continent == "north-america":
            group = [item for item in group if item["id"] in NORTH_AMERICA_KEEP]
            group.sort(key=lambda item: (item["id"] == "us", item["display"]))
        else:
            group.sort(key=lambda item: item["display"])
        for item in group:
            add(item)
    if "asia" in continents:
        add(
            {
                "id": "russia",
                "name": "Russian Federation",
                "display": "Russia",
                "parent": "russia",
                "iso": "RU",
                "query": "Russia",
            }
        )
    last_ids = [extract_id for extract_id in GLOBAL_LAST if extract_id in seen]
    if last_ids:
        last_set = set(last_ids)
        ordered = [item for item in ordered if item["id"] not in last_set]
        for extract_id in last_ids:
            ordered.append(by_id[extract_id])
    return ordered


def dataset_path(extract: dict) -> Path:
    alias = EXISTING_ALIASES.get(extract["id"])
    if alias:
        path = DATA_DIR / alias
        if path.exists():
            return path
    wanted = extract["display"].lower()
    for path in DATA_DIR.glob("*.json"):
        if path.name in {"countries.geojson"}:
            continue
        if path.stem.lower() == wanted:
            return path
    return DATA_DIR / f"{extract['display']}.json"


def load_records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"corrupt {path.name} ({exc!r}); removing so it can be re-fetched")
        try:
            path.unlink()
        except OSError:
            pass
        return []
    return data if isinstance(data, list) else []


def unlabeled_count(records: list[dict]) -> int:
    n = 0
    for rec in records:
        lang = rec.get("language")
        if lang is None:
            n += 1
        elif isinstance(lang, list) and not lang:
            n += 1
        elif isinstance(lang, str) and lang.strip() in {"", "unknown", "fetch_failed", "too_little_text"}:
            n += 1
    return n


def write_status(payload: dict, path: Path | None = None) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    (path or STATUS_PATH).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def run_step(title: str, argv: list[str], attempts: int = STEP_ATTEMPTS) -> None:
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        log(f"$ {' '.join(argv)}  (attempt {attempt}/{attempts})")
        result = subprocess.run(argv, cwd=ROOT)
        if result.returncode == 0:
            return
        last_error = RuntimeError(f"{title} failed with exit {result.returncode}")
        if attempt < attempts:
            sleep_s = min(30 * (2 ** (attempt - 1)), 180)
            log(f"{title}: attempt {attempt} failed, retrying in {sleep_s}s")
            time.sleep(sleep_s)
    raise last_error or RuntimeError(f"{title} failed")


def fetch_queries(extract: dict) -> list[str]:
    queries: list[str] = []
    for value in (
        extract.get("query"),
        extract.get("display"),
        extract.get("name"),
        extract["id"].split("/")[-1].replace("-", " "),
    ):
        text = (value or "").strip()
        if text and text not in queries:
            queries.append(text)
    return queries


def pbf_path_for(extract: dict) -> Path:
    leaf = extract["id"].split("/")[-1]
    return ROOT / ".geofabrik" / f"{leaf}-latest.osm.pbf"


def maybe_delete_large_pbf(extract: dict) -> None:
    path = pbf_path_for(extract)
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < LARGE_PBF_DELETE_BYTES:
        return
    log(f"removing {path.name} ({size / 1e9:.1f} GB) to free disk")
    try:
        path.unlink()
    except OSError as exc:
        log(f"could not remove {path.name}: {exc}")
    nidx = Path(str(path) + ".nidx")
    if nidx.exists():
        try:
            nidx.unlink()
        except OSError:
            pass


def process_country(extract: dict, status: dict) -> str:
    path = dataset_path(extract)
    records = load_records(path)
    unlabeled = unlabeled_count(records) if records else 0
    status["current"] = extract["display"]
    status["current_id"] = extract["id"]
    write_status(status)

    if not records:
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
        unlabeled = unlabeled_count(records)
        if records:
            maybe_delete_large_pbf(extract)
    else:
        log(
            f"{extract['display']}: keeping {path.name} "
            f"({len(records)} records, {unlabeled} unlabeled)"
        )

    if not records:
        return "empty"

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


def parse_continents(raw: str | None, europe_only: bool) -> list[str]:
    if europe_only:
        return ["europe"]
    names = [part.strip().lower() for part in (raw or ",".join(DEFAULT_CONTINENTS)).split(",") if part.strip()]
    unknown = [name for name in names if name not in CONTINENT_ORDER]
    if unknown:
        raise SystemExit(
            f"Unknown continent(s) {unknown}; choose from {', '.join(CONTINENT_ORDER)}"
        )
    return names


def read_status(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def europe_names_from_status(status: dict) -> list[str]:
    queue = status.get("queue") or []
    if "Algeria" in queue:
        return queue[: queue.index("Algeria")]
    return queue


def europe_section_done(status: dict) -> bool:
    europe_names = europe_names_from_status(status)
    if not europe_names:
        return False
    finished = {c.get("name") for c in status.get("completed") or []}
    finished.update(c.get("name") for c in status.get("failed") or [])
    return all(name in finished for name in europe_names)


def europe_entered_africa(status: dict) -> bool:
    queue = status.get("queue") or []
    current = status.get("current")
    if not current or "Algeria" not in queue:
        return False
    return current in queue[queue.index("Algeria") :]


def stop_process_tree(pid: int) -> None:
    log(f"Stopping Europe pipeline pid {pid} before it starts Africa")
    subprocess.run(["pkill", "-P", str(pid)], check=False)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def wait_for_europe_and_oceania(
    europe_status: Path,
    oceania_status: Path,
    stop_europe_pid: int | None,
) -> None:
    log(
        f"Waiting until Europe finishes ({europe_status}) "
        f"and Oceania finishes ({oceania_status})"
    )
    stopped_europe = False
    while True:
        eu = read_status(europe_status)
        oc = read_status(oceania_status)
        if stop_europe_pid and not stopped_europe and europe_entered_africa(eu):
            stop_process_tree(stop_europe_pid)
            stopped_europe = True
        europe_done = europe_section_done(eu)
        oceania_done = bool(oc.get("finished_at"))
        if europe_done and oceania_done:
            log("Europe and Oceania are done; continuing with the next continents")
            return
        time.sleep(30)


def main() -> None:
    global STATUS_PATH
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--continents",
        default=",".join(DEFAULT_CONTINENTS),
        help="Comma-separated Geofabrik parents "
        "(default: europe,asia,australia-oceania,africa,central-america,south-america,north-america)",
    )
    parser.add_argument("--europe-only", action="store_true")
    parser.add_argument("--start", default=None, help="Geofabrik id or country name to begin at")
    parser.add_argument("--limit", type=int, default=None, help="Max countries this run")
    parser.add_argument(
        "--status-file",
        default=None,
        help="Status JSON path (default: logs/world_pipeline_status.json)",
    )
    parser.add_argument(
        "--wait-for-europe-and-oceania",
        action="store_true",
        help="Delay this run until the Europe and Asia/Oceania jobs have finished those continents",
    )
    parser.add_argument(
        "--europe-status",
        default=str(LOG_DIR / "world_pipeline_status.json"),
    )
    parser.add_argument(
        "--oceania-status",
        default=str(LOG_DIR / "asia_oceania_pipeline_status.json"),
    )
    parser.add_argument(
        "--stop-europe-pid",
        type=int,
        default=None,
        help="If the old Europe job starts Africa early, stop that pid and wait for Oceania",
    )
    args = parser.parse_args()
    if args.status_file:
        STATUS_PATH = Path(args.status_file)

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    continents = parse_continents(args.continents, args.europe_only)
    extracts = country_extracts(load_index())
    countries = ordered_countries(extracts, continents)
    if args.start:
        key = args.start.strip().lower().replace(" ", "-")
        idx = next(
            (
                i
                for i, item in enumerate(countries)
                if key in {item["id"], item["display"].lower().replace(" ", "-")}
            ),
            None,
        )
        if idx is None:
            raise SystemExit(f"No country matching --start {args.start!r}")
        countries = countries[idx:]
    if args.limit is not None:
        countries = countries[: args.limit]

    status = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "continents": continents,
        "queue": [item["display"] for item in countries],
        "completed": [],
        "failed": [],
        "current": None,
        "waiting_for": ["europe", "oceania"] if args.wait_for_europe_and_oceania else [],
    }
    write_status(status)

    if args.wait_for_europe_and_oceania:
        wait_for_europe_and_oceania(
            Path(args.europe_status),
            Path(args.oceania_status),
            args.stop_europe_pid,
        )
        status["waiting_for"] = []
        write_status(status)

    log(
        f"Pipeline starting: {len(countries)} countries "
        f"({', '.join(continents)})"
    )

    for i, extract in enumerate(countries, 1):
        log(f"--- {i}/{len(countries)} {extract['display']} ({extract['id']}) ---")
        try:
            result = process_country(extract, status)
            status["completed"].append(
                {"id": extract["id"], "name": extract["display"], "result": result}
            )
        except Exception as exc:
            log(f"{extract['display']}: FAILED {exc!r}")
            status["failed"].append(
                {"id": extract["id"], "name": extract["display"], "error": repr(exc)}
            )
        status["current"] = None
        write_status(status)
        time.sleep(1)

    if status["failed"]:
        retry = list(status["failed"])
        status["failed"] = []
        log(f"Retrying {len(retry)} failed countries once more")
        write_status(status)
        for item in retry:
            extract = next((c for c in countries if c["id"] == item["id"]), None)
            if extract is None:
                status["failed"].append(item)
                continue
            log(f"--- retry {extract['display']} ({extract['id']}) ---")
            try:
                result = process_country(extract, status)
                status["completed"].append(
                    {"id": extract["id"], "name": extract["display"], "result": result}
                )
            except Exception as exc:
                log(f"{extract['display']}: FAILED again {exc!r}")
                status["failed"].append(
                    {"id": extract["id"], "name": extract["display"], "error": repr(exc)}
                )
            status["current"] = None
            write_status(status)
            time.sleep(1)

    status["finished_at"] = datetime.now(timezone.utc).isoformat()
    write_status(status)
    log(
        f"Pipeline finished. completed={len(status['completed'])} "
        f"failed={len(status['failed'])}"
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
