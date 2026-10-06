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
from typing import Any, Callable
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
# The public OpenStreetMap servers require requests to identify themselves.
HEADERS = {"User-Agent": "flush-hour-class-project/0.1 (Columbia IEOR 4570 student assignment)"}

RESTROOMS_URL = "https://data.cityofnewyork.us/resource/i7jb-7jku.json"
GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"
REFUGE_URL = "https://www.refugerestrooms.org/api/v1/restrooms/by_location"
OVERPASS_URLS = [  # two mirrors, tried in order
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

NEEDS = ["wheelchair", "changing_station", "gender_neutral"]  # the `needs` enum

# Search sizes and result caps
DEFAULT_RADIUS_M = 800
WIDENED_RADIUS_M = 2000  # find_restrooms retries once at this radius if 800 m finds nothing
# Per tool call. The page shows at most 3 cards per section, so the reply never names
# places it can't show.
MAX_RESULTS = 3
# A walk gets more room: the page shows up to 5 confirmed-open stops for a route.
MAX_ROUTE_STOPS = 5

# fallback_options matching
DUPLICATE_RADIUS_M = 40  # listings this close together are treated as the same place
# Community listing -> OSM business with hours. Volunteer pins are often tens of meters
# off, so the name must agree too.
HOURS_MATCH_RADIUS_M = 150
MAX_HOURS_LOOKUPS = 8  # Nominatim lookups per fallback_options call (about one second each)


class ToolError(Exception):
    """A problem the model can fix by changing its next call. The message is shown to it."""


# --- Small utilities ---

# key -> (time stored, value). In-memory, single process, like the session store.
_cache: dict = {}


def _cached(key: str, ttl_seconds: int, fn: Callable[[], Any]) -> Any:
    """Return the value cached under `key` if it is newer than the TTL, else call fn()."""
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < ttl_seconds:
        return hit[1]
    value = fn()
    if value:  # never remember an empty answer: it may just be a service hiccup
        _cache[key] = (time.time(), value)
    return value


def _get_json(url: str, params: dict | None = None, timeout: int = 10) -> Any:
    """GET a JSON API. Raises requests.RequestException on a timeout or an HTTP error."""
    response = requests.get(url, params=params, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _post_json(url: str, data: dict, timeout: int = 9) -> Any:
    """POST form data to a JSON API (Overpass). Raises like _get_json."""
    response = requests.post(url, data=data, headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    """Force a model-supplied number into [low, high]; None or junk becomes `default`."""
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _maps_link(point: tuple[float, float]) -> str:
    """Google Maps walking directions to `point`; the page turns it into a button."""
    return f"https://www.google.com/maps/dir/?api=1&destination={point[0]},{point[1]}&travelmode=walking"


def _parse_when(when: str | None) -> datetime:
    """NYC local time as a naive datetime. None means right now."""
    if not when:
        return datetime.now(NYC_TZ).replace(tzinfo=None, second=0, microsecond=0)
    try:
        parsed = datetime.fromisoformat(when.strip())
    except ValueError:
        raise ToolError(
            f"Could not read when='{when}'. Use ISO 8601 in NYC local time, "
            "e.g. '2026-10-05T01:00', or omit it for right now."
        )
    if parsed.tzinfo:  # a time with a UTC offset: convert it to the NYC wall clock
        parsed = parsed.astimezone(NYC_TZ).replace(tzinfo=None)
    return parsed


def _resolve_needs(state: dict, needs: list | None) -> list[str]:
    """Explicit needs win; otherwise use what the user told us earlier this session."""
    if needs is None:
        needs = state.get("needs", [])
    bad = [n for n in needs if n not in NEEDS]
    if bad:
        raise ToolError(f"Unknown need(s) {bad}. Allowed values: {NEEDS}.")
    return list(dict.fromkeys(needs))  # drop repeats, keep order


# --- Geocoding ---
#
# Two services turn the user's place text into coordinates: the city's GeoSearch knows
# addresses and intersections, Nominatim knows businesses and venues. geocode() below
# decides which to believe, and asks the user when a name fits several places.

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
GEOSEARCH_GAP_M = 1000  # street/address matches this far apart are different places
# Named places closer than this are one place (a bridge or a street comes back as many
# pieces); farther apart = different branches.
VENUE_GAP_M = 1000
# Nominatim's usage policy: at most one request per second (it answers 429 otherwise).
NOMINATIM_GAP_S = 1.1
_last_nominatim = [0.0]  # time of the last request; a one-item list so _nominatim can update it
MAX_OPTIONS = 4  # most choices offered in one "which place?" question
# This many distinct matches means a chain or a very common name: ask for a neighborhood
# instead of listing them.
TOO_MANY_PLACES = 5
NYC_VIEWBOX = "-74.27,40.92,-73.68,40.49"  # left,top,right,bottom
_COORDS = re.compile(r"(-?\d{1,3}\.\d+)\s*,\s*(-?\d{1,3}\.\d+)")  # 'lat,lon' anywhere in the text


class NeedsClarification(Exception):
    """The place matches several different places. Carries a payload the model relays to the user."""

    def __init__(self, query: str, candidates: list[dict], weak: bool = False):
        super().__init__(query)
        # Each option's `location` embeds its coordinates ('Label @ lat,lon'), which
        # geocode() reads straight back, so the follow-up call needs no second lookup.
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
    """Addresses, intersections and some landmarks, via the city's own geocoder."""
    data = _get_json(GEOSEARCH_URL, {"text": text, "size": 5})
    found = []
    for feature in data.get("features") or []:
        lon, lat = feature["geometry"]["coordinates"]  # GeoJSON order: lon first
        props = feature.get("properties", {})
        if in_nyc((lat, lon)):
            label = re.sub(r",\s*NY,\s*USA$", "", props.get("label") or props.get("name") or text)
            found.append({
                "label": label,
                "point": (lat, lon),
                "confidence": props.get("confidence"),
                "fallback": props.get("match_type") == "fallback",  # a fuzzy guess, not a real match
                # What _prefer_exact and _fits_query compare against the user's words
                "name_text": label.split(",")[0],
                "match_text": " ".join(str(props.get(k, "")) for k in ("label", "name", "street")),
            })
    if found and found[0]["confidence"] is not None:  # drop clearly weaker matches
        floor = found[0]["confidence"] - 0.1
        found = [c for c in found if c["confidence"] is None or c["confidence"] >= floor]
    return found


def _nominatim(params: dict) -> list[dict]:
    """One Nominatim search, spaced out to respect its rate limit."""
    for attempt in range(2):   # polite: one request per second, and one retry if we are told to slow down
        wait = NOMINATIM_GAP_S - (time.time() - _last_nominatim[0])
        if wait > 0:
            time.sleep(wait)
        _last_nominatim[0] = time.time()
        try:
            return _get_json(NOMINATIM_URL, params)
        except requests.HTTPError as e:
            if attempt == 1 or getattr(e.response, "status_code", None) != 429:
                raise
            time.sleep(2)
    return []


def _venue_candidates(text: str) -> list[dict]:
    """Business and venue names (GeoSearch only knows addresses), via OpenStreetMap Nominatim."""
    rows = _nominatim({"q": text, "format": "jsonv2", "limit": 10, "addressdetails": 1,
                       "countrycodes": "us", "viewbox": NYC_VIEWBOX, "bounded": 1})
    found = []
    for r in rows:
        try:
            point = (float(r["lat"]), float(r["lon"]))
        except (KeyError, TypeError, ValueError):
            continue  # no usable coordinates
        if not in_nyc(point):
            continue
        a = r.get("address", {})
        # The bounding box also covers New Jersey and Nassau County; keep only New York City addresses.
        if a.get("state") not in (None, "New York") or a.get("city") not in (None, "New York"):
            continue
        # Label shown to the user: "name, street address, neighborhood"
        street = " ".join(p for p in (a.get("house_number"), a.get("road")) if p)
        area = a.get("neighbourhood") or a.get("suburb") or a.get("city_district") or a.get("borough")
        name = r.get("name") or (r.get("display_name") or "").split(",")[0]
        if street.lower() == name.lower():  # the place is a street: don't say it twice
            street = ""
        label = ", ".join(p for p in (name, street, area) if p)
        found.append({"label": label or text, "point": point, "name_text": name,
                      "match_text": f"{r.get('name', '')} {r.get('display_name', '')}"})
    return found


def _tokens(s: str) -> set[str]:
    """The lowercase words and numbers in s, ignoring punctuation."""
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


# Set when the last place-name lookup errored (as opposed to finding nothing).
# A one-item list, like _last_nominatim, so _safe_venues can update it.
_venue_lookup_failed = [False]


def _safe_venues(text: str) -> list[dict]:
    """_venue_candidates, except a service error returns [] and raises the flag above."""
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

    # Coordinates in the text (GPS, or a chosen clarification option): no lookup needed
    coords = list(_COORDS.finditer(text))
    if coords:
        last = coords[-1]
        point = (float(last.group(1)), float(last.group(2)))
        label = text.split("@")[0].strip() if "@" in text else ""
        if not in_nyc(point):
            raise ToolError(f"'{text}' is outside New York City, and this agent only covers the five boroughs.")
        return point, label or "your GPS location"

    def lookup() -> dict:
        """Ask the services and decide which matches to believe.

        Returns {"candidates": [...], "weak": bool}, plus "too_many" for chains.
        """
        query = text.strip()
        geo = _geosearch_candidates(query)
        # Addresses and intersections: the city's own geocoder is authoritative.
        if re.search(r"\d|&|/|\b(?:and|at)\b", query.lower()):
            # GeoSearch guesses ('6 ST AND AVE B' for '14th Street & 6th Avenue') are not answers
            good = [c for c in geo if not c.get("fallback")]
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
        venue_fits = [c for c in _safe_venues(query) if _fits_query(c, words)]
        venues = _spread(_prefer_exact(words, venue_fits), VENUE_GAP_M)
        if len(venues) >= TOO_MANY_PLACES:
            return {"too_many": True, "candidates": venues, "weak": False}
        # Venues that sit within 1 km of a GeoSearch match
        near = [v for v in venues if any(haversine_m(v["point"], g["point"]) < GEOSEARCH_GAP_M for g in geo_good)]
        # Both services agree on one spot ('Astor Place' in Manhattan; another one may exist in Queens)
        if geo_good and near:
            return {"candidates": [near[0]], "weak": False}
        if len(venues) >= 2:  # several real places share this name (branches, or a long street): ask
            return {"candidates": venues, "weak": False}
        if geo_good:
            return {"candidates": _spread(geo_good, GEOSEARCH_GAP_M), "weak": False}
        if venues:
            return {"candidates": venues, "weak": False}
        return {"candidates": _spread(geo, GEOSEARCH_GAP_M), "weak": True}  # only weak guesses: confirm with the user

    # Geocodes are cached for 24 hours. `fresh` keeps this call's result even when it is
    # not worth caching: _cached stores nothing when lookup_for_cache returns None.
    fresh = {}

    def lookup_for_cache() -> dict | None:
        _venue_lookup_failed[0] = False
        fresh["result"] = lookup()
        # Do not remember an answer reached while the place-name service was erroring: it may be wrong or empty.
        return fresh["result"] if fresh["result"]["candidates"] and not _venue_lookup_failed[0] else None

    result = _cached("geo:" + text.strip().lower(), 86400, lookup_for_cache) or fresh.get("result")
    found = result["candidates"] if result else []

    # Turn the outcome into one place, a question for the user, or an error the model can act on
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

# The dataset's accessibility wording -> our three levels
_ACCESS = {
    "fully accessible": "full",
    "partially accessible": "partial",
    "limited accessibility": "partial",
    "not accessible": "no",
}


def _yes_no_unknown(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def _normalize_site(row: dict) -> dict | None:
    """One dataset row -> the fields the tools use, or None if it has no coordinates.

    wheelchair, gender_neutral and changing are None when the city's data does not say.
    """
    try:
        lat, lon = float(row["latitude"]), float(row["longitude"])  # stored as strings
    except (KeyError, TypeError, ValueError):
        return None
    name = (row.get("facility_name") or "Unnamed restroom").strip()
    rtype = (row.get("restroom_type") or "").strip()
    # Some values carry literal quote characters: '"Yes, in women's restroom only"'
    changing = (row.get("changing_stations") or "").strip().strip('"').strip()
    changing_ok = True if changing.lower().startswith("yes") else (False if changing.lower() == "no" else None)
    return {
        # The dataset has no id column, so build a stable one from name + coordinates
        "id": "nyc-" + hashlib.sha1(f"{name}|{lat:.5f}|{lon:.5f}".encode()).hexdigest()[:8],
        "name": name,
        "type": (row.get("location_type") or "").strip(),
        "operator": (row.get("operator") or "").strip(),
        "point": (lat, lon),
        "row": row,  # the raw row, kept for its hours / status / season columns
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

    def fetch() -> list[dict]:
        sites = {}  # keyed by id, so a row listed twice is kept once
        for row in _get_json(RESTROOMS_URL, {"$limit": 5000}, timeout=20):
            site = _normalize_site(row)
            if site:
                sites[site["id"]] = site
        return list(sites.values())

    return _cached("nyc_sites", 6 * 3600, fetch)


def _need_check(site: dict, need: str) -> str:
    """'yes' | 'no' | 'unknown' for one need."""
    if need == "wheelchair":
        # "Partially accessible" may or may not work for this user, so it counts as unconfirmed
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
    """The JSON record for one restroom: what the model reads and the page draws as a card."""
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


# --- The tools ---
#
# Every tool takes the session's `state` first (run_tool passes it; the model never sees
# it), then the arguments from its schema in TOOLS. Each returns a dict that run_tool
# serializes, and raises ToolError for problems the model can fix.


# --- Tool 1 (shared): find_restrooms ---


def find_restrooms(state: dict, location: str, radius_m: int | None = None, needs: list | None = None,
                   when: str | None = None, limit: int | None = None) -> dict:
    """Official restrooms near a place that are not closed at `when`, nearest first."""
    # Validate and fill in the arguments
    origin, label = geocode(location)
    when_dt = _parse_when(when)
    needs = _resolve_needs(state, needs)
    limit = _clamp(limit, 1, MAX_RESULTS, MAX_RESULTS)
    explicit_radius = radius_m is not None  # a distance the user asked for is respected, never widened
    radius_m = _clamp(radius_m, 100, 3000, DEFAULT_RADIUS_M) if explicit_radius else DEFAULT_RADIUS_M

    def scan(radius: int) -> tuple[list, list, list]:
        """Sort the sites within `radius` into: meets the needs, might meet them, closed."""
        confirmed, maybe, closed = [], [], []
        for site in nyc_sites():
            # Straight-line distance decides what is in range; the grid estimate is what we report
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

    # Search, widening once if nothing usable is in range
    widened_from = None
    confirmed, maybe, closed = scan(radius_m)
    if not (confirmed or maybe) and not explicit_radius:
        widened_from, radius_m = radius_m, WIDENED_RADIUS_M
        confirmed, maybe, closed = scan(radius_m)

    # Sites that are confirmed to fit come first; unconfirmed ones only fill the remaining slots.
    # Within each group: open before unclear, then the shortest walk.
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
    # Nothing to show: tell the model what to try next instead of returning a bare empty list
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
                          needs: list | None = None, when: str | None = None, limit: int | None = None) -> dict:
    """Official restrooms close to a walk from `start` to `end`, in walking order."""
    # Validate and fill in the arguments (a = start point, b = end point)
    a, start_label = geocode(start)
    b, end_label = geocode(end)
    when_dt = _parse_when(when)
    needs = _resolve_needs(state, needs)
    max_detour_m, limit = _clamp(max_detour_m, 50, 1500, 300), _clamp(limit, 1, MAX_ROUTE_STOPS, MAX_ROUTE_STOPS)

    total_m = grid_distance_m(a, b)
    if total_m < 200:
        raise ToolError(
            "Start and end are within a couple of minutes' walk of each other. "
            "Use find_restrooms near the start instead."
        )
    total_min = walk_minutes(total_m)

    # Keep sites that add little extra walking and are not closed when the walker gets there
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

    # Pick the best `limit`: sites confirmed to meet the needs first, then the smallest detour
    found.sort(key=lambda f: (f[5] != "yes", f[0]))
    picked = found[:limit]
    picked.sort(key=lambda f: route_position(a, b, f[2]["point"]))  # present in walking order

    # Same record as find_restrooms, with the walk fields swapped for route ones
    stops = []
    for detour, to_stop, site, verdict, unconfirmed, _fit in picked:
        record = _describe(site, to_stop, verdict, when_dt, unconfirmed)
        record["minutes_into_walk"] = record.pop("walk_min")
        record.pop("walk_m")
        record["extra_walk_min"] = walk_minutes(detour) if detour > 20 else 0  # under 20 m is on the way
        record["percent_of_way"] = int(round(100 * route_position(a, b, site["point"])))
        stops.append(record)

    # Longest stretch between consecutive stops, counting the start and the end of the walk
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
    """Status at `when`, the weekly hours and the next opening time for one official restroom."""
    when_dt = _parse_when(when)
    # Ids from fallback_options name another data source; say why instead of "unknown id"
    if restroom_id.startswith("refuge-") or restroom_id.startswith("osm-"):
        raise ToolError(
            "Community and OpenStreetMap listings do not include hours. "
            "Only ids starting 'nyc-' can be checked."
        )
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
    # A reopening time is only given for a site known to be closed, not for an unclear one
    if verdict["state"] != "open":
        reopening = next_open(row.get("hours_of_operation"), when_dt)
        if reopening and verdict["state"] == "closed":
            out["next_open"] = reopening
    if verdict["state"] == "unclear":
        out["advice"] = "Tell the user the hours are uncertain and suggest a backup (find_restrooms or fallback_options)."
    return out


# --- Tool 4: fallback_options ---


def _refuge_near(origin: tuple[float, float], per_page: int = 30) -> list[dict]:
    """Volunteer-submitted listings near `origin` from Refuge Restrooms, cut down to what we use."""
    year = datetime.now().year
    out = []
    for r in _get_json(REFUGE_URL, {"lat": origin[0], "lng": origin[1], "per_page": per_page}):
        if r.get("approved") is False or r.get("latitude") is None or r.get("longitude") is None:
            continue
        updated = (r.get("updated_at") or "")[:4]  # the year of an ISO timestamp
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
    """One Overpass round trip for everything fallback_options needs from OSM.

    Returns (toilets, hours_elements): public toilets, and (separately) anything nearby
    with a name and an opening_hours tag. Refuge Restrooms never gives hours itself, so
    a nearby OSM listing of the same business (matched by distance and name back in
    fallback_options) is the quickest source for those; _nominatim_hours covers the
    ones it misses. Both are fetched in one query because Overpass is slow and flaky.
    """
    lat, lon = origin
    # Overpass QL: toilets (nodes and ways) plus anything tagged opening_hours, within the radius
    query = (
        f'[out:json][timeout:8];('
        f'node["amenity"="toilets"](around:{radius_m},{lat},{lon});way["amenity"="toilets"](around:{radius_m},{lat},{lon});'
        f'node["opening_hours"](around:{radius_m},{lat},{lon});way["opening_hours"](around:{radius_m},{lat},{lon});'
        f');out center 80;'
    )
    # Try each mirror in turn; the for/else raises only if every one of them failed
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
        # A node has its own lat/lon; a way (a building outline) has a computed "center"
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


def _same_business(name_a: str, name_b: str) -> bool:
    """Do the two names share at least half of the shorter one's words?"""
    # Proximity alone isn't enough to prove it's the same place (dense blocks, shared addresses like a
    # food hall or a medical building can put several unrelated businesses right next to each other).
    a, b = _tokens(name_a), _tokens(name_b)
    return bool(a and b and len(a & b) / min(len(a), len(b)) >= 0.5)


def _nominatim_hours(name: str, point: tuple[float, float]) -> str | None:
    """Opening hours for a community-listed business, by searching its name in a small box around the
    listing (so a chain matches the branch at this spot, not one across town). Volunteer-entered coordinates
    are often off by a few dozen meters, which is why this finds far more than the Overpass match."""
    def lookup() -> dict:
        lat, lon = point
        dlat, dlon = 0.003, 0.004  # about 330 m each way
        rows = _nominatim({"q": name, "format": "jsonv2", "extratags": 1, "limit": 5,
                           "viewbox": f"{lon - dlon},{lat + dlat},{lon + dlon},{lat - dlat}", "bounded": 1})
        matches = []
        for r in rows:
            hours = (r.get("extratags") or {}).get("opening_hours")
            try:
                p = (float(r["lat"]), float(r["lon"]))
            except (KeyError, TypeError, ValueError):
                continue
            if hours and haversine_m(point, p) <= HOURS_MATCH_RADIUS_M and _same_business(name, r.get("name") or ""):
                matches.append((haversine_m(point, p), hours))
        # Closest match wins. Wrapped in a dict (never empty), so "no hours" is cached too
        return {"hours": min(matches)[1] if matches else None}

    key = f"hours:{name.lower()}:{point[0]:.4f},{point[1]:.4f}"
    return _cached(key, 24 * 3600, lookup)["hours"]


def fallback_options(state: dict, location: str, when: str | None = None, radius_m: int = 1000, limit: int | None = None) -> dict:
    """Less reliable places to try, in three tiers, each with a confidence level.

    1. official sites whose hours are uncertain at `when`
    2. volunteer-listed businesses (Refuge Restrooms), with hours borrowed from OpenStreetMap
    3. OpenStreetMap toilets that list hours
    The two outside sources are best-effort: if one fails, the other tiers still answer.
    """
    origin, label = geocode(location)
    when_dt = _parse_when(when)
    radius_m, limit = _clamp(radius_m, 200, 2000, 1000), _clamp(limit, 1, MAX_RESULTS, MAX_RESULTS)
    options: list[dict] = []
    official_points, confirmed_open = [], 0

    # Tier 1: official sites with uncertain hours. Also note where every official site
    # is, so the other tiers can skip listings of the same restroom.
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

    def near_official(point: tuple[float, float]) -> bool:
        return any(haversine_m(point, p) < DUPLICATE_RADIUS_M for p in official_points)

    # One OpenStreetMap request serves tier 3 (toilets) and tier 2 (business hours)
    osm_toilets: list[dict] = []
    osm_hours: list[dict] = []
    osm_error: requests.RequestException | None = None
    try:
        osm_toilets, osm_hours = _osm_near(origin, radius_m)
    except requests.RequestException as e:
        osm_error = e  # best-effort: the osm tier is skipped and community listings rely on the Nominatim lookup

    def overpass_hours(point: tuple[float, float], name: str) -> str | None:
        """Hours of the nearest OSM business that is close by and has a matching name."""
        matches = [m for m in osm_hours if haversine_m(point, m["point"]) <= HOURS_MATCH_RADIUS_M and _same_business(name, m["name"])]
        return min(matches, key=lambda m: haversine_m(point, m["point"]))["hours_raw"] if matches else None

    # Tier 2: community listings.
    # Only options with some opening hours are shown. Refuge Restrooms never has hours, so each listing is
    # matched to an OpenStreetMap business: first the Overpass results we already have, then a Nominatim
    # search. Nominatim allows one request per second, so look up the best candidates first and stop early.
    candidates, seen_points = [], []
    try:
        for r in _refuge_near(origin):
            if haversine_m(origin, r["point"]) > radius_m or near_official(r["point"]):
                continue
            if any(_same_business(r["name"], c["name"]) and haversine_m(r["point"], c["point"]) < DUPLICATE_RADIUS_M for c in candidates):
                continue  # the same place listed twice
            # An old listing, or one with more downvotes than upvotes, is trusted less
            old = r["listing_age_years"] is not None and r["listing_age_years"] >= 5
            r["confidence"] = "low" if (old or r["downvotes"] > r["upvotes"]) else "medium"
            r["walk_min"] = walk_minutes(grid_distance_m(origin, r["point"]))
            candidates.append(r)
            seen_points.append(r["point"])
    except requests.RequestException:
        pass  # best-effort, like the OSM tier: the other tiers still answer

    # Find hours for the most trusted, nearest candidates first, on a budget of lookups
    conf_rank = {"medium": 0, "low": 1}
    candidates.sort(key=lambda r: (conf_rank[r["confidence"]], r["walk_min"]))
    lookups_left, with_hours, nominatim_failed = min(limit + 3, MAX_HOURS_LOOKUPS), 0, False
    for r in candidates:
        if with_hours >= limit:
            break
        hours_raw = overpass_hours(r["point"], r["name"])
        if not hours_raw and lookups_left and not nominatim_failed:
            lookups_left -= 1
            try:
                hours_raw = _nominatim_hours(r["name"], r["point"])
            except (requests.RequestException, ValueError):
                nominatim_failed = True  # the service is down: stop asking it
        if not hours_raw:
            continue  # no known hours: left out
        with_hours += 1
        options.append({
            "tier": "community_listed", "confidence": r["confidence"],
            "id": r["id"], "name": r["name"], "address": r["address"], "walk_min": r["walk_min"],
            "wheelchair": r["wheelchair"], "all_gender": r["all_gender"], "changing_station": r["changing_station"],
            "community_votes": f"{r['upvotes']} up / {r['downvotes']} down",
            "listing_age_years": r["listing_age_years"],
            "hours_raw": hours_raw,
            "caveat": "Listed by volunteers; hours are from OpenStreetMap for this business, not verified by the city, "
                      "and the restroom may be for customers only.",
            "map_link": _maps_link(r["point"]),
        })
    # Tier 3: OpenStreetMap toilets that list hours and are not already covered above
    if not osm_error:
        for o in osm_toilets:
            if near_official(o["point"]) or any(haversine_m(o["point"], p) < DUPLICATE_RADIUS_M for p in seen_points):
                continue
            if not o["hours_raw"]:
                continue
            options.append({
                "tier": "openstreetmap", "confidence": "low", "id": o["id"], "name": o["name"],
                "walk_min": walk_minutes(grid_distance_m(origin, o["point"])),
                "access": o["access"], "fee": o["fee"], "wheelchair": o["wheelchair"],
                "hours_raw": o["hours_raw"], "map_link": _maps_link(o["point"]),
            })

    # Most trustworthy tier first, then confidence, then the shortest walk
    tier_rank = {"official_but_hours_uncertain": 0, "community_listed": 1, "openstreetmap": 2}
    options.sort(key=lambda o: (tier_rank[o["tier"]], conf_rank[o["confidence"]], o["walk_min"]))
    out = {
        "searched_near": label,
        "at_time": when_dt.strftime("%A %Y-%m-%d %H:%M") + " EST",
        "official_sites_confirmed_open_nearby": confirmed_open,
        "options": options[:limit],
    }
    if confirmed_open:
        out["hint"] = f"{confirmed_open} official site(s) in range are confirmed open; find_restrooms lists them with details."
    if not options:
        out["message"] = "Nothing found in range. Try a larger radius_m (up to 2000), or a different time."
    return out


# --- Tool 5: remember_needs ---


def remember_needs(state: dict, needs: list) -> dict:
    """Save the user's standing needs in session state; _resolve_needs reads them back."""
    bad = [n for n in needs if n not in NEEDS]
    if bad:
        raise ToolError(f"Unknown need(s) {bad}. Allowed values: {NEEDS}.")
    state["needs"] = list(dict.fromkeys(needs))
    return {"saved_needs": state["needs"], "effect": "Later searches in this chat apply these automatically unless `needs` is passed."}


# --- What the model sees ---

# Argument descriptions shared by several tools, written once so they stay consistent.
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
    "description": (
        "Optional. ISO 8601 NYC local time, e.g. '2026-10-05T01:00'. Omit for right now. "
        "Use for 'tonight at 1am', 'tomorrow morning'."
    ),
}

# The JSON schemas passed to the model on every completion. Each description says what
# the tool is for and when to use a different one instead.
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
            "radius_m": {"type": "integer", "description": (
                "Search radius in meters, 100-3000. Omit it unless the user gave a distance (1 short block is "
                "about 80 m, 1 avenue block about 270 m). When omitted the tool searches 800 m and widens to "
                "2000 m by itself if nothing is open."
            )},
            "needs": _NEEDS,
            "when": _WHEN,
            "limit": {"type": "integer", "description": "Max results, 1-3. Default 3 (the most the page can show)."},
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
            # Same as _WHEN, but here the time is when the walk starts
            "when": {**_WHEN, "description": "Optional. When the walk STARTS: ISO 8601 NYC local time. Omit for right now."},
            "limit": {"type": "integer", "description": "Max stops, 1-5. Default 5 (the most the page can show)."},
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
            "votes), and OpenStreetMap toilets. Only places with known opening hours are returned (hours_raw, from "
            "OpenStreetMap, in its opening_hours format); compare them to the requested time yourself. Each option "
            "carries a confidence and caveat; be honest with the user that these are less reliable. Call "
            "find_restrooms first."
        ),
        "parameters": {"type": "object", "properties": {
            "location": {"type": "string", "description": _LOCATION},
            "when": _WHEN,
            "radius_m": {"type": "integer", "description": "Search radius in meters, 200-2000. Default 1000."},
            "limit": {"type": "integer", "description": "Max options, 1-3. Default 3 (the most the page can show)."},
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

# What the harness runs: tool name -> Python function.
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
    # Every outcome, good or bad, goes back to the model as JSON it can act on
    try:
        return json.dumps(TOOL_MAP[name](state, **args), ensure_ascii=False)
    except NeedsClarification as e:  # not an error: a question for the model to pass on
        return json.dumps(e.payload, ensure_ascii=False)
    except ToolError as e:  # the message already says how to fix the call
        return json.dumps({"error": str(e)})
    except TypeError as e:  # a missing or made-up argument
        return json.dumps({"error": f"Bad arguments for {name}: {e}. Check the tool's parameter list."})
    except requests.RequestException as e:  # a data source timed out or failed
        return json.dumps({"error": (
            f"A city data service did not respond ({type(e).__name__}). Wait a moment and retry once; "
            "if it keeps failing, tell the user the data is temporarily unavailable."
        )})
    except Exception as e:  # never leak a stack trace to the model or the user
        return json.dumps({"error": (
            f"Unexpected problem in {name} ({type(e).__name__}). "
            "Try rephrasing the request or a different tool."
        )})
