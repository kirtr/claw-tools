#!/usr/bin/env python3
"""
instapaper.py — Instapaper CLI for claw-tools
----------------------------------------------
Uses the Simple API (Basic Auth) for adding URLs, and the Full API (OAuth 1.0a)
for listing, archiving, folders, etc. when OAuth credentials are available.

Credentials from ~/.config/claw-tools/secrets.json:
  - instapaper_username: email address
  - instapaper_password: Instapaper-specific password (not Gmail)
  - instapaper_oauth_key: OAuth consumer key (optional, for full API)
  - instapaper_oauth_secret: OAuth consumer secret (optional, for full API)

Usage:
  python instapaper.py add URL [--title "..."] [--description "..."] [--folder NAME]
  python instapaper.py list [--folder unread|starred|archive|NAME] [--limit N]
  python instapaper.py folders
  python instapaper.py archive QUERY         # Archive bookmark matching title/URL
  python instapaper.py star QUERY            # Star a bookmark
  python instapaper.py unstar QUERY          # Unstar a bookmark
  python instapaper.py delete QUERY          # Delete a bookmark
  python instapaper.py search QUERY          # Search bookmarks by title/URL
  python instapaper.py move QUERY --folder NAME  # Move bookmark to folder
"""

import argparse
import json
import sys
import time
import hashlib
import hmac
import urllib.parse
from pathlib import Path

import requests

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

SECRETS_PATH = Path.home() / ".config" / "claw-tools" / "secrets.json"

SIMPLE_API_BASE = "https://www.instapaper.com/api"
FULL_API_BASE = "https://www.instapaper.com/api/1"


def load_secrets() -> dict:
    """Load Instapaper credentials from secrets file."""
    if not SECRETS_PATH.exists():
        sys.exit(
            f"❌ No secrets file found at {SECRETS_PATH}\n"
            'Add instapaper_username and instapaper_password to your secrets.json'
        )
    with open(SECRETS_PATH) as f:
        secrets = json.load(f)
    return secrets


def get_basic_creds(secrets: dict) -> tuple[str, str]:
    """Get username/password for Simple API (Basic Auth)."""
    username = secrets.get("instapaper_username")
    password = secrets.get("instapaper_password", "")
    if not username:
        sys.exit("❌ 'instapaper_username' not found in secrets.json")
    return username, password


def get_oauth_creds(secrets: dict) -> tuple[str, str] | None:
    """Get OAuth consumer key/secret if available."""
    key = secrets.get("instapaper_oauth_key")
    secret = secrets.get("instapaper_oauth_secret")
    if key and secret:
        return key, secret
    return None


# ─────────────────────────────────────────────
# OAuth 1.0a (xAuth) helpers
# ─────────────────────────────────────────────

def percent_encode(s: str) -> str:
    return urllib.parse.quote(str(s), safe="")


def generate_nonce() -> str:
    import os
    return hashlib.sha1(os.urandom(32)).hexdigest()


def generate_timestamp() -> str:
    return str(int(time.time()))


def sign_request(method: str, url: str, params: dict,
                 consumer_secret: str, token_secret: str = "") -> str:
    """Create HMAC-SHA1 signature for OAuth 1.0a."""
    sorted_params = "&".join(
        f"{percent_encode(k)}={percent_encode(v)}"
        for k, v in sorted(params.items())
    )
    base_string = f"{method.upper()}&{percent_encode(url)}&{percent_encode(sorted_params)}"
    signing_key = f"{percent_encode(consumer_secret)}&{percent_encode(token_secret)}"
    signature = hmac.new(
        signing_key.encode("utf-8"),
        base_string.encode("utf-8"),
        hashlib.sha1
    ).digest()
    import base64
    return base64.b64encode(signature).decode("utf-8")


