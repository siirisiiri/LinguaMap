#!/usr/bin/env python3
"""
Lightweight homepage language classifier for Wales OSM websites.

Fetches only the start of each homepage, takes the first N visible words,
and scores English vs Welsh using distinctive function-word hits.
Does not follow internal pages or look for language-switcher tags.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from collections import Counter
from urllib.parse import urlparse

import aiohttp

# --- fetch limits (keep this tiny so we can scale) ---
MAX_BYTES = 192_000  # 4x prior 48KB; catches late <body> on store locators
N_WORDS = 200
CONNECT_TIMEOUT = 5
TOTAL_TIMEOUT = 12
MAX_REDIRECTS = 8
MAX_HEADER_BYTES = 32_768
READ_CHUNK = 8_192
WORD_CHECK_EVERY = 24_576
SKIP_SUFFIXES = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
    ".pdf", ".mp4", ".mp3", ".zip", ".css", ".js", ".xml",
    ".json", ".csv", ".doc", ".docx", ".xls", ".xlsx",
)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
RATE_LIMIT_BACKOFF_S = 1.0

# Distinctive tokens only. Shared/ambiguous words (a, i, o, am, on, to, in,
# is, at, an, or, as, be, no, me, if, can, all, dan, pan, dim, gall, hon)
# are excluded so they cannot inflate both scores.
ENGLISH_WORDS = frozenset({
    "the", "and", "of", "for", "that", "with", "this", "from", "have",
    "has", "not", "but", "they", "you", "was", "are", "been", "were",
    "their", "which", "would", "could", "should", "there", "what",
    "when", "who", "how", "will", "more", "than", "also", "about",
    "into", "your", "our", "his", "her", "its", "these", "those",
    "them", "then", "some", "any", "each", "other", "only", "over",
    "after", "before", "because", "while", "where", "here", "just",
    "most", "such", "very", "please", "welcome", "home", "contact",
    "services", "privacy", "cookies", "opening", "hours",
})

# Distinctive Welsh function/content words. Ambiguous short tokens (a, i, o,
# am, eu, na, hi, mis, dod, lle) are omitted.
WELSH_WORDS = frozenset({
    "yn", "yr", "mae", "yw", "oedd", "sydd", "wedi", "gyda", "neu",
    "ond", "fel", "rhwng", "dros", "trwy", "wrth", "ein", "eich",
    "nhw", "hwn", "hyn", "dyma", "dyna", "croeso", "diolch", "cymru",
    "cymraeg", "arall", "hefyd", "yma", "yna", "bydd", "rhaid", "ddim",
    "nid", "nad", "pam", "sut", "pwy", "faint", "iawn", "cyngor",
    "llywodraeth", "cymdeithas", "cwmni", "gwasanaethau", "cysylltwch",
    "hafan", "amdanom", "tudalen", "gwybodaeth", "newyddion", "cyswllt",
    "chwilio", "dewiswch", "iaith", "saesneg", "gymraeg", "gyfer",
    "pobl", "dydd", "noswaith", "prynhawn", "diwrnod", "wythnos",
    "flwyddyn", "heddiw", "yfory", "dweud", "gwneud", "mynd", "cael",
    "ydym", "ydych", "ydyn", "oes", "fy", "eglwys", "menter", "cynnwys",
    "cwcis", "safle", "defnyddio", "rydych", "cytuno", "neidio", "prif",
    "hanfodol", "ydynt", "gwella", "gwasanaeth", "tudalennau", "dewisiadau",
})

COMMENT_RE = re.compile(r"(?is)<!--.*?-->")
BLOCK_RE = re.compile(
    r"(?is)<(script|style|noscript|svg)[^>]*>.*?(?:</\1>|$)"
)
TAG_RE = re.compile(r"<[^>]+>")
ENTITY_RE = re.compile(r"&(?:[a-z]+|#\d+|#x[0-9a-f]+);", re.I)
WORD_RE = re.compile(r"[a-zA-ZâêîôûŵŷäëïöüáéíóúẃỳÀ-ÖØ-öø-ÿ']{2,}")
BODY_RE = re.compile(r"(?is)<body[^>]*>")

MIN_HITS = 2
BILINGUAL_HITS = 3
BILINGUAL_RATIO = 0.35
VOWELS = set("aeiouwyâêîôûŵŷäëïöüáéíóúẃỳ")


def normalize_url(url: str) -> str | None:
    url = (url or "").strip()
    if not url or url.startswith(("javascript:", "mailto:", "tel:")):
        return None
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    path = parsed.path.lower()
    if path.endswith(SKIP_SUFFIXES):
        return None
    return url


def _keep_token(word: str) -> bool:
    if len(word) > 24:
        return False
    return any(ch in VOWELS for ch in word)


def visible_words(html: str, n: int) -> list[str]:
    # Prefer <body> so inlined <head> CSS/theme metadata cannot dominate the
    # first N words when we only download a small prefix of the page.
    m = BODY_RE.search(html)
    if m:
        html = html[m.end():]
    html = COMMENT_RE.sub(" ", html)
    html = BLOCK_RE.sub(" ", html)
    text = TAG_RE.sub(" ", html)
    text = ENTITY_RE.sub(" ", text)
    words = [w for w in WORD_RE.findall(text.lower()) if _keep_token(w)]
    return words[:n]


def classify(words: list[str]) -> str:
    if not words:
        return "unknown"
    en = 0
    cy = 0
    for w in words:
        if w in ENGLISH_WORDS:
            en += 1
        elif w in WELSH_WORDS:
            cy += 1
    if en < MIN_HITS and cy < MIN_HITS:
        return "unknown"
    if en >= BILINGUAL_HITS and cy >= BILINGUAL_HITS:
        stronger = max(en, cy)
        weaker = min(en, cy)
        if weaker / stronger >= BILINGUAL_RATIO:
            return "english+welsh"
    if cy > en and cy >= MIN_HITS:
        return "welsh"
    if en >= MIN_HITS:
        return "english"
    return "unknown"


async def fetch_prefix(session: aiohttp.ClientSession, url: str) -> tuple[str | None, str]:
    """Return (html, 'ok') or (None, 'fetch_failed')."""
    for attempt in (0, 1):
        try:
            async with session.get(
                url, allow_redirects=True, max_redirects=MAX_REDIRECTS
            ) as resp:
                if resp.status == 429 and attempt == 0:
                    await asyncio.sleep(RATE_LIMIT_BACKOFF_S)
                    continue
                if resp.status >= 400:
                    return None, "fetch_failed"
                buf = bytearray()
                last_check = 0
                async for chunk in resp.content.iter_chunked(READ_CHUNK):
                    buf.extend(chunk)
                    if len(buf) >= MAX_BYTES:
                        break
                    if len(buf) - last_check < WORD_CHECK_EVERY:
                        continue
                    last_check = len(buf)
                    html = buf.decode("utf-8", errors="ignore")
                    # Only early-stop once <body> is present, so we do not
                    # freeze on a huge <head> before real page text.
                    if not BODY_RE.search(html):
                        continue
                    if len(visible_words(html, N_WORDS)) >= N_WORDS:
                        break
                text = buf.decode("utf-8", errors="ignore")
                if text.strip():
                    return text, "ok"
                return None, "fetch_failed"
        except Exception:
            return None, "fetch_failed"
    return None, "fetch_failed"


async def classify_url(session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore) -> str:
    async with sem:
        html, status = await fetch_prefix(session, url)
        if status != "ok" or not html:
            return "fetch_failed"
        label = classify(visible_words(html, N_WORDS))
        if label == "unknown":
            return "too_little_text"
        return label


async def crawl(urls: list[str], workers: int, batch_size: int = 500) -> list[str]:
    timeout = aiohttp.ClientTimeout(total=TOTAL_TIMEOUT, sock_connect=CONNECT_TIMEOUT)
    connector = aiohttp.TCPConnector(
        limit=workers,
        limit_per_host=8,
        ttl_dns_cache=300,
        ssl=False,
        enable_cleanup_closed=True,
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9,cy;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    sem = asyncio.Semaphore(workers)
    labels: list[str] = []
    async with aiohttp.ClientSession(
        timeout=timeout,
        connector=connector,
        headers=headers,
        raise_for_status=False,
        max_line_size=MAX_HEADER_BYTES,
        max_field_size=MAX_HEADER_BYTES,
    ) as session:
        total = len(urls)
        for start in range(0, total, batch_size):
            batch = urls[start : start + batch_size]
            batch_labels = await asyncio.gather(
                *(classify_url(session, u, sem) for u in batch)
            )
            labels.extend(batch_labels)
            done = len(labels)
            print(
                f"  progress {done}/{total} "
                f"({100.0 * done / total:.1f}%)",
                flush=True,
            )
    return labels


def load_records(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def unique_urls(records: list[dict], limit: int | None) -> list[str]:
    seen: set[str] = set()
    urls: list[str] = []
    for rec in records:
        url = normalize_url(rec.get("website", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
        if limit is not None and len(urls) >= limit:
            break
    return urls


def annotate_records(records: list[dict], url_to_lang: dict[str, str]) -> Counter:
    counts: Counter = Counter()
    for rec in records:
        url = normalize_url(rec.get("website", ""))
        if not url:
            lang = "fetch_failed"
        else:
            lang = url_to_lang.get(url, "fetch_failed")
        rec["language"] = lang
        counts[lang] += 1
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify homepage language from OSM website list.")
    parser.add_argument("--data", default="data/businesses.json")
    parser.add_argument("--output", default=None, help="Write annotated JSON here (default: overwrite --data)")
    parser.add_argument("--limit", type=int, default=None, help="Max unique URLs (default: all)")
    parser.add_argument("--workers", type=int, default=100)
    args = parser.parse_args()

    records = load_records(args.data)
    urls = unique_urls(records, args.limit)
    print(
        f"Crawling {len(urls)} unique homepages "
        f"({len(records)} records) with {args.workers} workers...",
        flush=True,
    )

    started = time.perf_counter()
    labels = asyncio.run(crawl(urls, args.workers))
    elapsed = time.perf_counter() - started

    url_to_lang = dict(zip(urls, labels))
    # If --limit was set, only annotate records whose URL was crawled;
    # otherwise annotate every record.
    if args.limit is None:
        counts = annotate_records(records, url_to_lang)
        out_path = args.output or args.data
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
            f.write("\n")
        print(f"\nWrote language field to {out_path}", flush=True)
    else:
        counts = Counter(labels)
        if args.output:
            # Annotate only crawled URLs; leave others without the field.
            for rec in records:
                url = normalize_url(rec.get("website", ""))
                if url in url_to_lang:
                    rec["language"] = url_to_lang[url]
            with open(args.output, "w", encoding="utf-8") as f:
                json.dump(records, f, indent=2, ensure_ascii=False)
                f.write("\n")
            print(f"\nWrote partial language field to {args.output}", flush=True)

    buckets = ("english", "welsh", "english+welsh", "too_little_text", "fetch_failed")
    print("\nLanguage counts (records)" if args.limit is None else "\nLanguage counts (unique URLs)")
    for key in buckets:
        print(f"  {key:16s} {counts.get(key, 0)}")
    extra = set(counts) - set(buckets)
    for key in sorted(extra):
        print(f"  {key:16s} {counts[key]}")
    print(f"\nTotal               {sum(counts.values())}")
    print(f"Unique URLs crawled {len(urls)}")
    print(f"Time taken          {elapsed:.2f}s")
    if elapsed:
        print(f"Throughput          {len(urls) / elapsed:.1f} sites/s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
