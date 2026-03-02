# claw-tools 🏔️

A collection of CLI tools built for AI assistant use — clean Python scripts that are easy to call from code or a terminal.

## Philosophy

- **Simple first** — one script per service, minimal dependencies
- **AI-friendly** — clear output, easy to parse, sane defaults
- **Human-friendly too** — works just as well from the terminal

## Setup

```bash
pip install -r requirements.txt
```

### Secrets

Tools read credentials from `~/.config/claw-tools/secrets.json`:

```json
{
  "todoist_api_token": "your_token_here"
}
```

```bash
mkdir -p ~/.config/claw-tools
chmod 700 ~/.config/claw-tools
# create secrets.json, then:
chmod 600 ~/.config/claw-tools/secrets.json
```

---

## Tools

### 📋 Todoist (`todoist/todoist.py`)

Interact with your Todoist projects and tasks.

**Get your API token:** Todoist → Settings → Integrations → Developer → API token

```bash
# List all projects
python todoist/todoist.py projects

# List all tasks
python todoist/todoist.py tasks

# List tasks for a specific project
python todoist/todoist.py tasks --project "Work"

# Show today's tasks + overdue
python todoist/todoist.py today

# Add a task
python todoist/todoist.py add "Buy groceries" --due "tomorrow"
python todoist/todoist.py add "Fix the thing" --project "Work" --due "next Friday"
```

---

## Adding New Tools

Each tool lives in its own directory. Follow the pattern:
- `toolname/toolname.py` — main script, self-contained CLI
- Reads from `~/.config/claw-tools/secrets.json` for credentials
- `argparse` for CLI, clean stdout output