def oauth_request(method: str, url: str, consumer_key: str, consumer_secret: str,
                  token: str = "", token_secret: str = "",
                  extra_params: dict = None) -> requests.Response:
    """Make an OAuth 1.0a signed request."""
    oauth_params = {
        "oauth_consumer_key": consumer_key,
        "oauth_nonce": generate_nonce(),
        "oauth_signature_method": "HMAC-SHA1",
        "oauth_timestamp": generate_timestamp(),
        "oauth_version": "1.0",
    }
    if token:
        oauth_params["oauth_token"] = token

    # All params for signature base string
    all_params = dict(oauth_params)
    if extra_params:
        all_params.update(extra_params)

    signature = sign_request(method, url, all_params, consumer_secret, token_secret)
    oauth_params["oauth_signature"] = signature

    # Build Authorization header
    auth_header = "OAuth " + ", ".join(
        f'{percent_encode(k)}="{percent_encode(v)}"'
        for k, v in sorted(oauth_params.items())
    )

    headers = {"Authorization": auth_header}
    r = requests.post(url, headers=headers, data=extra_params or {})
    return r


class InstapaperClient:
    """Full API client using OAuth 1.0a with xAuth."""

    def __init__(self, consumer_key: str, consumer_secret: str,
                 username: str, password: str):
        self.consumer_key = consumer_key
        self.consumer_secret = consumer_secret
        self.username = username
        self.password = password
        self.token = ""
        self.token_secret = ""

    def login(self):
        """Authenticate via xAuth to get access token."""
        url = f"{FULL_API_BASE}/oauth/access_token"
        params = {
            "x_auth_username": self.username,
            "x_auth_password": self.password,
            "x_auth_mode": "client_auth",
        }
        r = oauth_request("POST", url, self.consumer_key, self.consumer_secret,
                          extra_params=params)
        if r.status_code != 200:
            sys.exit(f"❌ OAuth login failed ({r.status_code}): {r.text}")
        parsed = dict(urllib.parse.parse_qsl(r.text))
        self.token = parsed.get("oauth_token", "")
        self.token_secret = parsed.get("oauth_token_secret", "")
        if not self.token:
            sys.exit(f"❌ No token received: {r.text}")

    def _api(self, endpoint: str, params: dict = None) -> list | dict:
        """Make authenticated API call."""
        if not self.token:
            self.login()
        url = f"{FULL_API_BASE}/{endpoint}"
        r = oauth_request("POST", url, self.consumer_key, self.consumer_secret,
                          self.token, self.token_secret, extra_params=params or {})
        if r.status_code != 200:
            sys.exit(f"❌ API error on {endpoint} ({r.status_code}): {r.text}")
        try:
            return r.json()
        except json.JSONDecodeError:
            sys.exit(f"❌ Invalid JSON from {endpoint}: {r.text[:200]}")

    def verify(self) -> dict:
        return self._api("account/verify_credentials")

    def list_bookmarks(self, folder_id: str = "unread", limit: int = 25) -> dict:
        params = {"limit": str(limit), "folder_id": folder_id}
        return self._api("bookmarks/list", params)

    def add_bookmark(self, url: str, title: str = None,
                     description: str = None, folder_id: str = None) -> list:
        params = {"url": url}
        if title:
            params["title"] = title
        if description:
            params["description"] = description
        if folder_id:
            params["folder_id"] = folder_id
        return self._api("bookmarks/add", params)

    def delete_bookmark(self, bookmark_id: str) -> list:
        return self._api("bookmarks/delete", {"bookmark_id": bookmark_id})

    def star_bookmark(self, bookmark_id: str) -> list:
        return self._api("bookmarks/star", {"bookmark_id": bookmark_id})

    def unstar_bookmark(self, bookmark_id: str) -> list:
        return self._api("bookmarks/unstar", {"bookmark_id": bookmark_id})

    def archive_bookmark(self, bookmark_id: str) -> list:
        return self._api("bookmarks/archive", {"bookmark_id": bookmark_id})

    def move_bookmark(self, bookmark_id: str, folder_id: str) -> list:
        return self._api("bookmarks/move", {
            "bookmark_id": bookmark_id,
            "folder_id": folder_id,
        })

    def list_folders(self) -> list:
        return self._api("folders/list")

    def find_folder_by_name(self, name: str) -> dict | None:
        folders = self.list_folders()
        name_lower = name.lower()
        return next((f for f in folders if f.get("title", "").lower() == name_lower), None)

    def find_bookmark(self, query: str, folder_id: str = "unread",
                      limit: int = 100) -> dict | None:
        """Find a bookmark by title or URL substring match."""
        data = self.list_bookmarks(folder_id=folder_id, limit=limit)
        bookmarks = data.get("bookmarks", [])
        query_lower = query.lower()
        # Exact title match first
        exact = next((b for b in bookmarks if b.get("title", "").lower() == query_lower), None)
        if exact:
            return exact
        # Substring match on title or URL
        return next(
            (b for b in bookmarks
             if query_lower in b.get("title", "").lower()
             or query_lower in b.get("url", "").lower()),
            None
        )


