#!/usr/bin/env python3
"""
feedfilter — Podcast feed filter proxy.

Takes upstream RSS/podcast feed URLs, filters episodes by title prefix,
and re-serves the filtered feed over HTTP. Zero media downloads — just
XML rewriting.

Usage:
    feedfilter serve [--host HOST] [--port PORT]
    feedfilter add --name NAME --upstream URL --prefix PREFIX --title TITLE
    feedfilter remove --name NAME
    feedfilter list
"""

import argparse
import io
import os
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse
from urllib.request import urlopen, Request
from urllib.error import URLError

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DB_DIR = os.path.expanduser("~/.config/feedfilter")
DB_PATH = os.path.join(DB_DIR, "feedfilter.db")

# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_db():
    """Open (and possibly create) the SQLite database."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS filters (
            name        TEXT PRIMARY KEY,
            upstream    TEXT NOT NULL,
            prefix      TEXT NOT NULL,
            title       TEXT NOT NULL
        )
    """)
    # Migration: add image_url column if missing
    cols = {r[1] for r in conn.execute("PRAGMA table_info(filters)").fetchall()}
    if "image_url" not in cols:
        conn.execute("ALTER TABLE filters ADD COLUMN image_url TEXT DEFAULT ''")
    conn.commit()
    return conn


def add_filter(name: str, upstream: str, prefix: str, title: str, image_url: str = ""):
    """Add or replace a filter definition."""
    conn = _get_db()
    conn.execute(
        "INSERT OR REPLACE INTO filters (name, upstream, prefix, title, image_url) VALUES (?, ?, ?, ?, ?)",
        (name, upstream, prefix, title, image_url),
    )
    conn.commit()
    conn.close()
    print(f"Added filter '{name}' — prefix '{prefix}' from {upstream}")


def set_image(name: str, image_url: str):
    """Set the custom image URL for an existing filter."""
    conn = _get_db()
    cur = conn.execute("UPDATE filters SET image_url = ? WHERE name = ?", (image_url, name))
    conn.commit()
    conn.close()
    if cur.rowcount:
        print(f"Set image for '{name}': {image_url}")
    else:
        print(f"Filter '{name}' not found.", file=sys.stderr)
        sys.exit(1)


def remove_filter(name: str):
    """Remove a filter by name."""
    conn = _get_db()
    cur = conn.execute("DELETE FROM filters WHERE name = ?", (name,))
    conn.commit()
    conn.close()
    if cur.rowcount:
        print(f"Removed filter '{name}'.")
    else:
        print(f"Filter '{name}' not found.", file=sys.stderr)
        sys.exit(1)


def list_filters():
    """Print all configured filters."""
    conn = _get_db()
    rows = conn.execute("SELECT name, upstream, prefix, title, image_url FROM filters ORDER BY name").fetchall()
    conn.close()
    if not rows:
        print("No filters configured.")
        return
    for r in rows:
        print(f"  {r['name']}")
        print(f"    upstream : {r['upstream']}")
        print(f"    prefix   : {r['prefix']}")
        print(f"    title    : {r['title']}")
        img = r['image_url'] or '(none)'
        print(f"    image    : {img}")
        print()


def get_all_filters() -> list[dict]:
    """Return all filters as a list of dicts."""
    conn = _get_db()
    rows = conn.execute("SELECT name, upstream, prefix, title, image_url FROM filters").fetchall()
    conn.close()
    return [dict(r) for r in rows]

# ---------------------------------------------------------------------------
# Feed fetching & caching
# ---------------------------------------------------------------------------

# In-memory cache: { upstream_url: (fetch_time, raw_bytes) }
_feed_cache: dict[str, tuple[float, bytes]] = {}
CACHE_TTL = 3600  # 1 hour

USER_AGENT = "feedfilter/1.0 (+https://github.com/openclaw)"


