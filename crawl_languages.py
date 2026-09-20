#!/usr/bin/env python3
"""
Lightweight multi-language homepage classifier for OSM websites.

Fetches a small homepage prefix, then labels it from three signals: the
writing system and script-exclusive letters, unique distinctive-word hits,
and language-switch markers (hreflang + nav links). Language data lives in
languages/*.json; adding a language should not require algorithm changes.

To add a language, create languages/<id>.json with `name`, `codes`,
`switch_labels`, `scripts`, and any of: `script_sufficient` (unique writing
system), `exclusive_letters`, `words`. If the writing system is not yet in
languages/_scripts.json, add its Unicode block there too.

Script evidence does most of the work outside the Latin alphabet. Ukrainian
and Russian share most of their short function words but never share і/ї/є/ґ
with ы/э/ё, and Greek, Armenian or Hebrew text needs no vocabulary at all.

`language` is always a list of language names, e.g.:
  ["english"]
  ["english", "welsh"]
  ["english", "french"]
  []   # unknown / fetch failed / too little text

Speed features (quality-preserving):
  - early connection close once a confident label is known
  - short connect/DNS timeout, longer total timeout for slow bodies
  - high global concurrency with low per-host caps
  - skip URLs that already have a non-empty language list
  - batch checkpoints + optional URL sharding for multi-machine runs
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

try:
    # uvloop resolves DNS on the libuv thread pool, which defaults to 4 threads.
    # Must be set before uvloop initializes the pool.
    os.environ.setdefault("UV_THREADPOOL_SIZE", "128")
    import uvloop
    asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
except ImportError:
    pass

# --- fetch limits ---
MAX_BYTES = 192_000
N_WORDS = 200
CONNECT_TIMEOUT = 2.0
SOCK_READ_TIMEOUT = 10.0
TOTAL_TIMEOUT = 12.0
MAX_REDIRECTS = 8
MAX_HEADER_BYTES = 32_768
READ_CHUNK = 8_192
LABEL_CHECK_EVERY = 12_288
DEFAULT_WORKERS = 250
DEFAULT_PER_HOST = 3
DEFAULT_BATCH_SIZE = 500
# asyncio.gather waits for every URL in the batch. If ClientTimeout never
# fires (uvloop FD-reuse bug), one hung socket stalls the whole crawl.
BATCH_HARD_TIMEOUT = TOTAL_TIMEOUT + SOCK_READ_TIMEOUT + 8.0
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

MIN_UNIQUE_HITS = 4
MIN_HITS = MIN_UNIQUE_HITS  # alias used by diagnostic scripts
BILINGUAL_RATIO = 0.35
LANGUAGES_DIR = Path(__file__).resolve().parent / "languages"
SCRIPTS_PATH = LANGUAGES_DIR / "_scripts.json"


@dataclass(frozen=True)
class ExclusiveLetters:
    script: str
    letters: str
    min_hits: int
    min_share: float


@dataclass(frozen=True)
class CyrillicSign:
    mode: str
    letter: str
    min_share: float = 0.0
    leaders: str = ""
    min_hits: int = 0
    blocked_by: frozenset[str] = frozenset()


@dataclass(frozen=True)
class LanguagePack:
    name: str
    codes: tuple[str, ...]
    switch_labels: tuple[str, ...]
    scripts: frozenset[str]
    script_sufficient: bool
    words: frozenset[str]
    exclusive_letters: ExclusiveLetters | None = None
    suppresses: frozenset[str] = frozenset()
    cyrillic_sign: CyrillicSign | None = None


def _parse_language_file(path: Path) -> LanguagePack:
    raw = json.loads(path.read_text(encoding="utf-8"))
    name = str(raw["name"]).strip().lower()
    exclusive = None
    if raw.get("exclusive_letters"):
        el = raw["exclusive_letters"]
        exclusive = ExclusiveLetters(
            script=str(el["script"]).lower(),
            letters=str(el["letters"]).lower(),
            min_hits=int(el.get("min_hits", 3)),
            min_share=float(el.get("min_share", 0.0)),
        )
    sign = None
    if raw.get("cyrillic_sign"):
        cs = raw["cyrillic_sign"]
        sign = CyrillicSign(
            mode=str(cs["mode"]).lower(),
            letter=str(cs.get("letter", "ъ")).lower(),
            min_share=float(cs.get("min_share", 0.0)),
            leaders=str(cs.get("leaders", "")).lower(),
            min_hits=int(cs.get("min_hits", 0)),
            blocked_by=frozenset(str(x).lower() for x in cs.get("blocked_by", [])),
        )
    return LanguagePack(
        name=name,
        codes=tuple(c.strip().lower() for c in raw.get("codes", []) if c),
        switch_labels=tuple(s.strip().lower() for s in raw.get("switch_labels", []) if s),
        scripts=frozenset(s.strip().lower() for s in raw.get("scripts", []) if s),
        script_sufficient=bool(raw.get("script_sufficient", False)),
        words=frozenset(str(w).lower() for w in raw.get("words", [])),
        exclusive_letters=exclusive,
        suppresses=frozenset(str(x).lower() for x in raw.get("suppresses", [])),
        cyrillic_sign=sign,
    )


def load_script_config(path: Path | None = None) -> dict:
    path = path or SCRIPTS_PATH
    return json.loads(path.read_text(encoding="utf-8"))


def load_language_packs(directory: Path | None = None) -> dict[str, LanguagePack]:
    directory = directory or LANGUAGES_DIR
    packs: dict[str, LanguagePack] = {}
    for lang_path in sorted(directory.glob("*.json")):
        if lang_path.name.startswith("_"):
            continue
        pack = _parse_language_file(lang_path)
        if pack.name in packs:
            raise ValueError(f"Duplicate language name {pack.name!r} in {lang_path}")
        packs[pack.name] = pack
    if not packs:
        raise FileNotFoundError(f"No language files in {directory}")
    return packs


def _index_codes(packs: dict[str, LanguagePack]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pack in packs.values():
        for code in pack.codes:
            out[code] = pack.name
    return out


def _index_switch_labels(packs: dict[str, LanguagePack]) -> dict[str, str]:
    out: dict[str, str] = {}
    for pack in packs.values():
        for label in pack.switch_labels:
            out[label] = pack.name
    return out


def _script_blocks(config: dict) -> tuple[tuple[str, int, int], ...]:
    return tuple(
        (str(block["script"]).lower(), int(block["start"]), int(block["end"]))
        for block in config["blocks"]
    )


def _build_word_re(blocks: tuple[tuple[str, int, int], ...], extra: str) -> re.Pattern[str]:
    ranges = "".join(f"{chr(low)}-{chr(high)}" for _, low, high in blocks)
    return re.compile(rf"[{ranges}{re.escape(extra)}]{{2,}}")


SCRIPT_CONFIG = load_script_config()
LANGUAGE_PACKS = load_language_packs()
LANGUAGE_WORDS: dict[str, frozenset[str]] = {name: pack.words for name, pack in LANGUAGE_PACKS.items()}
SUPPORTED_LANGUAGES = tuple(LANGUAGE_PACKS)
CODE_TO_LANGUAGE: dict[str, str] = _index_codes(LANGUAGE_PACKS)
SWITCH_LABEL_TO_LANGUAGE: dict[str, str] = _index_switch_labels(LANGUAGE_PACKS)
SCRIPT_BLOCKS = _script_blocks(SCRIPT_CONFIG)
AMBIGUOUS_PATH_CODES = frozenset(
    str(code).lower() for code in SCRIPT_CONFIG.get("ambiguous_path_codes", [])
)
MIN_SCRIPT_CHARS = int(SCRIPT_CONFIG.get("min_script_chars", 40))
WORD_RE = _build_word_re(SCRIPT_BLOCKS, SCRIPT_CONFIG.get("word_extra_chars", "'"))
# Compatibility alias for wayback_history: exclusive-letter rules from JSON packs.
SCRIPT_MARKERS: dict[str, tuple[str, str, int, float]] = {
    pack.name: (el.script, el.letters, el.min_hits, el.min_share)
    for pack in LANGUAGE_PACKS.values()
    if (el := pack.exclusive_letters)
}

LEGACY_LABEL_TO_LIST: dict[str, list[str]] = {
    "english": ["english"],
    "welsh": ["welsh"],
    "english+welsh": ["english", "welsh"],
    "fetch_failed": [],
    "too_little_text": [],
    "unknown": [],
}

COMMENT_RE = re.compile(r"(?is)<!--.*?-->")
BLOCK_RE = re.compile(
    r"(?is)<(script|style|noscript|svg)[^>]*>.*?(?:</\1>|$)"
)
TAG_RE = re.compile(r"<[^>]+>")
ENTITY_RE = re.compile(r"&(?:[a-z]+|#\d+|#x[0-9a-f]+);", re.I)
BODY_RE = re.compile(r"(?is)<body[^>]*>")
HTML_LANG_RE = re.compile(r"(?is)<html[^>]*\slang=[\"']([^\"']+)[\"']")

HREFLANG_RE = re.compile(
    r"(?is)<link[^>]*hreflang=[\"']([^\"']+)[\"'][^>]*href=[\"']([^\"']+)[\"']|"
    r"<link[^>]*href=[\"']([^\"']+)[\"'][^>]*hreflang=[\"']([^\"']+)[\"']"
)
ANCHOR_RE = re.compile(r"(?is)<a\b([^>]*)>(.*?)</a>")

_SWITCH_ALTS = "|".join(
    re.escape(label) for label in sorted(SWITCH_LABEL_TO_LANGUAGE, key=len, reverse=True)
)
SWITCH_LABEL_RE = re.compile(
    rf"(?is)^\s*(?:"
    rf"{_SWITCH_ALTS}|"
    rf"language\s*[:\-]?\s*(?:{_SWITCH_ALTS})|"
    rf"(?:view\s+)?(?:in\s+)?(?:{_SWITCH_ALTS})|"
    rf"newid\s+iaith|change\s+language|changer\s+de\s+langue|"
    rf"cambiar\s+idioma|sprache\s+ändern|sprache\s+andern"
    rf")\s*$"
)

VOWELS = set("aeiouwyâêîôûŵŷäëïöüáéíóúẃỳàèùåæøœõāēīūůěėįųąęőűýò")


def _script_of(ch: str) -> str | None:
    code = ord(ch)
    for name, low, high in SCRIPT_BLOCKS:
        if low <= code <= high:
            return name
    if ch.isalpha():
        return "latin"
    return None


def normalize_url(url: str) -> str | None:
    url = (url or "").strip()
    if not url or url.startswith(("javascript:", "mailto:", "tel:")):
        return None
    if not url.startswith(("http://", "https://")):
        url = "http://" + url
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    path = parsed.path.lower()
    if path.endswith(SKIP_SUFFIXES):
        return None
    return url


def normalize_language_list(value: object) -> list[str]:
    """Coerce legacy string labels or lists into a sorted unique language list."""
    if value is None:
        return []
    if isinstance(value, list):
        out: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, str):
                continue
            name = item.strip().lower()
            if name in LEGACY_LABEL_TO_LIST and name not in LANGUAGE_WORDS:
                for part in LEGACY_LABEL_TO_LIST[name]:
                    if part not in seen:
                        seen.add(part)
                        out.append(part)
                continue
            if name in LANGUAGE_WORDS and name not in seen:
                seen.add(name)
                out.append(name)
        return sorted(out)
    if isinstance(value, str):
        key = value.strip().lower()
        if key in LEGACY_LABEL_TO_LIST:
            return list(LEGACY_LABEL_TO_LIST[key])
        if key in LANGUAGE_WORDS:
            return [key]
    return []


def is_good_label(value: object) -> bool:
    return bool(normalize_language_list(value))


def _keep_token(word: str) -> bool:
    if len(word) > 32:
        return False
    # Non-Latin scripts have their own vowel systems, or none at all.
    if any(_script_of(ch) not in (None, "latin") for ch in word):
        return True
    return any(ch in VOWELS for ch in word.lower())


def visible_words(html: str, n: int) -> list[str]:
    m = BODY_RE.search(html)
    if m:
        html = html[m.end():]
    html = COMMENT_RE.sub(" ", html)
    html = BLOCK_RE.sub(" ", html)
    text = TAG_RE.sub(" ", html)
    text = ENTITY_RE.sub(" ", text)
    words = [w for w in WORD_RE.findall(text.lower()) if _keep_token(w)]
    return words[:n]


def _code_to_language(code: str) -> str | None:
    raw = (code or "").strip().lower().replace("_", "-")
    if not raw or raw == "x-default":
        return None
    primary = raw.split("-", 1)[0]
    return CODE_TO_LANGUAGE.get(primary)


def languages_from_switchers(html: str) -> set[str]:
    """Languages advertised via hreflang, html lang, or switcher-looking links."""
    found: set[str] = set()

    m = HTML_LANG_RE.search(html)
    if m:
        lang = _code_to_language(m.group(1))
        if lang:
            found.add(lang)

    for hm in HREFLANG_RE.finditer(html):
        code = hm.group(1) or hm.group(4) or ""
        lang = _code_to_language(code)
        if lang:
            found.add(lang)

    for am in ANCHOR_RE.finditer(html):
        attrs, inner = am.group(1), am.group(2)
        text = TAG_RE.sub(" ", inner)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 48 or not SWITCH_LABEL_RE.match(text):
            continue
        low = text.lower()
        href_m = re.search(r"href=[\"']([^\"']+)[\"']", attrs, re.I)
        href = (href_m.group(1) if href_m else "").lower()
        hreflang_m = re.search(r"hreflang=[\"']([^\"']+)[\"']", attrs, re.I)
        if hreflang_m:
            lang = _code_to_language(hreflang_m.group(1))
            if lang:
                found.add(lang)

        for label, lang in SWITCH_LABEL_TO_LANGUAGE.items():
            if low == label or low.endswith(label) or label in low and len(low) <= len(label) + 12:
                # Prefer exact / short labels; avoid "Cyrsiau Cymraeg"-style soft hits
                if low == label or low in {
                    f"language {label}",
                    f"langue {label}",
                    f"idioma {label}",
                    f"sprache {label}",
                    f"view in {label}",
                    f"in {label}",
                }:
                    found.add(lang)
                    break

        path_m = re.search(r"(?:^|/)([a-z]{2,3})(?:/|$|\?)", href)
        if path_m and path_m.group(1) not in AMBIGUOUS_PATH_CODES:
            lang = _code_to_language(path_m.group(1))
            if lang:
                found.add(lang)

    return found


def score_languages(words: list[str]) -> dict[str, int]:
    """Count unique sampled words that appear in each language's list."""
    unique_words = set(words)
    present_scripts = {
        script
        for word in unique_words
        for ch in word
        if (script := _script_of(ch))
    }
    scores: dict[str, int] = {}
    for name, pack in LANGUAGE_PACKS.items():
        if pack.scripts and present_scripts and pack.scripts.isdisjoint(present_scripts):
            scores[name] = 0
            continue
        scores[name] = len(unique_words & pack.words)
    return scores


