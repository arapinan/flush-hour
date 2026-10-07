"""Smoke-test every data source the restroom agent uses.

Run from your own terminal (stdlib only, no install needed):
    python3 check_apis.py

It prints the start of each live response. The tools in tools.py and the fixtures
in test_tools.py were written against these real response shapes, not guesses.
"""

import json
import time
import urllib.parse
import urllib.request

# Columbia, 116th St & Broadway
LAT, LON = 40.8075, -73.9626
UA = {"User-Agent": "ieor4570-restroom-agent/0.1 (class project)"}


def get(url, data=None, timeout=30):
    """Fetch JSON (POST when `data` is given). Returns (parsed body, seconds taken)."""
    req = urllib.request.Request(url, data=data, headers=UA)
    start = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode()
    return json.loads(body), time.time() - start


def show(title, fn):
    """Run one check and print the first part of its response, or why it failed."""
    print(f"\n=== {title} ===")
    try:
        data, secs = fn()
        print(f"OK in {secs:.2f}s")
        print(json.dumps(data, indent=2)[:1800])
    except Exception as e:  # report, never crash: we want every result
        print(f"FAILED: {type(e).__name__}: {e}")


# --- One function per data source (or per question about one) ---


def nyc_restrooms():
    """A few official restrooms near Columbia, filtered by distance on the server (SoQL)."""
    q = urllib.parse.urlencode({
        "$where": f"within_circle(location_1, {LAT}, {LON}, 800)",
        "$limit": 3,
    })
    return get(f"https://data.cityofnewyork.us/resource/i7jb-7jku.json?{q}")


def nyc_distinct_values():
    """What values do the messy text columns actually take, and how often?"""
    out = {}
    for col in ("open", "status", "accessibility", "restroom_type", "changing_stations"):
        q = urllib.parse.urlencode({"$select": f"{col}, count(*)", "$group": col})
        out[col], _ = get(f"https://data.cityofnewyork.us/resource/i7jb-7jku.json?{q}")
    return out, 0.0


def hours_samples():
    """The 40 most common hours_of_operation strings: what hours.py has to parse."""
    q = urllib.parse.urlencode({
        "$select": "hours_of_operation, count(*)",
        "$group": "hours_of_operation",
        "$order": "count DESC",
        "$limit": 40,
    })
    return get(f"https://data.cityofnewyork.us/resource/i7jb-7jku.json?{q}")


def geosearch():
    """The city's geocoder on a landmark: address text -> coordinates."""
    q = urllib.parse.urlencode({"text": "Astor Place, Manhattan", "size": 1})
    return get(f"https://geosearch.planninglabs.nyc/v2/search?{q}")


def refuge():
    """Wheelchair-accessible community listings near Columbia."""
    q = urllib.parse.urlencode({"lat": LAT, "lng": LON, "per_page": 3, "ada": "true"})
    return get(f"https://www.refugerestrooms.org/api/v1/restrooms/by_location?{q}")


def overpass():
    """OpenStreetMap toilets near Columbia (POST, since Overpass takes a query body)."""
    ql = f'[out:json][timeout:25];node["amenity"="toilets"](around:600,{LAT},{LON});out 3;'
    return get("https://overpass-api.de/api/interpreter", data=urllib.parse.urlencode({"data": ql}).encode())


def nominatim_amity():
    """A business name in Nominatim: does it return several branches?"""
    q = urllib.parse.urlencode({"q": "Amity Hall", "format": "jsonv2", "limit": 6, "addressdetails": 1,
                                "countrycodes": "us", "viewbox": "-74.27,40.92,-73.68,40.49", "bounded": 1})
    return get(f"https://nominatim.openstreetmap.org/search?{q}")


def geosearch_ambiguous():
    """An ambiguous street name in GeoSearch: confidence and borough per result."""
    q = urllib.parse.urlencode({"text": "Broadway", "size": 5})
    return get(f"https://geosearch.planninglabs.nyc/v2/search?{q}")


def libraries():
    """Search the NYC Open Data catalog for library datasets."""
    # NYC Open Data library locations: find the dataset by name via the catalog.
    q = urllib.parse.urlencode({"q": "libraries", "domains": "data.cityofnewyork.us", "limit": 5})
    return get(f"https://api.us.socrata.com/api/catalog/v1?{q}")


if __name__ == "__main__":
    show("NYC Public Restrooms near Columbia (800 m)", nyc_restrooms)
    show("NYC dataset: distinct values of the text columns", nyc_distinct_values)
    show("NYC dataset: most common hours_of_operation strings", hours_samples)
    show("NYC GeoSearch (address -> lat/lon)", geosearch)
    show("Refuge Restrooms (ADA, near Columbia)", refuge)
    show("OpenStreetMap Overpass (amenity=toilets, 600 m)", overpass)
    show("Socrata catalog: library datasets", libraries)
    show("Nominatim: business name 'Amity Hall' (does it return several branches?)", nominatim_amity)
    show("GeoSearch: ambiguous 'Broadway' (confidence + borough per result)", geosearch_ambiguous)
