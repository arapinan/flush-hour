"""Offline tests for hours.py and geo.py using strings from the real dataset.
Run: uv run python test_core.py
"""
from datetime import datetime

from geo import detour_m, grid_distance_m, haversine_m, in_nyc, route_position, walk_minutes
from hours import availability, hours_for_day, next_open, osm_status_at, parse_hours, parse_osm_hours, status_at

SUN = datetime(2026, 10, 4, 21, 15)   # Sunday 9:15 PM
MON_AM = datetime(2026, 10, 5, 9, 0)  # Monday 9 AM
MON_1AM = datetime(2026, 10, 5, 1, 0)
MON_11 = datetime(2026, 10, 5, 11, 0)
MON_5PM = datetime(2026, 10, 5, 17, 0)
JAN_NOON = datetime(2027, 1, 12, 12, 0)

PARKS = "8am-4pm, Open later seasonally"
WEEKLY = ("Sunday: Closed \nMonday: 10:00 am - 6:00 pm \nTuesday: 1:00 pm - 6:00 pm \nWednesday: 10:00 am - 6:00 pm \n"
          "Thursday: 12:00 pm - 8:00 pm \nFriday: 10:00 am - 6:00 pm \nSaturday: 10:00 am - 5:00 pm")
TABS = ("Monday\t10 am - 6 pm\nTuesday\t1 pm - 8 pm\nWednesday\t10 am - 6 pm\nThursday\t10 am - 8 pm\n"
        "Friday\t10 am - 6 pm\nSaturday\t10 am - 5 pm\nSunday\tCLOSED")
TYPO = ("Sunday: Closed \nMonday: 10:00 am - 7:00 pm \nTuesday: 10:00 pm - 7:00 pm \nWednesday: 10:00 am - 7:00 pm")


def state(text, when):
    """Just the open | closed | unclear part of status_at."""
    return status_at(text, when)["state"]


def test_hours():
    """Every hours format in the real data, including seasonal, overnight and typo ranges."""
    assert parse_hours(PARKS)["kind"] == "daily" and parse_hours(PARKS)["later_seasonally"]
    assert parse_hours(WEEKLY)["kind"] == "weekly" and parse_hours(TABS)["kind"] == "weekly"
    assert parse_hours(None)["kind"] == "none" and parse_hours("")["kind"] == "none"
    assert parse_hours("24 Hours")["kind"] == "always"
    assert parse_hours("call ahead")["kind"] == "unparsed"

    assert state(PARKS, MON_AM) == "open"
    assert state(PARKS, MON_5PM) == "unclear"          # past 4 PM but "later seasonally"
    assert state(PARKS, MON_1AM) == "closed"
    assert state("9:00am-5:00pm", MON_5PM) == "closed"  # no seasonal caveat -> plain closed
    assert state("6:00am-8:00pm Daily", MON_5PM) == "open"
    assert state("24 Hours", MON_1AM) == "open"
    assert state(None, MON_AM) == "unclear"

    assert state(WEEKLY, SUN) == "closed"               # Sunday: Closed
    assert state(WEEKLY, MON_AM) == "closed"   # opens at 10 AM
    assert state(WEEKLY, MON_11) == "open"
    assert state(TABS, SUN) == "closed"                 # Sunday<TAB>CLOSED
    assert state(TABS, MON_5PM) == "open"
    assert state(TYPO, datetime(2026, 10, 6, 12, 0)) == "unclear"   # Tuesday 10pm-7pm typo
    assert "typo" in status_at(TYPO, datetime(2026, 10, 6, 12, 0))["reason"]
    assert state("8pm-2am", datetime(2026, 10, 5, 23, 0)) == "open"  # genuine overnight


def test_helpers():
    """Per-day hours strings and the next opening time."""
    assert hours_for_day(WEEKLY, 0) == "10 AM – 6 PM" and hours_for_day(WEEKLY, 6) == "Closed"
    assert next_open(WEEKLY, SUN) == "Monday at 10 AM"
    assert next_open(PARKS, MON_5PM) == "Tuesday at 8 AM"
    assert next_open("24 Hours", SUN) == "now"
    assert next_open(None, SUN) is None


