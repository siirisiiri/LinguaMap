#!/usr/bin/env python3
"""Scratch: per-host language transitions between two Common Crawl snapshots.

Joins homepage language labels for the same registered domain across two
crawls, so we can see how many .ua sites actually switched primary language
rather than just how the aggregate mix moved (which confounds switching with
entry/exit of sites).
"""
import sys

import duckdb

from scratch_cc_timeseries import TLD, part_paths, slice_files


def host_lang_cte(con, crawl: str) -> str:
    files = slice_files(con, part_paths(crawl), TLD)
    if not files:
        raise RuntimeError(f"no index parts for {crawl}")
    lst = "[" + ",".join(f"'{f}'" for f in files) + "]"
    # One row per host: the primary language of its homepage.
    return f"""
        SELECT url_host_registered_domain AS host,
               any_value(split_part(content_languages, ',', 1)) AS lang,
               any_value(content_languages) AS langs
        FROM read_parquet({lst})
        WHERE url_surtkey >= '{TLD},' AND url_surtkey < '{TLD}-'
          AND url_host_tld = '{TLD}' AND fetch_status = 200
          AND url_path IN ('/', '')
          AND content_languages IS NOT NULL AND content_languages <> ''
        GROUP BY 1
    """


def main(crawl_a: str, crawl_b: str):
    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs; SET enable_progress_bar=false;")
    # data.commoncrawl.org throttles aggressively; retry rather than die mid-scan.
    con.execute("SET http_retries=12; SET http_retry_wait_ms=3000; SET http_retry_backoff=2; SET threads=4;")
    for name, crawl in (("a", crawl_a), ("b", crawl_b)):
        print(f"materializing {crawl} ...", flush=True)
        con.execute(f"CREATE TABLE {name} AS {host_lang_cte(con, crawl)}")
        print(f"  {con.execute(f'SELECT count(*) FROM {name}').fetchone()[0]} hosts", flush=True)
    df = con.execute("""
        SELECT a.lang AS from_lang, b.lang AS to_lang, count(*) AS hosts
        FROM a JOIN b USING (host)
        WHERE a.lang IN ('ukr','rus') AND b.lang IN ('ukr','rus')
        GROUP BY 1, 2 ORDER BY hosts DESC
    """).fetchdf()

    print(f"\nhosts present as a homepage in BOTH {crawl_a} and {crawl_b}:")
    print(df.to_string(index=False))
    tot = df["hosts"].sum()
    if tot:
        def g(f, t):
            m = df[(df.from_lang == f) & (df.to_lang == t)]["hosts"]
            return int(m.iloc[0]) if len(m) else 0
        rr, ru, ur, uu = g('rus','rus'), g('rus','ukr'), g('ukr','rus'), g('ukr','ukr')
        print(f"\nbalanced panel n={tot}")
        print(f"  rus -> ukr  {ru:>6}  ({ru/(rr+ru):.1%} of sites that were Russian-primary switched)")
        print(f"  ukr -> rus  {ur:>6}  ({ur/(uu+ur):.1%} of sites that were Ukrainian-primary switched)")
        print(f"  net toward Ukrainian: {ru - ur:+}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
