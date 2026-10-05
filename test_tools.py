"""Offline tests for tools.py. Network calls are replaced by fixtures shaped like the
real API responses. Run: python3 test_tools.py
"""
import json

import requests

import tools

COLUMBIA = (40.8075, -73.9626)
ASTOR = (40.729596, -73.99122)

WEEKLY = ("Sunday: Closed \nMonday: 10:00 am - 6:00 pm \nTuesday: 1:00 pm - 6:00 pm \nWednesday: 10:00 am - 6:00 pm \n"
          "Thursday: 12:00 pm - 8:00 pm \nFriday: 10:00 am - 6:00 pm \nSaturday: 10:00 am - 5:00 pm")
PARKS = "8am-4pm, Open later seasonally"


def row(name, lat, lon, **kw):
    base = {"facility_name": name, "location_type": "Park", "operator": "NYC Parks", "status": "Operational",
            "open": "Year Round", "hours_of_operation": PARKS, "accessibility": "Fully Accessible",
            "restroom_type": "Multi-Stall W/M Restrooms", "changing_stations": "Yes",
            "latitude": str(lat), "longitude": str(lon)}
    return {**base, **kw}


ROWS = [
    row("Anibal Aviles Playground", 40.801190, -73.962800),
    row("Playground 123", 40.809980, -73.955900, open="Seasonal", changing_stations="No"),
    row("Wildlife Sanct. & 119 St Tennis Courts", 40.811330, -73.965700,
        accessibility="Not Accessible", changing_stations='"Yes, in women\'s restroom only"'),
    row("Morningside Library", 40.8070, -73.9640, location_type="Library", operator="NYPL",
        hours_of_operation=WEEKLY, restroom_type="Single-Stall All Gender Restroom(s)", changing_stations=None),
    row("Under Construction Plaza", 40.8080, -73.9630, status="Closed for Construction"),
    row("Mystery Hours Park", 40.8090, -73.9610, hours_of_operation=None, accessibility=None),
    row("Midtown Plaza", 40.7685, -73.9769, hours_of_operation="24 Hours"),
    {"facility_name": "No coordinates", "status": "Operational"},
]
REFUGE = [
    {"id": 3653, "name": "Columbia's Morningside Campus", "street": "Broadway and 116th Street", "city": "Manhattan",
     "accessible": True, "unisex": True, "directions": "Several buildings on campus", "latitude": 40.80806,
     "longitude": -73.963989, "updated_at": "2014-02-02T20:54:21.316Z", "downvote": 1, "upvote": 2,
     "changing_table": False, "approved": True},
    {"id": 30731, "name": "Arts and Crafts Beer Parlor", "street": "1135 Amsterdam Ave", "city": "New York",
     "accessible": True, "unisex": False, "directions": "", "latitude": 40.8065685, "longitude": -73.9610072,
     "updated_at": "2017-02-26T20:09:54.014Z", "downvote": 0, "upvote": 2, "changing_table": False, "approved": True},
    {"id": 99999, "name": "Same spot as Anibal Aviles", "latitude": 40.80120, "longitude": -73.96281,
     "updated_at": "2024-01-01T00:00:00Z", "upvote": 1, "downvote": 0, "approved": True},
]

CALLS = {"osm_fails": True}


def fake_get(url, params=None, timeout=10):
    if url == tools.RESTROOMS_URL:
        return ROWS
    if url == tools.REFUGE_URL:
        return REFUGE
    if url == tools.GEOSEARCH_URL:
        text = params["text"].lower()
        if "nowhere" in text:
            return {"features": []}
        point = ASTOR if "astor" in text or "union" in text else COLUMBIA
        return {"features": [{"geometry": {"coordinates": [point[1], point[0]]}, "properties": {"label": params["text"]}}]}
    raise AssertionError(url)


def fake_post(url, data, timeout=9):
    if CALLS["osm_fails"]:
        raise requests.ConnectionError("504")
    return {"elements": [{"id": 5, "lat": 40.8076, "lon": -73.9628, "tags": {"name": "Pier toilets", "access": "yes"}},
                         {"id": 6, "lat": 40.8077, "lon": -73.9629, "tags": {"access": "private"}}]}


tools._get_json, tools._post_json = fake_get, fake_post


def call(name, state=None, **args):
    return json.loads(tools.run_tool(name, args, state if state is not None else {}))


def test_find():
    out = call("find_restrooms", location="Broadway & 116th St", when="2026-10-05T11:00")  # Monday 11 AM
    names = [r["name"] for r in out["results"]]
    assert "Morningside Library" in names and "Anibal Aviles Playground" in names, names
    assert "Under Construction Plaza" not in names and out["closed_nearby_omitted"] == 1
    assert out["nearest_closed"]["name"] == "Under Construction Plaza"
    assert all("map_link" in r and r["id"].startswith("nyc-") for r in out["results"])
    for group in ("open", "unclear"):  # confirmed-open first, then by walking time within each group
        times = [r["walk_min"] for r in out["results"] if r["status"] == group]
        assert times == sorted(times), (group, times)
    statuses = [r["status"] for r in out["results"]]
    assert statuses == sorted(statuses, key=lambda s: s != "open")
    mystery = next(r for r in out["results"] if r["name"] == "Mystery Hours Park")
    assert mystery["status"] == "unclear"