# ─────────────────────────────────────────────
# Simple API (Basic Auth) — no OAuth needed
# ─────────────────────────────────────────────

def simple_add(username: str, password: str, url: str,
               title: str = None, selection: str = None) -> bool:
    """Add URL via Simple API. Returns True on success."""
    params = {"url": url}
    if title:
        params["title"] = title
    if selection:
        params["selection"] = selection
    r = requests.post(
        f"{SIMPLE_API_BASE}/add",
        auth=(username, password),
        data=params,
    )
    if r.status_code == 201:
        return True
    elif r.status_code == 400:
        sys.exit("❌ Bad request — missing URL?")
    elif r.status_code == 403:
        sys.exit("❌ Invalid Instapaper credentials")
    elif r.status_code == 500:
        sys.exit("❌ Instapaper server error — try again later")
    else:
        sys.exit(f"❌ Unexpected response: {r.status_code}")


def simple_auth(username: str, password: str) -> bool:
    """Verify credentials via Simple API."""
    r = requests.post(
        f"{SIMPLE_API_BASE}/authenticate",
        auth=(username, password),
    )
    return r.status_code == 200


# ─────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────

def print_bookmarks(data: dict | list):
    """Print bookmarks from Full API list response."""
    if isinstance(data, dict):
        bookmarks = data.get("bookmarks", [])
    else:
        bookmarks = [b for b in data if b.get("type") == "bookmark"]

    if not bookmarks:
        print("  (no bookmarks)")
        return

    print(f"\n{'─'*70}")
    for b in bookmarks:
        title = b.get("title", "(untitled)")
        url = b.get("url", "")
        bid = b.get("bookmark_id", "")
        starred = " ⭐" if b.get("starred", "0") == "1" else ""
        progress = b.get("progress", 0)
        prog_str = f" [{int(float(progress)*100)}%]" if float(progress) > 0 else ""
        print(f"  {title}{starred}{prog_str}")
        print(f"    {url}  #{bid}")
    print(f"{'─'*70}")
    print(f"  {len(bookmarks)} bookmark(s)\n")


def print_folders(folders: list):
    print(f"\n{'─'*45}")
    print(f"  {'FOLDER':<25} {'ID'}")
    print(f"{'─'*45}")
    # Built-in folders
    for name, fid in [("Unread", "unread"), ("Starred", "starred"), ("Archive", "archive")]:
        print(f"  {name:<25} {fid}")
    # Custom folders
    for f in folders:
        if f.get("type") == "folder":
            print(f"  {f.get('title', '?'):<25} {f.get('folder_id', '?')}")
    print(f"{'─'*45}\n")


# ─────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────

def get_client(secrets: dict) -> InstapaperClient | None:
    """Get a Full API client if OAuth creds are available."""
    oauth = get_oauth_creds(secrets)
    if not oauth:
        return None
    username, password = get_basic_creds(secrets)
    client = InstapaperClient(oauth[0], oauth[1], username, password)
    return client


