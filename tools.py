"""Tools for the Flush Hour restroom agent, and the JSON that describes them to the model.

Data sources (all free, no API key):
  - NYC Open Data "Public Restrooms" (official, ~1,000 sites, has hours)
  - NYC Planning Labs GeoSearch (address -> coordinates)

Design rules from the tool-calling lecture:
  * descriptions say what a tool is for AND when not to use it
  * enums make invalid arguments impossible (`needs`)
  * the harness fills what the model should not have to (session needs, the clock)
  * tools that are always used together are merged (geocode + search + hours)
  * errors are JSON with an actionable next step, never a stack trace
"""

import hashlib
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from geo import (
    grid_distance_m,
    haversine_m,
    in_nyc,
    walk_minutes,
)
from hours import availability, hours_for_day

NYC_TZ = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "flush-hour-class-project/0.1 (Columbia IEOR 4570 student assignment)"}

RESTROOMS_URL = "https://data.cityofnewyork.us/resource/i7jb-7jku.json"
GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"

NEEDS = ["wheelchair", "changing_station", "gender_neutral"]
DEFAULT_RADIUS_M = 800
WIDENED_RADIUS_M = 2000


class ToolError(Exception):
    """A problem the model can fix by changing its next call. The message is shown to it."""


# --- Small utilities ---

_cache: dict = {}


def _cached(key: str, ttl_seconds: int, fn):
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl_seconds:
        return hit[1]
    value = fn()
    if value:  # never remember an empty answer: it may just be a service hiccup
        _cache[key] = (time.time(), value)
    return value


