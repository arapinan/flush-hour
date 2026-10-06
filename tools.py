"""Tools for the Flush Hour restroom agent, and the JSON that describes them to the model.

Data sources (all free, no API key):
  - NYC Open Data "Public Restrooms" (official, ~1,000 sites, has hours)
  - NYC Planning Labs GeoSearch (address -> coordinates)
  - OpenStreetMap Nominatim (business and venue names -> coordinates; GeoSearch does not know these)
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
from hours import availability, hours_for_day, next_open

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
HOURS_MATCH_RADIUS_M = 5
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

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOSEARCH_GAP_M = 1000  # street/address matches this far apart are different places
VENUE_GAP_M = 1000  # named places closer than this are one place (a bridge or a street comes back as many pieces); farther apart = different branches
NOMINATIM_GAP_S = 1.1  # Nominatim's usage policy: at most one request per second (it answers 429 otherwise)
_last_nominatim = [0.0]
MAX_OPTIONS = 4
TOO_MANY_PLACES = 5  # this many distinct matches means a chain or a very common name: ask for a neighborhood instead
NYC_VIEWBOX = "-74.27,40.92,-73.68,40.49"  # left,top,right,bottom
_COORDS = re.compile(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")


class NeedsClarification(Exception):
    """The place matches several different places. Carries a payload the model relays to the user."""

    def __init__(self, query: str, candidates: list[dict], weak: bool = False):
        super().__init__(query)
        options = [
            {"label": c["label"], "location": f"{c['label']} @ {c['point'][0]:.5f},{c['point'][1]:.5f}"}
            for c in candidates[:MAX_OPTIONS]
        ]
        self.payload = {
            "needs_clarification": True,
            "question": (
                f"I could not find an exact match for '{query}'. The closest matches are listed. "
                "Ask the user whether one of them is right, or for a nearby cross street."
                if weak else
                f"'{query}' matches {len(options)} different places in NYC. Ask the user which one they mean."
            ),
            "options": options,
            "advice": (
                "List the options briefly and wait for the answer. Then call the tool again with the chosen "
                "option's `location` value exactly as given. Do not guess."
            ),
        }


def _spread(candidates: list[dict], min_gap_m: float) -> list[dict]:
    """One candidate per distinct place, keeping the API's ranking order."""
    kept: list[dict] = []
    for c in candidates:
        if all(haversine_m(c["point"], k["point"]) >= min_gap_m for k in kept):
            kept.append(c)
    return kept


def _geosearch_candidates(text: str) -> list[dict]:
    data = _get_json(GEOSEARCH_URL, {"text": text, "size": 5})
    found = []
    for feature in data.get("features") or []:
        lon, lat = feature["geometry"]["coordinates"]
        props = feature.get("properties", {})
        if in_nyc((lat, lon)):
            label = re.sub(r",\s*NY,\s*USA$", "", props.get("label") or props.get("name") or text)
            found.append({"label": label, "point": (lat, lon), "confidence": props.get("confidence"), "fallback": props.get("match_type") == "fallback",
                          "name_text": label.split(",")[0],
                          "match_text": " ".join(str(props.get(k, "")) for k in ("label", "name", "street"))})
    if found and found[0]["confidence"] is not None:  # drop clearly weaker matches
        floor = found[0]["confidence"] - 0.1
        found = [c for c in found if c["confidence"] is None or c["confidence"] >= floor]
    return found


