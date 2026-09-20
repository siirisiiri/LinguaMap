# LinguaMap

A map of the languages shops, schools, and other local places use on their websites.

LinguaMap looks up places from OpenStreetMap that publish a website, classifies the language of each homepage, and colors countries and subdivisions by what those sites actually use. Click a country to drill into provinces, then counties. Switch between **Language** (which homepages use) and **Multilingual** (how many sites list more than one).

The clone ships with map outlines (`data/admin1/`, `data/admin2/`) and language packs (`languages/`). Classified site lists are generated locally; the viewer picks up every region JSON it finds under `data/`.

## Setup

Python 3.10+ is required (`osmium` reads OSM extracts; `aiohttp` and `uvloop` crawl homepages).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Country outlines are not in the repo. Build them once before opening the map:

```bash
python3 tools/fetch_countries.py
```

## View the map

```bash
python3 serve.py
```

Opens http://127.0.0.1:8000/. Use `--port` or `--no-browser` if needed.

Until you generate at least one region file, the outlines load but there is nothing to color. After a crawl, drop the JSON in `data/` (for example `data/Portugal.json`) and refresh; `/api/datasets` lists every region file automatically.

**On the map**

- **Language** — choropleth of the languages selected in the sidebar. One language scales by share of classified sites; several mix toward the leader. Use All / None, or expand past the top eight.
- **Multilingual** — share of classified sites that list more than one language. The place list sorts by that share; the sidebar shows a monolingual → multilingual ramp.
- **Search** — type a country or subdivision. The place list filters as you type; pick a hit to jump there. `/` focuses the search box.
- **Hover** — website count and language (or language-pair) bars for the area under the pointer.
- **Timeline** — appears when records include `lang_history` (see below). Play through monthly snapshots of the landing language.

The header count is how many websites are loaded. Hide the side panel with ›. After a drill-down, breadcrumbs walk back up: country → subdivision 1 (states, oblasts, cantons) → subdivision 2 (counties, raions, local authorities).

## Collect a region

Two steps: harvest OSM websites, then classify each homepage.

```bash
python3 osm_business_websites.py "Portugal" -o data/Portugal.json
python3 crawl_languages.py --data data/Portugal.json
```

`osm_business_websites.py` geocodes the place with Nominatim, downloads the matching Geofabrik daily extract, and keeps every node or way with a `website` or `contact:website` tag. Each record looks like:

```json
{
  "name": "Livraria Bertrand",
  "website": "https://www.bertrand.pt/",
  "category": "shop=books",
  "lat": 38.710,
  "lon": -9.139,
  "osm_type": "node",
  "osm_id": 123,
  "language": ["portuguese"]
}
```

PBF extracts skip OSM relations (some schools and museums mapped as multipolygons). For a live query that includes them:

```bash
python3 osm_business_websites.py "Portugal" --overpass --by-area -o data/Portugal.json
```

`--by-area` clips to the OSM boundary and fetches in tiles. `--merge` updates an existing file instead of replacing it.

`crawl_languages.py` fetches a short homepage prefix and writes a `language` list onto each record (`[]` when the fetch fails or there is too little text). It skips URLs that already have a non-empty list. Useful flags:

| Flag | What it does |
| --- | --- |
| `--refetch-all` | Classify every URL again |
| `--limit N` | Cap unique URLs (smoke tests) |
| `--workers` / `--per-host` | Concurrency (defaults 250 / 3) |
| `--shard 0/8` | Split unique URLs across machines |

## World pipeline

To walk Geofabrik country extracts in batch:

```bash
python3 run_world_pipeline.py --continents europe
python3 run_world_pipeline.py --continents north-america --start us
```

Continents: `europe`, `asia`, `australia-oceania`, `africa`, `central-america`, `south-america`, `north-america` (Canada and the United States only). Smaller countries run first so the map starts filling sooner.

The pipeline never overwrites an existing region JSON. If a file is already there it only classifies unlabeled URLs. Run `osm_business_websites.py` once beforehand so the Geofabrik catalog is available.

`--europe-only`, `--start <name-or-id>`, and `--limit N` restrict the queue.

## Optional history

The timeline in the viewer reads `lang_history` on each record.

**Wayback Machine** — bisect archived snapshots to find when the landing language changed, without sampling every month:

```bash
python3 wayback_history.py --data data/Portugal.json
```

