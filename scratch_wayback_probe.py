#!/usr/bin/env python3
"""Scratch: measure Wayback CDX coverage for a random sample of dataset URLs."""
import asyncio, json, random, sys, collections
from urllib.parse import urlparse, quote
import aiohttp

CDX = "http://web.archive.org/cdx/search/cdx"

async def probe(session, url, sem):
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,statuscode,digest,mimetype",
        "filter": "statuscode:200",
        "collapse": "timestamp:4",   # one row per year
        "limit": "60",
    }
    async with sem:
        try:
            async with session.get(CDX, params=params, timeout=aiohttp.ClientTimeout(total=45)) as r:
                if r.status != 200:
                    return url, None, f"http{r.status}"
                txt = await r.text()
        except Exception as e:
            return url, None, type(e).__name__
    if not txt.strip():
        return url, [], "empty"
    try:
        rows = json.loads(txt)
    except Exception:
        return url, None, "badjson"
    return url, rows[1:] if rows else [], "ok"

async def main(path, n):
    recs = json.load(open(path))
    urls = []
    seen = set()
    for r in recs:
        w = (r.get("website") or "").strip()
        if not w:
            continue
        if not w.startswith(("http://", "https://")):
            w = "http://" + w
        host = urlparse(w).netloc.lower()
        if not host or host in seen:
            continue
        seen.add(host)
        urls.append(w)
    random.seed(7)
    sample = random.sample(urls, min(n, len(urls)))
    sem = asyncio.Semaphore(6)
    async with aiohttp.ClientSession(headers={"User-Agent": "LinguaMap-research/0.1 (contact: YOUR_EMAIL@example.com)"}) as s:
        results = await asyncio.gather(*(probe(s, u, sem) for u in sample))

    status = collections.Counter()
    first_year = collections.Counter()
    years_covered = []
    with_any = 0
    for url, rows, st in results:
        status[st] += 1
        if not rows:
            continue
        with_any += 1
        ys = sorted({row[0][:4] for row in rows})
        years_covered.append(len(ys))
        first_year[ys[0]] += 1

    print(f"sampled {len(sample)} unique hosts from {path}")
    print("probe status:", dict(status))
    print(f"hosts with >=1 archived 200 snapshot: {with_any}/{len(sample)} = {100*with_any/len(sample):.0f}%")
    if years_covered:
        years_covered.sort()
        mid = years_covered[len(years_covered)//2]
        print(f"distinct years covered: median={mid} mean={sum(years_covered)/len(years_covered):.1f} max={max(years_covered)}")
        ge5 = sum(1 for y in years_covered if y >= 5)
        ge10 = sum(1 for y in years_covered if y >= 10)
        print(f"hosts with >=5 distinct years: {ge5} ({100*ge5/len(sample):.0f}% of sample)")
        print(f"hosts with >=10 distinct years: {ge10} ({100*ge10/len(sample):.0f}% of sample)")
        print("first archived year histogram:", dict(sorted(first_year.items())))

if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 60))
