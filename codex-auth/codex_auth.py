#!/usr/bin/env python3
"""Codex OAuth token manager for OpenClaw.

Refreshes OpenAI Codex OAuth tokens and keeps both the Codex CLI
(~/.codex/auth.json) and OpenClaw auth profiles in sync.

Designed to run as a cron job to prevent the recurring "refresh token
burned" problem with OpenAI's single-use rotating refresh tokens.

Usage:
    python codex_auth.py status      # Show token status
    python codex_auth.py refresh     # Refresh tokens now
    python codex_auth.py install     # Install cron job (every 7 days)
    python codex_auth.py uninstall   # Remove cron job
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# OpenAI OAuth client ID (Codex CLI)
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
TOKEN_URL = "https://auth.openai.com/oauth/token"

# Default paths
CODEX_AUTH = Path.home() / ".codex" / "auth.json"
OPENCLAW_AUTH = Path.home() / ".openclaw" / "agents" / "main" / "agent" / "auth-profiles.json"
OPENCLAW_PROFILE_KEY = "openai-codex:default"

CRON_COMMENT = "claw-tools: codex-auth refresh"
CRON_INTERVAL_DAYS = 7


def decode_jwt_exp(token: str) -> float | None:
    """Extract expiry timestamp from a JWT without validation."""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims.get("exp")
    except Exception:
        return None


def hours_until(ts: float) -> float:
    """Hours from now until a Unix timestamp."""
    return (ts - time.time()) / 3600


def load_json(path: Path) -> dict:
    """Load a JSON file."""
    with open(path) as f:
        return json.load(f)


def save_json(path: Path, data: dict) -> None:
    """Save a JSON file atomically-ish (write + rename)."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    tmp.rename(path)


def get_codex_tokens() -> dict:
    """Read current tokens from Codex CLI auth."""
    if not CODEX_AUTH.exists():
        print(f"ERROR: Codex auth not found at {CODEX_AUTH}", file=sys.stderr)
        print("Run 'codex login' first.", file=sys.stderr)
        sys.exit(1)
    data = load_json(CODEX_AUTH)
    tokens = data.get("tokens", {})
    return {
        "access_token": tokens.get("access_token", ""),
        "refresh_token": tokens.get("refresh_token", ""),
        "id_token": tokens.get("id_token", ""),
        "account_id": tokens.get("account_id", ""),
        "raw": data,
    }


def refresh_tokens(refresh_token: str) -> dict:
    """Exchange a refresh token for new access + refresh tokens."""
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": CLIENT_ID,
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        resp = urllib.request.urlopen(req, timeout=30)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        print(f"ERROR: Token refresh failed: {e.code}", file=sys.stderr)
        print(body, file=sys.stderr)
        sys.exit(1)


def update_codex_cli(result: dict, current: dict) -> None:
    """Write refreshed tokens back to Codex CLI auth.json."""
    raw = current["raw"]
    tokens = raw.setdefault("tokens", {})
    tokens["access_token"] = result["access_token"]
    tokens["refresh_token"] = result.get("refresh_token", current["refresh_token"])
    if "id_token" in result:
        tokens["id_token"] = result["id_token"]
    save_json(CODEX_AUTH, raw)
    print(f"  Updated {CODEX_AUTH}")


def update_openclaw(result: dict) -> None:
    """Write refreshed tokens to OpenClaw auth-profiles.json."""
    if not OPENCLAW_AUTH.exists():
        print(f"  Skipped OpenClaw (not found: {OPENCLAW_AUTH})")
        return

    data = load_json(OPENCLAW_AUTH)
    profiles = data.get("profiles", {})
    if OPENCLAW_PROFILE_KEY not in profiles:
        print(f"  Skipped OpenClaw (no '{OPENCLAW_PROFILE_KEY}' profile)")
        return

    profile = profiles[OPENCLAW_PROFILE_KEY]
    profile["access"] = result["access_token"]
    if result.get("refresh_token"):
        profile["refresh"] = result["refresh_token"]

    exp = decode_jwt_exp(result["access_token"])
    if exp:
        profile["expires"] = int(exp * 1000)  # OpenClaw uses milliseconds

    save_json(OPENCLAW_AUTH, data)
    print(f"  Updated {OPENCLAW_AUTH}")


def reload_openclaw_secrets() -> None:
    """Tell the running gateway to reload secrets from disk."""
    try:
        subprocess.run(
            ["openclaw", "secrets", "reload"],
            capture_output=True,
            timeout=15,
        )
        print("  OpenClaw secrets reloaded")
    except FileNotFoundError:
        print("  Skipped secrets reload (openclaw CLI not found)")
    except subprocess.TimeoutExpired:
        print("  Warning: secrets reload timed out")