def languages_from_words(words: list[str]) -> set[str]:
    if not words:
        return set()
    scores = score_languages(words)
    hits = {name: n for name, n in scores.items() if n >= MIN_UNIQUE_HITS}
    if not hits:
        return set()
    # Keep languages with solid unique-word support; if several fire, require
    # weaker ones to be reasonably close to the strongest signal.
    strongest = max(hits.values())
    return {
        name
        for name, n in hits.items()
        if n / strongest >= BILINGUAL_RATIO
    }


def languages_from_script(words: list[str]) -> set[str]:
    """Languages implied by the writing system and script-exclusive letters."""
    text = " ".join(words)
    script_totals: Counter = Counter()
    letters: Counter = Counter()
    for ch in text:
        script = _script_of(ch)
        if script:
            script_totals[script] += 1
            letters[ch.lower()] += 1

    found: set[str] = set()
    for pack in LANGUAGE_PACKS.values():
        if pack.exclusive_letters:
            el = pack.exclusive_letters
            total = script_totals[el.script]
            if total < MIN_SCRIPT_CHARS:
                continue
            hits = sum(letters[mark] for mark in el.letters)
            if hits >= el.min_hits and hits / total >= el.min_share:
                found.add(pack.name)
        elif pack.script_sufficient:
            if any(script_totals[script] >= MIN_SCRIPT_CHARS for script in pack.scripts):
                found.add(pack.name)

    for pack in LANGUAGE_PACKS.values():
        if pack.name in found and pack.suppresses:
            found.difference_update(pack.suppresses)

    cyrillic = script_totals["cyrillic"]
    if cyrillic >= MIN_SCRIPT_CHARS:
        digraph_used: Counter = Counter()
        for pack in LANGUAGE_PACKS.values():
            rule = pack.cyrillic_sign
            if not rule or rule.mode != "digraph" or not letters[rule.letter]:
                continue
            n = sum(
                1
                for i, ch in enumerate(text)
                if ch.lower() == rule.letter
                and i
                and text[i - 1].lower() in rule.leaders
            )
            if n >= rule.min_hits:
                found.add(pack.name)
                digraph_used[rule.letter] += n
        for pack in LANGUAGE_PACKS.values():
            rule = pack.cyrillic_sign
            if not rule or rule.mode != "vowel_share" or not letters[rule.letter]:
                continue
            if found & rule.blocked_by:
                continue
            remaining = letters[rule.letter] - digraph_used[rule.letter]
            if remaining / cyrillic >= rule.min_share:
                found.add(pack.name)
    return found