def test_radius_widening():
    far = "40.7545,-73.9769"  # about 1.6 km south of Midtown Plaza (24 hours)
    out = call("find_restrooms", location=far, when="2026-10-05T11:00")
    assert out["widened_from_m"] == 800 and out["radius_m_searched"] == 2000
    assert [r["name"] for r in out["results"]] == ["Midtown Plaza"]
    exact = call("find_restrooms", location=far, radius_m=800, when="2026-10-05T11:00")  # explicit distance is respected
    assert exact["results"] == [] and "widened_from_m" not in exact and exact["radius_m_searched"] == 800
    assert "fallback_options" in exact["message"]
    night = call("find_restrooms", location="Broadway & 116th St", needs=["gender_neutral"], when="2026-10-05T01:00")  # only the library qualifies, and it is shut at 1am
    assert night["results"] == [] and night["closed_nearby_omitted"] > 0
    assert "unlikely to help" in night["message"] and "fallback_options" in night["message"]


def test_needs_and_session_memory():
    out = call("find_restrooms", location="40.8075,-73.9626", needs=["wheelchair"], when="2026-10-05T11:00")
    assert out["searched_near"] == "your GPS location"
    names = [r["name"] for r in out["results"]]
    assert "Wildlife Sanct. & 119 St Tennis Courts" not in names             # Not Accessible
    mystery_idx = names.index("Mystery Hours Park")                          # accessibility unknown -> after confirmed
    assert mystery_idx > names.index("Anibal Aviles Playground")
    assert "wheelchair" in out["results"][mystery_idx]["unconfirmed_needs"]


def test_errors():
    assert "Could not find" in call("find_restrooms", location="nowhere land")["error"]
    assert "outside New York City" in call("find_restrooms", location="34.05,-118.24")["error"]
    assert "Allowed values" in call("find_restrooms", location="Columbia", needs=["jacuzzi"])["error"]
    assert "ISO 8601" in call("find_restrooms", location="Columbia", when="next tuesday-ish")["error"]
    assert "Unknown tool" in call("teleport")["error"]
    assert "Bad arguments" in call("find_restrooms", bogus=1)["error"]
    assert "Unknown restroom_id" in call("check_open_status", restroom_id="nyc-deadbeef")["error"]
    assert "do not include hours" in call("check_open_status", restroom_id="refuge-3653")["error"]


def test_check_open_status():
    lib = next(r for r in call("find_restrooms", location="Columbia", when="2026-10-05T11:00")["results"]
               if r["name"] == "Morningside Library")
    sunday_night = call("check_open_status", restroom_id=lib["id"], when="2026-10-04T21:15")
    assert sunday_night["status"] == "closed" and sunday_night["next_open"] == "Monday at 10 AM"
    assert sunday_night["weekly_hours"]["Sun"] == "Closed" and sunday_night["weekly_hours"]["Mon"] == "10 AM – 6 PM"
    assert call("check_open_status", restroom_id=lib["id"], when="2026-10-05T12:00")["status"] == "open"


def test_route():
    out = call("restrooms_along_route", start="Columbia", end="Astor Place", max_detour_m=400, when="2026-10-05T11:00")
    assert "error" not in out, out
    assert out["walk_min_total"] > 60 and out["route"].startswith("Columbia")
    stops = out["stops_in_walking_order"]
    assert [s["name"] for s in stops].count("Midtown Plaza") == 1               # 24-hour site on the line
    assert [s["percent_of_way"] for s in stops] == sorted(s["percent_of_way"] for s in stops)
    assert 0 < out["longest_stretch_without_a_stop_min"] <= out["walk_min_total"]
    # arrival-time awareness: leaving Columbia at 3:30 PM reaches the 8am-4pm park sites only if close
    late = call("restrooms_along_route", start="Columbia", end="Astor Place", max_detour_m=400, when="2026-10-05T15:30")
    assert late["closed_on_arrival_omitted"] >= 0 and "error" not in late
    assert "within a couple of minutes" in call("restrooms_along_route", start="Columbia", end="Columbia")["error"]


def test_fallback():
    out = call("fallback_options", location="Columbia", when="2026-10-05T01:00")   # 1 AM Monday
    tiers = [o["tier"] for o in out["options"]]
    assert "community_listed" in tiers
    assert "Same spot as Anibal Aviles" not in [o["name"] for o in out["options"]]  # duplicate of an official site
    assert any("OpenStreetMap" in n for n in out["notes"])                           # soft failure noted
    old = next(o for o in out["options"] if o["name"].startswith("Columbia's"))
    assert old["confidence"] == "low" and old["listing_age_years"] >= 10
    CALLS["osm_fails"] = False
    out = call("fallback_options", location="Columbia", when="2026-10-05T01:00")
    osm = [o for o in out["options"] if o["tier"] == "openstreetmap"]
    assert [o["name"] for o in osm] == ["Pier toilets"]                              # private one filtered out
    CALLS["osm_fails"] = True


if __name__ == "__main__":
    for fn in (test_find, test_radius_widening, test_needs_and_session_memory, test_errors, test_check_open_status, test_route, test_fallback):
        fn()
        print("ok", fn.__name__)
