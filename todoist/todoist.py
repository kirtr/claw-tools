#!/usr/bin/env python3
"""
todoist.py — Todoist CLI for claw-tools
----------------------------------------
Reads API token from ~/.config/claw-tools/secrets.json
Uses Todoist API v1 (api.todoist.com/api/v1)

Usage:
  python todoist.py projects                          # List all projects
  python todoist.py tasks [--project NAME] [--ids]    # List tasks (with optional IDs)
  python todoist.py today                             # Tasks due today or overdue
  python todoist.py add "Task" [--project NAME] [--due "tomorrow"] [--parent "Parent task"]
  python todoist.py complete "Task name" [--project NAME]   # Complete/close a task
  python todoist.py update "Task name" [--project NAME] [--due DATE] [--content "new name"]
"""

import argparse
import json
import sys
from pathlib import Path
from datetime import date

import requests

# ─────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────

SECRETS_PATH = Path.home() / ".config" / "claw-tools" / "secrets.json"
API_BASE = "https://api.todoist.com/api/v1"


def load_token() -> str:
    """Load Todoist API token from secrets file."""
    if not SECRETS_PATH.exists():
        sys.exit(
            f"❌ No secrets file found at {SECRETS_PATH}\n"
            'Create it with: {"todoist_api_token": "your_token_here"}'
        )
    with open(SECRETS_PATH) as f:
        secrets = json.load(f)
    token = secrets.get("todoist_api_token")
    if not token:
        sys.exit("❌ 'todoist_api_token' not found in secrets.json")
    return token


def make_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ─────────────────────────────────────────────
# API helpers
# ─────────────────────────────────────────────

def paginate(token: str, endpoint: str, params: dict = None) -> list:
    """Fetch all pages from a paginated endpoint (cursor-based)."""
    params = dict(params or {})
    results = []
    while True:
        r = requests.get(
            f"{API_BASE}/{endpoint}",
            headers=make_headers(token),
            params=params,
        )
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        cursor = data.get("next_cursor")
        if not cursor:
            break
        params["cursor"] = cursor
    return results


def get_projects(token: str) -> list:
    return paginate(token, "projects")


def get_tasks(token: str, project_id: str = None, filter_str: str = None) -> list:
    params = {}
    if project_id:
        params["project_id"] = project_id
    if filter_str:
        params["filter"] = filter_str
    return paginate(token, "tasks", params)


def add_task(token: str, content: str, project_id: str = None,
             due_string: str = None, parent_id: str = None) -> dict:
    payload = {"content": content}
    if project_id:
        payload["project_id"] = project_id
    if due_string:
        payload["due_string"] = due_string
    if parent_id:
        payload["parent_id"] = parent_id
    r = requests.post(
        f"{API_BASE}/tasks",
        headers=make_headers(token),
        json=payload,
    )
    r.raise_for_status()
    return r.json()


def close_task(token: str, task_id: str) -> None:
    """Complete/close a task by ID."""
    r = requests.post(
        f"{API_BASE}/tasks/{task_id}/close",
        headers=make_headers(token),
    )
    r.raise_for_status()


def update_task(token: str, task_id: str, **fields) -> dict:
    """Update a task's fields."""
    r = requests.post(
        f"{API_BASE}/tasks/{task_id}",
        headers=make_headers(token),
        json=fields,
    )
    r.raise_for_status()
    return r.json()


def find_project_by_name(projects: list, name: str) -> dict | None:
    """Case-insensitive project name lookup."""
    name_lower = name.lower()
    return next((p for p in projects if p["name"].lower() == name_lower), None)


def find_task_by_name(tasks: list, name: str) -> dict | None:
    """Case-insensitive, fuzzy task name lookup. Exact match preferred, then substring."""
    name_lower = name.lower()
    # Exact match first
    exact = next((t for t in tasks if t["content"].lower() == name_lower), None)
    if exact:
        return exact
    # Substring match
    return next((t for t in tasks if name_lower in t["content"].lower()), None)


# ─────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────

def print_projects(projects: list):
    print(f"\n{'─'*55}")
    print(f"  {'NAME':<30} {'ID'}")
    print(f"{'─'*55}")
    for p in sorted(projects, key=lambda x: x.get("child_order", 0)):
        inbox = " 📥" if p.get("inbox_project") else ""
        shared = " 👥" if p.get("is_shared") else ""
        fav = " ⭐" if p.get("is_favorite") else ""
        flags = f"{inbox}{shared}{fav}"
        print(f"  {p['name'] + flags:<35} {p['id']}")
    print(f"{'─'*55}")
    print(f"  {len(projects)} project(s) total\n")


def build_task_tree(tasks: list) -> tuple[list, dict]:
    """Build a parent-child hierarchy. Returns (root_tasks, children_map)."""
    children_map = {}  # parent_id -> [child_tasks]
    roots = []
    for t in tasks:
        pid = t.get("parent_id")
        if pid:
            children_map.setdefault(pid, []).append(t)
        else:
            roots.append(t)
    return roots, children_map