def languages_from_content(words: list[str]) -> set[str]:
    found = languages_from_script(words) | languages_from_words(words)
    for pack in LANGUAGE_PACKS.values():
        if pack.name in found and pack.suppresses:
            found.difference_update(pack.suppresses)
    return found


def classify_unknown_with_llm(html: str, words: list[str], url: str | None = None) -> list[str]:
    """Stub for LLM language classification and word-list expansion.

    Later this should identify the page language(s), generate a distinctive
    word list for any unknown language, and persist it under languages/ so
    future pages classify locally.
    """
    where = f" for {url}" if url else ""
    print(
        f"LLM backup needed{where}: no language reached {MIN_UNIQUE_HITS} unique "
        f"word hits ({len(words)} sampled words). Would ask an LLM to classify "
        f"and add a language file under {LANGUAGES_DIR.name}/.",
        flush=True,
    )
    return []


def classify_words(words: list[str]) -> list[str]:
    return sorted(languages_from_content(words))


def classify_html(html: str, url: str | None = None) -> list[str]:
    switch = languages_from_switchers(html)
    words = visible_words(html, N_WORDS)
    langs = sorted(switch | languages_from_content(words))
    if langs:
        return langs
    if words:
        return classify_unknown_with_llm(html, words, url=url)
    return []


