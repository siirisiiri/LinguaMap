#!/usr/bin/env python3
"""Scratch: measure real Wayback replay throughput, so job estimates rest on
a measured number rather than a guess.

Two request types matter for a bisection crawl:
  resolve  - GET with redirects disabled; the 302 Location reveals which
             snapshot a target date maps to, without transferring a body.
  fetch    - full body read (capped) so the page can be classified.
"""
import asyncio
import json
import statistics
import sys
import time
from urllib.parse import urlparse

import aiohttp

UA = {"User-Agent": "LinguaMap-research/0.1 (contact: YOUR_EMAIL@example.com)"}
REPLAY = "https://web.archive.org/web/{ts}id_/{url}"
MAX_BYTES = 64_000


def hosts_from(path, n):
    seen, out = set(), []
    for r in json.load(open(path)):
        w = (r.get("website") or "").strip()
        if not w:
            continue
        if not w.startswith(("http://", "https://")):
            w = "http://" + w
        p = urlparse(w)
        h = p.netloc.lower().split(":")[0]
        if h.startswith("www."):
            h = h[4:]
        if not h.endswith(".ua") or h in seen or p.path.strip("/"):
            continue
        seen.add(h)
        out.append(f"https://{h}/")
        if len(out) >= n:
            break
    return out


async def resolve(session, url, ts, sem, stats):
    t0 = time.perf_counter()
    async with sem:
        try:
            async with session.get(REPLAY.format(ts=ts, url=url), allow_redirects=False,
                                   timeout=aiohttp.ClientTimeout(total=40)) as r:
                loc = r.headers.get("Location", "")
                stats.append((r.status, time.perf_counter() - t0, loc))
                return r.status, loc
        except Exception as e:
            stats.append((type(e).__name__, time.perf_counter() - t0, ""))
            return None, ""


async def fetch(session, url, ts, sem, stats):
    t0 = time.perf_counter()
    async with sem:
        try:
            async with session.get(REPLAY.format(ts=ts, url=url), allow_redirects=True,
                                   timeout=aiohttp.ClientTimeout(total=60)) as r:
                buf = bytearray()
                async for chunk in r.content.iter_chunked(8192):
                    buf.extend(chunk)
                    if len(buf) >= MAX_BYTES:
                        break
                stats.append((r.status, time.perf_counter() - t0, len(buf)))
                return r.status, len(buf)
        except Exception as e:
            stats.append((type(e).__name__, time.perf_counter() - t0, 0))
            return None, 0


def report(name, stats, wall):
    ok = [s for s in stats if isinstance(s[0], int) and s[0] < 400]
    throttled = [s for s in stats if s[0] in (429, 503)]
    errs = [s for s in stats if not isinstance(s[0], int)]
    lat = sorted(s[1] for s in stats)
    p50 = statistics.median(lat) if lat else 0
    p95 = lat[int(0.95 * (len(lat) - 1))] if lat else 0
    print(f"  {name:<28} n={len(stats):<4} ok={len(ok):<4} 429/503={len(throttled):<3} err={len(errs):<3} "
          f"p50={p50:5.2f}s p95={p95:5.2f}s  -> {len(stats)/wall:5.1f} req/s")


async def main(path, n):
    urls = hosts_from(path, n)
    print(f"benchmarking against {len(urls)} Kharkiv .ua homepages\n")
    conn = aiohttp.TCPConnector(limit=64, ttl_dns_cache=600, ssl=False)
    async with aiohttp.ClientSession(headers=UA, connector=conn) as s:
        for conc in (4, 8, 16):
            stats = []
            sem = asyncio.Semaphore(conc)
            t0 = time.perf_counter()
            await asyncio.gather(*(resolve(s, u, "20220601000000", sem, stats) for u in urls))
            report(f"resolve (302 only) c={conc}", stats, time.perf_counter() - t0)
            await asyncio.sleep(3)

        print()
        for conc in (8, 16):
            stats = []
            sem = asyncio.Semaphore(conc)
            t0 = time.perf_counter()
            await asyncio.gather(*(fetch(s, u, "20220601000000", sem, stats) for u in urls))
            report(f"fetch (<=64KB body) c={conc}", stats, time.perf_counter() - t0)
            await asyncio.sleep(3)

        # How often does a target date land on a genuinely nearby snapshot?
        stats = []
        sem = asyncio.Semaphore(8)
        res = await asyncio.gather(*(resolve(s, u, "20220601000000", sem, stats) for u in urls))
        near = miss = 0
        for status, loc in res:
            if status == 302 and "/web/" in loc:
                got = loc.split("/web/")[1][:8]
                if got.isdigit():
                    delta = abs(int(got[:6]) - 202206)
                    near += delta <= 6
                    miss += delta > 6
        print(f"\n  target 2022-06 resolved within 6 months: {near}, further away: {miss}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 60))
