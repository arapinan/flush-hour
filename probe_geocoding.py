"""See what the location lookup does with real queries (needs network).

    uv run python probe_geocoding.py

For each query it prints the top raw matches from both services, then what
tools.geocode() decided: a single place, a "which one?" question, or an error.
Run it after changing the location logic, and not repeatedly: Nominatim rate-limits.
"""

import tools

# The README's sample places, then the edge cases: a street in two boroughs,
# intersections written several ways, a single business, and a chain.
QUERIES = [
    "Columbia University",
    "Union Square",
    "Brooklyn Bridge",
    "Astor Place",
    "Amity Hall",
    "Metropolitan Museum of Art",
    "the Met",
    "Broadway",
    "Broadway & 116th St",
    "116th St and Amsterdam Ave",
    "Amsterdam Avenue & West 116th Street",
    "14th Street & 6th Avenue",
    "Katz's Delicatessen",
    "Starbucks",
]


def short(candidates, n=3):
    """The top n candidates as 'label (lat,lon)' strings."""
    return [f"{c['label'][:70]} ({c['point'][0]:.4f},{c['point'][1]:.4f})" for c in candidates[:n]] or ["(nothing)"]


for q in QUERIES:
    print(f"\n### {q}")
    try:
        print("  GeoSearch top:", *short(tools._geosearch_candidates(q)), sep="\n    ")
    except Exception as e:
        print("  GeoSearch failed:", type(e).__name__, e)
    try:
        print("  Nominatim top:", *short(tools._venue_candidates(q)), sep="\n    ")
    except Exception as e:
        print("  Nominatim failed:", type(e).__name__, e)
    try:
        point, label = tools.geocode(q)
        print(f"  DECISION: place -> {label} {point}")
    except tools.NeedsClarification as e:
        kind = "confirm weak match" if "exact match" in e.payload["question"] else "which one?"
        print(f"  DECISION: ask ({kind}) ->", [o["label"][:60] for o in e.payload["options"]])
    except tools.ToolError as e:
        print("  DECISION: error ->", e)