def cmd_add(args, secrets):
    """Add a URL. Uses Simple API if no OAuth, Full API if available."""
    oauth = get_oauth_creds(secrets)
    if oauth:
        client = get_client(secrets)
        folder_id = None
        if args.folder:
            folder = client.find_folder_by_name(args.folder)
            if folder:
                folder_id = str(folder.get("folder_id"))
            elif args.folder in ("unread", "starred", "archive"):
                folder_id = args.folder
            else:
                sys.exit(f"❌ Folder '{args.folder}' not found")
        result = client.add_bookmark(
            args.url,
            title=args.title,
            description=args.description,
            folder_id=folder_id,
        )
        bookmark = next((b for b in result if b.get("type") == "bookmark"), {})
        title = bookmark.get("title", args.url)
        print(f"\n✅ Saved: {title}")
        if folder_id:
            print(f"   Folder: {args.folder}")
        print(f"   URL: {args.url}\n")
    else:
        # Simple API fallback
        username, password = get_basic_creds(secrets)
        simple_add(username, password, args.url, title=args.title)
        print(f"\n✅ Saved: {args.title or args.url}")
        print(f"   URL: {args.url}")
        print(f"   (Simple API — add OAuth creds for folder support)\n")


def cmd_list(args, secrets):
    client = get_client(secrets)
    if not client:
        sys.exit("❌ Full API requires OAuth credentials (instapaper_oauth_key/secret)")

    folder_id = args.folder or "unread"
    # Resolve folder name to ID
    if folder_id not in ("unread", "starred", "archive"):
        folder = client.find_folder_by_name(folder_id)
        if folder:
            folder_id = str(folder.get("folder_id"))
        else:
            sys.exit(f"❌ Folder '{args.folder}' not found")

    data = client.list_bookmarks(folder_id=folder_id, limit=args.limit)
    label = args.folder or "Unread"
    print(f"\n📑 {label}:")
    print_bookmarks(data)


def cmd_folders(args, secrets):
    client = get_client(secrets)
    if not client:
        sys.exit("❌ Full API requires OAuth credentials (instapaper_oauth_key/secret)")
    folders = client.list_folders()
    print_folders(folders)


def _find_in_all_folders(client: InstapaperClient, query: str) -> dict | None:
    """Search across unread, starred, archive for a bookmark."""
    for folder in ("unread", "starred", "archive"):
        found = client.find_bookmark(query, folder_id=folder)
        if found:
            return found
    return None


def cmd_archive(args, secrets):
    client = get_client(secrets)
    if not client:
        sys.exit("❌ Full API requires OAuth credentials")
    bookmark = _find_in_all_folders(client, args.query)
    if not bookmark:
        sys.exit(f"❌ No bookmark matching '{args.query}'")
    client.archive_bookmark(str(bookmark["bookmark_id"]))
    print(f"\n📦 Archived: {bookmark.get('title', '?')}\n")


def cmd_star(args, secrets):
    client = get_client(secrets)
    if not client:
        sys.exit("❌ Full API requires OAuth credentials")
    bookmark = _find_in_all_folders(client, args.query)
    if not bookmark:
        sys.exit(f"❌ No bookmark matching '{args.query}'")
    client.star_bookmark(str(bookmark["bookmark_id"]))
    print(f"\n⭐ Starred: {bookmark.get('title', '?')}\n")


def cmd_unstar(args, secrets):
    client = get_client(secrets)
    if not client:
        sys.exit("❌ Full API requires OAuth credentials")
    bookmark = _find_in_all_folders(client, args.query)
    if not bookmark:
        sys.exit(f"❌ No bookmark matching '{args.query}'")
    client.unstar_bookmark(str(bookmark["bookmark_id"]))
    print(f"\n☆ Unstarred: {bookmark.get('title', '?')}\n")


def cmd_delete(args, secrets):
    client = get_client(secrets)
    if not client:
        sys.exit("❌ Full API requires OAuth credentials")
    bookmark = _find_in_all_folders(client, args.query)
    if not bookmark:
        sys.exit(f"❌ No bookmark matching '{args.query}'")
    client.delete_bookmark(str(bookmark["bookmark_id"]))
    print(f"\n🗑️  Deleted: {bookmark.get('title', '?')}\n")