**OSM name tags** — for Ukraine (`name` / `name:uk` / `name:ru`) and Wales (`name` / `name:cy` / `name:en`), yearly Geofabrik snapshots can show language on the public name without hitting Wayback:

```bash
python3 osm_name_history.py --region ukraine --data data/Ukraine.json
python3 osm_name_history.py --region wales --data data/Wales.json
```

The OSM fetch also attaches a name-tag guess (`history_source: "osm_name"`) when the letters on `name` already decide Ukrainian vs Russian.

## How it works

```
OSM extract  →  website list  →  homepage classifier  →  Leaflet map
 Nominatim        nodes/ways         languages/*.json      admin outlines
 Geofabrik        lat, lon, URL      writing system,       language /
 or Overpass      category           words, switchers      multilingual
```

**1. Harvest.** Nominatim turns a place name into a bounding box. The matching Geofabrik PBF is scanned with `osmium` for `website` / `contact:website`. A human-readable `category` is taken from the first matching OSM key (`shop`, `amenity`, `office`, …) but nothing is filtered out.

**2. Classify.** `crawl_languages.py` downloads up to ~192 KB of the homepage, strips scripts and styles, and samples the first 200 visible words. Three signals are unioned:

- **Writing system.** Japanese, Thai, Armenian, Hebrew, and other unique scripts are enough on their own (`script_sufficient`). Close cousins share a script but not letters: Ukrainian `їєґ` vs Russian `ыэё`.
- **Distinctive words.** Each pack lists function words and UI phrases that other languages in the same script rarely use. A language needs at least four unique hits; bilingual pages keep any language within 35% of the strongest score.
- **Language switchers.** `hreflang`, `<html lang>`, and nav links whose text matches a pack’s `switch_labels` (and unambiguous `/en/`-style paths).

The result is always a list: `["english"]`, `["english", "welsh"]`, or `[]`. Pages that advertise several versions early can stop downloading before the full body arrives.

**3. Draw.** `serve.py` is a small static server plus a few APIs. The viewer (`map_viewer.html`) loads every region JSON, point-in-polygon assigns sites to the current admin polygons, and colors each area from the selected languages or from the multilingual share. Hovering an area opens a card with bars. Subdivision 1 comes from `data/admin1/<ISO>.geojson`; subdivision 2 from `data/admin2/<osm_id>.geojson`. Search uses those files first, then Nominatim.

## Adding a language

Create `languages/<id>.json`. Adding a language should not require algorithm changes.

```json
{
  "name": "portuguese",
  "codes": ["pt", "por"],
  "switch_labels": ["português", "portuguese"],
  "scripts": ["latin"],
  "script_sufficient": false,
  "words": ["não", "você", "olá", "contacto"]
}
```

| Field | Role |
| --- | --- |
| `name` | Label stored on records and shown in the legend |
| `codes` | ISO codes for `hreflang` / `lang` / URL paths |
| `switch_labels` | Nav-link text that means “this language” |
| `scripts` | Writing systems from `languages/_scripts.json` |
| `script_sufficient` | `true` if the script alone identifies the language |
| `exclusive_letters` | Letters unique inside a shared script (`їєґ`, `ыэё`, …) |
| `words` | Distinctive vocabulary (prefer items other close languages do not share) |
| `suppresses` | Languages to drop when this one fires |
| `cyrillic_sign` | Extra Cyrillic rules (Bulgarian ъ, Serbian digraphs, …) |

If the writing system is new, add its Unicode block to `languages/_scripts.json`. Avoid two-letter path codes that collide with country codes (`uk`, `be`, …); those are listed as `ambiguous_path_codes` and ignored.

## Rebuild map outlines

Subdivision polygons are already in the repo. Rebuild them from Natural Earth, Overpass, and geoBoundaries if the admin geography changes:

```bash
python3 tools/fetch_countries.py
python3 tools/fetch_admin1.py
python3 tools/fetch_admin2.py
```

`fetch_admin2.py` defaults to a major-country set. Pass `--iso CA,UA,GB` (or any ISO 3166-1 alpha-2 list) to rebuild a subset. Canada uses census divisions rather than municipalities or economic regions.

## License

MIT. OSM data is © OpenStreetMap contributors, ODbL. Homepage text is fetched only to classify language and is not redistributed.
