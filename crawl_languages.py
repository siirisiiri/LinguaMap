#!/usr/bin/env python3
"""
Lightweight multi-language homepage classifier for OSM websites.

Fetches a small homepage prefix, then labels it from three signals: the
writing system and script-exclusive letters, distinctive function-word hits,
and language-switch markers (hreflang + nav links).

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

MIN_HITS = 2
BILINGUAL_HITS = 3
BILINGUAL_RATIO = 0.35

# ---------------------------------------------------------------------------
# Language packs: distinctive words + switcher labels + ISO-ish codes.
# Shared/ambiguous short tokens across packs are avoided where possible.
# ---------------------------------------------------------------------------

LANGUAGE_WORDS: dict[str, frozenset[str]] = {
    "english": frozenset({
        "the", "and", "of", "for", "that", "with", "this", "from", "have",
        "has", "not", "but", "they", "you", "was", "are", "been", "were",
        "their", "which", "would", "could", "should", "there", "what",
        "when", "who", "how", "will", "more", "than", "also", "about",
        "into", "your", "our", "his", "her", "its", "these", "those",
        "them", "then", "some", "any", "each", "other", "only", "over",
        "after", "before", "because", "while", "where", "here", "just",
        "most", "such", "very", "please", "welcome", "home", "contact",
        "services", "privacy", "cookies", "opening", "hours",
    }),
    "welsh": frozenset({
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
    }),
    "french": frozenset({
        "les", "des", "une", "est", "dans", "pour", "qui", "que", "avec",
        "nous", "vous", "sont", "cette", "tout", "plus", "mais", "comme",
        "aussi", "bien", "être", "etre", "avoir", "faire", "merci",
        "accueil", "contactez", "confidentialité", "confidentialite",
        "français", "francais", "anglais", "politique", "propos", "notre",
        "votre", "leurs", "elle", "elles", "ils", "était", "etait",
        "étaient", "etaient", "après", "apres", "avant", "entre", "sous",
        "chez", "donc", "alors", "où", "ou", "très", "tres", "encore",
        "toujours", "maintenant", "aujourd", "service", "services",
        "informations", "recherche", "connexion", "inscription",
    }),
    "spanish": frozenset({
        "los", "las", "una", "del", "que", "con", "por", "para", "como",
        "más", "mas", "pero", "todo", "esta", "estos", "estas",
        "hay", "son", "está", "esta", "también", "tambien", "gracias",
        "inicio", "contacto", "español", "espanol", "inglés", "ingles",
        "privacidad", "nosotros", "nuestro", "nuestra", "sobre", "desde",
        "hasta", "entre", "cuando", "donde", "dónde", "porque", "según",
        "segun", "muy", "más", "solo", "sólo", "todos", "todas", "aquí",
        "aqui", "ahora", "después", "despues", "antes", "bienvenida",
        "bienvenido", "acerca", "política", "politica", "cookies",
    }),
    "german": frozenset({
        "der", "die", "das", "und", "ist", "nicht", "mit", "von", "den",
        "dem", "auf", "für", "fur", "eine", "einer", "einem", "einen",
        "werden", "wurde", "haben", "wird", "auch", "nach", "bei", "über",
        "uber", "sowie", "bitte", "willkommen", "kontakt", "deutsch",
        "impressum", "datenschutz", "startseite", "mehr", "oder", "noch",
        "nur", "sich", "wir", "sie", "ihr", "ihre", "ihnen", "kann",
        "können", "konnen", "durch", "zwischen", "unter", "wenn", "weil",
        "aber", "schon", "wieder", "hier", "dort", "diese", "dieser",
        "dieses", "alle", "vom", "zur", "zum",
    }),
    # Canadian Indigenous languages: sparse web function-word evidence, so
    # packs emphasize endonyms + common portal vocabulary; switcher/hreflang
    # detection carries most of the weight.
    "inuktitut": frozenset({
        "inuktitut", "inuit", "nunavut", "inuk", "inuktitutitut",
        "ᐃᓄᒃᑎᑐᑦ", "ᐃᓄᐃᑦ", "ᓄᓇᕗᑦ",
    }),
    "cree": frozenset({
        "nêhiyawêwin", "nehiyawewin", "nêhiyaw", "nehiyaw", "nehiyawak",
        "cree", "iyiniw", "ᓀᐦᐃᔭᐍᐏᐣ",
    }),
    "ojibwe": frozenset({
        "anishinaabemowin", "anishinaabe", "anishinaabeg", "ojibwe",
        "ojibwa", "ojibway", "anishinabe",
    }),
    "mikmaq": frozenset({
        "mi'kmaq", "mikmaq", "mi'kmaw", "mikmaw", "lnu", "lnuismk",
        "miꞌkmaq", "miꞌkmaw",
    }),
    "mohawk": frozenset({
        "kanien'kéha", "kanienkeha", "kanien'keha", "kanienkehaka",
        "mohawk", "kanyen'kéha", "kanyenkeha",
    }),
    "innu": frozenset({
        "innu-aimun", "innu", "aimun", "ilnu", "innush",
    }),
    "dene": frozenset({
        "denesuline", "dëne", "dene", "chipewyan", "denésuliné",
        "denesuliné", "sahtú", "sahtu", "tlicho", "tłı̨chǫ",
    }),
    "blackfoot": frozenset({
        "niitsitapi", "blackfoot", "siksika", "kainai", "piikani",
        "niitsi'powahsin",
    }),
    # Languages of Ukraine. Packs stay disjoint because score_languages gives
    # a shared token to whichever pack is declared first; for these, letter
    # evidence in SCRIPT_MARKERS is the stronger signal anyway.
    "ukrainian": frozenset({
        "що", "але", "від", "або", "дуже", "ласка", "головна", "послуги",
        "детальніше", "також", "щоб", "який", "немає", "українська",
        "українською", "вартість", "замовити", "розклад", "сторінка",
        "зателефонуйте", "наші", "ваші", "адреса", "більше",
    }),
    "russian": frozenset({
        "что", "это", "очень", "пожалуйста", "главная", "подробнее",
        "новости", "если", "чтобы", "можно", "была", "были", "заказать",
        "стоимость", "русский", "сейчас", "здесь", "наши", "ваши",
    }),
    "belarusian": frozenset({
        "беларуская", "беларусь", "што", "кантакты", "галоўная", "навіны",
        "паслугі", "таксама", "падрабязней", "старонка", "нашы",
    }),
    "bulgarian": frozenset({
        "български", "съм", "това", "които", "към", "ще", "също", "моля",
        "начало", "повече", "всички", "каквото", "защото", "дошли",
    }),
    "rusyn": frozenset({
        "русинськый", "русиньскый", "русины", "руснак", "русинська",
    }),
    "crimean tatar": frozenset({
        "qırım", "qırımtatar", "qırımtatarca", "qırımtatarlar",
        "къырым", "къырымтатар", "къырымтатарджа",
    }),
    "romanian": frozenset({
        "și", "este", "pentru", "această", "acest", "despre", "servicii",
        "acasă", "sunt", "către", "română", "românește", "mulțumim",
        "contactați", "informații", "pagina",
    }),
    "hungarian": frozenset({
        "és", "nem", "hogy", "egy", "vagy", "meg", "kapcsolat",
        "kezdőlap", "szolgáltatások", "magyar", "több", "minden", "csak",
        "már", "köszönjük", "elérhetőség", "rólunk", "hírek",
    }),
    "polish": frozenset({
        "się", "nie", "jest", "oraz", "przez", "strona", "główna",
        "usługi", "więcej", "wszystkie", "polski", "dziękujemy",
        "zapraszamy", "można", "naszej", "oferta", "aktualności",
    }),
    "slovak": frozenset({
        "ktoré", "viac", "domov", "služby", "slovenčina", "ďakujeme",
        "všetky", "môže", "stránka", "ponuka", "informácie",
    }),
    "gagauz": frozenset({
        "gagauzca", "gagauz", "gagauziya", "gagauzlar",
    }),
    "greek": frozenset({
        "ελληνικά", "και", "για", "στην", "είναι", "των", "αρχική",
        "επικοινωνία", "υπηρεσίες", "περισσότερα", "μας",
    }),
    "yiddish": frozenset({
        "ייִדיש", "אונדזער", "מיר", "זענען", "פֿון", "אויף",
    }),
    "armenian": frozenset({
        "հայերեն", "մեր", "ենք", "կապ", "ծառայություններ", "մասին",
        "գլխավոր", "նորություններ",
    }),
}

# ISO / BCP47 style codes -> language name
CODE_TO_LANGUAGE: dict[str, str] = {
    "en": "english",
    "eng": "english",
    "cy": "welsh",
    "cym": "welsh",
    "fr": "french",
    "fra": "french",
    "fre": "french",
    "es": "spanish",
    "spa": "spanish",
    "de": "german",
    "deu": "german",
    "ger": "german",
    "iu": "inuktitut",
    "iku": "inuktitut",
    "ike": "inuktitut",
    "ikt": "inuktitut",
    "cr": "cree",
    "cre": "cree",
    "cwd": "cree",
    "csw": "cree",
    "crk": "cree",
    "oj": "ojibwe",
    "oji": "ojibwe",
    "ojg": "ojibwe",
    "ciw": "ojibwe",
    "mic": "mikmaq",
    "moh": "mohawk",
    "moe": "innu",
    "chp": "dene",
    "den": "dene",
    "scs": "dene",
    "bla": "blackfoot",
    "uk": "ukrainian",
    "ukr": "ukrainian",
    "ru": "russian",
    "rus": "russian",
    "be": "belarusian",
    "bel": "belarusian",
    "bg": "bulgarian",
    "bul": "bulgarian",
    "rue": "rusyn",
    "crh": "crimean tatar",
    "ro": "romanian",
    "ron": "romanian",
    "rum": "romanian",
    "mo": "romanian",
    "mol": "romanian",
    "hu": "hungarian",
    "hun": "hungarian",
    "pl": "polish",
    "pol": "polish",
    "sk": "slovak",
    "slk": "slovak",
    "slo": "slovak",
    "gag": "gagauz",
    "el": "greek",
    "ell": "greek",
    "gre": "greek",
    "yi": "yiddish",
    "yid": "yiddish",
    "hy": "armenian",
    "hye": "armenian",
    "arm": "armenian",
}

# Codes that routinely appear in URL paths as something other than a language
# ("/uk/" for United Kingdom, "/be/" for Belgium). hreflang and switcher text
# for these are still trusted; only the path guess is not.
AMBIGUOUS_PATH_CODES = frozenset({"uk", "be", "el", "ro", "sk", "mo", "no", "is"})

# Nav / switcher link labels (lowercased exact-ish matches via SWITCH_LABEL_RE)
SWITCH_LABEL_TO_LANGUAGE: dict[str, str] = {
    "english": "english",
    "anglais": "english",
    "saesneg": "english",
    "inglés": "english",
    "ingles": "english",
    "englisch": "english",
    "welsh": "welsh",
    "cymraeg": "welsh",
    "gymraeg": "welsh",
    "french": "french",
    "français": "french",
    "francais": "french",
    "française": "french",
    "francaise": "french",
    "spanish": "spanish",
    "español": "spanish",
    "espanol": "spanish",
    "castellano": "spanish",
    "german": "german",
    "deutsch": "german",
    "inuktitut": "inuktitut",
    "ᐃᓄᒃᑎᑐᑦ": "inuktitut",
    "cree": "cree",
    "nêhiyawêwin": "cree",
    "nehiyawewin": "cree",
    "ojibwe": "ojibwe",
    "ojibwa": "ojibwe",
    "ojibway": "ojibwe",
    "anishinaabemowin": "ojibwe",
    "mi'kmaq": "mikmaq",
    "mikmaq": "mikmaq",
    "mi'kmaw": "mikmaq",
    "mohawk": "mohawk",
    "kanien'kéha": "mohawk",
    "kanienkeha": "mohawk",
    "innu": "innu",
    "innu-aimun": "innu",
    "dene": "dene",
    "denesuline": "dene",
    "chipewyan": "dene",
    "blackfoot": "blackfoot",
    "niitsitapi": "blackfoot",
    "siksika": "blackfoot",
    "ukrainian": "ukrainian",
    "українська": "ukrainian",
    "українською": "ukrainian",
    "укр": "ukrainian",
    "russian": "russian",
    "русский": "russian",
    "російська": "russian",
    "рус": "russian",
    "belarusian": "belarusian",
    "беларуская": "belarusian",
    "білоруська": "belarusian",
    "bulgarian": "bulgarian",
    "български": "bulgarian",
    "болгарська": "bulgarian",
    "rusyn": "rusyn",
    "русинськый": "rusyn",
    "crimean tatar": "crimean tatar",
    "qırımtatarca": "crimean tatar",
    "kırımtatarca": "crimean tatar",
    "къырымтатарджа": "crimean tatar",
    "кримськотатарська": "crimean tatar",
    "romanian": "romanian",
    "română": "romanian",
    "romana": "romanian",
    "moldovenească": "romanian",
    "румунська": "romanian",
    "hungarian": "hungarian",
    "magyar": "hungarian",
    "magyarul": "hungarian",
    "угорська": "hungarian",
    "polish": "polish",
    "polski": "polish",
    "польська": "polish",
    "slovak": "slovak",
    "slovenčina": "slovak",
    "slovensky": "slovak",
    "gagauz": "gagauz",
    "gagauzca": "gagauz",
    "greek": "greek",
    "ελληνικά": "greek",
    "грецька": "greek",
    "yiddish": "yiddish",
    "ייִדיש": "yiddish",
    "їдиш": "yiddish",
    "armenian": "armenian",
    "հայերեն": "armenian",
    "вірменська": "armenian",
}

SUPPORTED_LANGUAGES = tuple(LANGUAGE_WORDS.keys())

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
# Latin (incl. accents and the extended blocks Polish/Romanian/Slovak need),
# Greek, Cyrillic, Armenian, Hebrew, Canadian Aboriginal Syllabics
WORD_RE = re.compile(
    r"[a-zA-ZÀ-ÖØ-öø-ÿ\u0100-\u024f\u0370-\u03ff\u0400-\u052f\u0530-\u058f"
    r"\u0590-\u05ff\u1400-\u167f\u1e00-\u1eff\u1f00-\u1fff']{2,}"
)
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

VOWELS = set("aeiouwyâêîôûŵŷäëïöüáéíóúẃỳàèù")

# ---------------------------------------------------------------------------
# Script signals.
#
# Outside the Latin alphabet the writing system alone narrows a page to a
# handful of candidates, and inside a script a few exclusive letters separate
# them. This beats function words for Cyrillic in particular: Ukrainian and
# Russian share most short words, but і/ї/є/ґ and ы/э/ё never co-occur in
# monolingual text.
# ---------------------------------------------------------------------------

MIN_SCRIPT_CHARS = 40

SCRIPT_BLOCKS: tuple[tuple[str, int, int], ...] = (
    ("greek", 0x0370, 0x03FF),
    ("greek", 0x1F00, 0x1FFF),
    ("cyrillic", 0x0400, 0x052F),
    ("armenian", 0x0530, 0x058F),
    ("hebrew", 0x0590, 0x05FF),
    ("syllabics", 0x1400, 0x167F),
)

# language -> (script, letters only this language uses, min hits, min share
# of that script's letters). Shares are set an order of magnitude below the
# natural frequency of the letters so a short page still trips them.
SCRIPT_MARKERS: dict[str, tuple[str, str, int, float]] = {
    # Ukrainian also uses і, but so do Belarusian and Rusyn; ї/є/ґ are its own.
    "ukrainian": ("cyrillic", "їєґ", 3, 0.004),
    "russian": ("cyrillic", "ыэё", 3, 0.008),
    "belarusian": ("cyrillic", "ў", 3, 0.003),
    "polish": ("latin", "ąćęłńśźż", 4, 0.004),
    "hungarian": ("latin", "őű", 3, 0.002),
    "romanian": ("latin", "ășțşţ", 4, 0.003),
    "slovak": ("latin", "ľĺŕďťň", 4, 0.003),
    # Dotless i marks the Latin Crimean Tatar orthography; ñ alone would
    # collide with Spanish.
    "crimean tatar": ("latin", "ı", 4, 0.002),
}

YIDDISH_LETTERS = "װױײ"
# Bulgarian uses ъ as a plain vowel (~1.5% of letters); Russian barely uses it.
BULGARIAN_HARD_SIGN_SHARE = 0.006
# Cyrillic Crimean Tatar writes ъ only inside the къ/гъ/нъ digraphs.
CRIMEAN_DIGRAPH_LEADERS = "кгн"


def _script_of(ch: str) -> str | None:
    code = ord(ch)
    for name, low, high in SCRIPT_BLOCKS:
        if low <= code <= high:
            return name
    if ch.isalpha() and (code < 0x0250 or 0x1E00 <= code <= 0x1EFF):
        return "latin"
    return None


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
    scores = {name: 0 for name in LANGUAGE_WORDS}
    for w in words:
        for name, vocab in LANGUAGE_WORDS.items():
            if w in vocab:
                scores[name] += 1
                break  # first matching pack wins; packs are mostly disjoint
    return scores


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
    for name, (script, marks, min_hits, min_share) in SCRIPT_MARKERS.items():
        total = script_totals[script]
        if total < MIN_SCRIPT_CHARS:
            continue
        hits = sum(letters[mark] for mark in marks)
        if hits >= min_hits and hits / total >= min_share:
            found.add(name)

    if "belarusian" in found:
        # Belarusian shares ы/э with Russian; only ў is exclusive to it.
        found.discard("russian")

    # Scripts with a single candidate language in this set.
    if script_totals["armenian"] >= MIN_SCRIPT_CHARS:
        found.add("armenian")
    if script_totals["greek"] >= MIN_SCRIPT_CHARS:
        found.add("greek")
    if script_totals["hebrew"] >= MIN_SCRIPT_CHARS and any(
        letters[ch] for ch in YIDDISH_LETTERS
    ):
        found.add("yiddish")

    cyrillic = script_totals["cyrillic"]
    if cyrillic >= MIN_SCRIPT_CHARS and letters["ъ"]:
        digraphs = sum(
            1
            for i, ch in enumerate(text)
            if ch.lower() == "ъ" and i and text[i - 1].lower() in CRIMEAN_DIGRAPH_LEADERS
        )
        if digraphs >= 3:
            found.add("crimean tatar")
        elif (letters["ъ"] - digraphs) / cyrillic >= BULGARIAN_HARD_SIGN_SHARE and not (
            found & {"ukrainian", "russian", "belarusian"}
        ):
            found.add("bulgarian")
    return found


def languages_from_words(words: list[str]) -> set[str]:
    if not words:
        return set()
    scores = score_languages(words)
    hits = {name: n for name, n in scores.items() if n >= MIN_HITS}
    if not hits:
        return set()
    # Keep languages with solid support; if several fire, require weaker ones
    # to be reasonably close to the strongest signal.
    strongest = max(hits.values())
    out: set[str] = set()
    for name, n in hits.items():
        if n >= BILINGUAL_HITS and n / strongest >= BILINGUAL_RATIO:
            out.add(name)
        elif n >= MIN_HITS and (len(hits) == 1 or n / strongest >= BILINGUAL_RATIO):
            out.add(name)
    return out


def languages_from_content(words: list[str]) -> set[str]:
    return languages_from_script(words) | languages_from_words(words)


def classify_words(words: list[str]) -> list[str]:
    return sorted(languages_from_content(words))


def classify_html(html: str) -> list[str]:
    switch = languages_from_switchers(html)
    words = visible_words(html, N_WORDS)
    return sorted(switch | languages_from_content(words))


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
                return classify_html(text)
        except Exception:
            return []
    return []


async def classify_url(
    session: aiohttp.ClientSession, url: str, sem: asyncio.Semaphore
) -> list[str]:
    async with sem:
        return await fetch_and_classify(session, url)


async def crawl(
    urls: list[str],
    workers: int,
    per_host: int = DEFAULT_PER_HOST,
    batch_size: int = DEFAULT_BATCH_SIZE,
    on_batch: Callable[[list[str], list[list[str]], int, int], None] | None = None,
) -> list[list[str]]:
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
            if on_batch:
                on_batch(batch, list(batch_labels), done, total)
    return labels


def load_records(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_records(path: str, records: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
        f.write("\n")


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
            annotate_records(records, url_to_lang, prev_url_to_lang)
            write_records(out_path, records)
            print(f"  checkpoint wrote {out_path} ({done}/{total})", flush=True)

    started = time.perf_counter()
    if urls:
        asyncio.run(
            crawl(
                urls,
                workers=args.workers,
                per_host=args.per_host,
                batch_size=args.batch_size,
                on_batch=on_batch,
            )
        )
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
