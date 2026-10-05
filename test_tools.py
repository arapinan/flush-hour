"""Offline tests for tools.py. Network calls are replaced by fixtures taken from the real
check_apis.py output. Run: python3 test_tools.py
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

CALLS = {"osm_fails": True, "nominatim_down": False, "rate": 0}


def fake_get(url, params=None, timeout=10):
    if url == tools.RESTROOMS_URL:
        return ROWS
    if url == tools.REFUGE_URL:
        return REFUGE
    if url == tools.GEOSEARCH_URL:
        text = params["text"].lower()
        if text == "broadway":   # one street name, two boroughs, equal confidence
            return {"features": [
                {"geometry": {"coordinates": [-73.9626, 40.8075]}, "properties": {"label": "Broadway, Manhattan", "confidence": 0.8}},
                {"geometry": {"coordinates": [-73.9300, 40.7000]}, "properties": {"label": "Broadway, Brooklyn", "confidence": 0.8}},
                {"geometry": {"coordinates": [-73.9610, 40.8070]}, "properties": {"label": "Broadway, Manhattan (north)", "confidence": 0.8}},
                {"geometry": {"coordinates": [-73.9000, 40.7500]}, "properties": {"label": "Broadway, weak match", "confidence": 0.3}},
            ]}
        if "amity" in text:   # fuzzy fallback: a street that merely shares a word with the business name
            return {"features": [{"geometry": {"coordinates": [-73.9930, 40.6870]},
                                  "properties": {"label": "AMITY STREET, Brooklyn, NY, USA", "name": "AMITY STREET",
                                                 "confidence": 0.8, "match_type": "fallback"}}]}
        if "columbia university" in text:   # real shape: the campus, plus a far-away boathouse that shares the name
            def f(lon, lat, label):
                return {"geometry": {"coordinates": [lon, lat]}, "properties": {"label": label, "name": label.split(",")[0], "confidence": 1}}
            return {"features": [f(-73.9615, 40.8087, "COLUMBIA UNIVERSITY, New York, NY, USA"),
                                 f(-73.9155, 40.8731, "COLUMBIA UNIVERSITY BOATHOUSE, New York, NY, USA")]}
        if "14th street" in text:   # real behavior: GeoSearch answers an intersection with an unrelated fuzzy guess
            return {"features": [{"geometry": {"coordinates": [-73.9820, 40.7243]},
                                  "properties": {"label": "6 ST AND AVE B COMMUNITY GDN, New York", "confidence": 0.8, "match_type": "fallback"}}]}
        if "zzz quay" in text:   # only a weak guess exists anywhere
            return {"features": [{"geometry": {"coordinates": [-73.7949, 40.7282]},
                                  "properties": {"label": "ZZZ AVENUE, Queens, NY, USA", "confidence": 0.8, "match_type": "fallback"}}]}
        if any(w in text for w in ("nowhere", "solo bar", "unknown cafe", "twin tavern", "starbucks", "garden cafe", "rate cafe", "14th street", "brooklyn bridge")):  # businesses: GeoSearch has nothing
            return {"features": []}
        point = ASTOR if "astor" in text or "union" in text else COLUMBIA
        return {"features": [{"geometry": {"coordinates": [point[1], point[0]]}, "properties": {"label": params["text"]}}]}
    if url == tools.NOMINATIM_URL:
        text = params["q"].lower()
        if CALLS["nominatim_down"] and ("unknown cafe" in text or "amity" in text):
            raise requests.ConnectionError("down")
        if "amity hall" in text:   # real OpenStreetMap response shape: one branch
            return [{"lat": "40.7297099", "lon": "-73.9988571", "name": "Amity Hall", "category": "amenity", "type": "pub",
                     "display_name": "Amity Hall, 80, West 3rd Street, University Village, Manhattan, New York County, New York, 10012, United States",
                     "address": {"amenity": "Amity Hall", "house_number": "80", "road": "West 3rd Street",
                                 "neighbourhood": "University Village", "suburb": "Manhattan", "city": "New York"}}]
        if "twin tavern" in text:   # a business with two branches (fixture coordinates)
            return [
                {"lat": "40.73080", "lon": "-73.99880", "name": "Twin Tavern", "display_name": "Twin Tavern, 80, West 3rd Street",
                 "address": {"house_number": "80", "road": "West 3rd Street", "suburb": "Greenwich Village"}},
                {"lat": "40.80370", "lon": "-73.96690", "name": "Twin Tavern", "display_name": "Twin Tavern, 984, Amsterdam Avenue",
                 "address": {"house_number": "984", "road": "Amsterdam Avenue", "suburb": "Morningside Heights"}},
                {"lat": "40.73082", "lon": "-73.99881", "name": "Twin Tavern", "display_name": "duplicate of the first branch",
                 "address": {"house_number": "80", "road": "West 3rd Street"}},
                {"lat": "34.05", "lon": "-118.24", "name": "Twin Tavern", "display_name": "outside NYC"},
            ]
        if "astor place" in text:
            return [{"lat": "40.7298", "lon": "-73.9914", "name": "Astor Place", "display_name": "Astor Place, Lafayette Street",
                     "address": {"road": "Lafayette Street", "suburb": "East Village", "state": "New York", "city": "New York"}},
                    {"lat": "40.6000", "lon": "-73.8000", "name": "Astor Place", "display_name": "Astor Place, Queens",
                     "address": {"road": "Astor Place", "suburb": "Queens", "state": "New York", "city": "New York"}}]
        if "14th street" in text:
            return [{"lat": "40.7370", "lon": "-73.9971", "name": "14th Street & 6th Avenue", "display_name": "14th Street & 6th Avenue",
                     "address": {"road": "6th Avenue", "state": "New York", "city": "New York"}}]
        if "brooklyn bridge" in text:   # one bridge, returned as several pieces within ~1 km of each other
            return [{"lat": la, "lon": lo, "name": "Brooklyn Bridge", "display_name": "Brooklyn Bridge",
                     "address": {"road": "Brooklyn Bridge", "state": "New York", "city": "New York"}}
                    for la, lo in (("40.7062", "-73.9970"), ("40.7125", "-74.0010"), ("40.7036", "-73.9912"))]
        if "rate cafe" in text:   # Nominatim says 429 once, then answers
            CALLS["rate"] += 1
            if CALLS["rate"] == 1:
                err = requests.HTTPError("429")
                err.response = type("R", (), {"status_code": 429})()
                raise err
            return [{"lat": "40.7300", "lon": "-73.9900", "name": "Rate Cafe", "display_name": "Rate Cafe", "address": {"road": "Main St", "state": "New York"}}]
        if "columbia university" in text:
            return [{"lat": "40.8078", "lon": "-73.9625", "name": "Columbia University", "display_name": "Columbia University, Amsterdam Avenue",
                     "address": {"road": "Amsterdam Avenue", "suburb": "Morningside Heights", "state": "New York", "city": "New York"}}]
        if "starbucks" in text:   # a chain: many NYC stores, plus New Jersey and Nassau County ones that must be filtered out
            out = [{"lat": str(40.60 + i * 0.05), "lon": str(-74.00 + i * 0.04), "name": "Starbucks", "display_name": f"Starbucks {i}",
                    "address": {"road": f"Road {i}", "state": "New York", "city": "New York"}} for i in range(6)]
            out.append({"lat": "40.7357", "lon": "-74.1724", "name": "Starbucks", "display_name": "Starbucks, Newark",
                        "address": {"road": "Broad St", "state": "New Jersey", "city": "Newark"}})
            out.append({"lat": "40.7200", "lon": "-73.6400", "name": "Starbucks", "display_name": "Starbucks, Franklin Square",
                        "address": {"road": "Hempstead Tpke", "state": "New York", "city": "Franklin Square"}})
            return out
        if "garden cafe" in text:   # only 2 NYC branches + a New Jersey one: the NJ one must not become a third option
            return [{"lat": "40.7300", "lon": "-73.9900", "name": "Garden Cafe", "display_name": "Garden Cafe A", "address": {"road": "A St", "state": "New York", "city": "New York"}},
                    {"lat": "40.8000", "lon": "-73.9500", "name": "Garden Cafe", "display_name": "Garden Cafe B", "address": {"road": "B St", "state": "New York", "city": "New York"}},
                    {"lat": "40.7357", "lon": "-74.1724", "name": "Garden Cafe", "display_name": "Garden Cafe NJ", "address": {"road": "C St", "state": "New Jersey", "city": "Newark"}}]
        if "solo bar" in text:
            return [{"lat": "40.7300", "lon": "-73.9900", "name": "Solo Bar", "display_name": "Solo Bar", "address": {"road": "Main St"}}]
        return []
    raise AssertionError(url)


def fake_post(url, data, timeout=9):
    if CALLS["osm_fails"]:
        raise requests.ConnectionError("504")
    return {"elements": [{"id": 5, "lat": 40.8076, "lon": -73.9628, "tags": {"name": "Pier toilets", "access": "yes"}},
                         {"id": 6, "lat": 40.8077, "lon": -73.9629, "tags": {"access": "private"}}]}


tools._get_json, tools._post_json = fake_get, fake_post
tools.NOMINATIM_GAP_S = 0


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

    state = {}
    assert call("remember_needs", state, needs=["changing_station"])["saved_needs"] == ["changing_station"]
    out = call("find_restrooms", state, location="Broadway & 116th St", when="2026-10-05T11:00")
    assert out["needs_applied"] == ["changing_station"]
    assert "Playground 123" not in [r["name"] for r in out["results"]]        # changing_stations = No
    assert call("find_restrooms", state, location="Broadway & 116th St", needs=[])["needs_applied"] == []
    other_session = call("find_restrooms", {}, location="Broadway & 116th St")
    assert other_session["needs_applied"] == []                               # sessions stay separate


def test_disambiguation():
    when = "2026-10-05T11:00"
    out = call("find_restrooms", location="Twin Tavern", when=when)              # business with two branches
    assert out["needs_clarification"] is True and "error" not in out
    labels = [o["label"] for o in out["options"]]
    assert len(labels) == 2, labels                                               # duplicate + out-of-NYC dropped
    assert labels[0].startswith("Twin Tavern, 80 West 3rd Street") and "Morningside" in labels[1]
    assert all(" @ " in o["location"] for o in out["options"])
    assert "exactly" in out["advice"] and "matches 2 different places" in out["question"]

    chosen = out["options"][1]["location"]                                        # the user picks the uptown branch
    resolved = call("find_restrooms", location=chosen, when=when)
    assert "needs_clarification" not in resolved and resolved["searched_near"].startswith("Twin Tavern, 984")
    assert resolved["results"]                                                    # it is near the Columbia fixtures

    street = call("find_restrooms", location="Broadway")                          # street name matching two boroughs
    assert street["needs_clarification"] and len(street["options"]) == 2          # weak match + same-spot match dropped

    assert "needs_clarification" not in call("find_restrooms", location="Solo Bar")   # one branch: no question asked
    route = call("restrooms_along_route", start="Twin Tavern", end="Astor Place")     # works for routes too
    assert route["needs_clarification"] is True

    CALLS["nominatim_down"] = True                                                # lookup service down -> plain not-found advice
    down = call("find_restrooms", location="Unknown Cafe")["error"]
    assert "Could not find" in down and "busy" in down
    CALLS["nominatim_down"] = False


def test_business_beats_fuzzy_match():
    """Real behavior seen on 2026-10-05: GeoSearch answers a business name with a street that shares one word."""
    out = call("find_restrooms", location="Amity Hall", when="2026-10-05T01:00")
    assert "needs_clarification" not in out and "error" not in out, out
    assert out["searched_near"].startswith("Amity Hall, 80 West 3rd Street"), out["searched_near"]   # not 'AMITY STREET, Brooklyn'

    weak = call("find_restrooms", location="Zzz Quay")                            # only weak guesses exist: confirm, don't guess
    assert weak["needs_clarification"] is True and "exact match" in weak["question"]
    assert weak["options"][0]["label"].startswith("ZZZ AVENUE")

    exact = call("find_restrooms", location="Astor Place")                        # a real match is accepted silently
    assert "needs_clarification" not in exact


def test_exact_name_and_chains():
    """Probe findings from 2026-10-05: exact-name preference, chains, and out-of-NYC filtering."""
    out = call("find_restrooms", location="Columbia University", when="2026-10-05T11:00")   # campus beats the boathouse
    assert "needs_clarification" not in out and "error" not in out, out
    assert out["searched_near"].upper().startswith("COLUMBIA UNIVERSITY") and "BOATHOUSE" not in out["searched_near"].upper()

    chain = call("find_restrooms", location="Starbucks")                             # >=5 distinct NYC stores
    assert "error" in chain and "neighborhood" in chain["error"] and "needs_clarification" not in chain

    bridge = call("find_restrooms", location="Brooklyn Bridge")                     # one landmark in pieces: no question
    assert "needs_clarification" not in bridge and "error" not in bridge, bridge

    corner = call("find_restrooms", location="14th Street & 6th Avenue")           # fuzzy GeoSearch guess is ignored
    assert "needs_clarification" not in corner and corner["searched_near"].startswith("14th Street"), corner

    slow = call("find_restrooms", location="Rate Cafe")                              # one 429 from Nominatim is retried
    assert "error" not in slow and CALLS["rate"] == 2, slow

    two = call("find_restrooms", location="Garden Cafe")                             # NJ branch dropped: exactly 2 options
    assert two["needs_clarification"] and len(two["options"]) == 2, two


def test_outage_is_not_cached():
    """A 429 must not leave a wrong or empty answer cached for 24 hours."""
    tools._cache.clear()
    CALLS["nominatim_down"] = True
    weak = call("find_restrooms", location="Amity Hall")                              # Nominatim down: only the fuzzy guess exists
    assert weak.get("needs_clarification") and weak["options"][0]["label"].startswith("AMITY")
    CALLS["nominatim_down"] = False
    ok = call("find_restrooms", location="Amity Hall", when="2026-10-05T01:00")      # service back: real answer, no stale weak result
    assert "needs_clarification" not in ok and ok["searched_near"].startswith("Amity Hall, 80 West 3rd Street"), ok


def test_services_agree_on_one_spot():
    """Probe 2026-10-05: 'Astor Place' asked which one because OSM also lists a far-away Astor Place."""
    out = call("find_restrooms", location="Astor Place", when="2026-10-05T11:00")
    assert "needs_clarification" not in out and out["searched_near"].startswith("Astor Place, Lafayette"), out


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
    for fn in (test_find, test_disambiguation, test_business_beats_fuzzy_match, test_exact_name_and_chains, test_outage_is_not_cached, test_services_agree_on_one_spot, test_radius_widening, test_needs_and_session_memory, test_errors, test_check_open_status, test_route, test_fallback):
        fn()
        print("ok", fn.__name__)