def cmd_search(args, secrets):
    client = get_client(secrets)
    if not client:
        sys.exit("❌ Full API requires OAuth credentials")
    results = []
    for folder in ("unread", "starred", "archive"):
        data = client.list_bookmarks(folder_id=folder, limit=100)
        bookmarks = data.get("bookmarks", [])
        query_lower = args.query.lower()
        for b in bookmarks:
            if (query_lower in b.get("title", "").lower()
                    or query_lower in b.get("url", "").lower()):
                b["_folder"] = folder
                results.append(b)
    if not results:
        print(f"\n  No bookmarks matching '{args.query}'\n")
    else:
        print(f"\n🔍 Results for '{args.query}':")
        print_bookmarks(results)


def cmd_move(args, secrets):
    client = get_client(secrets)
    if not client:
        sys.exit("❌ Full API requires OAuth credentials")
    if not args.folder:
        sys.exit("❌ --folder required for move")
    folder = client.find_folder_by_name(args.folder)
    if not folder:
        sys.exit(f"❌ Folder '{args.folder}' not found")
    bookmark = _find_in_all_folders(client, args.query)
    if not bookmark:
        sys.exit(f"❌ No bookmark matching '{args.query}'")
    client.move_bookmark(str(bookmark["bookmark_id"]), str(folder["folder_id"]))
    print(f"\n📁 Moved '{bookmark.get('title', '?')}' → {args.folder}\n")


def cmd_auth(args, secrets):
    """Test authentication."""
    username, password = get_basic_creds(secrets)
    if simple_auth(username, password):
        print(f"\n✅ Authenticated as {username}")
    else:
        print(f"\n❌ Authentication failed for {username}")
    oauth = get_oauth_creds(secrets)
    if oauth:
        client = get_client(secrets)
        try:
            client.login()
            user = client.verify()
            uid = next((u for u in user if u.get("type") == "user"), {})
            print(f"✅ OAuth working — user_id: {uid.get('user_id', '?')}")
        except SystemExit as e:
            print(f"❌ OAuth: {e}")
    else:
        print("ℹ️  No OAuth credentials — using Simple API only (add URLs)")
    print()


# ─────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Instapaper CLI — claw-tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    # add
    p_add = sub.add_parser("add", help="Add a URL to Instapaper")
    p_add.add_argument("url", help="URL to save")
    p_add.add_argument("--title", "-t", help="Article title")
    p_add.add_argument("--description", "-d", help="Brief description")
    p_add.add_argument("--folder", "-f", help="Folder name (requires OAuth)")

    # list
    p_list = sub.add_parser("list", help="List bookmarks")
    p_list.add_argument("--folder", "-f", default="unread",
                        help="Folder: unread, starred, archive, or custom name")
    p_list.add_argument("--limit", "-l", type=int, default=25, help="Max results")

    # folders
    sub.add_parser("folders", help="List folders")

    # archive
    p_archive = sub.add_parser("archive", help="Archive a bookmark")
    p_archive.add_argument("query", help="Title or URL substring")

    # star
    p_star = sub.add_parser("star", help="Star a bookmark")
    p_star.add_argument("query", help="Title or URL substring")

    # unstar
    p_unstar = sub.add_parser("unstar", help="Unstar a bookmark")
    p_unstar.add_argument("query", help="Title or URL substring")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a bookmark")
    p_delete.add_argument("query", help="Title or URL substring")

    # search
    p_search = sub.add_parser("search", help="Search bookmarks")
    p_search.add_argument("query", help="Search term")

    # move
    p_move = sub.add_parser("move", help="Move bookmark to folder")
    p_move.add_argument("query", help="Title or URL substring")
    p_move.add_argument("--folder", "-f", required=True, help="Target folder name")

    # auth
    sub.add_parser("auth", help="Test authentication")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    secrets = load_secrets()

    dispatch = {
        "add": cmd_add,
        "list": cmd_list,
        "folders": cmd_folders,
        "archive": cmd_archive,
        "star": cmd_star,
        "unstar": cmd_unstar,
        "delete": cmd_delete,
        "search": cmd_search,
        "move": cmd_move,
        "auth": cmd_auth,
    }
    dispatch[args.command](args, secrets)


if __name__ == "__main__":
    main()