def test_availability():
    """The city's status and season columns overrule the listed hours."""
    base = {"status": "Operational", "open": "Year Round", "hours_of_operation": PARKS}
    assert availability(base, MON_AM)["state"] == "open"
    assert availability({**base, "status": "Closed for Construction"}, MON_AM)["state"] == "closed"
    assert availability({**base, "open": "Future"}, MON_AM)["state"] == "closed"
    seasonal = {**base, "open": "Seasonal"}
    assert availability(seasonal, MON_AM)["state"] == "open" and availability(seasonal, MON_AM)["seasonal"]
    assert availability(seasonal, JAN_NOON)["state"] == "unclear"
    assert availability({**base, "open": None}, MON_AM)["state"] == "open"


def test_osm_hours():
    """OpenStreetMap opening_hours: overnight ranges, days off, holiday closures, and formats we do not read."""
    tavern = "Mo-Th 12:00-24:00; Fr-Sa 12:00-01:00; Su 12:00-24:00"
    assert osm_status_at(tavern, datetime(2026, 10, 6, 22, 0))["state"] == "open"      # Tuesday 10 PM
    assert osm_status_at(tavern, datetime(2026, 10, 6, 1, 0))["state"] == "closed"     # Monday's hours end at midnight
    assert osm_status_at(tavern, datetime(2026, 10, 10, 0, 30))["state"] == "open"     # Friday night runs to 1 AM
    weekdays = "Mo-Fr 08:00-12:00,13:00-17:00; Sa,Su off; PH off"
    assert osm_status_at(weekdays, MON_11)["state"] == "open"
    assert osm_status_at(weekdays, datetime(2026, 10, 5, 12, 30))["state"] == "closed"  # lunch break
    assert osm_status_at(weekdays, datetime(2026, 10, 10, 10, 0))["state"] == "closed"  # Saturday off
    assert parse_osm_hours("Fr-Mo 09:00-17:00")["week"][6] == [(540, 1020)]                     # span wraps past Sunday
    assert osm_status_at("24/7", MON_1AM)["state"] == "open"
    assert osm_status_at("Mo-Fr 10:00+", MON_11)["state"] == "unclear"                  # open-ended: not guessed
    assert osm_status_at("Jan-Mar Mo-Fr 09:00-17:00", MON_11)["state"] == "unclear"     # months: not read
    assert osm_status_at(None, MON_11)["state"] == "unclear"

    met = "10:00-17:00; Fr-Sa 10:00-21:00; We off; May Mo[1] off; Nov Th[4] off; Dec 25 off; Jan 01 off"  # real listing
    assert osm_status_at(met, MON_1AM)["state"] == "closed"                             # holiday rules can't open it
    assert osm_status_at(met, MON_11)["state"] == "open"
    assert osm_status_at(met, datetime(2026, 11, 26, 12, 0))["state"] == "closed"       # Thanksgiving: 4th Thursday
    assert osm_status_at(met, datetime(2026, 12, 25, 12, 0))["state"] == "closed"
    assert osm_status_at(met, datetime(2026, 5, 4, 12, 0))["state"] == "closed"         # first Monday in May
    easter = "10:00-17:00; Easter off"                                                  # a closure we can't date
    assert osm_status_at(easter, MON_11)["state"] == "unclear" and osm_status_at(easter, MON_1AM)["state"] == "closed"


def test_geo():
    """Grid walking distance, walking minutes, and position along and detour from a route."""
    columbia, astor = (40.8075, -73.9626), (40.7296, -73.9912)
    assert in_nyc(columbia) and not in_nyc((34.05, -118.24))
    straight, grid = haversine_m(columbia, astor), grid_distance_m(columbia, astor)
    assert 8_500 < straight < 9_200, straight
    assert straight <= grid <= straight * 1.45, (straight, grid)   # grid distance is never shorter
    assert walk_minutes(0) == 1 and walk_minutes(160) == 2 and walk_minutes(161) == 3
    mid = (40.7685, -73.9769)
    assert 0.35 < route_position(columbia, astor, mid) < 0.65
    assert detour_m(columbia, astor, mid) < 400
    assert detour_m(columbia, astor, (40.85, -73.88)) > 3000          # far off-route


if __name__ == "__main__":
    for fn in (test_hours, test_helpers, test_availability, test_osm_hours, test_geo):
        fn()
        print("ok", fn.__name__)
