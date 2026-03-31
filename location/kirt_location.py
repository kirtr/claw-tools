#!/usr/bin/env python3
"""
Fetch Kirt's location from Google Maps location sharing.

Uses OpenClaw's headless Chromium (CDP on localhost:18800) to query
Google Maps' internal location sharing RPC endpoint with authenticated cookies.

Usage:
    python3 kirt_location.py          # JSON output
    python3 kirt_location.py --human  # Human-readable output

Requires:
    - OpenClaw browser running with authenticated Google session
    - pip: websocket-client
Falls back to stdin pipe if browser fetch fails.
"""

import json
import sys
import datetime
import urllib.request


CDP_HOST = "localhost"
CDP_PORT = 18800

MAPS_RPC_URL = (
    "https://www.google.com/maps/rpc/locationsharing/read"
    "?authuser=0&hl=en&gl=us&pb="
)


def find_google_maps_tab():
    """Find an existing Google Maps tab, or return any google.com tab."""
    url = f"http://{CDP_HOST}:{CDP_PORT}/json/list"
    with urllib.request.urlopen(url, timeout=5) as resp:
        tabs = json.loads(resp.read())

    # Prefer a maps tab
    for tab in tabs:
        if "google.com/maps" in tab.get("url", ""):
            return tab
    # Fall back to any google.com tab
    for tab in tabs:
        if "google.com" in tab.get("url", ""):
            return tab
    return None


def fetch_via_cdp():
    """Fetch location data using CDP websocket to evaluate JS in browser."""
    import websocket

    tab = find_google_maps_tab()
    if not tab:
        raise RuntimeError("No Google Maps tab found in browser")

    ws_url = tab["webSocketDebuggerUrl"]
    ws = websocket.create_connection(ws_url, timeout=15, suppress_origin=True)

    try:
        # First navigate to maps if not already there
        if "google.com/maps" not in tab.get("url", ""):
            ws.send(json.dumps({
                "id": 1,
                "method": "Page.navigate",
                "params": {"url": "https://www.google.com/maps"}
            }))
            ws.recv()  # ack
            import time
            time.sleep(2)

        # Evaluate the fetch in the page context
        js_expr = (
            f"fetch('{MAPS_RPC_URL}', "
            f"{{ method: 'GET', credentials: 'include' }})"
            f".then(r => r.text())"
        )
        ws.send(json.dumps({
            "id": 2,
            "method": "Runtime.evaluate",
            "params": {
                "expression": js_expr,
                "awaitPromise": True,
                "returnByValue": True,
            }
        }))

        # Read responses until we get id=2
        while True:
            msg = json.loads(ws.recv())
            if msg.get("id") == 2:
                result = msg.get("result", {}).get("result", {})
                if result.get("type") == "string":
                    return result["value"]
                # Check for errors
                exc = msg.get("result", {}).get("exceptionDetails")
                if exc:
                    raise RuntimeError(f"JS error: {exc}")
                raise RuntimeError(f"Unexpected CDP result: {msg}")
    finally:
        ws.close()


def parse_location_data(raw_text):
    """Parse the Google Maps location sharing response."""
    # Strip the XSSI prefix  )]}'  plus newline
    if raw_text.startswith(")]}'"):
        raw_text = raw_text[4:]
        raw_text = raw_text.lstrip("\n")

    data = json.loads(raw_text)

    results = []
    if not data or not data[0]:
        return results

    people_list = data[0]

    for person_data in people_list:
        if not person_data or not isinstance(person_data, list) or len(person_data) < 2:
            continue

        person_info = person_data[0]
        location = person_data[1]

        if not location or not isinstance(location, list) or len(location) < 3:
            continue

        coords = location[1]
        if not coords or not isinstance(coords, list) or len(coords) < 3:
            continue

        # coords format: [None, longitude, latitude]
        lng, lat = coords[1], coords[2]
        timestamp_ms = location[2] if len(location) > 2 else None
        address = location[4] if len(location) > 4 else None

        # Battery info is at index 13 of the person_data array
        battery = None
        if len(person_data) > 13 and person_data[13]:
            battery_data = person_data[13]
            if isinstance(battery_data, list) and len(battery_data) >= 2:
                battery = battery_data[1]  # percentage

        # Name: prefer short name from index 6, fall back to full name from index 0
        name = "Unknown"
        short_name_info = person_data[6] if len(person_data) > 6 else None
        if isinstance(short_name_info, list) and len(short_name_info) > 3:
            name = short_name_info[3]  # short name like "Kirt"
        elif isinstance(person_info, list) and len(person_info) > 3:
            name = person_info[3]  # full name like "Kirt Runolfson"
        elif isinstance(person_info, str):
            name = person_info

        ts = None
        if timestamp_ms:
            ts = datetime.datetime.fromtimestamp(
                timestamp_ms / 1000, tz=datetime.timezone.utc
            )

        results.append({
            "name": name,
            "latitude": lat,
            "longitude": lng,
            "address": address,
            "timestamp": ts.isoformat() if ts else None,
            "timestamp_unix": timestamp_ms / 1000 if timestamp_ms else None,
            "battery_percent": battery,
        })

    return results


def format_human(locations):
    """Format locations for human-readable output."""
    for loc in locations:
        age = ""
        if loc["timestamp_unix"]:
            age_secs = (
                datetime.datetime.now(datetime.timezone.utc).timestamp()
                - loc["timestamp_unix"]
            )
            if age_secs < 60:
                age = f" ({int(age_secs)}s ago)"
            elif age_secs < 3600:
                age = f" ({int(age_secs / 60)}m ago)"
            else:
                age = f" ({int(age_secs / 3600)}h ago)"

        battery_str = (
            f", battery {loc['battery_percent']}%"
            if loc["battery_percent"] is not None
            else ""
        )
        print(f"{loc['name']}: {loc['latitude']:.6f}, {loc['longitude']:.6f}")
        print(f"  {loc['address']}{age}{battery_str}")


if __name__ == "__main__":
    raw = None

    # Try stdin if data is being piped (check for --stdin flag or actual piped data)
    if "--stdin" in sys.argv:
        raw = sys.stdin.read()
    elif not sys.stdin.isatty():
        # Peek to see if there's actually data on stdin
        import select
        if select.select([sys.stdin], [], [], 0.1)[0]:
            raw = sys.stdin.read()

    if not raw:
        # Try CDP browser fetch
        try:
            raw = fetch_via_cdp()
        except Exception as e:
            print(f"Browser fetch failed: {e}", file=sys.stderr)

    if not raw:
        print("Could not fetch location data.", file=sys.stderr)
        print("Pipe data via stdin or ensure OpenClaw browser is running.", file=sys.stderr)
        sys.exit(1)

    locations = parse_location_data(raw)

    if not locations:
        print("No location data found in response.", file=sys.stderr)
        sys.exit(1)

    if "--human" in sys.argv:
        format_human(locations)
    else:
        print(json.dumps(locations, indent=2))
