#!/usr/bin/env python3
"""Scratch: for Kharkiv business sites, pull Wayback snapshots across years and
label each as Ukrainian / Russian using script-level cues, to see whether a
ru -> uk switch is detectable from archived HTML."""
import asyncio, json, re, sys, collections
from urllib.parse import urlparse
import aiohttp

CDX = "http://web.archive.org/cdx/search/cdx"
REPLAY = "https://web.archive.org/web/{ts}id_/{url}"
UA = {"User-Agent": "LinguaMap-research/0.1 (contact: YOUR_EMAIL@example.com)"}

UK_CHARS = set("іїєґ")
RU_CHARS = set("ыъэё")
UK_WORDS = {"та","що","для","який","яка","але","він","вона","ми","ви","його","її",
            "це","як","бути","є","не","на","або","від","також","послуги","ціна",
            "ціни","контакти","головна","про","нас","замовити","кошик","доставка",
            "вартість","детальніше","більше","години","телефон","адреса","новини"}
RU_WORDS = {"что","для","это","как","был","была","они","его","ее","мы","вы","так",
            "или","при","все","услуги","цена","цены","контакты","главная","заказать",
            "корзина","доставка","стоимость","подробнее","больше","часы","телефон",
            "адрес","новости","компания","наши"}

TAG_RE = re.compile(r"(?is)<(script|style|noscript)[^>]*>.*?(?:</\1>|$)")
STRIP_RE = re.compile(r"<[^>]+>")
WORD_RE = re.compile(r"[а-яёіїєґA-Za-z']{2,}")
LANG_ATTR = re.compile(r"(?is)<html[^>]*\slang=[\"']([^\"']+)[\"']")


def label(html: str):
    m = LANG_ATTR.search(html)
    html_lang = (m.group(1).lower().split("-")[0] if m else None)
    text = STRIP_RE.sub(" ", TAG_RE.sub(" ", html)).lower()
    words = WORD_RE.findall(text)[:1500]
    uk_c = sum(1 for ch in text if ch in UK_CHARS)
    ru_c = sum(1 for ch in text if ch in RU_CHARS)
    uk_w = sum(1 for w in words if w in UK_WORDS)
    ru_w = sum(1 for w in words if w in RU_WORDS)
    cyr = sum(1 for ch in text if "\u0400" <= ch <= "\u04ff")
    if cyr < 100:
        return None, dict(html_lang=html_lang, uk_c=uk_c, ru_c=ru_c, uk_w=uk_w, ru_w=ru_w, cyr=cyr)
    uk_score = uk_c + 3 * uk_w
    ru_score = ru_c + 3 * ru_w
    if uk_score == 0 and ru_score == 0:
        lab = None
    elif uk_score >= 2 * max(ru_score, 1):
        lab = "uk"
    elif ru_score >= 2 * max(uk_score, 1):
        lab = "ru"
    else:
        lab = "mixed"
    return lab, dict(html_lang=html_lang, uk_c=uk_c, ru_c=ru_c, uk_w=uk_w, ru_w=ru_w, cyr=cyr)


async def cdx(session, url, sem):
    params = [("url", url), ("output", "json"), ("fl", "timestamp,digest"),
              ("filter", "statuscode:200"), ("filter", "mimetype:text/html"),
              ("collapse", "timestamp:4"), ("limit", "40")]
    async with sem:
        try:
            async with session.get(CDX, params=params, timeout=aiohttp.ClientTimeout(total=60)) as r:
                txt = await r.text()
        except Exception:
            return []
    try:
        rows = json.loads(txt) if txt.strip() else []
    except Exception:
        return []
    return rows[1:]


async def fetch(session, ts, url, sem):
    async with sem:
        for _ in range(2):
            try:
                async with session.get(REPLAY.format(ts=ts, url=url), allow_redirects=True,
                                       timeout=aiohttp.ClientTimeout(total=60)) as r:
                    if r.status == 429:
                        await asyncio.sleep(5)
                        continue
                    if r.status >= 400:
                        return None
                    return (await r.read())[:250_000].decode("utf-8", errors="ignore")
            except Exception:
                return None
    return None


async def one_site(session, url, sem_cdx, sem_fetch, want_years):
    rows = await cdx(session, url, sem_cdx)
    if not rows:
        return url, {}
    by_year = {}
    for ts, digest in rows:
        by_year.setdefault(ts[:4], ts)
    picks = {y: by_year[y] for y in want_years if y in by_year}
    out = {}
    for y, ts in picks.items():
        html = await fetch(session, ts, url, sem_fetch)
        if html is None:
            continue
        lab, ev = label(html)
        out[y] = (lab, ev["html_lang"], ev["uk_c"], ev["ru_c"], ev["cyr"])
    return url, out


async def main(path, n, years):
    recs = json.load(open(path))
    seen, urls = set(), []
    for r in recs:
        w = (r.get("website") or "").strip()
        if not w:
            continue
        if not w.startswith(("http://", "https://")):
            w = "http://" + w
        host = urlparse(w).netloc.lower()
        if not host or host in seen or host.endswith(("facebook.com", "instagram.com")):
            continue
        # homepages only: skip deep paths, they archive worse
        if urlparse(w).path.strip("/"):
            continue
        seen.add(host)
        urls.append(w)
    sample = urls[:n]
    sem_cdx, sem_fetch = asyncio.Semaphore(6), asyncio.Semaphore(4)
    async with aiohttp.ClientSession(headers=UA) as s:
        res = await asyncio.gather(*(one_site(s, u, sem_cdx, sem_fetch, years) for u in sample))

    transitions = collections.Counter()
    per_year = {y: collections.Counter() for y in years}
    n_with_data = 0
    for url, out in res:
        if not out:
            continue
        n_with_data += 1
        seq = [(y, out[y][0]) for y in years if y in out]
        for y, lab in seq:
            per_year[y][lab or "none"] += 1
        labs = [l for _, l in seq if l in ("uk", "ru", "mixed")]
        if len(labs) >= 2 and labs[0] != labs[-1]:
            transitions[f"{labs[0]}->{labs[-1]}"] += 1
            print(f"CHANGE {labs[0]}->{labs[-1]}  {url}")
            for y, lab in seq:
                print(f"    {y}: {lab}  html_lang={out[y][1]} uk_chars={out[y][2]} ru_chars={out[y][3]}")
    print()
    print(f"sites sampled {len(sample)}, with any snapshot {n_with_data}")
    for y in years:
        print(f"  {y}: {dict(per_year[y])}")
    print("transitions (first vs last observed):", dict(transitions))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2]), sys.argv[3].split(",")))
