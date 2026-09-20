#!/usr/bin/env python3
"""Serve the map viewer on localhost with every dataset in data/ preloaded.

Usage: python3 serve.py [--port 8000] [--no-browser]

The viewer asks /api/datasets which files to load, so dropping a new
data/<Region>.json in place is enough to make it show up on the next refresh.
Boundary lookups go through /api/overpass, which caches every response under
.cache/overpass so drilling into an area is slow only the first time.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import http.server
import json
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

try:  # python.org builds on macOS ship without a usable system CA bundle
    import certifi

    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CONTEXT = ssl.create_default_context()

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / ".cache" / "overpass"
VIEWER = "business_map_viewer.html"

# Boundary outlines the viewer fetches by name; they are not language datasets.
NON_DATASET_FILES = {"countries.geojson", "authorities.geojson"}

OVERPASS_URLS = [
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]
OVERPASS_TIMEOUT_S = 240
USER_AGENT = "linguamap-viewer/1.0"


def discover_datasets() -> list[dict]:
    """List data/*.json region files, largest last so the log reads naturally."""
    found = []
    for path in sorted(DATA_DIR.glob("*.json")):
        if path.name in NON_DATASET_FILES:
            continue
        found.append(
            {
                "name": path.stem,
                "url": f"data/{path.name}",
                "bytes": path.stat().st_size,
            }
        )
    return found


def run_overpass(query: str) -> tuple[bytes, bool]:
    """Return (response bytes, cache_hit). Geometry queries can take minutes."""
    cached = CACHE_DIR / (hashlib.sha1(query.encode("utf-8")).hexdigest() + ".json")
    if cached.exists():
        return cached.read_bytes(), True

    body = urllib.parse.urlencode({"data": query}).encode("utf-8")
    problems = []
    for url in OVERPASS_URLS:
        req = urllib.request.Request(url, data=body, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=OVERPASS_TIMEOUT_S, context=SSL_CONTEXT) as resp:
                payload = resp.read()
            parsed = json.loads(payload)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
            problems.append(f"{url}: {e}")
            continue
        # A "remark" with an error means the query ran but the server gave up.
        if "error" in str(parsed.get("remark", "")).lower():
            problems.append(f"{url}: {parsed['remark'][:120]}")
            continue
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(payload)
        return payload, False
    raise RuntimeError("; ".join(problems) or "all Overpass endpoints failed")


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def do_GET(self):
        route = self.path.split("?")[0]
        if route == "/api/datasets":
            self._send_json(discover_datasets())
            return
        if route in ("/", "/index.html"):
            self.path = "/" + VIEWER
        super().do_GET()

    def do_POST(self):
        if self.path.split("?")[0] != "/api/overpass":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length") or 0)
        query = self.rfile.read(length).decode("utf-8")
        try:
            payload, hit = run_overpass(query)
        except RuntimeError as e:
            self._send_json({"error": str(e)}, status=502)
            return
        self._send_raw(payload, "application/json", {"X-Overpass-Cache": "hit" if hit else "miss"})

    def _send_json(self, payload, status: int = 200) -> None:
        self._send_raw(json.dumps(payload).encode("utf-8"), "application/json", status=status)

    def _send_raw(self, body: bytes, content_type: str, extra=None, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # the per-tile and per-dataset request log drowns out the startup banner


def bind(port: int, tries: int = 20) -> http.server.ThreadingHTTPServer:
    """Take the requested port, or the next free one if something else has it."""
    for offset in range(tries):
        try:
            return http.server.ThreadingHTTPServer(("127.0.0.1", port + offset), Handler)
        except OSError as e:
            if e.errno not in (errno.EADDRINUSE, errno.EACCES):
                raise
    raise SystemExit(f"No free port in {port}-{port + tries - 1}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if not (ROOT / VIEWER).exists():
        raise SystemExit(f"Missing {VIEWER} next to serve.py.")

    datasets = discover_datasets()
    if not datasets:
        print(f"Warning: no datasets found in {DATA_DIR}")
    for d in datasets:
        print(f"  {d['name']:<16} {d['bytes'] / 1e6:6.1f} MB  {d['url']}")

    httpd = bind(args.port)
    url = "http://{}:{}/".format(*httpd.socket.getsockname())
    print(f"\nLinguaMap viewer: {url}\nPress Ctrl+C to stop.")

    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
