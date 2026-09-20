#!/usr/bin/env python3
"""Scratch: what fraction of OSM business hosts does Common Crawl actually cover?

The question that decides whether CC can carry a temporal language series for
*these specific businesses*, rather than for the web at large. Measures, per
crawl: hosts seen at all, hosts with a fetched homepage, and hosts with a
usable language label (homepage-only vs. any-page).
"""
import json
import sys
from urllib.parse import urlparse

import duckdb

from scratch_cc_timeseries import TLD, part_paths, slice_files


def osm_hosts(path: str) -> list[str]:
    hosts = set()
    for r in json.load(open(path)):
        w = (r.get("website") or "").strip()
        if not w:
            continue
        if not w.startswith(("http://", "https://")):
            w = "http://" + w
        h = urlparse(w).netloc.lower().split(":")[0]
        if h.startswith("www."):
            h = h[4:]
        if h.endswith(f".{TLD}"):
            hosts.add(h)
    return sorted(hosts)


def main(path: str, crawls: list[str]):
    hosts = osm_hosts(path)
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET enable_progress_bar=false;")
    con.execute("SET http_retries=12; SET http_retry_wait_ms=3000; SET http_retry_backoff=2; SET threads=4;")
    con.execute("CREATE TABLE osm(host VARCHAR)")
    con.executemany("INSERT INTO osm VALUES (?)", [(h,) for h in hosts])
    print(f"{len(hosts)} OSM .{TLD} hosts from {path}\n")

    print(f"{'crawl':<18} {'seen':>12} {'homepage':>12} {'home+lang':>12} {'anypage+lang':>13}")
    for crawl in crawls:
        files = slice_files(con, part_paths(crawl), TLD)
        if not files:
            print(f"{crawl:<18} no index parts")
            continue
        lst = "[" + ",".join(f"'{f}'" for f in files) + "]"
        con.execute(f"DROP TABLE IF EXISTS cc")
        con.execute(f"""
            CREATE TABLE cc AS
            SELECT
              CASE WHEN starts_with(url_host_name, 'www.')
                   THEN substr(url_host_name, 5) ELSE url_host_name END AS host,
              count(*) FILTER (WHERE url_path IN ('/', '')) AS homepages,
              count(*) FILTER (WHERE url_path IN ('/', '')
                               AND content_languages IS NOT NULL
                               AND content_languages <> '') AS home_lang,
              count(*) FILTER (WHERE content_languages IS NOT NULL
                               AND content_languages <> '') AS any_lang
            FROM read_parquet({lst})
            WHERE url_surtkey >= '{TLD},' AND url_surtkey < '{TLD}-'
              AND url_host_tld = '{TLD}' AND fetch_status = 200
            GROUP BY 1
        """)
        r = con.execute("""
            SELECT count(*) FILTER (WHERE cc.host IS NOT NULL),
                   count(*) FILTER (WHERE cc.homepages > 0),
                   count(*) FILTER (WHERE cc.home_lang > 0),
                   count(*) FILTER (WHERE cc.any_lang > 0)
            FROM osm LEFT JOIN cc USING (host)
        """).fetchone()
        n = len(hosts)
        print(f"{crawl:<18} " + " ".join(
            f"{v:>6} ({100*v/n:>3.0f}%)" for v in r), flush=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