def confident_label_from_prefix(html: str) -> list[str] | None:
    """
    Return a final language list if the prefix is enough to decide.

    Switcher/hreflang can decide early. Word-list labels wait for a full
    N_WORDS body sample so mixed-language pages are not cut off early.
    """
    switch = languages_from_switchers(html)
    if len(switch) >= 2:
        # Multiple explicit language versions advertised.
        return sorted(switch)
    if not BODY_RE.search(html):
        return None
    words = visible_words(html, N_WORDS)
    if len(words) < N_WORDS:
        return None
    combined = switch | languages_from_content(words)
    if combined:
        return sorted(combined)
    return None


def merge_label(new_label: list[str], prev_label: list[str] | None) -> list[str]:
    """Keep a prior non-empty classification if this run found nothing usable."""
    if not new_label and prev_label:
        return list(prev_label)
    return list(new_label)


async def fetch_and_classify(session: aiohttp.ClientSession, url: str) -> list[str]:
    for attempt in (0, 1):
        try:
            async with session.get(
                url, allow_redirects=True, max_redirects=MAX_REDIRECTS
            ) as resp:
                if resp.status == 429 and attempt == 0:
                    await asyncio.sleep(RATE_LIMIT_BACKOFF_S)
                    continue
                if resp.status >= 400:
                    return []
                buf = bytearray()
                last_check = 0
                early: list[str] | None = None
                async for chunk in resp.content.iter_chunked(READ_CHUNK):
                    buf.extend(chunk)
                    if len(buf) >= MAX_BYTES:
                        break
                    if len(buf) - last_check < LABEL_CHECK_EVERY:
                        continue
                    last_check = len(buf)
                    html = buf.decode("utf-8", errors="ignore")
                    early = confident_label_from_prefix(html)
                    if early is not None:
                        break
                if early is not None:
                    return early
                text = buf.decode("utf-8", errors="ignore")
                if not text.strip():
                    return []
                return classify_html(text, url=url)
        except Exception:
            return []
    return []