def _get_json(url: str, params: dict | None = None, timeout: int = 10):
    response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _clamp(value, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _maps_link(point: tuple[float, float]) -> str:
    return f"https://www.google.com/maps/dir/?api=1&destination={point[0]},{point[1]}&travelmode=walking"


def _parse_when(when: str | None) -> datetime:
    """NYC local time as a naive datetime. None means right now."""
    if not when:
        return datetime.now(NYC_TZ).replace(tzinfo=None, second=0, microsecond=0)
    try:
        parsed = datetime.fromisoformat(when.strip())
    except ValueError:
        raise ToolError(
            f"Could not read when='{when}'. Use ISO 8601 in NYC local time, e.g. '2026-10-05T01:00', or omit it for right now."
        )
    if parsed.tzinfo:
        parsed = parsed.astimezone(NYC_TZ).replace(tzinfo=None)
    return parsed


def _resolve_needs(state: dict, needs: list | None) -> list[str]:
    """Explicit needs win; otherwise use what the user told us earlier this session."""
    if needs is None:
        needs = state.get("needs", [])
    bad = [n for n in needs if n not in NEEDS]
    if bad:
        raise ToolError(f"Unknown need(s) {bad}. Allowed values: {NEEDS}.")
    return list(dict.fromkeys(needs))


# --- Geocoding ---

_COORDS = re.compile(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")


def geocode(text: str) -> tuple[tuple[float, float], str]:
    """Place text or 'lat,lon' -> ((lat, lon), label). Raises ToolError if it cannot be placed in NYC."""
    if not text or not text.strip():
        raise ToolError("No location given. Ask the user where they are (address, intersection, or landmark).")

    coords = _COORDS.search(text)
    if coords:
        point = (float(coords.group(1)), float(coords.group(2)))
        if not in_nyc(point):
            raise ToolError(f"'{text}' is outside New York City, and this agent only covers the five boroughs.")
        return point, "your GPS location"

    def lookup():
        data = _get_json(GEOSEARCH_URL, {"text": text.strip(), "size": 5})
        for feature in data.get("features") or []:
            lon, lat = feature["geometry"]["coordinates"]
            if in_nyc((lat, lon)):
                return {"point": (lat, lon), "label": feature.get("properties", {}).get("label") or text.strip()}
        return None

    found = _cached("geo:" + text.strip().lower(), 86400, lookup)
    if not found:
        raise ToolError(
            f"Could not find '{text}' in NYC. Try a street address, an intersection like 'Broadway & 116th St', "
            "or a well-known landmark, or ask the user for a nearby cross street."
        )
    return found["point"], found["label"]


# --- Official NYC data ---

_ACCESS = {
    "fully accessible": "full",
    "partially accessible": "partial",
    "limited accessibility": "partial",
    "not accessible": "no",
}


def _yes_no_unknown(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def _normalize_site(row: dict) -> dict | None:
    try:
        lat, lon = float(row["latitude"]), float(row["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    name = (row.get("facility_name") or "Unnamed restroom").strip()
    rtype = (row.get("restroom_type") or "").strip()
    changing = (row.get("changing_stations") or "").strip().strip('"').strip()
    changing_ok = True if changing.lower().startswith("yes") else (False if changing.lower() == "no" else None)
    return {
        "id": "nyc-" + hashlib.sha1(f"{name}|{lat:.5f}|{lon:.5f}".encode()).hexdigest()[:8],
        "name": name,
        "type": (row.get("location_type") or "").strip(),
        "operator": (row.get("operator") or "").strip(),
        "point": (lat, lon),
        "row": row,
        "wheelchair": _ACCESS.get((row.get("accessibility") or "").strip().lower()),
        "accessibility_text": (row.get("accessibility") or "").strip(),
        "gender_neutral": ("all gender" in rtype.lower()) if rtype else None,
        "restroom_type": rtype,
        "changing": changing_ok,
        "changing_detail": changing,
        "notes": (row.get("additional_notes") or "").strip(),
    }


def nyc_sites() -> list[dict]:
    """All official sites (about 1,000 rows, so fetch once and filter locally). Cached 6 hours."""

    def fetch():
        sites = {}
        for row in _get_json(RESTROOMS_URL, {"$limit": 5000}, timeout=20):
            site = _normalize_site(row)
            if site:
                sites[site["id"]] = site
        return list(sites.values())

    return _cached("nyc_sites", 6 * 3600, fetch)


def _need_check(site: dict, need: str) -> str:
    """'yes' | 'no' | 'unknown' for one need."""
    if need == "wheelchair":
        return {"full": "yes", "partial": "unknown", "no": "no"}.get(site["wheelchair"], "unknown")
    if need == "gender_neutral":
        return _yes_no_unknown(site["gender_neutral"])
    return _yes_no_unknown(site["changing"])


def _meets(site: dict, needs: list[str]) -> tuple[str, list[str]]:
    """Overall 'yes' | 'no' | 'unknown', plus the needs the data could not confirm."""
    checks = {n: _need_check(site, n) for n in needs}
    if "no" in checks.values():
        return "no", []
    unconfirmed = [n for n, v in checks.items() if v == "unknown"]
    return ("unknown" if unconfirmed else "yes"), unconfirmed


def _describe(site: dict, walk_m: float, verdict: dict, when: datetime, unconfirmed: list[str]) -> dict:
    record = {
        "id": site["id"],
        "name": site["name"],
        "type": site["type"],
        "operator": site["operator"],
        "walk_min": walk_minutes(walk_m),
        "walk_m": int(round(walk_m, -1)),
        "status": verdict["state"],  # open | closed | unclear, at the requested time
        "status_note": verdict["reason"],
        "hours_that_day": hours_for_day(site["row"].get("hours_of_operation"), when.weekday()),
        "seasonal": verdict["seasonal"],
        "accessibility": site["accessibility_text"] or "not listed",
        "all_gender": site["gender_neutral"],
        "changing_station": site["changing_detail"] or "not listed",
        "map_link": _maps_link(site["point"]),
    }
    if site["notes"]:
        record["notes"] = site["notes"][:140]
    if unconfirmed:
        record["unconfirmed_needs"] = unconfirmed
    return record


# --- find_restrooms ---


def find_restrooms(state: dict, location: str, radius_m: int | None = None, needs: list | None = None,
                   when: str | None = None, limit: int = 5) -> dict:
    origin, label = geocode(location)
    when_dt = _parse_when(when)
    needs = _resolve_needs(state, needs)
    limit = _clamp(limit, 1, 10, 5)
    explicit_radius = radius_m is not None  # a distance the user asked for is respected, never widened
    radius_m = _clamp(radius_m, 100, 3000, DEFAULT_RADIUS_M) if explicit_radius else DEFAULT_RADIUS_M

    def scan(radius: int):
        confirmed, maybe, closed = [], [], []
        for site in nyc_sites():
            if haversine_m(origin, site["point"]) > radius:
                continue
            verdict = availability(site["row"], when_dt)
            walk_m = grid_distance_m(origin, site["point"])
            if verdict["state"] == "closed":
                closed.append((walk_m, site, verdict))
                continue
            fit, unconfirmed = _meets(site, needs)
            if fit == "no":
                continue
            entry = (walk_m, site, verdict, unconfirmed)
            (confirmed if fit == "yes" else maybe).append(entry)
        return confirmed, maybe, closed

    widened_from = None
    confirmed, maybe, closed = scan(radius_m)
    if not (confirmed or maybe) and not explicit_radius:
        widened_from, radius_m = radius_m, WIDENED_RADIUS_M
        confirmed, maybe, closed = scan(radius_m)

    # Sites that are confirmed to fit come first; unconfirmed ones only fill the remaining slots.
    confirmed.sort(key=lambda e: (e[2]["state"] != "open", e[0]))
    maybe.sort(key=lambda e: (e[2]["state"] != "open", e[0]))
    chosen = (confirmed + maybe)[:limit]
    results = [_describe(site, walk_m, verdict, when_dt, unconfirmed) for walk_m, site, verdict, unconfirmed in chosen]

    out = {
        "searched_near": label,
        "at_time": when_dt.strftime("%A %Y-%m-%d %H:%M") + " (NYC time)",
        "radius_m_searched": radius_m,
        "needs_applied": needs,
        "walk_times": "estimates along Manhattan's street grid, ~80 m/min",
        "results": results,
        "closed_nearby_omitted": len(closed),
    }
    if widened_from:
        out["widened_from_m"] = widened_from  # tell the user the search was widened automatically
    if closed:
        walk_m, site, verdict = min(closed, key=lambda c: c[0])
        out["nearest_closed"] = {"name": site["name"], "walk_min": walk_minutes(walk_m), "why": verdict["reason"]}
    if not results:
        needs_text = f" and meets the user's needs ({', '.join(n.replace('_', ' ') for n in needs)})" if needs else ""
        if closed:
            out["message"] = (
                f"No official restroom within {radius_m} m is open at that time{needs_text}; {len(closed)} nearby are closed then. "
                "A bigger radius is unlikely to help at this hour; tell the user the radius you searched."
            )
        else:
            out["message"] = (
                f"No official restroom within {radius_m} m is open at that time{needs_text}. "
                "Retry with a larger radius_m (up to 3000) if the user is happy to walk further."
            )
    return out


# --- What the model sees ---

_LOCATION = (
    "Where to search: a NYC street address, intersection, landmark, or neighborhood "
    "(e.g. 'Union Square', 'Broadway & 116th St'), "
    "or GPS as 'lat,lon' (e.g. '40.8075,-73.9626'). "
    "If the user's message contains '[my GPS location: lat,lon]', pass those coordinates."
)
_NEEDS = {
    "type": "array",
    "items": {"type": "string", "enum": NEEDS},
    "description": (
        "Requirements the restroom must meet. Omit to use the needs the user already told us this chat; "
        "pass [] to search with no requirements."
    ),
}
_WHEN = {
    "type": "string",
    "description": "Optional. ISO 8601 NYC local time, e.g. '2026-10-05T01:00'. Omit for right now. Use for 'tonight at 1am', 'tomorrow morning'.",
}

TOOLS = [
    {"type": "function", "function": {
        "name": "find_restrooms",
        "description": (
            "Find official NYC public restrooms (parks, libraries, plazas) near a place, ranked by estimated "
            "walking time, skipping sites that are closed at the requested time. Use for any 'nearest restroom / "
            "bathroom / toilet' question."
        ),
        "parameters": {"type": "object", "properties": {
            "location": {"type": "string", "description": _LOCATION},
            "radius_m": {"type": "integer", "description": "Search radius in meters, 100-3000. Omit it unless the user gave a distance (1 short block is about 80 m, 1 avenue block about 270 m). When omitted the tool searches 800 m and widens to 2000 m by itself if nothing is open."},
            "needs": _NEEDS,
            "when": _WHEN,
            "limit": {"type": "integer", "description": "Max results, 1-10. Default 5."},
        }, "required": ["location"]},
    }},
]

TOOL_MAP = {
    "find_restrooms": find_restrooms,
}


def run_tool(name: str, args: dict, state: dict) -> str:
    """Run one tool call. Models invent tool names and arguments; never let that crash the loop.

    `state` is this session's meta state (not the LLM context). Tools read it so the model
    does not have to re-supply things it was already told.
    """
    if name not in TOOL_MAP:
        return json.dumps({"error": f"Unknown tool '{name}'. Available: {list(TOOL_MAP)}"})
    try:
        return json.dumps(TOOL_MAP[name](state, **args), ensure_ascii=False)
    except ToolError as e:
        return json.dumps({"error": str(e)})
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}. Check the tool's parameter list."})
    except requests.RequestException as e:
        return json.dumps({"error": f"A city data service did not respond ({type(e).__name__}). Wait a moment and retry once; if it keeps failing, tell the user the data is temporarily unavailable."})
    except Exception as e:  # never leak a stack trace to the model or the user
        return json.dumps({"error": f"Unexpected problem in {name} ({type(e).__name__}). Try rephrasing the request or a different tool."})