def _venue_candidates(text: str) -> list[dict]:
    """Business and venue names (GeoSearch only knows addresses), via OpenStreetMap Nominatim."""
    params = {"q": text, "format": "jsonv2", "limit": 10, "addressdetails": 1,
              "countrycodes": "us", "viewbox": NYC_VIEWBOX, "bounded": 1}
    found = []
    for attempt in range(2):   # polite: one request per second, and one retry if we are told to slow down
        wait = NOMINATIM_GAP_S - (time.time() - _last_nominatim[0])
        if wait > 0:
            time.sleep(wait)
        _last_nominatim[0] = time.time()
        try:
            rows = _get_json(NOMINATIM_URL, params)
            break
        except requests.HTTPError as e:
            if attempt == 1 or getattr(e.response, "status_code", None) != 429:
                raise
            time.sleep(2)
    for r in rows:
        try:
            point = (float(r["lat"]), float(r["lon"]))
        except (KeyError, TypeError, ValueError):
            continue
        if not in_nyc(point):
            continue
        a = r.get("address", {})
        # The bounding box also covers New Jersey and Nassau County; keep only New York City addresses.
        if a.get("state") not in (None, "New York") or a.get("city") not in (None, "New York"):
            continue
        street = " ".join(p for p in (a.get("house_number"), a.get("road")) if p)
        area = a.get("neighbourhood") or a.get("suburb") or a.get("city_district") or a.get("borough")
        name = r.get("name") or (r.get("display_name") or "").split(",")[0]
        if street.lower() == name.lower():
            street = ""
        label = ", ".join(p for p in (name, street, area) if p)
        found.append({"label": label or text, "point": point, "name_text": name,
                      "match_text": f"{r.get('name', '')} {r.get('display_name', '')}"})
    return found


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def _fits_query(candidate: dict, query_tokens: set[str], threshold: float = 0.75) -> bool:
    """Does the candidate actually contain the words the user typed? Rejects fuzzy guesses
    (the GeoSearch 'fallback' that answers 'Amity Hall' with some Amity Street)."""
    if not query_tokens:
        return True
    return len(query_tokens & _tokens(candidate["match_text"])) / len(query_tokens) >= threshold


def _prefer_exact(query_tokens: set[str], candidates: list[dict]) -> list[dict]:
    """If some candidates are named exactly what the user typed, drop the ones that merely contain it
    ('Columbia University' should not also match 'Columbia University Boathouse')."""
    exact = [c for c in candidates if _tokens(c.get("name_text", "")) == query_tokens]
    return exact or candidates


_venue_lookup_failed = [False]  # set when the last place-name lookup errored (as opposed to finding nothing)


def _safe_venues(text: str) -> list[dict]:
    try:
        _venue_lookup_failed[0] = False
        return _venue_candidates(text)
    except (requests.RequestException, ValueError):
        _venue_lookup_failed[0] = True
        return []


def geocode(text: str) -> tuple[tuple[float, float], str]:
    """Place text or 'lat,lon' -> ((lat, lon), label).

    Raises ToolError (fixable) or NeedsClarification (several matching places).
    A location of the form 'Some label @ lat,lon' is what the clarification options use.
    """
    if not text or not text.strip():
        raise ToolError("No location given. Ask the user where they are (address, intersection, or landmark).")

    coords = list(_COORDS.finditer(text))
    if coords:
        last = coords[-1]
        point = (float(last.group(1)), float(last.group(2)))
        label = text.split("@")[0].strip() if "@" in text else ""
        if not in_nyc(point):
            raise ToolError(f"'{text}' is outside New York City, and this agent only covers the five boroughs.")
        return point, label or "your GPS location"

    def lookup():
        query = text.strip()
        geo = _geosearch_candidates(query)
        # Addresses and intersections: the city's own geocoder is authoritative.
        if re.search(r"\d|&|/|\b(?:and|at)\b", query.lower()):
            good = [c for c in geo if not c.get("fallback")]   # GeoSearch guesses ('6 ST AND AVE B' for '14th Street & 6th Avenue') are not answers
            if good:
                return {"candidates": _spread(good, GEOSEARCH_GAP_M), "weak": False}
            venues = _spread(_safe_venues(query), VENUE_GAP_M)
            if venues:
                return {"candidates": venues, "weak": False}
            return {"candidates": _spread(geo, GEOSEARCH_GAP_M), "weak": bool(geo)}
        # Names (places, businesses, streets): trust a result only if it contains the words typed,
        # and prefer ones named exactly that.
        words = _tokens(query)
        geo_good = _prefer_exact(words, [c for c in geo if _fits_query(c, words)])
        venues = _spread(_prefer_exact(words, [c for c in _safe_venues(query) if _fits_query(c, words)]), VENUE_GAP_M)
        if len(venues) >= TOO_MANY_PLACES:
            return {"too_many": True, "candidates": venues, "weak": False}
        near = [v for v in venues if any(haversine_m(v["point"], g["point"]) < GEOSEARCH_GAP_M for g in geo_good)]
        if geo_good and near:   # both services agree on one spot ('Astor Place' in Manhattan; another one may exist in Queens)
            return {"candidates": [near[0]], "weak": False}
        if len(venues) >= 2:  # several real places share this name (branches, or a long street): ask
            return {"candidates": venues, "weak": False}
        if geo_good:
            return {"candidates": _spread(geo_good, GEOSEARCH_GAP_M), "weak": False}
        if venues:
            return {"candidates": venues, "weak": False}
        return {"candidates": _spread(geo, GEOSEARCH_GAP_M), "weak": True}  # only weak guesses: confirm with the user

    fresh = {}

    def lookup_for_cache():
        _venue_lookup_failed[0] = False
        fresh["result"] = lookup()
        # Do not remember an answer reached while the place-name service was erroring: it may be wrong or empty.
        return fresh["result"] if fresh["result"]["candidates"] and not _venue_lookup_failed[0] else None

    result = _cached("geo:" + text.strip().lower(), 86400, lookup_for_cache) or fresh.get("result")
    found = result["candidates"] if result else []
    if result and result.get("too_many"):
        raise ToolError(
            f"'{text}' matches many places across NYC (a chain, or a long street). Ask the user for a "
            "neighborhood, cross street or address near where they are."
        )
    if not found:
        raise ToolError(
            f"Could not find '{text}' in NYC. Try a street address, a landmark or business name, or an "
            "intersection written with full street names like 'Amsterdam Avenue & West 116th Street'. "
            "If that fails, ask the user for a nearby landmark or address."
            + (" (The place-name lookup service is busy right now, so a street address or intersection is the safer input.)"
               if _venue_lookup_failed[0] else "")
        )
    if result["weak"] or len(found) > 1:
        raise NeedsClarification(text.strip(), found, weak=result["weak"])
    return found[0]["point"], found[0]["label"]


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