def cmd_status(_args: argparse.Namespace) -> None:
    """Show current token status."""
    tokens = get_codex_tokens()

    print("Codex CLI tokens:")
    exp = decode_jwt_exp(tokens["access_token"])
    if exp:
        h = hours_until(exp)
        status = f"{h:.1f}h remaining" if h > 0 else f"EXPIRED ({-h:.1f}h ago)"
        print(f"  Access token: {status}")
    else:
        print("  Access token: unknown expiry")

    print(f"  Refresh token: {'present' if tokens['refresh_token'] else 'MISSING'}")
    if tokens["refresh_token"]:
        print(f"    prefix: {tokens['refresh_token'][:25]}...")

    if OPENCLAW_AUTH.exists():
        oc_data = load_json(OPENCLAW_AUTH)
        oc_profile = oc_data.get("profiles", {}).get(OPENCLAW_PROFILE_KEY)
        if oc_profile:
            oc_exp = decode_jwt_exp(oc_profile.get("access", ""))
            print("\nOpenClaw profile:")
            if oc_exp:
                h = hours_until(oc_exp)
                status = f"{h:.1f}h remaining" if h > 0 else f"EXPIRED ({-h:.1f}h ago)"
                print(f"  Access token: {status}")
            oc_refresh = oc_profile.get("refresh", "")
            in_sync = oc_refresh == tokens["refresh_token"]
            print(f"  In sync with Codex CLI: {'yes' if in_sync else 'NO — refresh needed'}")


def cmd_refresh(_args: argparse.Namespace) -> None:
    """Refresh tokens and update all stores."""
    tokens = get_codex_tokens()

    if not tokens["refresh_token"]:
        print("ERROR: No refresh token available. Run 'codex login' first.", file=sys.stderr)
        sys.exit(1)

    print(f"Refreshing tokens...")
    result = refresh_tokens(tokens["refresh_token"])

    exp = decode_jwt_exp(result["access_token"])
    if exp:
        print(f"  New access token expires in {hours_until(exp):.1f}h")

    update_codex_cli(result, tokens)
    update_openclaw(result)
    reload_openclaw_secrets()
    print("Done.")


def get_cron_line() -> str:
    """Build the cron line for scheduled refresh."""
    script = Path(__file__).resolve()
    python = sys.executable
    # Run at 3:00 AM every 7 days (1st, 8th, 15th, 22nd, 29th)
    return f"0 3 */7 * * {python} {script} refresh >> /tmp/codex-auth-refresh.log 2>&1 # {CRON_COMMENT}"


def cmd_install(_args: argparse.Namespace) -> None:
    """Install cron job for automatic refresh."""
    cron_line = get_cron_line()

    # Read existing crontab
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
    except FileNotFoundError:
        print("ERROR: crontab not available", file=sys.stderr)
        sys.exit(1)

    # Remove any existing codex-auth lines
    lines = [l for l in existing.splitlines() if CRON_COMMENT not in l]
    lines.append(cron_line)

    # Write back
    new_crontab = "\n".join(lines) + "\n"
    proc = subprocess.run(["crontab", "-"], input=new_crontab, text=True, capture_output=True)
    if proc.returncode != 0:
        print(f"ERROR: Failed to install crontab: {proc.stderr}", file=sys.stderr)
        sys.exit(1)

    print(f"Cron job installed (every {CRON_INTERVAL_DAYS} days at 3:00 AM):")
    print(f"  {cron_line}")


def cmd_uninstall(_args: argparse.Namespace) -> None:
    """Remove cron job."""
    try:
        result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
        existing = result.stdout if result.returncode == 0 else ""
    except FileNotFoundError:
        print("ERROR: crontab not available", file=sys.stderr)
        sys.exit(1)

    lines = [l for l in existing.splitlines() if CRON_COMMENT not in l]
    new_crontab = "\n".join(lines) + "\n" if lines else ""

    subprocess.run(["crontab", "-"], input=new_crontab, text=True, capture_output=True)
    print("Cron job removed.")


def main():
    parser = argparse.ArgumentParser(
        description="Codex OAuth token manager for OpenClaw",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("status", help="Show token status")
    sub.add_parser("refresh", help="Refresh tokens now")
    sub.add_parser("install", help="Install cron job")
    sub.add_parser("uninstall", help="Remove cron job")

    args = parser.parse_args()

    commands = {
        "status": cmd_status,
        "refresh": cmd_refresh,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
