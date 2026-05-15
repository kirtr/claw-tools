# claw-tools 🏔️

A collection of CLI tools built for AI assistant use — clean Python scripts that are straightforward to call from code or a terminal.

## Philosophy

- **Simple first** — one script per service, minimal dependencies
- **AI-friendly** — clear output, easy to parse, sane defaults
- **Human-friendly too** — works just as well from a terminal

## Setup

Most tools only need `requests`, which is usually already available:

```bash
pip install requests
```

If you're in an externally-managed Python environment (Debian/Ubuntu), `requests` is typically pre-installed. You can also use a virtualenv if you prefer.

### Secrets

All credentials live in `~/.config/claw-tools/secrets.json`. Never committed, never hardcoded.

```bash
mkdir -p ~/.config/claw-tools
chmod 700 ~/.config/claw-tools
```

Create `~/.config/claw-tools/secrets.json`:

```json
{
  "todoist_api_token": "your_token_here"
}
```

```bash
chmod 600 ~/.config/claw-tools/secrets.json
```

---

## Tools

### 📋 Todoist (`todoist/todoist.py`)

Read and manage your Todoist projects and tasks from the command line.

Uses the [Todoist REST API v1](https://developer.todoist.com/api/v1/).

**Get your API token:** Todoist → Settings → Integrations → Developer → API token

#### Commands

```bash
# List all projects
python todoist/todoist.py projects

# List all active tasks
python todoist/todoist.py tasks

# List tasks for a specific project
python todoist/todoist.py tasks --project "Work"

# Show today's tasks + overdue
python todoist/todoist.py today

# Add a task
python todoist/todoist.py add "Review quarterly goals"
python todoist/todoist.py add "Fix the thing" --project "Work" --due "next Friday"
```

#### Features

- Handles cursor-based pagination automatically (no task/project limits)
- Priority indicators: 🔴 P1 · 🟠 P2 · 🟡 P3 · ⚪ P4
- Inbox and shared project indicators
- Case-insensitive project name lookup

---

### 🔐 Codex Auth (`codex-auth/codex_auth.py`)

Manage OpenAI Codex OAuth tokens for OpenClaw. Prevents the recurring "refresh token burned" problem by proactively refreshing tokens before they expire.

Keeps both the Codex CLI (`~/.codex/auth.json`) and OpenClaw auth profiles in sync, then triggers a live secrets reload — no gateway restart needed.

**Prerequisites:** `codex login` must have been run at least once.

#### Commands

```bash
# Show token status (expiry, sync state)
python codex-auth/codex_auth.py status

# Refresh tokens now (updates Codex CLI + OpenClaw + reloads gateway)
python codex-auth/codex_auth.py refresh

# Install cron job (every 7 days at 3 AM)
python codex-auth/codex_auth.py install

# Remove cron job
python codex-auth/codex_auth.py uninstall
```

#### Why?

OpenAI uses single-use rotating refresh tokens. If the gateway tries to refresh and anything goes wrong (network blip, race condition, double-refresh), the token burns permanently. This cron job refreshes proactively so the gateway always has a valid access token and never needs to refresh itself.

---

### 🎧 Feedfilter (`feedfilter/feedfilter.py`)

Create filtered podcast RSS feeds from broader upstream feeds. Each filter keeps
items whose title starts with a configured prefix, rewrites the feed metadata,
and serves the result over HTTP.

Configuration lives in `~/.config/feedfilter/feedfilter.db`.

#### Commands

```bash
# Add or replace a filter
python feedfilter/feedfilter.py add \
  --name bitcoin_fundamentals \
  --upstream "https://example.com/feed.xml" \
  --prefix BTC \
  --title "Bitcoin Fundamentals"

# Optional custom artwork
python feedfilter/feedfilter.py set-image --name bitcoin_fundamentals --url "https://example.com/image.jpg"

# List configured filters
python feedfilter/feedfilter.py list

# Serve locally by default
python feedfilter/feedfilter.py serve --port 8044

# Explicitly expose beyond localhost
python feedfilter/feedfilter.py serve --host 0.0.0.0 --port 8044
```

#### Notes

- Python stdlib only.
- Default bind host is `127.0.0.1`; use `--host 0.0.0.0` only when the feed
  should be reachable from other machines.

---

### 🎙️ Podqueue (`podqueue/podqueue.py`)

Maintain a personal podcast queue from YouTube, podcast pages, direct media URLs,
or other `yt-dlp`-supported sources. Downloads media, stores metadata in SQLite,
generates an RSS feed, and serves feed/media files for podcast clients.

Data lives in `~/podqueue` by default.

#### Dependencies

- `yt-dlp`
- `ffmpeg`

#### Commands

```bash
# Download audio and add to the queue
python podqueue/podqueue.py add "https://example.com/watch-or-episode-url"

# Download video instead of audio-only
python podqueue/podqueue.py add --video "https://example.com/watch-url"

# List queued episodes
python podqueue/podqueue.py list

# Remove an episode and its local media file
python podqueue/podqueue.py remove 12

# Regenerate feed.xml
python podqueue/podqueue.py feed

# Serve locally by default
python podqueue/podqueue.py serve --port 8043

# Explicitly expose beyond localhost
python podqueue/podqueue.py serve --host 0.0.0.0 --port 8043
```

#### Environment

```bash
PODQUEUE_DIR=~/podqueue
PODQUEUE_PORT=8043
PODQUEUE_HOST=127.0.0.1
PODQUEUE_PUBLIC_HOST=athena
PODQUEUE_BASE_URL=http://athena:8043/media/
PODQUEUE_TITLE="Kirt's Queue"
PODQUEUE_IMAGE=http://athena:8043/media/cover.jpg
YTDLP_BIN=yt-dlp
FFMPEG_BIN=ffmpeg
```

#### Notes

- Default bind host is `127.0.0.1`; use `--host 0.0.0.0` only when the feed
  should be reachable from other machines.
- `remove` deletes the local media file as well as the database row.
- Be mindful of copyright/licensing and disk usage when downloading media.

---

## Adding New Tools

Each tool lives in its own directory and follows the same pattern:

```
toolname/
  toolname.py    ← main script, self-contained CLI
  __init__.py
```

**Conventions:**
- Read credentials from `~/.config/claw-tools/secrets.json`
- Use `argparse` for the CLI
- Print clean, human-readable output to stdout
- `raise_for_status()` on all API calls — fail loudly, not silently
