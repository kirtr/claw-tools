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