# --- Tool 1 (shared): find_restrooms ---


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
        "at_time": when_dt.strftime("%A %Y-%m-%d %H:%M") + " EST",
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


# --- Tool 2: restrooms_along_route ---


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
        "leaving_at": when_dt.strftime("%A %H:%M") + " EST",
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


# --- Tool 3: check_open_status ---


def check_open_status(state: dict, restroom_id: str, when: str | None = None) -> dict:
    when_dt = _parse_when(when)
    if restroom_id.startswith("refuge-") or restroom_id.startswith("osm-"):
        raise ToolError("Community and OpenStreetMap listings do not include hours. Only ids starting 'nyc-' can be checked.")
    site = next((s for s in nyc_sites() if s["id"] == restroom_id), None)
    if not site:
        raise ToolError(
            f"Unknown restroom_id '{restroom_id}'. Ids come from earlier find_restrooms or "
            "restrooms_along_route results; call one of those first."
        )
    row = site["row"]
    verdict = availability(row, when_dt)
    out = {
        "id": site["id"],
        "name": site["name"],
        "checked_for": when_dt.strftime("%A %Y-%m-%d %H:%M") + " EST",
        "status": verdict["state"],
        "status_note": verdict["reason"],
        "city_status": row.get("status") or "not listed",
        "season": row.get("open") or "not listed",
        "weekly_hours": {d: hours_for_day(row.get("hours_of_operation"), i)
                         for i, d in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"])},
        "map_link": _maps_link(site["point"]),
    }
    if verdict["state"] != "open":
        reopening = next_open(row.get("hours_of_operation"), when_dt)
        if reopening and verdict["state"] == "closed":
            out["next_open"] = reopening
    if verdict["state"] == "unclear":
        out["advice"] = "Tell the user the hours are uncertain and suggest a backup (find_restrooms or fallback_options)."
    return out


# --- Tool 4: fallback_options ---


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


def _osm_near(origin: tuple[float, float], radius_m: int) -> tuple[list[dict], list[dict]]:
    """One Overpass round trip for everything fallback_options needs from OSM: toilets, and (separately)
    anything nearby with an opening_hours tag. Refuge Restrooms never gives hours itself, so a nearby OSM
    listing of the same business (matched by distance back in fallback_options) is the only free source we
    have for those. Two queries in one call instead of two round trips, since Overpass is already slow/flaky.
    Returns (toilets, hours_elements).
    """
    lat, lon = origin
    query = (
        f'[out:json][timeout:8];('
        f'node["amenity"="toilets"](around:{radius_m},{lat},{lon});way["amenity"="toilets"](around:{radius_m},{lat},{lon});'
        f'node["opening_hours"](around:{radius_m},{lat},{lon});way["opening_hours"](around:{radius_m},{lat},{lon});'
        f');out center 80;'
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

    toilets, hours = [], []
    for el in elements:
        tags = el.get("tags", {})
        point = (el.get("lat"), el.get("lon")) if "lat" in el else (el.get("center", {}).get("lat"), el.get("center", {}).get("lon"))
        if None in point:
            continue
        if tags.get("amenity") == "toilets" and tags.get("access") not in ("private", "no"):
            toilets.append({
                "id": f"osm-{el['id']}",
                "name": tags.get("name") or "Unnamed toilets (OpenStreetMap)",
                "point": point,
                "wheelchair": tags.get("wheelchair") == "yes",
                "access": tags.get("access", "not stated"),
                "fee": tags.get("fee", "not stated"),
                "hours_raw": tags.get("opening_hours"),
            })
        if tags.get("opening_hours") and tags.get("name"):
            hours.append({"point": point, "name": tags["name"], "hours_raw": tags["opening_hours"]})
    return toilets, hours


def fallback_options(state: dict, location: str, when: str | None = None, radius_m: int = 1000, limit: int = 3) -> dict:
    origin, label = geocode(location)
    when_dt = _parse_when(when)
    radius_m, limit = _clamp(radius_m, 200, 2000, 1000), _clamp(limit, 1, 10, 3)
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

    osm_toilets: list[dict] = []
    osm_hours: list[dict] = []
    osm_error: requests.RequestException | None = None
    try:
        osm_toilets, osm_hours = _osm_near(origin, radius_m)
    except requests.RequestException as e:
        osm_error = e  # best-effort: community listings just show as hours-unknown, and the osm tier is skipped

    def same_business(name_a: str, name_b: str) -> bool:
        # Proximity alone isn't enough to prove it's the same place (dense blocks, shared addresses like a
        # food hall or a medical building can put several unrelated businesses within HOURS_MATCH_RADIUS_M).
        a, b = _tokens(name_a), _tokens(name_b)
        return bool(a and b and len(a & b) / min(len(a), len(b)) >= 0.5)

    def nearest_hours(point, name):
        matches = [m for m in osm_hours if haversine_m(point, m["point"]) <= HOURS_MATCH_RADIUS_M and same_business(name, m["name"])]
        return min(matches, key=lambda m: haversine_m(point, m["point"])) if matches else None

    seen_points = []
    try:
        for r in _refuge_near(origin):
            if haversine_m(origin, r["point"]) > radius_m or near_official(r["point"]):
                continue
            old = r["listing_age_years"] is not None and r["listing_age_years"] >= 5
            disliked = r["downvotes"] > r["upvotes"]
            match = nearest_hours(r["point"], r["name"])
            option = {
                "tier": "community_listed", "confidence": "low" if (old or disliked) else "medium",
                "id": r["id"], "name": r["name"], "address": r["address"],
                "walk_min": walk_minutes(grid_distance_m(origin, r["point"])),
                "wheelchair": r["wheelchair"], "all_gender": r["all_gender"], "changing_station": r["changing_station"],
                "community_votes": f"{r['upvotes']} up / {r['downvotes']} down",
                "listing_age_years": r["listing_age_years"],
                "map_link": _maps_link(r["point"]),
            }
            if match:
                option["hours_raw"] = match["hours_raw"]
                option["caveat"] = "Listed by volunteers; hours below are from OpenStreetMap for this address, not verified by the city."
            else:
                option["caveat"] = "Listed by volunteers; hours unknown and may be for customers only."
            options.append(option)
            seen_points.append(r["point"])
    except requests.RequestException:
        notes.append("Refuge Restrooms (community listings) did not respond, so those are missing.")

    if osm_error:
        notes.append("OpenStreetMap did not respond (its public servers are often busy), so those are missing.")
    else:
        for o in osm_toilets:
            if near_official(o["point"]) or any(haversine_m(o["point"], p) < DUPLICATE_RADIUS_M for p in seen_points):
                continue
            options.append({
                "tier": "openstreetmap", "confidence": "low", "id": o["id"], "name": o["name"],
                "walk_min": walk_minutes(grid_distance_m(origin, o["point"])),
                "access": o["access"], "fee": o["fee"], "wheelchair": o["wheelchair"],
                "hours_raw": o["hours_raw"], "map_link": _maps_link(o["point"]),
            })

    tier_rank = {"official_but_hours_uncertain": 0, "community_listed": 1, "openstreetmap": 2}
    conf_rank = {"medium": 0, "low": 1}
    options.sort(key=lambda o: (tier_rank[o["tier"]], conf_rank[o["confidence"]], o["walk_min"]))
    out = {
        "searched_near": label,
        "at_time": when_dt.strftime("%A %Y-%m-%d %H:%M") + " EST",
        "official_sites_confirmed_open_nearby": confirmed_open,
        "options": options[:limit],
        "notes": notes,
    }
    if confirmed_open:
        out["hint"] = f"{confirmed_open} official site(s) in range are confirmed open; find_restrooms lists them with details."
    if not options:
        out["message"] = "Nothing found in range. Try a larger radius_m (up to 2000), or a different time."
    return out


# --- Tool 5: remember_needs ---


def remember_needs(state: dict, needs: list) -> dict:
    bad = [n for n in needs if n not in NEEDS]
    if bad:
        raise ToolError(f"Unknown need(s) {bad}. Allowed values: {NEEDS}.")
    state["needs"] = list(dict.fromkeys(needs))
    return {"saved_needs": state["needs"], "effect": "Later searches in this chat apply these automatically unless `needs` is passed."}


# --- What the model sees ---

_LOCATION = (
    "Where to search: a NYC street address, intersection, landmark, or neighborhood "
    "(e.g. 'Union Square', 'Amsterdam Avenue & West 116th Street'; write intersections with full street names), "
    "or GPS as 'lat,lon' (e.g. '40.8075,-73.9626'). "
    "If the user's message contains '[my GPS location: lat,lon]', pass those coordinates. "
    "If a result says needs_clarification, ask the user which place they mean, then pass the chosen option's `location` exactly."
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
        "name": "check_open_status",
        "description": (
            "Check whether one specific official restroom is open at a given time, with its weekly hours and next "
            "opening time. Use to follow up on a restroom already returned by find_restrooms or "
            "restrooms_along_route (e.g. 'will that one be open at 9pm?'). Not for discovering restrooms."
        ),
        "parameters": {"type": "object", "properties": {
            "restroom_id": {"type": "string", "description": "The `id` of a restroom from an earlier tool result, e.g. 'nyc-3fa91c2b'. Must start with 'nyc-'."},
            "when": _WHEN,
        }, "required": ["restroom_id"]},
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
            "limit": {"type": "integer", "description": "Max options, 1-10. Default 3; raise it if the user asks for more options."},
        }, "required": ["location"]},
    }},
    {"type": "function", "function": {
        "name": "remember_needs",
        "description": (
            "Save the user's restroom requirements for the rest of this chat so later searches apply them "
            "automatically. Call when the user states a standing need (\"I use a wheelchair\", \"I have a baby\"). "
            "Pass [] to clear them."
        ),
        "parameters": {"type": "object", "properties": {
            "needs": {"type": "array", "items": {"type": "string", "enum": NEEDS}, "description": "The full list of needs to remember. Replaces any earlier list."},
        }, "required": ["needs"]},
    }},
]

TOOL_MAP = {
    "find_restrooms": find_restrooms,
    "restrooms_along_route": restrooms_along_route,
    "check_open_status": check_open_status,
    "fallback_options": fallback_options,
    "remember_needs": remember_needs,
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
    except NeedsClarification as e:
        return json.dumps(e.payload, ensure_ascii=False)
    except ToolError as e:
        return json.dumps({"error": str(e)})
    except TypeError as e:
        return json.dumps({"error": f"Bad arguments for {name}: {e}. Check the tool's parameter list."})
    except requests.RequestException as e:
        return json.dumps({"error": f"A city data service did not respond ({type(e).__name__}). Wait a moment and retry once; if it keeps failing, tell the user the data is temporarily unavailable."})
    except Exception as e:  # never leak a stack trace to the model or the user
        return json.dumps({"error": f"Unexpected problem in {name} ({type(e).__name__}). Try rephrasing the request or a different tool."})
