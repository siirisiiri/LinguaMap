#!/usr/bin/env python3
"""
Temporal language history for OSM business websites, from the Wayback Machine.

For each site this reconstructs *when* its landing language changed, not just
what it is today, by binary-searching archived snapshots for change points.

Why bisection: about four fifths of sites never change language, so sampling
every month wastes almost all of its requests confirming nothing happened.
Probing a coarse grid and bisecting only the intervals whose endpoints differ
costs a handful of requests per site and pins each change to a tighter bracket
than uniform sampling would.

Two request types, deliberately separated:
  resolve  - GET with redirects disabled. The 302 Location names the snapshot
             a target date maps to, without transferring a body. Measured at
             ~0 throttling, so the search runs on these.
  fetch    - full body read, needed to actually label a snapshot. This is what
             the Internet Archive rate-limits, so it is only issued once per
             *distinct* snapshot; adjacent probes usually collapse onto the
             same capture.

Politeness: adaptive concurrency (additive increase, multiplicative decrease),
exponential backoff with jitter, Retry-After support, and a circuit breaker.
The crawler is checkpointed to an append-only JSONL log, so a throttle or a
laptop lid closing costs time and nothing else.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from urllib.parse import urlparse

import aiohttp

from crawl_languages import (
    AMBIGUOUS_PATH_CODES,
    MIN_SCRIPT_CHARS,
    N_WORDS,
    SCRIPT_MARKERS,
    _code_to_language,
    _script_of,
    apply_shard,
    languages_from_content,
    languages_from_switchers,
    load_records,
    normalize_url,
    score_languages,
    visible_words,
    write_records,
)

try:
    os.environ.setdefault("UV_THREADPOOL_SIZE", "128")
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

_MISSING = object()

REPLAY = "https://web.archive.org/web/{ts}id_/{url}"
TS_RE = re.compile(r"/web/(\d{4,14})(?:id_|im_|if_|cs_|js_)?/")
ORIGINAL_RE = re.compile(r"/web/\d{4,14}(?:id_|im_|if_|cs_|js_)?/(.+)$")

MAX_BYTES = 48_000
READ_CHUNK = 8_192
RESOLVE_TIMEOUT = 30.0
FETCH_TIMEOUT = 60.0

# Measured against web.archive.org: body fetches start returning 429/503 above
# about 8 in flight, while redirect-only lookups stayed clean throughout.
DEFAULT_START_CONCURRENCY = 4
DEFAULT_MAX_CONCURRENCY = 8
MIN_CONCURRENCY = 1
INCREASE_AFTER_CLEAN = 25
BACKOFF_BASE_S = 2.0
BACKOFF_CAP_S = 120.0
MAX_ATTEMPTS = 5

# Circuit breaker: if this fraction of a recent window fails, stop entirely
# for a cooldown rather than digging the hole deeper.
BREAKER_WINDOW = 60
BREAKER_ERROR_RATE = 0.5
BREAKER_COOLDOWN_S = 45.0

# A snapshot this far from the requested month is evidence of a coverage gap,
# not an answer to the question we asked.
MAX_SNAPSHOT_DRIFT_MONTHS = 9
# Too little text to trust a label; recorded as an observation with no label
# so it is distinguishable from "we never looked".
MIN_WORDS_FOR_LABEL = 25


# ---------------------------------------------------------------------------
# Month arithmetic. Months are ints (year * 12 + month - 1) so bisection is
# plain integer midpointing.
# ---------------------------------------------------------------------------

def month_index(year: int, month: int) -> int:
    return year * 12 + (month - 1)


def parse_month(s: str) -> int:
    y, m = s.split("-")
    return month_index(int(y), int(m))


def month_to_timestamp(idx: int) -> str:
    y, m = divmod(idx, 12)
    return f"{y:04d}{m + 1:02d}15000000"


def timestamp_to_month(ts: str) -> int:
    return month_index(int(ts[:4]), int(ts[4:6]))


def timestamp_to_date(ts: str) -> str:
    return f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"


# ---------------------------------------------------------------------------
# Landing language.
#
# `classify_html` answers "which languages does this page offer". For tracking
# a switch that is the wrong question: bilingual ru/uk sites were bilingual
# before and after 2022, and what moved was which language a visitor is served
# by default. So alongside the available set we pick a single primary, from the
# declared html lang, the language path the archive redirected us onto, and the
# body's own script/word evidence.
# ---------------------------------------------------------------------------

HTML_LANG_RE = re.compile(r"(?is)<html[^>]*\slang=[\"']([^\"']+)[\"']")
PATH_CODE_RE = re.compile(r"^/([a-z]{2,3})(?:/|$)")


def original_url(replay_url: str) -> str:
    m = ORIGINAL_RE.search(replay_url)
    return m.group(1) if m else replay_url


def landing_path_language(final_url: str) -> str | None:
    """Language implied by the path the site redirected a neutral visitor to."""
    m = PATH_CODE_RE.match(urlparse(original_url(final_url)).path)
    if not m or m.group(1) in AMBIGUOUS_PATH_CODES:
        return None
    return _code_to_language(m.group(1))


def script_hit_counts(text: str) -> dict[str, int]:
    """Hits on each language's script-exclusive letters, as raw counts.

    crawl_languages returns these as a set; the counts matter here because for
    a bilingual page they are the cleanest evidence of which language dominates.
    """
    totals: Counter = Counter()
    letters: Counter = Counter()
    for ch in text:
        script = _script_of(ch)
        if script:
            totals[script] += 1
            letters[ch.lower()] += 1
    out: dict[str, int] = {}
    for name, (script, marks, min_hits, min_share) in SCRIPT_MARKERS.items():
        total = totals[script]
        if total < MIN_SCRIPT_CHARS:
            continue
        hits = sum(letters[mark] for mark in marks)
        if hits >= min_hits and hits / total >= min_share:
            out[name] = hits
    return out


def primary_language(words: list[str], available: list[str], final_url: str,
                     html: str) -> str | None:
    if not available:
        return None
    if len(available) == 1:
        return available[0]

    scores = {k: v for k, v in score_languages(words).items() if v}
    for name, hits in script_hit_counts(" ".join(words)).items():
        scores[name] = scores.get(name, 0) + hits

    declared = None
    m = HTML_LANG_RE.search(html)
    if m:
        declared = _code_to_language(m.group(1))
    path_lang = landing_path_language(final_url)

    # A declaration is only trusted when the body does not contradict it: sites
    # routinely ship lang="en" boilerplate on a page written in something else.
    for candidate in (path_lang, declared):
        if candidate and candidate in available and scores.get(candidate, 0) > 0:
            return candidate
    if scores:
        return max(scores, key=lambda k: (scores[k], k in available))
    return declared or path_lang or available[0]


@dataclass
class Label:
    primary: str | None
    available: list[str]
    words: int

    def state(self, track: str) -> tuple:
        # Tracking the primary alone is the default because `available` wobbles:
        # a cookie banner, an English footer or a redirect onto a different
        # path can add or drop a language without anything having changed, and
        # every wobble would otherwise register as a switch.
        return (self.primary,) if track == "primary" else (self.primary, tuple(self.available))

    def usable(self) -> bool:
        return self.words >= MIN_WORDS_FOR_LABEL and bool(self.available)


def classify_snapshot(html: str, final_url: str) -> Label:
    words = visible_words(html, N_WORDS)
    available = sorted(languages_from_switchers(html) | languages_from_content(words))
    return Label(primary_language(words, available, final_url, html), available, len(words))


# ---------------------------------------------------------------------------
# Rate control
# ---------------------------------------------------------------------------

class AdaptiveLimiter:
    """AIMD concurrency limiter.

    Climbs one slot per clean streak and halves on any throttle, which finds
    the archive's current ceiling without having to guess it and adapts if that
    ceiling moves mid-run.
    """

    def __init__(self, start: int, maximum: int):
        self.limit = float(start)
        self.maximum = maximum
        self.active = 0
        self.paused_until = 0.0
        self._clean = 0
        self._cond = asyncio.Condition()
        self.throttles = 0
        self.breaker_trips = 0
        self._recent: deque[bool] = deque(maxlen=BREAKER_WINDOW)

    async def acquire(self) -> None:
        while True:
            wait = self.paused_until - time.monotonic()
            if wait > 0:
                await asyncio.sleep(min(wait, 5.0))
                continue
            async with self._cond:
                if self.active < max(MIN_CONCURRENCY, int(self.limit)):
                    self.active += 1
                    return
                try:
                    await asyncio.wait_for(self._cond.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass

    async def release(self) -> None:
        async with self._cond:
            self.active -= 1
            self._cond.notify_all()

    def on_success(self) -> None:
        self._recent.append(True)
        self._clean += 1
        if self._clean >= INCREASE_AFTER_CLEAN and self.limit < self.maximum:
            self.limit = min(self.maximum, self.limit + 1)
            self._clean = 0

    def on_throttle(self, retry_after: float | None = None) -> None:
        self._recent.append(False)
        self.throttles += 1
        self._clean = 0
        self.limit = max(MIN_CONCURRENCY, self.limit / 2)
        pause = retry_after if retry_after else BACKOFF_BASE_S
        self.paused_until = max(self.paused_until, time.monotonic() + pause)
        self._trip_breaker_if_needed()

    def on_error(self) -> None:
        self._recent.append(False)
        self._clean = 0
        self._trip_breaker_if_needed()

    def _trip_breaker_if_needed(self) -> None:
        if len(self._recent) < BREAKER_WINDOW:
            return
        if self._recent.count(False) / len(self._recent) >= BREAKER_ERROR_RATE:
            self.breaker_trips += 1
            self.limit = MIN_CONCURRENCY
            self.paused_until = time.monotonic() + BREAKER_COOLDOWN_S
            self._recent.clear()
            print(
                f"  [breaker] error rate high; pausing {BREAKER_COOLDOWN_S:.0f}s "
                f"and dropping concurrency to {MIN_CONCURRENCY}",
                flush=True,
            )


def _retry_after_seconds(resp: aiohttp.ClientResponse) -> float | None:
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return min(float(raw), BACKOFF_CAP_S)
    except ValueError:
        return None


def _backoff(attempt: int) -> float:
    return min(BACKOFF_BASE_S * (2 ** attempt), BACKOFF_CAP_S) * (0.5 + random.random())


class WaybackClient:
    def __init__(self, session: aiohttp.ClientSession, limiter: AdaptiveLimiter):
        self.session = session
        self.limiter = limiter
        self.resolves = 0
        self.fetches = 0
        self.bytes = 0
        self.errors: Counter = Counter()

    async def _request(self, url: str, *, body: bool, timeout: float):
        for attempt in range(MAX_ATTEMPTS):
            delay: float | None = None
            await self.limiter.acquire()
            try:
                async with self.session.get(
                    url,
                    allow_redirects=body,
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as resp:
                    if resp.status in (429, 503, 504):
                        self.limiter.on_throttle(_retry_after_seconds(resp))
                        delay = _backoff(attempt)
                    elif not body:
                        self.limiter.on_success()
                        return resp.status, resp.headers.get("Location", ""), None
                    elif resp.status >= 400:
                        self.limiter.on_success()  # a clean 404 is not overload
                        return resp.status, str(resp.url), None
                    else:
                        buf = bytearray()
                        async for chunk in resp.content.iter_chunked(READ_CHUNK):
                            buf.extend(chunk)
                            if len(buf) >= MAX_BYTES:
                                break
                        self.limiter.on_success()
                        self.bytes += len(buf)
                        return (resp.status, str(resp.url),
                                buf.decode("utf-8", errors="ignore"))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.errors[type(e).__name__] += 1
                # Refused or dropped connections are how the archive sheds load
                # once it stops answering politely, so treat them as backpressure
                # rather than as random failures to retry at the same rate.
                if isinstance(e, (aiohttp.ClientConnectorError,
                                  aiohttp.ServerDisconnectedError,
                                  asyncio.TimeoutError)):
                    self.limiter.on_throttle()
                else:
                    self.limiter.on_error()
                delay = _backoff(attempt)
            finally:
                # Released before sleeping so a backing-off request does not
                # sit on a concurrency slot it is not using.
                await self.limiter.release()
            if delay is None or attempt == MAX_ATTEMPTS - 1:
                break
            await asyncio.sleep(delay)
        return None, "", None

    async def resolve(self, url: str, month: int) -> str | None:
        """Snapshot timestamp nearest the given month, without fetching a body."""
        self.resolves += 1
        ts = month_to_timestamp(month)
        status, location, _ = await self._request(
            REPLAY.format(ts=ts, url=url), body=False, timeout=RESOLVE_TIMEOUT
        )
        if status is None or status == 404:
            return None
        if status == 200:
            return ts  # exact capture at the requested instant; rare but valid
        m = TS_RE.search(location)
        return m.group(1).ljust(14, "0") if m else None

    async def fetch(self, url: str, ts: str) -> tuple[Label | None, str, int | None]:
        self.fetches += 1
        status, final_url, html = await self._request(
            REPLAY.format(ts=ts, url=url), body=True, timeout=FETCH_TIMEOUT
        )
        if html is None:
            return None, final_url, status
        return classify_snapshot(html, final_url), final_url, status


# ---------------------------------------------------------------------------
# Per-site change-point search
# ---------------------------------------------------------------------------

@dataclass
class SiteResult:
    url: str
    observations: list[dict] = field(default_factory=list)
    status: str = "ok"


class HistorySearch:
    def __init__(self, client: WaybackClient, url: str, lo: int, hi: int, coarse: int,
                 track: str = "primary"):
        self.client = client
        self.url = url
        self.lo, self.hi, self.coarse = lo, hi, coarse
        self.track = track
        self.by_month: dict[int, str | None] = {}
        self.by_snapshot: dict[str, Label | None] = {}
        self.records: dict[str, dict] = {}
        self._snap_locks: dict[str, asyncio.Lock] = {}

    async def label_at(self, month: int, *, peek: bool = False) -> tuple[str | None, Label | None]:
        """Label for the snapshot nearest `month`, fetching each capture once.

        `peek` only does the 302 lookup: used while bisecting so we can stop
        when the midpoint is a capture we already classified, without paying
        for another body.
        """
        ts = self.by_month.get(month, _MISSING)
        if ts is _MISSING:
            ts = await self.client.resolve(self.url, month)
            if ts and abs(timestamp_to_month(ts) - month) > MAX_SNAPSHOT_DRIFT_MONTHS:
                ts = None
            self.by_month[month] = ts
        if ts is None:
            return None, None
        if ts in self.by_snapshot or peek:
            return ts, self.by_snapshot.get(ts)

        lock = self._snap_locks.setdefault(ts, asyncio.Lock())
        async with lock:
            if ts in self.by_snapshot:
                return ts, self.by_snapshot[ts]
            label, final_url, status = await self.client.fetch(self.url, ts)
            self.by_snapshot[ts] = label
            self.records[ts] = {
                "url": self.url,
                "snapshot": ts,
                "date": timestamp_to_date(ts),
                "final_url": original_url(final_url),
                "http_status": status,
                "primary": label.primary if label else None,
                "available": label.available if label else [],
                "words": label.words if label else 0,
            }
            return ts, label

    async def _bisect(self, lo: int, hi: int, lo_state, hi_state, depth: int = 0) -> None:
        # Stop once the bracket is a single month, or once both ends land on the
        # same capture — past that the archive simply has nothing finer to say.
        if hi - lo <= 1 or depth > 8:
            return
        if self.by_month.get(lo) and self.by_month.get(lo) == self.by_month.get(hi):
            return
        mid = (lo + hi) // 2
        ts, mid_label = await self.label_at(mid, peek=True)
        if ts and ts not in self.by_snapshot:
            _, mid_label = await self.label_at(mid)
        if mid_label is None or not mid_label.usable():
            # No usable capture at the midpoint: try each half once, then give up.
            if depth < 3:
                await asyncio.gather(
                    self._bisect(lo, mid, lo_state, hi_state, depth + 1),
                    self._bisect(mid, hi, lo_state, hi_state, depth + 1),
                )
            return
        mid_state = mid_label.state(self.track)
        branches = []
        if mid_state != lo_state:
            branches.append(self._bisect(lo, mid, lo_state, mid_state, depth + 1))
        if mid_state != hi_state:
            branches.append(self._bisect(mid, hi, mid_state, hi_state, depth + 1))
        if branches:
            await asyncio.gather(*branches)

    async def run(self) -> SiteResult:
        grid = list(range(self.lo, self.hi + 1, self.coarse))
        if grid[-1] != self.hi:
            grid.append(self.hi)

        labelled = await asyncio.gather(*(self.label_at(month) for month in grid))
        coarse_states: list[tuple[int, tuple | None]] = []
        for month, (_, label) in zip(grid, labelled):
            coarse_states.append(
                (month, label.state(self.track) if label and label.usable() else None))

        if not any(state for _, state in coarse_states):
            return SiteResult(self.url, [], "no_usable_snapshots")

        # Refine only the spans whose endpoints disagree. Spans with a missing
        # endpoint are left alone: bisecting into a coverage hole burns requests
        # without narrowing anything.
        for (m_lo, s_lo), (m_hi, s_hi) in zip(coarse_states, coarse_states[1:]):
            if s_lo and s_hi and s_lo != s_hi:
                await self._bisect(m_lo, m_hi, s_lo, s_hi)

        obs = [self.records[ts] for ts in sorted(self.records)]
        return SiteResult(self.url, obs, "ok")


# ---------------------------------------------------------------------------
# Observation log (append-only; doubles as the resume checkpoint)
# ---------------------------------------------------------------------------

def read_log(path: str) -> tuple[set[str], dict[str, list[dict]]]:
    done: set[str] = set()
    obs: dict[str, list[dict]] = {}
    if not os.path.exists(path):
        return done, obs
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue  # torn final line from an interrupted run
            if rec.get("complete"):
                done.add(rec["url"])
            elif "snapshot" in rec:
                obs.setdefault(rec["url"], []).append(rec)
    return done, obs


class LogWriter:
    def __init__(self, path: str):
        self.f = open(path, "a", encoding="utf-8")

    def write(self, result: SiteResult) -> None:
        for rec in result.observations:
            self.f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.f.write(json.dumps(
            {"url": result.url, "complete": True, "status": result.status},
            ensure_ascii=False,
        ) + "\n")
        self.f.flush()

    def close(self) -> None:
        self.f.close()


# ---------------------------------------------------------------------------
# Observations -> run-length intervals
# ---------------------------------------------------------------------------

def build_intervals(observations: list[dict], track: str = "primary") -> list[dict]:
    """Collapse consecutive equal states into intervals.

    The gap between one interval's `to` and the next one's `from` is the
    censoring bracket: all we know is the switch happened somewhere inside it.
    Storing it as a gap keeps the uncertainty visible instead of inventing a date.
    """
    usable = [o for o in sorted(observations, key=lambda o: o["snapshot"])
              if o.get("available")]
    intervals: list[dict] = []
    for obs in usable:
        state = ((obs["primary"],) if track == "primary"
                 else (obs["primary"], tuple(obs["available"])))
        path = urlparse(obs.get("final_url") or "").path or "/"
        if intervals and intervals[-1]["_state"] == state:
            intervals[-1]["to"] = obs["date"]
            intervals[-1]["observations"] += 1
            intervals[-1]["_seen"].update(obs["available"])
            intervals[-1]["_paths"][path] += 1
            continue
        intervals.append({
            "_state": state,
            "_seen": set(obs["available"]),
            "_paths": Counter({path: 1}),
            "from": obs["date"],
            "to": obs["date"],
            "primary": obs["primary"],
            "observations": 1,
        })
    for iv in intervals:
        # Union across the interval: which languages the site was seen offering
        # while this primary held.
        iv["available"] = sorted(iv.pop("_seen"))
        # Which archived URL the captures in this interval actually resolved to.
        # A "change" where this also moved (say / -> /ua) may be the archive
        # having captured a different page rather than the site having changed,
        # so keep it attached to the interval for downstream filtering.
        iv["landing_path"] = iv.pop("_paths").most_common(1)[0][0]
        del iv["_state"]
    return intervals


def annotate_records(records: list[dict], obs_by_url: dict[str, list[dict]],
                     track: str = "primary") -> Counter:
    stats: Counter = Counter()
    for rec in records:
        url = normalize_url(rec.get("website", ""))
        obs = obs_by_url.get(url or "")
        if not obs:
            stats["no_history"] += 1
            continue
        intervals = build_intervals(obs, track)
        if not intervals:
            stats["no_usable_label"] += 1
            continue
        rec["lang_history"] = intervals
        stats["with_history"] += 1
        if len(intervals) > 1:
            stats["changed"] += 1
            same_path = all(a["landing_path"] == b["landing_path"]
                            for a, b in zip(intervals, intervals[1:]))
            stats["changed_same_landing_path" if same_path
                  else "changed_with_landing_path_move"] += 1
            first, last = intervals[0]["primary"], intervals[-1]["primary"]
            if first != last:
                stats[f"{first} -> {last}"] += 1
    return stats


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

async def run_all(urls: list[str], args, log: LogWriter) -> None:
    limiter = AdaptiveLimiter(args.start_concurrency, args.max_concurrency)
    headers = {
        # Identified research traffic: the Internet Archive is markedly more
        # tolerant of it, and can reach a human instead of blocking an IP.
        "User-Agent": f"LinguaMap/0.1 (language-map research; contact: {args.contact})",
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }
    connector = aiohttp.TCPConnector(
        limit=args.max_concurrency * 2,
        ttl_dns_cache=600,
        enable_cleanup_closed=True,
        # Matches crawl_languages: several Python installs on macOS have no
        # usable CA bundle, and every request fails verification without this.
        ssl=False,
    )
    lo, hi = parse_month(args.start), parse_month(args.end)
    started = time.perf_counter()
    completed = 0

    async with aiohttp.ClientSession(headers=headers, connector=connector) as session:
        client = WaybackClient(session, limiter)
        queue: asyncio.Queue[str] = asyncio.Queue()
        for url in urls:
            queue.put_nowait(url)

        async def worker() -> None:
            nonlocal completed
            while True:
                try:
                    url = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    result = await HistorySearch(
                        client, url, lo, hi, args.coarse_months, args.track).run()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    result = SiteResult(url, [], f"error:{type(e).__name__}")
                log.write(result)
                completed += 1
                if completed % args.progress_every == 0:
                    elapsed = time.perf_counter() - started
                    rate = completed / elapsed
                    remaining = (len(urls) - completed) / rate if rate else 0
                    print(
                        f"  {completed}/{len(urls)} sites  "
                        f"{client.resolves} resolves  {client.fetches} fetches  "
                        f"conc={limiter.limit:.1f}  throttles={limiter.throttles}  "
                        f"{rate * 60:.0f} sites/min  eta {remaining / 60:.0f}m",
                        flush=True,
                    )

        # One task per maximum slot; the limiter, not the task count, sets the rate.
        await asyncio.gather(*(worker() for _ in range(max(2, args.max_concurrency))))

    elapsed = time.perf_counter() - started
    print(
        f"\nDone in {elapsed / 60:.1f}m: {completed} sites, "
        f"{client.resolves} resolves, {client.fetches} fetches "
        f"({client.bytes / 1e6:.0f} MB), {limiter.throttles} throttles, "
        f"{limiter.breaker_trips} breaker trips",
        flush=True,
    )
    if client.errors:
        print("Transport errors: " + ", ".join(
            f"{k}={v}" for k, v in client.errors.most_common()), flush=True)
    if completed:
        print(f"Requests per site: {(client.resolves + client.fetches) / completed:.1f} "
              f"({client.fetches / completed:.1f} bodies)", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--data", required=True, help="Input records JSON (OSM businesses)")
    p.add_argument("--log", default=None,
                   help="Observation JSONL; also the resume checkpoint "
                        "(default: <data>.history.jsonl)")
    p.add_argument("--output", default=None,
                   help="Write records annotated with lang_history here")
    p.add_argument("--start", default="2019-01", help="Window start YYYY-MM")
    p.add_argument("--end", default="2025-12", help="Window end YYYY-MM")
    p.add_argument("--coarse-months", type=int, default=84,
                   help="Initial grid spacing in months. Default 84 is just the "
                        "window endpoints (2019 and 2025); only sites that actually "
                        "changed get extra Wayback hits.")
    p.add_argument("--track", choices=("primary", "state"), default="primary",
                   help="What counts as a change: the landing language alone, or "
                        "the landing language plus the available set (default: primary)")
    p.add_argument("--limit", type=int, default=None, help="Max unique URLs")
    p.add_argument("--shard", default=None, help="Shard INDEX/COUNT, e.g. 0/8")
    p.add_argument("--start-concurrency", type=int, default=DEFAULT_START_CONCURRENCY)
    p.add_argument("--max-concurrency", type=int, default=DEFAULT_MAX_CONCURRENCY)
    p.add_argument("--contact", default="YOUR_EMAIL@example.com",
                   help="Contact address advertised in the User-Agent")
    p.add_argument("--progress-every", type=int, default=25)
    p.add_argument("--resume", action=argparse.BooleanOptionalAction, default=True)
    p.add_argument("--annotate-only", action="store_true",
                   help="Skip crawling; rebuild lang_history from an existing log")
    args = p.parse_args()

    records = load_records(args.data)
    log_path = args.log or f"{args.data}.history.jsonl"
    done, prior_obs = read_log(log_path)
    if done or prior_obs:
        print(f"Log {log_path}: {len(done)} sites complete, "
              f"{sum(len(v) for v in prior_obs.values())} observations", flush=True)

    seen: set[str] = set()
    urls: list[str] = []
    for rec in records:
        url = normalize_url(rec.get("website", ""))
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    urls = apply_shard(urls, args.shard)
    if args.limit is not None:
        urls = urls[: args.limit]

    if not args.annotate_only:
        todo = [u for u in urls if u not in done] if args.resume else urls
        print(f"{len(records)} records, {len(urls)} unique URLs "
              f"(shard={args.shard or 'all'}), {len(todo)} to crawl", flush=True)
        print(f"Window {args.start}..{args.end}, coarse grid every "
              f"{args.coarse_months} months, concurrency {args.start_concurrency}"
              f"-{args.max_concurrency} (adaptive)", flush=True)
        if todo:
            log = LogWriter(log_path)
            try:
                asyncio.run(run_all(todo, args, log))
            finally:
                log.close()
        done, prior_obs = read_log(log_path)

    stats = annotate_records(records, prior_obs, args.track)
    out_path = args.output or args.data
    write_records(out_path, records)
    print(f"\nWrote {out_path}")
    for key, n in stats.most_common():
        print(f"  {key:40s} {n}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