def print_tasks(tasks: list, projects: list = None, show_ids: bool = False):
    proj_map = {p["id"]: p["name"] for p in projects} if projects else {}
    priority_icon = {4: "🔴", 3: "🟠", 2: "🟡", 1: "⚪"}

    roots, children_map = build_task_tree(tasks)
    sort_key = lambda x: (x.get("due") or {}).get("date", "9999")

    print(f"\n{'─'*65}")
    if not tasks:
        print("  (no tasks)")
    else:
        def print_task(t, indent=0):
            due = (t.get("due") or {}).get("date", "")
            due_str = f" [{due}]" if due else ""
            proj_name = proj_map.get(t.get("project_id", ""), "")
            proj_str = f" ({proj_name})" if proj_name else ""
            icon = priority_icon.get(t.get("priority", 1), "⚪")
            id_str = f" #{t['id']}" if show_ids else ""
            prefix = "  " + "    " * indent
            print(f"{prefix}{icon} {t['content']}{due_str}{proj_str}{id_str}")
            # Print subtasks indented
            for child in sorted(children_map.get(t["id"], []), key=sort_key):
                print_task(child, indent + 1)

        for t in sorted(roots, key=sort_key):
            print_task(t)
    print(f"{'─'*65}")
    print(f"  {len(tasks)} task(s)\n")


# ─────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────

def cmd_projects(args, token):
    projects = get_projects(token)
    print_projects(projects)


def cmd_tasks(args, token):
    projects = get_projects(token)
    project_id = None
    if args.project:
        proj = find_project_by_name(projects, args.project)
        if not proj:
            sys.exit(f"❌ Project '{args.project}' not found")
        project_id = proj["id"]
    tasks = get_tasks(token, project_id=project_id)
    print_tasks(tasks, projects, show_ids=args.ids)


def cmd_today(args, token):
    projects = get_projects(token)
    today = date.today().isoformat()
    tasks = get_tasks(token, filter_str="today | overdue")
    print(f"\n📅 Due today / overdue ({today}):")
    print_tasks(tasks, projects)


def cmd_add(args, token):
    projects = get_projects(token)
    project_id = None
    if args.project:
        proj = find_project_by_name(projects, args.project)
        if not proj:
            sys.exit(f"❌ Project '{args.project}' not found")
        project_id = proj["id"]

    parent_id = None
    if args.parent:
        # Look up parent task by name or use as ID directly
        all_tasks = get_tasks(token, project_id=project_id)
        parent = find_task_by_name(all_tasks, args.parent)
        if parent:
            parent_id = parent["id"]
        else:
            # Try treating it as a raw ID
            parent_id = args.parent

    task = add_task(token, args.task, project_id=project_id,
                    due_string=args.due, parent_id=parent_id)
    label = "subtask" if parent_id else "task"
    print(f"\n✅ Created {label}: {task['content']}")
    if task.get("due"):
        print(f"   Due: {task['due']['date']}")
    if parent_id:
        print(f"   Parent ID: {parent_id}")
    url = task.get("url") or f"https://todoist.com/app/task/{task.get('id', '')}"
    print(f"   URL: {url}\n")


def cmd_complete(args, token):
    projects = get_projects(token)
    project_id = None
    if args.project:
        proj = find_project_by_name(projects, args.project)
        if not proj:
            sys.exit(f"❌ Project '{args.project}' not found")
        project_id = proj["id"]

    tasks = get_tasks(token, project_id=project_id)
    task = find_task_by_name(tasks, args.task)
    if not task:
        sys.exit(f"❌ Task '{args.task}' not found")

    close_task(token, task["id"])
    print(f"\n✅ Completed: {task['content']} (#{task['id']})\n")


def cmd_update(args, token):
    projects = get_projects(token)
    project_id = None
    if args.project:
        proj = find_project_by_name(projects, args.project)
        if not proj:
            sys.exit(f"❌ Project '{args.project}' not found")
        project_id = proj["id"]

    tasks = get_tasks(token, project_id=project_id)
    task = find_task_by_name(tasks, args.task)
    if not task:
        sys.exit(f"❌ Task '{args.task}' not found")

    fields = {}
    if args.content:
        fields["content"] = args.content
    if args.due:
        fields["due_string"] = args.due

    if not fields:
        sys.exit("❌ Nothing to update. Use --content or --due.")

    updated = update_task(token, task["id"], **fields)
    print(f"\n✏️  Updated: {updated['content']} (#{updated['id']})")
    if updated.get("due"):
        print(f"   Due: {updated['due']['date']}")
    print()


# ─────────────────────────────────────────────
# CLI entry point
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Todoist CLI — claw-tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command")

    # projects
    sub.add_parser("projects", help="List all projects")

    # tasks
    p_tasks = sub.add_parser("tasks", help="List tasks")
    p_tasks.add_argument("--project", "-p", help="Filter by project name")
    p_tasks.add_argument("--ids", action="store_true", help="Show task IDs in output")

    # today
    sub.add_parser("today", help="Show today's tasks and overdue")

    # add
    p_add = sub.add_parser("add", help="Add a new task")
    p_add.add_argument("task", help="Task content")
    p_add.add_argument("--project", "-p", help="Project name")
    p_add.add_argument("--due", "-d", help="Due date string (e.g. 'tomorrow', 'next Monday')")
    p_add.add_argument("--parent", help="Parent task name or ID (creates a subtask)")

    # complete
    p_complete = sub.add_parser("complete", help="Complete/close a task")
    p_complete.add_argument("task", help="Task name (fuzzy match)")
    p_complete.add_argument("--project", "-p", help="Filter by project name")

    # update
    p_update = sub.add_parser("update", help="Update a task")
    p_update.add_argument("task", help="Task name (fuzzy match)")
    p_update.add_argument("--project", "-p", help="Filter by project name")
    p_update.add_argument("--due", "-d", help="New due date string")
    p_update.add_argument("--content", "-c", help="New task content/name")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    token = load_token()

    dispatch = {
        "projects": cmd_projects,
        "tasks": cmd_tasks,
        "today": cmd_today,
        "add": cmd_add,
        "complete": cmd_complete,
        "update": cmd_update,
    }
    dispatch[args.command](args, token)


if __name__ == "__main__":
    main()
