#!/usr/bin/env python3
"""
Build lang_history for OSM businesses from Geofabrik yearly snapshots.

Ukraine uses name / name:uk / name:ru. Wales uses name / name:cy / name:en,
which is how we watch Welsh appearing on shopfronts without hitting Wayback.

Usage:
    python3 osm_name_history.py --region ukraine --data data/Ukraine.json
    python3 osm_name_history.py --region wales --data data/Wales.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter

try:
    import osmium
except ImportError:
    sys.exit("Needs pyosmium. Install with: pip install osmium")

UA = "LinguaMap/0.1 (OSM name history; contact: YOUR_EMAIL@example.com)"

UK_MARKS = "їєґ"
RU_MARKS = "ыэё"
WELSH_MARKS = "ŵŷẁẃẅỳýÿ"
WORD_RE = re.compile(r"[A-Za-zÀ-ÿŵŷẁẃẅỳýÿ']+", re.IGNORECASE)
WELSH_NAME_WORDS = frozenset({
    "siop", "ysgol", "caffi", "tafarn", "capel", "eglwys", "swyddfa",
    "llyfrgell", "meddygfa", "fferyllfa", "cymru", "cymraeg", "gymraeg",
    "ganolfan", "canolfan", "gwesty", "bwyd", "bara", "gofal", "cartref",
    "deintydd", "optegydd", "marchnad", "cwmni", "cyngor", "llywodraeth",
    "menter", "croeso", "cymdeithas",
})


def stamp_to_date(stamp: str) -> str:
    return f"20{stamp[0:2]}-{stamp[2:4]}-{stamp[4:6]}"


def classify_uk_ru_name(name: str) -> str | None:
    text = (name or "").lower()
    if not text:
        return None
    uk = sum(text.count(ch) for ch in UK_MARKS)
    ru = sum(text.count(ch) for ch in RU_MARKS)
    if "і" in text:
        uk += 2
    if uk >= 1 and uk > ru:
        return "ukrainian"
    if ru >= 1 and ru > uk:
        return "russian"
    return None


def classify_ukraine_tags(tags: dict[str, str]) -> tuple[str | None, list[str]]:
    uk_tag = (tags.get("name:uk") or "").strip()
    ru_tag = (tags.get("name:ru") or "").strip()
    name = (tags.get("name") or "").strip()
    available: set[str] = set()
    if uk_tag:
        available.add("ukrainian")
    if ru_tag:
        available.add("russian")
    primary = classify_uk_ru_name(name)
    if primary:
        available.add(primary)
        return primary, sorted(available)
    name_l = name.lower()
    if name and uk_tag and name_l == uk_tag.lower():
        return "ukrainian", sorted(available)
    if name and ru_tag and name_l == ru_tag.lower():
        return "russian", sorted(available)
    return None, sorted(available)


# osm_business_websites.py still imports this name for Ukraine crawls.
classify_tags = classify_ukraine_tags


def classify_welsh_name(name: str) -> str | None:
    text = (name or "").strip()
    if not text:
        return None
    lower = text.lower()
    if any(ch in lower for ch in WELSH_MARKS):
        return "welsh"
    tokens = {t.lower() for t in WORD_RE.findall(text)}
    if tokens & WELSH_NAME_WORDS:
        return "welsh"
    if any("a" <= ch.lower() <= "z" or ch in "àáâäéèêëîïóôöúùûü" for ch in text):
        return "english"
    return None


def classify_wales_tags(tags: dict[str, str]) -> tuple[str | None, list[str]]:
    cy = (tags.get("name:cy") or "").strip()
    en = (tags.get("name:en") or "").strip()
    name = (tags.get("name") or "").strip()
    available: set[str] = set()
    if cy:
        available.add("welsh")
    if en:
        available.add("english")
    name_l = name.lower()
    if name and cy and name_l == cy.lower():
        available.add("welsh")
        return "welsh", sorted(available)
    if name and en and name_l == en.lower():
        available.add("english")
        return "english", sorted(available)
    primary = classify_welsh_name(name)
    if primary:
        available.add(primary)
        return primary, sorted(available)
    return None, sorted(available)


REGIONS = {
    "ukraine": {
        "url": "https://download.geofabrik.de/europe/ukraine-{stamp}.osm.pbf",
        "file": "ukraine-{stamp}.osm.pbf",
        # First-of-year extracts Geofabrik still hosts.
        "stamps": (
            "190101", "200101", "210101", "220101",
            "230101", "240101", "250101",
        ),
        "classify": classify_ukraine_tags,
        "default_data": "data/Ukraine.json",
    },
    "wales": {
        "url": "https://download.geofabrik.de/europe/united-kingdom/wales-{stamp}.osm.pbf",
        "file": "wales-{stamp}.osm.pbf",
        # Public yearly extracts start 2014-01-01; nothing older is still hosted.
        "stamps": tuple(f"{y:02d}0101" for y in range(14, 27)),
        "classify": classify_wales_tags,
        "default_data": "data/Wales.json",
    },
}


class SnapshotHandler(osmium.SimpleHandler):
    def __init__(self, wanted: set[tuple[str, int]], classify):
        super().__init__()
        self.wanted = wanted
        self.classify = classify
        self.found: dict[tuple[str, int], dict] = {}

    def _take(self, kind: str, osm_id: int, tags) -> None:
        key = (kind, osm_id)
        if key not in self.wanted:
            return
        tagmap = {t.k: t.v for t in tags}
        primary, available = self.classify(tagmap)
        self.found[key] = {
            "primary": primary,
            "available": available,
            "name": tagmap.get("name") or "",
        }

    def node(self, n):
        self._take("node", n.id, n.tags)

    def way(self, w):
        self._take("way", w.id, w.tags)

    def relation(self, r):
        self._take("relation", r.id, r.tags)


def download(url: str, dest: str) -> None:
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    print(f"Downloading {url} -> {dest}", flush=True)
    subprocess.run(
        ["curl", "-L", "-C", "-", "-f", "--retry", "5", "--retry-delay", "5",
         "-A", UA, "-o", dest, url],
        check=True,
    )


def extract_snapshot(pbf: str, wanted: set[tuple[str, int]], classify) -> dict:
    print(f"Reading {pbf} ...", flush=True)
    handler = SnapshotHandler(wanted, classify)
    handler.apply_file(pbf, locations=False)
    print(f"  matched {len(handler.found)} / {len(wanted)} current businesses", flush=True)
    return handler.found


def build_intervals(obs: list[tuple[str, dict]]) -> list[dict]:
    intervals: list[dict] = []
    for date, rec in obs:
        primary = rec.get("primary")
        available = list(rec.get("available") or [])
        if not primary and not available:
            continue
        # For Wales, an added name:cy with an unchanged English sign is still
        # a change in Welsh *use*, so the available set is part of the state.
        state = (primary, tuple(available))
        if intervals and intervals[-1]["_state"] == state:
            intervals[-1]["to"] = date
            intervals[-1]["observations"] += 1
            continue
        intervals.append({
            "_state": state,
            "from": date,
            "to": date,
            "primary": primary,
            "available": available,
            "observations": 1,
        })
    for iv in intervals:
        del iv["_state"]
    return intervals


def guess_region(data_path: str) -> str | None:
    name = os.path.basename(data_path).lower()
    if "ukraine" in name:
        return "ukraine"
    if "wales" in name:
        return "wales"
    return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--region", choices=sorted(REGIONS), default=None)
    p.add_argument("--data", default=None)
    p.add_argument("--output", default=None)
    p.add_argument("--pbf-dir", default=".geofabrik")
    p.add_argument("--stamps", default=None,
                   help="Comma-separated Geofabrik YYMMDD stamps")
    args = p.parse_args()

    region_name = args.region or (guess_region(args.data) if args.data else None)
    if not region_name:
        p.error("Pass --region ukraine|wales, or a --data path that names one")
    region = REGIONS[region_name]
    data_path = args.data or region["default_data"]
    stamps = (args.stamps.split(",") if args.stamps else list(region["stamps"]))
    classify = region["classify"]

    records = json.load(open(data_path, encoding="utf-8"))
    wanted = {(r["osm_type"], int(r["osm_id"])) for r in records
              if r.get("osm_type") and r.get("osm_id") is not None}
    print(f"{region_name}: {len(records)} records, {len(wanted)} unique OSM ids", flush=True)

    series: dict[tuple[str, int], list[tuple[str, dict]]] = {k: [] for k in wanted}
    for stamp in stamps:
        stamp = stamp.strip()
        dest = os.path.join(args.pbf_dir, region["file"].format(stamp=stamp))
        if not os.path.exists(dest) or os.path.getsize(dest) < 1_000_000:
            download(region["url"].format(stamp=stamp), dest)
        found = extract_snapshot(dest, wanted, classify)
        date = stamp_to_date(stamp)
        for key, rec in found.items():
            series[key].append((date, rec))

    today = subprocess.check_output(["date", "+%Y-%m-%d"], text=True).strip()
    present = 0
    for rec in records:
        if rec.get("osm_id") is None:
            continue
        key = (rec.get("osm_type"), int(rec["osm_id"]))
        if key not in series:
            continue
        name = rec.get("name") or ""
        last = series[key][-1] if series[key] else None
        # The JSON only stores the displayed name. If it has not changed since
        # the last yearly extract, keep that extract's name:* tags so a Welsh
        # (or Ukrainian) translation tag is not dropped at "today".
        if last and name == (last[1].get("name") or ""):
            series[key].append((today, last[1]))
            present += 1
            continue
        primary, available = classify({"name": name})
        if available or primary:
            series[key].append((today, {
                "primary": primary,
                "available": available,
                "name": name,
            }))
            present += 1
    print(f"Appended current names for {present} records as {today}", flush=True)

    stats: Counter = Counter()
    with_hist = 0
    changed = 0
    for rec in records:
        if rec.get("osm_id") is None:
            rec.pop("lang_history", None)
            rec.pop("history_source", None)
            continue
        key = (rec.get("osm_type"), int(rec["osm_id"]))
        intervals = build_intervals(series.get(key) or [])
        if not intervals:
            rec.pop("lang_history", None)
            rec.pop("history_source", None)
            stats["no_history"] += 1
            continue
        rec["lang_history"] = intervals
        rec["history_source"] = "osm_name"
        with_hist += 1
        stats["with_history"] += 1
        if len(intervals) > 1:
            changed += 1
            stats["changed"] += 1
            a, b = intervals[0]["primary"], intervals[-1]["primary"]
            stats[f"{a} -> {b}"] += 1
            if "welsh" in (intervals[-1].get("available") or []) and "welsh" not in (intervals[0].get("available") or []):
                stats["gained welsh"] += 1
            if "welsh" in (intervals[0].get("available") or []) and "welsh" not in (intervals[-1].get("available") or []):
                stats["lost welsh"] += 1

    out = args.output or data_path
    with open(out, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
        f.write("\n")
    print(f"\nWrote {out}")
    print(f"  with history {with_hist}  changed {changed}")
    for k, n in stats.most_common(20):
        print(f"  {k:40s} {n}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
