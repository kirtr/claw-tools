#!/usr/bin/env python3
"""
podqueue.py — Curated podcast episode queue with RSS feed
----------------------------------------------------------
Downloads episodes via yt-dlp, stores metadata in SQLite, and serves
an RSS feed that AntennaPod can subscribe to.

Data directory: ~/podqueue/
  - podqueue.db     — SQLite database
  - feed.xml        — Generated RSS feed
  - <media files>   — Downloaded episodes

Usage:
  podqueue.py add <url> [--video]          # Download + add to queue (audio-only by default)
  podqueue.py list                        # Show queued episodes
  podqueue.py remove <id>                 # Remove episode by ID
  podqueue.py feed                        # Regenerate feed.xml
  podqueue.py serve [--host 127.0.0.1] [--port 8043]  # Start HTTP server

Environment Variables:
  PODQUEUE_DIR       Data directory (default: ~/podqueue)
  PODQUEUE_PORT      HTTP server port (default: 8043)
  PODQUEUE_HOST      HTTP server bind host (default: 127.0.0.1)
  PODQUEUE_BASE_URL  Base URL for enclosures (default: http://athena:8043/media/)
  PODQUEUE_TITLE     Feed title (default: Kirt's Queue)
"""

import argparse
import json
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

PODCAST_CLIENT_NS = "http://www.itunes.com/dtds/podcast-1.0.dtd"

# ─────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────

DATA_DIR = Path(os.environ.get("PODQUEUE_DIR", Path.home() / "podqueue"))
DB_PATH = DATA_DIR / "podqueue.db"
FEED_PATH = DATA_DIR / "feed.xml"

DEFAULT_PORT = int(os.environ.get("PODQUEUE_PORT", 8043))
DEFAULT_HOST = os.environ.get("PODQUEUE_HOST", "127.0.0.1")
DEFAULT_PUBLIC_HOST = os.environ.get("PODQUEUE_PUBLIC_HOST", "athena")
DEFAULT_BASE_URL = os.environ.get("PODQUEUE_BASE_URL", f"http://{DEFAULT_PUBLIC_HOST}:{DEFAULT_PORT}/media/")
DEFAULT_FEED_TITLE = os.environ.get("PODQUEUE_TITLE", "Kirt's Queue")
DEFAULT_FEED_DESC = "Curated episode queue"
DEFAULT_FEED_IMAGE = os.environ.get("PODQUEUE_IMAGE", f"http://{DEFAULT_PUBLIC_HOST}:{DEFAULT_PORT}/media/cover.jpg")

# yt-dlp and ffmpeg — check local installs first, fall back to PATH
YTDLP_BIN = os.environ.get("YTDLP_BIN") or (
    "/home/openclaw/.local/bin/yt-dlp"
    if Path("/home/openclaw/.local/bin/yt-dlp").exists()
    else shutil.which("yt-dlp") or "yt-dlp"
)
FFMPEG_BIN = os.environ.get("FFMPEG_BIN") or (
    "/home/openclaw/.local/bin/ffmpeg"
    if Path("/home/openclaw/.local/bin/ffmpeg").exists()
    else shutil.which("ffmpeg") or "ffmpeg"
)


# ─────────────────────────────────────────────────────────────
# Database
# ─────────────────────────────────────────────────────────────

def init_db():
    """Initialize SQLite database and create tables if needed."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS episodes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            description TEXT,
            source_url  TEXT NOT NULL,
            filename    TEXT NOT NULL,
            mime_type   TEXT NOT NULL DEFAULT 'audio/mpeg',
            filesize    INTEGER NOT NULL DEFAULT 0,
            duration    TEXT,
            thumbnail   TEXT,
            added_at    TEXT NOT NULL
        )
    """)
    conn.commit()
    return conn


