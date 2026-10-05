"""Tools for the Flush Hour restroom agent, and the JSON that describes them to the model.

Data sources (all free, no API key):
  - NYC Open Data "Public Restrooms" (official, ~1,000 sites, has hours)
  - NYC Planning Labs GeoSearch (address -> coordinates)
  - Refuge Restrooms (community-submitted listings; can be old)
  - OpenStreetMap via Overpass (best effort: public servers time out often)

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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from geo import (
    detour_m,
    grid_distance_m,
    haversine_m,
    in_nyc,
    route_position,
    walk_minutes,
)
from hours import availability, hours_for_day

NYC_TZ = ZoneInfo("America/New_York")
HEADERS = {"User-Agent": "flush-hour-class-project/0.1 (Columbia IEOR 4570 student assignment)"}

RESTROOMS_URL = "https://data.cityofnewyork.us/resource/i7jb-7jku.json"
GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"
REFUGE_URL = "https://www.refugerestrooms.org/api/v1/restrooms/by_location"
OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

NEEDS = ["wheelchair", "changing_station", "gender_neutral"]
DUPLICATE_RADIUS_M = 40
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


def _post_json(url: str, data: dict, timeout: int = 9):
    response = requests.post(url, data=data, headers=HEADERS, timeout=timeout)
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
                "A bigger radius is unlikely to help at this hour. Call fallback_options now, and tell the user the radius you searched."
            )
        else:
            out["message"] = (
                f"No official restroom within {radius_m} m is open at that time{needs_text}. "
                "Call fallback_options, or retry with a larger radius_m (up to 3000) if the user is happy to walk further."
            )
    return out


# --- restrooms_along_route ---


def restrooms_along_route(state: dict, start: str, end: str, max_detour_m: int = 300,
                          needs: list | None = None, when: str | None = None, limit: int = 5) -> dict:
    a, start_label = geocode(start)
    b, end_label = geocode(end)
    when_dt = _parse_when(when)
    needs = _resolve_needs(state, needs)
    max_detour_m, limit = _clamp(max_detour_m, 50, 1500, 300), _clamp(limit, 1, 8, 5)

    total_m = grid_distance_m(a, b)
    if total_m < 200:
        raise ToolError("Start and end are within a couple of minutes' walk of each other. Use find_restrooms near the start instead.")
    total_min = walk_minutes(total_m)

    found, closed = [], 0
    for site in nyc_sites():
        detour = detour_m(a, b, site["point"])
        if detour > max_detour_m:
            continue
        to_stop = grid_distance_m(a, site["point"])
        arrival = when_dt + timedelta(minutes=walk_minutes(to_stop))  # judge hours at the time you would arrive
        verdict = availability(site["row"], arrival)
        if verdict["state"] == "closed":
            closed += 1
            continue
        fit, unconfirmed = _meets(site, needs)
        if fit == "no":
            continue
        found.append((detour, to_stop, site, verdict, unconfirmed, fit))

    found.sort(key=lambda f: (f[5] != "yes", f[0]))
    picked = found[:limit]
    picked.sort(key=lambda f: route_position(a, b, f[2]["point"]))  # present in walking order

    stops = []
    for detour, to_stop, site, verdict, unconfirmed, _fit in picked:
        record = _describe(site, to_stop, verdict, when_dt, unconfirmed)
        record["minutes_into_walk"] = record.pop("walk_min")
        record.pop("walk_m")
        record["extra_walk_min"] = walk_minutes(detour) if detour > 20 else 0
        record["percent_of_way"] = int(round(100 * route_position(a, b, site["point"])))
        stops.append(record)

    fractions = [0.0] + [s["percent_of_way"] / 100 for s in stops] + [1.0]
    longest_gap = max(y - x for x, y in zip(fractions, fractions[1:])) * total_min
    out = {
        "route": f"{start_label} → {end_label}",
        "walk_min_total": total_min,
        "leaving_at": when_dt.strftime("%A %H:%M") + " (NYC time)",
        "needs_applied": needs,
        "stops_in_walking_order": stops,
        "longest_stretch_without_a_stop_min": int(round(longest_gap)),
        "closed_on_arrival_omitted": closed,
        "note": "Route is approximated as a grid walk between the two points, not turn-by-turn directions.",
    }
    if not stops:
        out["message"] = (
            f"No official restroom within {max_detour_m} m of the route is open when you would pass it. "
            "Try a larger max_detour_m (up to 1500) or call fallback_options near the middle of the route."
        )
    return out


# --- fallback_options ---


def _refuge_near(origin: tuple[float, float], per_page: int = 30) -> list[dict]:
    year = datetime.now().year
    out = []
    for r in _get_json(REFUGE_URL, {"lat": origin[0], "lng": origin[1], "per_page": per_page}):
        if r.get("approved") is False or r.get("latitude") is None or r.get("longitude") is None:
            continue
        updated = (r.get("updated_at") or "")[:4]
        out.append({
            "id": f"refuge-{r['id']}",
            "name": (r.get("name") or "Unnamed place").strip(),
            "address": ", ".join(p for p in (r.get("street"), r.get("city")) if p),
            "point": (float(r["latitude"]), float(r["longitude"])),
            "wheelchair": bool(r.get("accessible")),
            "all_gender": bool(r.get("unisex")),
            "changing_station": bool(r.get("changing_table")),
            "upvotes": r.get("upvote") or 0,
            "downvotes": r.get("downvote") or 0,
            "listing_age_years": (year - int(updated)) if updated.isdigit() else None,
            "directions": (r.get("directions") or "").replace("\r", " ").strip()[:160],
        })
    return out


def _osm_toilets(origin: tuple[float, float], radius_m: int) -> list[dict]:
    lat, lon = origin
    query = (
        f'[out:json][timeout:8];(node["amenity"="toilets"](around:{radius_m},{lat},{lon});'
        f'way["amenity"="toilets"](around:{radius_m},{lat},{lon}););out center 25;'
    )
    last_error: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            elements = _post_json(url, {"data": query}).get("elements", [])
            break
        except (requests.RequestException, ValueError) as e:
            last_error = e
    else:
        raise requests.RequestException(f"OpenStreetMap servers did not respond ({last_error})")
    out = []
    for el in elements:
        tags = el.get("tags", {})
        point = (el.get("lat"), el.get("lon")) if "lat" in el else (el.get("center", {}).get("lat"), el.get("center", {}).get("lon"))
        if None in point or tags.get("access") in ("private", "no"):
            continue
        out.append({
            "id": f"osm-{el['id']}",
            "name": tags.get("name") or "Unnamed toilets (OpenStreetMap)",
            "point": point,
            "wheelchair": tags.get("wheelchair") == "yes",
            "access": tags.get("access", "not stated"),
            "fee": tags.get("fee", "not stated"),
            "hours_raw": tags.get("opening_hours"),
        })
    return out


def fallback_options(state: dict, location: str, when: str | None = None, radius_m: int = 1000, limit: int = 6) -> dict:
    origin, label = geocode(location)
    when_dt = _parse_when(when)
    radius_m, limit = _clamp(radius_m, 200, 2000, 1000), _clamp(limit, 1, 10, 6)
    notes: list[str] = []
    options: list[dict] = []
    official_points, confirmed_open = [], 0

    for site in nyc_sites():
        if haversine_m(origin, site["point"]) > radius_m:
            continue
        verdict = availability(site["row"], when_dt)
        if verdict["state"] == "open":
            confirmed_open += 1
        official_points.append(site["point"])
        if verdict["state"] == "unclear":
            options.append({
                "tier": "official_but_hours_uncertain", "confidence": "medium",
                "id": site["id"], "name": site["name"], "walk_min": walk_minutes(grid_distance_m(origin, site["point"])),
                "why_uncertain": verdict["reason"], "map_link": _maps_link(site["point"]),
            })

    def near_official(point):
        return any(haversine_m(point, p) < DUPLICATE_RADIUS_M for p in official_points)

    seen_points = []
    try:
        for r in _refuge_near(origin):
            if haversine_m(origin, r["point"]) > radius_m or near_official(r["point"]):
                continue
            old = r["listing_age_years"] is not None and r["listing_age_years"] >= 5
            disliked = r["downvotes"] > r["upvotes"]
            options.append({
                "tier": "community_listed", "confidence": "low" if (old or disliked) else "medium",
                "id": r["id"], "name": r["name"], "address": r["address"],
                "walk_min": walk_minutes(grid_distance_m(origin, r["point"])),
                "wheelchair": r["wheelchair"], "all_gender": r["all_gender"], "changing_station": r["changing_station"],
                "community_votes": f"{r['upvotes']} up / {r['downvotes']} down",
                "listing_age_years": r["listing_age_years"],
                "caveat": "Listed by volunteers; hours unknown and may be for customers only.",
                "map_link": _maps_link(r["point"]),
            })
            seen_points.append(r["point"])
    except requests.RequestException:
        notes.append("Refuge Restrooms (community listings) did not respond, so those are missing.")

    try:
        for o in _osm_toilets(origin, radius_m):
            if near_official(o["point"]) or any(haversine_m(o["point"], p) < DUPLICATE_RADIUS_M for p in seen_points):
                continue
            options.append({
                "tier": "openstreetmap", "confidence": "low", "id": o["id"], "name": o["name"],
                "walk_min": walk_minutes(grid_distance_m(origin, o["point"])),
                "access": o["access"], "fee": o["fee"], "wheelchair": o["wheelchair"],
                "hours_raw": o["hours_raw"], "map_link": _maps_link(o["point"]),
            })
    except requests.RequestException:
        notes.append("OpenStreetMap did not respond (its public servers are often busy), so those are missing.")

    tier_rank = {"official_but_hours_uncertain": 0, "community_listed": 1, "openstreetmap": 2}
    conf_rank = {"medium": 0, "low": 1}
    options.sort(key=lambda o: (tier_rank[o["tier"]], conf_rank[o["confidence"]], o["walk_min"]))
    out = {
        "searched_near": label,
        "at_time": when_dt.strftime("%A %Y-%m-%d %H:%M") + " (NYC time)",
        "official_sites_confirmed_open_nearby": confirmed_open,
        "options": options[:limit],
        "notes": notes,
    }
    if confirmed_open:
        out["hint"] = f"{confirmed_open} official site(s) in range are confirmed open; find_restrooms lists them with details."
    if not options:
        out["message"] = "Nothing found in range. Try a larger radius_m (up to 2000), or a different time."
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
            "bathroom / toilet' question. Not for walking routes (use restrooms_along_route) and does not include "
            "businesses (use fallback_options when nothing suitable is open)."
        ),
        "parameters": {"type": "object", "properties": {
            "location": {"type": "string", "description": _LOCATION},
            "radius_m": {"type": "integer", "description": "Search radius in meters, 100-3000. Omit it unless the user gave a distance (1 short block is about 80 m, 1 avenue block about 270 m). When omitted the tool searches 800 m and widens to 2000 m by itself if nothing is open."},
            "needs": _NEEDS,
            "when": _WHEN,
            "limit": {"type": "integer", "description": "Max results, 1-10. Default 5."},
        }, "required": ["location"]},
    }},
    {"type": "function", "function": {
        "name": "restrooms_along_route",
        "description": (
            "Find official NYC public restrooms near a walking route from a start to an end, ranked by how little "
            "extra walking they add, in the order the walker would reach them. Checks each site's hours for the time "
            "the walker would actually arrive, and reports the longest stretch without a stop. Use when the user is "
            "going from A to B. Not for 'near me' questions (use find_restrooms)."
        ),
        "parameters": {"type": "object", "properties": {
            "start": {"type": "string", "description": "Start of the walk. " + _LOCATION},
            "end": {"type": "string", "description": "End of the walk (same formats as start)."},
            "max_detour_m": {"type": "integer", "description": "Longest acceptable extra walking distance per stop, in meters, 50-1500. Default 300."},
            "needs": _NEEDS,
            "when": {**_WHEN, "description": "Optional. When the walk STARTS: ISO 8601 NYC local time. Omit for right now."},
            "limit": {"type": "integer", "description": "Max stops, 1-8. Default 5."},
        }, "required": ["start", "end"]},
    }},
    {"type": "function", "function": {
        "name": "fallback_options",
        "description": (
            "Last-resort places to try when find_restrooms has nothing open or suitable: official sites whose hours "
            "are uncertain, volunteer-listed restrooms in businesses and campus buildings (with listing age and "
            "votes), and OpenStreetMap toilets. Each option carries a confidence and caveat; be honest with the "
            "user that these are less reliable. Call find_restrooms first."
        ),
        "parameters": {"type": "object", "properties": {
            "location": {"type": "string", "description": _LOCATION},
            "when": _WHEN,
            "radius_m": {"type": "integer", "description": "Search radius in meters, 200-2000. Default 1000."},
            "limit": {"type": "integer", "description": "Max options, 1-10. Default 6."},
        }, "required": ["location"]},
    }},
]

TOOL_MAP = {
    "find_restrooms": find_restrooms,
    "restrooms_along_route": restrooms_along_route,
    "fallback_options": fallback_options,
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