def fetch_feed(upstream: str) -> bytes:
    """Fetch an upstream feed, returning cached bytes if fresh enough."""
    now = time.time()
    cached = _feed_cache.get(upstream)
    if cached and (now - cached[0]) < CACHE_TTL:
        return cached[1]

    req = Request(upstream, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
    except URLError as e:
        print(f"[feedfilter] Failed to fetch {upstream}: {e}", file=sys.stderr)
        # Return stale cache if available, otherwise raise
        if cached:
            return cached[1]
        raise

    _feed_cache[upstream] = (now, data)
    return data

# ---------------------------------------------------------------------------
# XML namespace handling
# ---------------------------------------------------------------------------

# Common podcast namespaces. We register them so ET preserves the prefixes
# instead of inventing ns0, ns1, etc.
_KNOWN_NS = {
    "itunes":  "http://www.itunes.com/dtds/podcast-1.0.dtd",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "atom":    "http://www.w3.org/2005/Atom",
    "media":   "http://search.yahoo.com/mrss/",
    "dc":      "http://purl.org/dc/elements/1.1/",
    "sy":      "http://purl.org/rss/1.0/modules/syndication/",
    "slash":   "http://purl.org/rss/1.0/modules/slash/",
    "podcast": "https://podcastindex.org/namespace/1.0",
    "googleplay": "http://www.google.com/schemas/play-podcasts/1.0",
    "rawvoice": "http://www.rawvoice.com/rawvoiceRssModule/",
    "wfw":     "http://wellformedweb.org/CommentAPI/",
}


def _register_namespaces_from_feed(raw: bytes):
    """
    Parse namespace declarations from the raw XML and register them with ET
    so serialization preserves the original prefixes.
    """
    # First register our known set
    for prefix, uri in _KNOWN_NS.items():
        ET.register_namespace(prefix, uri)

    # Then scan for any xmlns:foo="..." in the raw bytes and register those too.
    # This catches namespaces we didn't anticipate.
    for match in re.finditer(rb'xmlns:([a-zA-Z0-9_-]+)\s*=\s*"([^"]+)"', raw):
        prefix = match.group(1).decode("utf-8", errors="replace")
        uri = match.group(2).decode("utf-8", errors="replace")
        if prefix != "xml":  # xml namespace is reserved
            ET.register_namespace(prefix, uri)

# ---------------------------------------------------------------------------
# Feed filtering
# ---------------------------------------------------------------------------

def _title_matches(item_title: str, prefix: str) -> bool:
    """
    Check if an episode title starts with the given prefix followed by
    a delimiter (digit, colon, space, dash). Case-insensitive.

    Examples with prefix "BTC":
        "BTC259: Bitcoin & Physics"   → True
        "BTC 100: Signal in Noise"    → True
        "BTC: Special Episode"        → True
        "BTCX something"              → False (X is not a delimiter)
    """
    t = item_title.strip().lower()
    p = prefix.lower()
    if not t.startswith(p):
        return False
    rest = t[len(p):]
    # If nothing follows the prefix, it's a match (exact title = prefix)
    if not rest:
        return True
    # First char after prefix must be a delimiter
    return rest[0] in " :\t-0123456789"


def filter_feed(raw: bytes, prefix: str, custom_title: str, image_url: str = "") -> bytes:
    """
    Parse an RSS feed, keep only items matching the prefix, rewrite the
    channel title/description/image, and return the filtered XML as bytes.
    """
    _register_namespaces_from_feed(raw)

    tree = ET.parse(io.BytesIO(raw))
    root = tree.getroot()

    channel = root.find("channel")
    if channel is None:
        # Not a valid RSS feed
        return raw

    # Rewrite channel title
    title_el = channel.find("title")
    if title_el is not None:
        title_el.text = custom_title

    # Rewrite channel description
    desc_el = channel.find("description")
    if desc_el is not None:
        desc_el.text = f"Filtered feed: episodes matching prefix '{prefix}'"

    # Also update common podcast-client title metadata if present.
    podcast_client_ns = _KNOWN_NS["itunes"]
    podcast_client_title = channel.find(f"{{{podcast_client_ns}}}title")
    if podcast_client_title is not None:
        podcast_client_title.text = custom_title

    # Override channel image if a custom image_url is set
    if image_url:
        # Common podcast-client image metadata — replace or create.
        podcast_client_image = channel.find(f"{{{podcast_client_ns}}}image")
        if podcast_client_image is not None:
            podcast_client_image.set("href", image_url)
        else:
            img_el = ET.SubElement(channel, f"{{{podcast_client_ns}}}image")
            img_el.set("href", image_url)

        # googleplay:image — replace or create
        gp_ns = _KNOWN_NS.get("googleplay", "")
        if gp_ns:
            gp_image = channel.find(f"{{{gp_ns}}}image")
            if gp_image is not None:
                gp_image.set("href", image_url)
            else:
                gp_el = ET.SubElement(channel, f"{{{gp_ns}}}image")
                gp_el.set("href", image_url)

        # Standard RSS <image> block — replace or create
        rss_image = channel.find("image")
        if rss_image is not None:
            url_el = rss_image.find("url")
            if url_el is not None:
                url_el.text = image_url
            else:
                url_el = ET.SubElement(rss_image, "url")
                url_el.text = image_url
        else:
            rss_image = ET.SubElement(channel, "image")
            ET.SubElement(rss_image, "url").text = image_url
            ET.SubElement(rss_image, "title").text = custom_title
            ET.SubElement(rss_image, "link").text = ""

    # Filter items — remove non-matching ones
    items = channel.findall("item")
    for item in items:
        item_title = item.find("title")
        if item_title is None or item_title.text is None:
            channel.remove(item)
            continue
        if not _title_matches(item_title.text, prefix):
            channel.remove(item)

    # Serialize back to bytes with XML declaration
    out = io.BytesIO()
    tree.write(out, encoding="utf-8", xml_declaration=True)
    return out.getvalue()

# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class FeedHandler(BaseHTTPRequestHandler):
    """Serves filtered podcast feeds at /<name>.xml"""

    # Loaded once at server start, refreshed on each request from DB
    # so new filters are picked up without restart.

    def do_GET(self):
        # Parse the path: expect /<name>.xml
        path = urlparse(self.path).path.strip("/")
        if not path.endswith(".xml"):
            self.send_error(404, "Not found. Use /<filter_name>.xml")
            return

        name = path[:-4]  # strip .xml

        # Look up filter
        conn = _get_db()
        row = conn.execute(
            "SELECT upstream, prefix, title, image_url FROM filters WHERE name = ?", (name,)
        ).fetchone()
        conn.close()

        if not row:
            self.send_error(404, f"No filter named '{name}'")
            return

        try:
            raw = fetch_feed(row["upstream"])
        except Exception as e:
            self.send_error(502, f"Failed to fetch upstream feed: {e}")
            return

        filtered = filter_feed(raw, row["prefix"], row["title"], row["image_url"] or "")

        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml; charset=utf-8")
        self.send_header("Content-Length", str(len(filtered)))
        self.end_headers()
        self.wfile.write(filtered)

    def log_message(self, format, *args):
        """Prefix log lines with timestamp."""
        print(f"[feedfilter] {self.address_string()} - {format % args}")


def serve(port: int, host: str = "127.0.0.1"):
    """Start the HTTP server."""
    class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedHTTPServer((host, port), FeedHandler)
    print(f"[feedfilter] Serving on {host}:{port}")
    print(f"[feedfilter] Feed URLs: http://localhost:{port}/<name>.xml")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[feedfilter] Shutting down.")
        server.server_close()

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="feedfilter",
        description="Podcast feed filter proxy — filter episodes by title prefix.",
    )
    sub = parser.add_subparsers(dest="command")

    # serve
    serve_p = sub.add_parser("serve", help="Start the HTTP server")
    serve_p.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host/interface to bind (default: 127.0.0.1; use 0.0.0.0 to expose on all interfaces)",
    )
    serve_p.add_argument("--port", type=int, default=8044, help="Port to listen on (default: 8044)")

    # add
    add_p = sub.add_parser("add", help="Add a filter")
    add_p.add_argument("--name", required=True, help="Filter name (used in URL)")
    add_p.add_argument("--upstream", required=True, help="Upstream RSS feed URL")
    add_p.add_argument("--prefix", required=True, help="Episode title prefix to match")
    add_p.add_argument("--title", required=True, help="Custom title for the filtered feed")
    add_p.add_argument("--image", default="", help="Custom image URL for the feed")

    # set-image
    img_p = sub.add_parser("set-image", help="Set custom image URL for a filter")
    img_p.add_argument("--name", required=True, help="Filter name")
    img_p.add_argument("--url", required=True, help="Image URL")

    # remove
    rm_p = sub.add_parser("remove", help="Remove a filter")
    rm_p.add_argument("--name", required=True, help="Filter name to remove")

    # list
    sub.add_parser("list", help="List configured filters")

    args = parser.parse_args()

    if args.command == "serve":
        serve(args.port, args.host)
    elif args.command == "add":
        add_filter(args.name, args.upstream, args.prefix, args.title, args.image)
    elif args.command == "set-image":
        set_image(args.name, args.url)
    elif args.command == "remove":
        remove_filter(args.name)
    elif args.command == "list":
        list_filters()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