async def classify_url(
    session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore
) -> list[str]:
    async with sem:
        try:
            return await fetch_and_classify(session, url)
        except asyncio.CancelledError:
            return []
        except Exception:
            return []


def _loop_exception_handler(loop: asyncio.AbstractEventLoop, context: dict) -> None:
    """Keep uvloop/aiohttp callback failures from aborting the crawl.

    Python 3.13 + uvloop can raise
    `RuntimeError: File descriptor N is used by transport` inside
    `asyncio.Timeout._on_timeout` when a socket is cancelled. That runs in a
    loop callback, so `except` in fetch_and_classify never sees it.
    """
    exc = context.get("exception")
    msg = context.get("message", "Unhandled event-loop exception")
    print(f"event-loop: {msg}: {exc!r}", flush=True)


async def crawl(
    urls: list[str],
    workers: int,
    per_host: int = DEFAULT_PER_HOST,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_batch: Callable[[list[str], list[list[str]], int, int], None] | None = None,
) -> list[list[str]]:
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(_loop_exception_handler)
    timeout = aiohttp.ClientTimeout(
        total=TOTAL_TIMEOUT,
        sock_connect=CONNECT_TIMEOUT,
        sock_read=SOCK_READ_TIMEOUT,
    )
    connector = aiohttp.TCPConnector(
        limit=workers,
        limit_per_host=per_host,
        ttl_dns_cache=600,
        ssl=False,
        enable_cleanup_closed=True,
        force_close=False,
    )
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-GB,en;q=0.9,fr;q=0.8,cy;q=0.7",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }
    sem = asyncio.Semaphore(workers)
    labels: list[list[str]] = []
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
            tasks = [
                asyncio.create_task(classify_url(session, u, sem))
                for u in batch
            ]
            _done, pending = await asyncio.wait(
                tasks, timeout=BATCH_HARD_TIMEOUT
            )
            if pending:
                print(
                    f"  batch timeout: cancelling {len(pending)}/{len(tasks)} hung urls",
                    flush=True,
                )
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
            raw = []
            for task in tasks:
                try:
                    raw.append(task.result())
                except BaseException as exc:
                    raw.append(exc)
            batch_labels: list[list[str]] = []
            for item in raw:
                if isinstance(item, BaseException):
                    print(f"  url failed: {item!r}", flush=True)
                    batch_labels.append([])
                else:
                    batch_labels.append(item)
            labels.extend(batch_labels)
            done = len(labels)
            print(
                f"  progress {done}/{total} "
                f"({100.0 * done / total:.1f}%)",
                flush=True,
            )
            if on_batch:
                try:
                    on_batch(batch, list(batch_labels), done, total)
                except Exception as e:
                    print(f"  checkpoint failed ({done}/{total}): {e!r}", flush=True)
    return labels


