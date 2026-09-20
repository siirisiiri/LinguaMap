#!/usr/bin/env python3
"""A/B the libuv resolver thread pool at high concurrency on cold-DNS URLs."""
import asyncio
import json
import os
import random
import sys
import time

import crawl_languages as c

half = sys.argv[1]  # "a" or "b"

records = json.load(open("data/Quebec.json", encoding="utf-8"))
seen: set[str] = set()
unknown: list[str] = []
for rec in records:
    u = c.normalize_url(rec.get("website", ""))
    if not u or u in seen:
        continue
    seen.add(u)
    if not rec.get("language"):
        unknown.append(u)

# Exclude every URL touched by earlier probes so DNS is genuinely cold.
warm: set[str] = set()
for seed, n in ((7, 600), (99, 4000)):
    random.seed(seed)
    warm.update(random.sample(unknown, n))
cold = [u for u in unknown if u not in warm]

random.seed(123)
pool = random.sample(cold, 1000)
urls = pool[:500] if half == "a" else pool[500:]

t0 = time.perf_counter()
labels = asyncio.run(c.crawl(urls, workers=250, per_host=3, batch_size=500))
ok = sum(1 for lab in labels if lab)
print(
    f"half={half}  UV_THREADPOOL_SIZE={os.environ.get('UV_THREADPOOL_SIZE', 'unset(4)')}  "
    f"workers=250  labeled {ok}/{len(urls)} = {100.0 * ok / len(urls):.1f}%  "
    f"in {time.perf_counter() - t0:.1f}s"
)
