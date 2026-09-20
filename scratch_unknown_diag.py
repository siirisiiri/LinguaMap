#!/usr/bin/env python3
"""Diagnose why URLs came back with an empty language list.

Pass A replays a random sample of the unknown URLs with the production
timeouts at modest concurrency. Pass B retries whatever failed in pass A with
generous timeouts and low concurrency, which separates "site is genuinely
unreachable" from "we gave up too early / got throttled".
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import ssl
from collections import Counter
from urllib.parse import urlparse

import aiohttp

import crawl_languages as c

PROD = dict(connect=2.0, sock_read=10.0, total=12.0)
RELAXED = dict(connect=10.0, sock_read=30.0, total=45.0)


def categorize_exception(e: BaseException) -> str:
    if isinstance(e, asyncio.TimeoutError):
        return "timeout"
    if isinstance(e, aiohttp.TooManyRedirects):
        return "too_many_redirects"
    if isinstance(e, aiohttp.ClientConnectorCertificateError) or isinstance(e, ssl.SSLError):
        return "tls_error"
    if isinstance(e, aiohttp.ClientConnectorSSLError):
        return "tls_error"
    if isinstance(e, aiohttp.ClientConnectorError):
        msg = str(e).lower()
        if "name or service not known" in msg or "nodename nor servname" in msg or "getaddrinfo" in msg:
            return "dns_error"
        if "refused" in msg:
            return "conn_refused"
        return "conn_error"
    if isinstance(e, aiohttp.ServerDisconnectedError):
        return "server_disconnect"
    if isinstance(e, aiohttp.ClientOSError):
        return "conn_reset"
    if isinstance(e, aiohttp.ClientResponseError):
        return f"http_{e.status}"
    if isinstance(e, aiohttp.ClientPayloadError):
        return "payload_error"
    if isinstance(e, UnicodeError):
        return "bad_hostname"
    return f"other:{type(e).__name__}"


async def probe(session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore) -> dict:
    out = {"url": url, "category": None, "status": None, "words": 0, "langs": []}
    async with sem:
        try:
            async with session.get(url, allow_redirects=True, max_redirects=c.MAX_REDIRECTS) as resp:
                out["status"] = resp.status
                if resp.status >= 400:
                    out["category"] = f"http_{resp.status}"
                    return out
                ctype = (resp.headers.get("Content-Type") or "").lower()
                buf = bytearray()
                async for chunk in resp.content.iter_chunked(c.READ_CHUNK):
                    buf.extend(chunk)
                    if len(buf) >= c.MAX_BYTES:
                        break
                text = buf.decode("utf-8", errors="ignore")
                if "html" not in ctype and "xml" not in ctype and "<html" not in text[:4000].lower():
                    out["category"] = f"non_html:{ctype.split(';')[0] or 'unknown'}"
                    return out
                if not text.strip():
                    out["category"] = "empty_body"
                    return out
                words = c.visible_words(text, c.N_WORDS)
                out["words"] = len(words)
                langs = c.classify_html(text)
                out["langs"] = langs
                if langs:
                    out["category"] = "would_classify"
                elif len(words) < c.MIN_HITS * 4:
                    out["category"] = "too_little_text"
                else:
                    out["category"] = "abstain_unmatched_language"
                return out
        except Exception as e:  # noqa: BLE001 - diagnostic bucketing
            out["category"] = categorize_exception(e)
            return out


async def run_pass(urls: list[str], limits: dict, workers: int) -> list[dict]:
    timeout = aiohttp.ClientTimeout(
        total=limits["total"], sock_connect=limits["connect"], sock_read=limits["sock_read"]
    )
    connector = aiohttp.TCPConnector(
        limit=workers, limit_per_host=c.DEFAULT_PER_HOST, ttl_dns_cache=600,
        ssl=False, enable_cleanup_closed=True,
    )
    headers = {
        "User-Agent": c.USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-CA,en;q=0.9,fr;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    sem = asyncio.Semaphore(workers)
    async with aiohttp.ClientSession(
        timeout=timeout, connector=connector, headers=headers,
        max_line_size=c.MAX_HEADER_BYTES, max_field_size=c.MAX_HEADER_BYTES,
    ) as session:
        return await asyncio.gather(*(probe(session, u, sem) for u in urls))


def show(title: str, rows: list[dict], total_hint: int | None = None) -> None:
    counts = Counter(r["category"] for r in rows)
    n = total_hint or len(rows)
    print(f"\n{title}  (n={len(rows)})")
    for key, cnt in counts.most_common():
        print(f"  {key:34s} {cnt:5d}  {100.0*cnt/n:5.1f}%")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default="data/Quebec.json")
    ap.add_argument("--sample", type=int, default=600)
    ap.add_argument("--workers", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="scratch_unknown_diag.json")
    args = ap.parse_args()

    records = json.load(open(args.data, encoding="utf-8"))
    seen: set[str] = set()
    unknown: list[str] = []
    for rec in records:
        u = c.normalize_url(rec.get("website", ""))
        if not u or u in seen:
            continue
        seen.add(u)
        if not rec.get("language"):
            unknown.append(u)

    random.seed(args.seed)
    sample = random.sample(unknown, min(args.sample, len(unknown)))
    print(f"unknown unique URLs: {len(unknown)}; sampling {len(sample)}")

    rows_a = asyncio.run(run_pass(sample, PROD, args.workers))
    show("PASS A - production timeouts, concurrency %d" % args.workers, rows_a)

    retry = [r["url"] for r in rows_a if r["category"] not in
             ("would_classify", "abstain_unmatched_language", "too_little_text")
             and not r["category"].startswith(("http_4", "non_html"))]
    print(f"\nretrying {len(retry)} failures with relaxed timeouts, concurrency 10")
    rows_b = asyncio.run(run_pass(retry, RELAXED, 10))
    show("PASS B - relaxed timeouts, concurrency 10 (pass A failures only)", rows_b)

    recovered = [r for r in rows_b if r["category"] == "would_classify"]
    print(f"\nrecovered on relaxed retry: {len(recovered)}/{len(retry)} "
          f"({100.0*len(recovered)/max(len(retry),1):.1f}% of retried, "
          f"{100.0*len(recovered)/len(sample):.1f}% of sample)")

    hosts = Counter(urlparse(r["url"]).netloc.lower() for r in rows_a
                    if r["category"] != "would_classify")
    print("\ntop failing hosts in sample:")
    for host, cnt in hosts.most_common(12):
        print(f"  {cnt:4d}  {host}")

    json.dump({"pass_a": rows_a, "pass_b": rows_b}, open(args.out, "w"), indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