def load_records(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_records(path: str, records: list[dict]) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def migrate_record_languages(records: list[dict]) -> int:
    """Convert legacy string language fields to lists. Returns changed count."""
    changed = 0
    for rec in records:
        raw = rec.get("language")
        normalized = normalize_language_list(raw)
        if raw != normalized:
            rec["language"] = normalized
            changed += 1
        else:
            rec["language"] = normalized
    return changed


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


def apply_shard(urls: list[str], shard: str | None) -> list[str]:
    if not shard:
        return urls
    try:
        index_s, count_s = shard.split("/", 1)
        index, count = int(index_s), int(count_s)
    except ValueError as e:
        raise SystemExit(f"Invalid --shard {shard!r}; expected INDEX/COUNT") from e
    if count < 1 or index < 0 or index >= count:
        raise SystemExit(f"Invalid --shard {shard!r}; need 0 <= INDEX < COUNT")
    return [u for i, u in enumerate(urls) if i % count == index]


def previous_url_labels(records: list[dict]) -> dict[str, list[str]]:
    prev: dict[str, list[str]] = {}
    for rec in records:
        url = normalize_url(rec.get("website", ""))
        langs = normalize_language_list(rec.get("language"))
        if url and langs:
            prev.setdefault(url, langs)
    return prev


def annotate_records(
    records: list[dict],
    url_to_lang: dict[str, list[str]],
    prev_url_to_lang: dict[str, list[str]] | None = None,
) -> Counter:
    prev_url_to_lang = prev_url_to_lang or {}
    counts: Counter = Counter()
    for rec in records:
        url = normalize_url(rec.get("website", ""))
        if not url:
            new_lang: list[str] = []
            prev = None
        else:
            new_lang = url_to_lang.get(url, normalize_language_list(rec.get("language")))
            prev = prev_url_to_lang.get(url)
        lang = merge_label(new_lang, prev)
        rec["language"] = lang
        key = ",".join(lang) if lang else "unknown"
        counts[key] += 1
    return counts


def summarize_language_lists(records: Iterable[dict]) -> tuple[Counter, Counter]:
    combo = Counter()
    presence = Counter()
    for rec in records:
        langs = normalize_language_list(rec.get("language"))
        combo[",".join(langs) if langs else "unknown"] += 1
        if not langs:
            presence["unknown"] += 1
        for name in langs:
            presence[name] += 1
    return combo, presence


def main() -> None:
    parser = argparse.ArgumentParser(description="Classify homepage languages from OSM website list.")
    parser.add_argument("--data", default="data/Wales.json")
    parser.add_argument("--output", default=None, help="Write annotated JSON here (default: overwrite --data)")
    parser.add_argument("--limit", type=int, default=None, help="Max unique URLs (default: all)")
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--per-host", type=int, default=DEFAULT_PER_HOST)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--skip-good",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Skip URLs that already have a non-empty language list (default: true)",
    )
    parser.add_argument(
        "--refetch-all",
        action="store_true",
        help="Force refetch of every URL (same as --no-skip-good)",
    )
    parser.add_argument(
        "--migrate-only",
        action="store_true",
        help="Only convert legacy language strings to lists; do not crawl",
    )
    parser.add_argument("--shard", default=None, help="Shard INDEX/COUNT of unique URLs, e.g. 0/8")
    parser.add_argument(
        "--checkpoint",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Rewrite output JSON after each batch (default: true)",
    )
    args = parser.parse_args()
    if args.refetch_all:
        args.skip_good = False

    records = load_records(args.data)
    changed = migrate_record_languages(records)
    out_path = args.output or args.data
    if changed:
        print(f"Migrated {changed} legacy language fields to lists.", flush=True)

    if args.migrate_only:
        write_records(out_path, records)
        combo, presence = summarize_language_lists(records)
        print(f"Wrote {out_path}")
        print("\nCombination counts")
        for key, n in combo.most_common():
            print(f"  {key:40s} {n}")
        print("\nLanguage presence")
        for key, n in presence.most_common():
            print(f"  {key:40s} {n}")
        return

    prev_url_to_lang = previous_url_labels(records)
    prev_combo, _ = summarize_language_lists(records)
    all_urls = unique_urls(records, args.limit)
    all_urls = apply_shard(all_urls, args.shard)

    if args.skip_good:
        urls = [u for u in all_urls if u not in prev_url_to_lang]
        skipped = len(all_urls) - len(urls)
    else:
        urls = all_urls
        skipped = 0

    print(
        f"Crawling {len(urls)} URLs "
        f"(skipped {skipped} already-good; shard={args.shard or 'all'}; "
        f"{len(records)} records) with {args.workers} workers "
        f"(per_host={args.per_host})...",
        flush=True,
    )
    print(f"Supported languages: {', '.join(SUPPORTED_LANGUAGES)}", flush=True)

    url_to_lang: dict[str, list[str]] = {u: list(langs) for u, langs in prev_url_to_lang.items()}
    raw_counts: Counter = Counter()
    kept_urls = 0

    def on_batch(batch: list[str], batch_labels: list[list[str]], done: int, total: int) -> None:
        nonlocal kept_urls
        for url, new_label in zip(batch, batch_labels):
            key = ",".join(new_label) if new_label else "unknown"
            raw_counts[key] += 1
            prev = prev_url_to_lang.get(url)
            merged = merge_label(new_label, prev)
            if merged != new_label:
                kept_urls += 1
            url_to_lang[url] = merged
        if args.checkpoint and args.limit is None:
            try:
                annotate_records(records, url_to_lang, prev_url_to_lang)
                write_records(out_path, records)
                print(f"  checkpoint wrote {out_path} ({done}/{total})", flush=True)
            except Exception as e:
                print(f"  checkpoint write failed ({done}/{total}): {e!r}", flush=True)

    started = time.perf_counter()
    if urls:
        try:
            asyncio.run(
                crawl(
                    urls,
                    workers=args.workers,
                    per_host=args.per_host,
                    batch_size=args.batch_size,
                    on_batch=on_batch,
                )
            )
        except Exception as e:
            print(f"Crawl aborted with {e!r}; writing whatever was checkpointed.", flush=True)
    elapsed = time.perf_counter() - started

    if args.limit is None:
        annotate_records(records, url_to_lang, prev_url_to_lang)
        write_records(out_path, records)
        print(f"\nWrote language field to {out_path}", flush=True)
        combo, presence = summarize_language_lists(records)
    else:
        combo = Counter()
        presence = Counter()
        for u in all_urls:
            langs = url_to_lang.get(u, [])
            combo[",".join(langs) if langs else "unknown"] += 1
            if not langs:
                presence["unknown"] += 1
            for name in langs:
                presence[name] += 1
        if args.output:
            for rec in records:
                url = normalize_url(rec.get("website", ""))
                if url in url_to_lang:
                    rec["language"] = url_to_lang[url]
            write_records(args.output, records)
            print(f"\nWrote partial language field to {args.output}", flush=True)

    print("\nPrevious combination counts")
    for key, n in prev_combo.most_common():
        print(f"  {key:40s} {n}")

    print("\nRaw crawl combinations (URLs fetched this run)")
    for key, n in raw_counts.most_common():
        print(f"  {key:40s} {n}")
    print(f"\nSkipped already-good URLs {skipped}")
    print(f"Kept previous good label on {kept_urls} unique URLs")

    print("\nCombination counts")
    for key, n in combo.most_common():
        print(f"  {key:40s} {n}")
    print("\nLanguage presence (a record can count in multiple)")
    for key, n in presence.most_common():
        print(f"  {key:40s} {n}")

    print(f"\nTotal records/URLs     {sum(combo.values())}")
    print(f"Unique URLs fetched   {len(urls)}")
    print(f"Time taken            {elapsed:.2f}s")
    if elapsed and urls:
        print(f"Throughput            {len(urls) / elapsed:.1f} sites/s")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