def get_episodes(conn):
    """Return all episodes ordered newest-first."""
    cur = conn.execute("""
        SELECT id, title, description, source_url, filename,
               mime_type, filesize, duration, thumbnail, added_at
        FROM episodes ORDER BY id DESC
    """)
    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def insert_episode(conn, ep: dict) -> int:
    """Insert a new episode and return its ID."""
    cur = conn.execute("""
        INSERT INTO episodes
            (title, description, source_url, filename, mime_type,
             filesize, duration, thumbnail, added_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        ep["title"], ep.get("description"), ep["source_url"],
        ep["filename"], ep.get("mime_type", "audio/mpeg"),
        ep.get("filesize", 0), ep.get("duration"),
        ep.get("thumbnail"), ep.get("added_at", datetime.now(timezone.utc).isoformat()),
    ))
    conn.commit()
    return cur.lastrowid


def delete_episode(conn, ep_id: int):
    """Delete an episode record by ID."""
    conn.execute("DELETE FROM episodes WHERE id = ?", (ep_id,))
    conn.commit()


# ─────────────────────────────────────────────────────────────
# Download
# ─────────────────────────────────────────────────────────────

def check_ytdlp():
    """Verify yt-dlp is available."""
    if not Path(YTDLP_BIN).exists() and not shutil.which(YTDLP_BIN):
        sys.exit(
            f"❌ yt-dlp not found at '{YTDLP_BIN}'\n"
            "Install it: pip install yt-dlp  or  brew install yt-dlp"
        )


def download_episode(url: str, audio_only: bool = False) -> dict:
    """
    Download a URL via yt-dlp. Returns a dict with episode metadata:
    title, description, filename, mime_type, filesize, duration, thumbnail.
    """
    check_ytdlp()
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # First, extract metadata without downloading
    print(f"🔍 Fetching metadata for: {url}")
    meta_cmd = [
        YTDLP_BIN,
        "--dump-json",
        "--no-playlist",
        url,
    ]
    try:
        result = subprocess.run(meta_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            # Some URLs don't support --dump-json (direct MP3 links, etc.)
            print(f"   (no metadata available, will use URL as title)")
            meta = {}
        else:
            meta = json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        sys.exit("❌ Metadata fetch timed out. Check your connection.")
    except json.JSONDecodeError:
        meta = {}

    title = meta.get("title") or url
    description = meta.get("description") or ""
    duration_secs = meta.get("duration")
    thumbnail_url = meta.get("thumbnail")

    # Format duration as HH:MM:SS for common podcast-client metadata.
    duration_str = None
    if duration_secs:
        h = int(duration_secs) // 3600
        m = (int(duration_secs) % 3600) // 60
        s = int(duration_secs) % 60
        duration_str = f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    # Build download command
    # Use a timestamp-prefixed output template to avoid collisions
    ts = int(time.time())
    # yt-dlp will append the correct extension
    out_template = str(DATA_DIR / f"{ts}_%(title).80s.%(ext)s")

    if audio_only:
        dl_cmd = [
            YTDLP_BIN,
            "--ffmpeg-location", str(Path(FFMPEG_BIN).parent),
            "--no-playlist",
            "-x", "--audio-format", "mp3",
            "-o", out_template,
            url,
        ]
        expected_mime = "audio/mpeg"
    else:
        dl_cmd = [
            YTDLP_BIN,
            "--ffmpeg-location", str(Path(FFMPEG_BIN).parent),
            "--no-playlist",
            "-f", "bestvideo+bestaudio/best",
            "--merge-output-format", "mp4",
            "-o", out_template,
            url,
        ]
        expected_mime = "video/mp4"

    print(f"⬇️  Downloading: {title}")
    result = subprocess.run(dl_cmd, capture_output=False, text=True)
    if result.returncode != 0:
        sys.exit(f"❌ yt-dlp failed (exit {result.returncode})")

    # Find the downloaded file — it'll have the timestamp prefix
    candidates = sorted(DATA_DIR.glob(f"{ts}_*"))
    if not candidates:
        sys.exit("❌ Download seemed to succeed but no file found in data dir.")

    # Pick the largest file (in case yt-dlp left temp artifacts)
    media_file = max(candidates, key=lambda p: p.stat().st_size)
    filesize = media_file.stat().st_size

    # Determine actual MIME type
    mime, _ = mimetypes.guess_type(media_file.name)
    if not mime:
        mime = expected_mime

    print(f"✅ Downloaded: {media_file.name} ({filesize // 1024 // 1024:.1f} MB)")

    return {
        "title": title,
        "description": description[:4096] if description else "",  # keep it sane
        "source_url": url,
        "filename": media_file.name,
        "mime_type": mime,
        "filesize": filesize,
        "duration": duration_str,
        "thumbnail": thumbnail_url,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────
# RSS Feed
# ─────────────────────────────────────────────────────────────

def generate_feed(
    conn,
    base_url: str = DEFAULT_BASE_URL,
    feed_title: str = DEFAULT_FEED_TITLE,
):
    """
    Generate feed.xml as a valid RSS 2.0 feed with enclosures.
    Writes to FEED_PATH.
    """
    episodes = get_episodes(conn)

    # Register namespaces so they render cleanly
    ET.register_namespace("itunes", PODCAST_CLIENT_NS)
    ET.register_namespace("content", "http://purl.org/rss/1.0/modules/content/")

    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")

    ET.SubElement(channel, "title").text = feed_title
    ET.SubElement(channel, "link").text = base_url
    ET.SubElement(channel, "description").text = DEFAULT_FEED_DESC
    ET.SubElement(channel, "language").text = "en-us"
    ET.SubElement(channel, "lastBuildDate").text = formatdate(localtime=False)

    # Channel-level artwork
    feed_image_url = os.environ.get("PODQUEUE_IMAGE", DEFAULT_FEED_IMAGE)
    ET.SubElement(channel, f"{{{PODCAST_CLIENT_NS}}}image", {"href": feed_image_url})

    # Standard RSS <image> block
    rss_image = ET.SubElement(channel, "image")
    ET.SubElement(rss_image, "url").text = feed_image_url
    ET.SubElement(rss_image, "title").text = feed_title
    ET.SubElement(rss_image, "link").text = base_url

    for ep in episodes:
        item = ET.SubElement(channel, "item")

        ET.SubElement(item, "title").text = ep["title"]
        ET.SubElement(item, "description").text = ep.get("description") or ""
        ET.SubElement(item, "guid").text = ep["source_url"]
        ET.SubElement(item, "link").text = ep["source_url"]

        # pubDate from ISO timestamp stored in DB
        try:
            dt = datetime.fromisoformat(ep["added_at"])
            pub_date = formatdate(dt.timestamp(), localtime=False)
        except (ValueError, TypeError):
            pub_date = formatdate(localtime=False)
        ET.SubElement(item, "pubDate").text = pub_date

        # Enclosure — URL-encode the filename for the URL
        media_url = base_url.rstrip("/") + "/" + quote(ep["filename"])
        ET.SubElement(item, "enclosure", {
            "url": media_url,
            "length": str(ep.get("filesize") or 0),
            "type": ep.get("mime_type") or "audio/mpeg",
        })

        # Common podcast-client metadata.
        if ep.get("duration"):
            ET.SubElement(item, f"{{{PODCAST_CLIENT_NS}}}duration").text = ep["duration"]
        if ep.get("thumbnail"):
            ET.SubElement(item, f"{{{PODCAST_CLIENT_NS}}}image", {"href": ep["thumbnail"]})

    # Write with XML declaration
    tree = ET.ElementTree(rss)
    ET.indent(tree, space="  ")  # Python 3.9+
    with open(FEED_PATH, "wb") as f:
        f.write(b'<?xml version="1.0" encoding="UTF-8"?>\n')
        tree.write(f, encoding="utf-8", xml_declaration=False)

    print(f"📡 Feed written: {FEED_PATH} ({len(episodes)} episode(s))")


# ─────────────────────────────────────────────────────────────
# HTTP Server
# ─────────────────────────────────────────────────────────────

def make_handler(data_dir: Path):
    """Factory to create a request handler with the data dir baked in."""

    class PodqueueHandler(BaseHTTPRequestHandler):
        """
        Simple HTTP handler:
          GET /feed.xml        → serve the RSS feed
          GET /media/<file>    → serve a media file
        """

        def do_HEAD(self):
            """Handle HEAD requests (AntennaPod uses these to pre-check file sizes)."""
            self._handle_request(head_only=True)

        def do_GET(self):
            """Handle GET requests."""
            self._handle_request(head_only=False)

        def _handle_request(self, head_only: bool = False):
            path = unquote(urlparse(self.path).path)

            if path == "/feed.xml" or path == "/":
                self._serve_file(data_dir / "feed.xml", "application/rss+xml", head_only=head_only)
            elif path.startswith("/media/"):
                filename = path[len("/media/"):]
                # Sanitize: no directory traversal
                if ".." in filename or "/" in filename:
                    self._send_error(400, "Bad request")
                    return
                self._serve_file(data_dir / filename, head_only=head_only)
            else:
                self._send_error(404, "Not found")

        def _serve_file(self, filepath: Path, content_type: str = None, head_only: bool = False):
            """Stream a file to the client (or just send headers for HEAD requests)."""
            if not filepath.exists():
                self._send_error(404, f"File not found: {filepath.name}")
                return
            if content_type is None:
                content_type, _ = mimetypes.guess_type(filepath.name)
                content_type = content_type or "application/octet-stream"

            size = filepath.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.end_headers()

            if not head_only:
                with open(filepath, "rb") as f:
                    while chunk := f.read(65536):
                        self.wfile.write(chunk)

        def _send_error(self, code: int, msg: str):
            body = msg.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):
            # Quieter log format
            print(f"  {self.address_string()} - {fmt % args}")

    return PodqueueHandler


def serve(port: int = DEFAULT_PORT, host: str = DEFAULT_HOST):
    """Start HTTP server. Blocks until interrupted."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    handler = make_handler(DATA_DIR)
    server = HTTPServer((host, port), handler)
    print(f"🎙️  podqueue serving on {host}:{port}")
    print(f"   Feed:  http://localhost:{port}/feed.xml")
    print(f"   Media: http://localhost:{port}/media/<filename>")
    print("   Press Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n👋 Server stopped.")


# ─────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────

def cmd_add(args):
    """Download a URL and add it to the queue."""
    url = args.url
    audio_only = not args.video if args.video else args.audio_only

    conn = init_db()
    ep = download_episode(url, audio_only=audio_only)
    ep_id = insert_episode(conn, ep)

    base_url = args.base_url or DEFAULT_BASE_URL
    feed_title = args.feed_title or DEFAULT_FEED_TITLE
    generate_feed(conn, base_url=base_url, feed_title=feed_title)

    print(f"✅ Added episode #{ep_id}: {ep['title']}")
    conn.close()


def cmd_list(args):
    """List all queued episodes."""
    conn = init_db()
    episodes = get_episodes(conn)
    conn.close()

    if not episodes:
        print("📭 Queue is empty.")
        return

    print(f"{'ID':>4}  {'Added':>20}  {'Dur':>8}  Title")
    print("─" * 80)
    for ep in episodes:
        added = ep["added_at"][:16].replace("T", " ") if ep.get("added_at") else "unknown"
        dur = ep.get("duration") or "—"
        title = ep["title"]
        if len(title) > 46:
            title = title[:43] + "..."
        print(f"{ep['id']:>4}  {added:>20}  {dur:>8}  {title}")
        print(f"       Source: {ep['source_url']}")


def cmd_remove(args):
    """Remove an episode by ID, delete its file, regenerate feed."""
    conn = init_db()
    ep_id = args.id

    # Find the episode
    cur = conn.execute("SELECT * FROM episodes WHERE id = ?", (ep_id,))
    row = cur.fetchone()
    if not row:
        print(f"❌ No episode with ID {ep_id}")
        conn.close()
        return

    cols = [c[0] for c in cur.description]
    ep = dict(zip(cols, row))

    # Delete the file
    media_path = DATA_DIR / ep["filename"]
    if media_path.exists():
        media_path.unlink()
        print(f"🗑️  Deleted file: {ep['filename']}")
    else:
        print(f"⚠️  File not found (already deleted?): {ep['filename']}")

    delete_episode(conn, ep_id)

    base_url = args.base_url or DEFAULT_BASE_URL
    feed_title = args.feed_title or DEFAULT_FEED_TITLE
    generate_feed(conn, base_url=base_url, feed_title=feed_title)

    print(f"✅ Removed episode #{ep_id}: {ep['title']}")
    conn.close()


def cmd_feed(args):
    """Regenerate feed.xml from the database."""
    conn = init_db()
    base_url = args.base_url or DEFAULT_BASE_URL
    feed_title = args.feed_title or DEFAULT_FEED_TITLE
    generate_feed(conn, base_url=base_url, feed_title=feed_title)
    conn.close()


def cmd_serve(args):
    """Start the HTTP server."""
    port = args.port or DEFAULT_PORT
    host = args.host or DEFAULT_HOST
    serve(port=port, host=host)


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def build_parser():
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="podqueue",
        description="Curated podcast episode queue with RSS feed",
    )

    # Global flags (available to all subcommands)
    parser.add_argument(
        "--base-url",
        default=None,
        help=f"Base URL for media enclosures (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--feed-title",
        default=None,
        help=f"RSS feed title (default: {DEFAULT_FEED_TITLE})",
    )

    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # add
    p_add = sub.add_parser("add", help="Download and add an episode to the queue")
    p_add.add_argument("url", help="URL to download (YouTube, podcast page, direct MP3/MP4)")
    p_add.add_argument(
        "--audio-only", action="store_true", default=True,
        help="Extract audio only (mp3). This is the default.",
    )
    p_add.add_argument(
        "--video", action="store_true",
        help="Download video instead of audio-only",
    )

    # list
    sub.add_parser("list", help="List queued episodes")

    # remove
    p_remove = sub.add_parser("remove", help="Remove an episode by ID")
    p_remove.add_argument("id", type=int, help="Episode ID (from 'list')")

    # feed
    sub.add_parser("feed", help="Regenerate feed.xml without serving")

    # serve
    p_serve = sub.add_parser("serve", help="Start HTTP server")
    p_serve.add_argument(
        "--host", default=None,
        help=f"Host/interface to bind (default: {DEFAULT_HOST}; use 0.0.0.0 to expose on all interfaces)",
    )
    p_serve.add_argument(
        "--port", type=int, default=None,
        help=f"Port to listen on (default: {DEFAULT_PORT})",
    )

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "add": cmd_add,
        "list": cmd_list,
        "remove": cmd_remove,
        "feed": cmd_feed,
        "serve": cmd_serve,
    }

    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
