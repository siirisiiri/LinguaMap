#!/usr/bin/env python3
"""Scratch: language mix of .ua homepages per Common Crawl snapshot.

Locates the ccTLD slice of each crawl's columnar index by reading parquet
footers (cheap), then aggregates `content_languages` for homepages only.
"""
import concurrent.futures as cf
import gzip
import subprocess
import sys
import time

import duckdb

BASE = "https://data.commoncrawl.org/"
TLD = "ua"


def part_paths(crawl: str) -> list[str]:
    for attempt in range(5):
        raw = subprocess.run(
            ["curl", "-s", "-m", "90", "--retry", "3", "--retry-delay", "5",
             f"{BASE}crawl-data/{crawl}/cc-index-table.paths.gz"],
            capture_output=True, check=True,
        ).stdout
        try:
            lines = gzip.decompress(raw).decode().split("\n")
        except gzip.BadGzipFile:
            time.sleep(20 * (attempt + 1))  # throttled; back off
            continue
        return [BASE + l for l in lines if "/subset=warc/" in l]
    raise RuntimeError(f"could not fetch index paths for {crawl}")


def slice_files(con, paths: list[str], tld: str) -> list[str]:
    lo_key, hi_key = f"{tld},", f"{tld}-"

    def rng(p):
        c = con.cursor()
        for attempt in range(4):
            try:
                return p, c.execute(
                    f"""SELECT min(stats_min_value), max(stats_max_value)
                        FROM parquet_metadata('{p}') WHERE path_in_schema='url_surtkey'"""
                ).fetchone()
            except Exception:
                time.sleep(3 * (attempt + 1))
        return p, (None, None)

    with cf.ThreadPoolExecutor(8) as ex:
        out = list(ex.map(rng, paths))
    return [p for p, (lo, hi) in out if lo and hi and not (hi < lo_key or lo > hi_key)]


def crawl_mix(con, crawl: str):
    paths = part_paths(crawl)
    t = time.time()
    files = slice_files(con, paths, TLD)
    scan_s = time.time() - t
    if not files:
        return None
    lst = "[" + ",".join(f"'{f}'" for f in files) + "]"
    t = time.time()
    df = con.execute(f"""
        SELECT
          CASE
            WHEN content_languages IS NULL OR content_languages = '' THEN 'none'
            WHEN split_part(content_languages, ',', 1) = 'ukr' THEN 'ukr-primary'
            WHEN split_part(content_languages, ',', 1) = 'rus' THEN 'rus-primary'
            WHEN split_part(content_languages, ',', 1) = 'eng' THEN 'eng-primary'
            ELSE 'other'
          END AS primary_lang,
          count(DISTINCT url_host_registered_domain) AS hosts
        FROM read_parquet({lst})
        WHERE url_surtkey >= '{TLD},' AND url_surtkey < '{TLD}-'
          AND url_host_tld = '{TLD}' AND fetch_status = 200
          AND url_path IN ('/', '')
        GROUP BY 1
    """).fetchdf()
    return df, len(paths), len(files), scan_s, time.time() - t


def main(crawls):
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET enable_progress_bar=false;")
    print(f"{'crawl':<20} {'parts':>6} {'ukr':>8} {'rus':>8} {'eng':>7} {'other':>7} {'none':>7}  {'ukr share':>9}")
    for crawl in crawls:
        try:
            res = crawl_mix(con, crawl)
        except Exception as e:
            print(f"{crawl:<20} ERROR {type(e).__name__}: {e}")
            continue
        if res is None:
            print(f"{crawl:<20} no matching index parts")
            continue
        df, n_parts, n_files, scan_s, q_s = res
        d = dict(zip(df["primary_lang"], df["hosts"]))
        ukr, rus = d.get("ukr-primary", 0), d.get("rus-primary", 0)
        eng, other, none = d.get("eng-primary", 0), d.get("other", 0), d.get("none", 0)
        share = ukr / (ukr + rus) if (ukr + rus) else float("nan")
        print(f"{crawl:<20} {n_files:>2}/{n_parts:<3} {ukr:>8} {rus:>8} {eng:>7} {other:>7} {none:>7}  {share:>8.1%}"
              f"   (scan {scan_s:.0f}s, query {q_s:.0f}s)", flush=True)
        time.sleep(20)  # be polite between crawls


if __name__ == "__main__":
    main(sys.argv[1:])
