"""Turn the NYC restroom dataset's messy `hours_of_operation` text into answers.

Formats seen in the real data (check_apis.py output):
  "8am-4pm, Open later seasonally"      single daily range (643 rows)
  "9:00am-5:00pm", "6:00am-8:00pm Daily", "8am-8pm"
  "24 Hours"
  "Sunday: Closed \\nMonday: 10:00 am - 6:00 pm \\n..."   weekly table
  "Monday\\t10 am - 6 pm\\n...Sunday\\tCLOSED"             weekly table, tabs
  empty / missing (about 9% of rows)
Some weekly tables contain typos like "Tuesday: 10:00 pm - 7:00 pm"; we flag
those as unclear instead of trusting them.

Also reads OpenStreetMap's `opening_hours` format, used by community-listed places
(see the section at the end).

Pure standard library so it can be tested offline.
"""

import re
from datetime import datetime, timedelta

# --- The city's hours_of_operation text ---

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]  # index = datetime.weekday()
_T = r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?"  # one clock time: hour, optional :minutes, am/pm
_RANGE = re.compile(rf"{_T}\s*(?:-|–|—|to)\s*{_T}", re.I)  # "8am-4pm", "10:00 am - 6:00 pm"
_DAY_LINE = re.compile(rf"^\s*({'|'.join(DAYS)})\b[\s:]*(.*)$", re.I)  # "Monday: ..." or "Monday<TAB>..."
SEASONAL_WINTER_MONTHS = (11, 12, 1, 2, 3)  # when seasonal sites are often shut

# Times of day below are minutes after midnight (480 = 8 AM).


def _minutes(h: str, m: str | None, ap: str) -> int:
    """12-hour clock parts -> minutes after midnight (12 am is 0, 12 pm is 720)."""
    hour = int(h) % 12 + (12 if ap.lower() == "p" else 0)
    return hour * 60 + int(m or 0)


def fmt(minute: int) -> str:
    """480 -> '8 AM', 1230 -> '8:30 PM'."""
    h, m = divmod(minute % 1440, 60)
    suffix = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}{f':{m:02d}' if m else ''} {suffix}"


def _range(text: str) -> tuple[int, int] | None:
    """The first 'open - close' range in text as (open, close) minutes, or None."""
    found = _RANGE.search(text)
    if not found:
        return None
    h1, m1, a1, h2, m2, a2 = found.groups()
    return _minutes(h1, m1, a1), _minutes(h2, m2, a2)


def parse_hours(text: str | None) -> dict:
    """Return {"kind", "days", "later_seasonally"}.

    kind: always | daily | weekly | none | unparsed
    days: {0..6: (open, close) | "closed" | "bad"}  (0 = Monday)
    """
    raw = (text or "").strip()
    if not raw:
        return {"kind": "none", "days": {}, "later_seasonally": False}
    low = raw.lower()
    later = "later seasonally" in low
    if re.search(r"24\s*hours?|24\s*/\s*7", low):
        return {"kind": "always", "days": {}, "later_seasonally": later}

    # Weekly table: one line per day
    days: dict[int, object] = {}
    for line in raw.splitlines():
        match = _DAY_LINE.match(line)
        if not match:
            continue
        idx, rest = DAYS.index(match.group(1).lower()), match.group(2)
        if "closed" in rest.lower():
            days[idx] = "closed"
        else:
            days[idx] = _range(rest) or "bad"
    if days:
        return {"kind": "weekly", "days": days, "later_seasonally": later}

    # No day names: a single range that applies every day
    single = _range(raw)
    if single:
        return {"kind": "daily", "days": {i: single for i in range(7)}, "later_seasonally": later}
    return {"kind": "unparsed", "days": {}, "later_seasonally": later}


def _window_state(window: tuple[int, int], minute: int, later: bool) -> tuple[str, str]:
    """(state, reason) for one day's (open, close) window at `minute` of that day."""
    start, end = window
    # Closing time at or before opening time: an overnight range if it ends by 6 AM, else a typo
    if end <= start:
        if end <= 6 * 60:  # genuine overnight range, e.g. 8pm-2am
            if minute >= start or minute < end:
                return "open", f"open until {fmt(end)}"
            return "closed", f"opens at {fmt(start)}"
        return "unclear", f"listed hours ({fmt(start)}–{fmt(end)}) look like a typo in the city's data"
    if start <= minute < end:
        return "open", f"open until {fmt(end)}"
    if minute < start:
        return "closed", f"opens at {fmt(start)}"
    # Past closing time. "Open later seasonally" means we cannot be sure it has closed.
    if later:
        return "unclear", f"regular hours end at {fmt(end)}, but this site stays open later in some seasons"
    return "closed", f"closed since {fmt(end)}"


def status_at(text: str | None, when: datetime) -> dict:
    """Is a site with these hours open at `when`? -> {"state", "reason"}."""
    sched = parse_hours(text)
    kind = sched["kind"]
    if kind == "always":
        return {"state": "open", "reason": "open 24 hours"}
    if kind == "none":
        return {"state": "unclear", "reason": "no hours are listed for this site"}
    if kind == "unparsed":
        return {"state": "unclear", "reason": f"could not interpret the listed hours: {(text or '')[:60]!r}"}

    day = when.weekday()
    entry = sched["days"].get(day)
    name = DAYS[day].capitalize()
    if entry is None:
        return {"state": "unclear", "reason": f"hours for {name} are not listed"}
    if entry == "closed":
        return {"state": "closed", "reason": f"closed on {name}s"}
    if entry == "bad":
        return {"state": "unclear", "reason": f"could not interpret the {name} hours"}
    state, reason = _window_state(entry, when.hour * 60 + when.minute, sched["later_seasonally"])
    return {"state": state, "reason": reason}


def hours_for_day(text: str | None, weekday: int) -> str:
    """Short human string for one weekday, e.g. '10 AM – 6 PM' or 'Closed'."""
    sched = parse_hours(text)
    if sched["kind"] == "always":
        return "24 hours"
    if sched["kind"] in ("none", "unparsed"):
        return "not listed" if sched["kind"] == "none" else (text or "")[:40]
    entry = sched["days"].get(weekday)
    if entry is None:
        return "not listed"
    if entry == "closed":
        return "Closed"
    if entry == "bad":
        return "unreadable"
    note = " (later in some seasons)" if sched["later_seasonally"] else ""
    return f"{fmt(entry[0])} – {fmt(entry[1])}{note}"


def next_open(text: str | None, when: datetime) -> str | None:
    """Next time (within a week) a site with these hours opens, or None."""
    sched = parse_hours(text)
    if sched["kind"] == "always":
        return "now"
    if sched["kind"] not in ("daily", "weekly"):
        return None
    for offset in range(8):  # today, then each of the next seven days
        day = when + timedelta(days=offset)
        entry = sched["days"].get(day.weekday())
        # Skip closed or unreadable days, and typo ranges (same rule as _window_state)
        if not isinstance(entry, tuple) or entry[1] <= entry[0] and entry[1] > 6 * 60:
            continue
        opens = day.replace(hour=entry[0] // 60, minute=entry[0] % 60, second=0, microsecond=0)
        if opens > when:
            return f"{opens.strftime('%A')} at {fmt(entry[0])}"
    return None


def availability(row: dict, when: datetime) -> dict:
    """Combine status / open / hours columns into one verdict for a dataset row."""
    # The city's own status and season columns overrule the listed hours
    status = (row.get("status") or "").strip()
    if status and status != "Operational":
        return {"state": "closed", "reason": f"the city lists it as '{status}'", "seasonal": False}
    season = (row.get("open") or "").strip()
    if season == "Future":
        return {"state": "closed", "reason": "planned but not open yet", "seasonal": False}

    verdict = status_at(row.get("hours_of_operation"), when)
    verdict["seasonal"] = season == "Seasonal"
    # The data does not say which months a seasonal site runs, so in winter "open" becomes "unclear"
    if verdict["seasonal"] and when.month in SEASONAL_WINTER_MONTHS and verdict["state"] != "closed":
        verdict["state"] = "unclear"
        verdict["reason"] += "; seasonal site, and winter months are often closed — verify before walking over"
    return verdict


# --- OpenStreetMap opening_hours ---
#
# Community-listed places and OpenStreetMap toilets carry hours in OSM's own format:
#   "24/7"
#   "Mo-Th 12:00-24:00; Fr-Sa 12:00-01:00; Su 12:00-24:00"
#   "Mo-Fr 08:00-12:00,13:00-17:00; Sa,Su off; PH off"
# Only this common subset is read. Anything else (months, "sunset", "10:00+") is unclear
# rather than guessed. Ranges here are minutes after midnight; a close past 1440 runs overnight.

OSM_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]  # index = datetime.weekday()
_OSM_RULE = re.compile(r"(?:([A-Za-z]{2}(?:\s*[-,]\s*[A-Za-z]{2})*)\s+)?(.+)")  # optional days, then times
_OSM_RANGE = re.compile(r"(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})")  # "12:00-01:00"


def _osm_days(text: str) -> list[int] | None:
    """'Mo-Fr,Su' -> [0, 1, 2, 3, 4, 6]; a wrapping span like 'Fr-Mo' works too. None if not understood."""
    days = []
    for part in text.replace(" ", "").split(","):
        ends = part.split("-")
        if len(ends) > 2 or any(e not in OSM_DAYS for e in ends):
            return None
        first, last = OSM_DAYS.index(ends[0]), OSM_DAYS.index(ends[-1])
        days += [(first + i) % 7 for i in range((last - first) % 7 + 1)]
    return days


def parse_osm_hours(text: str | None) -> dict[int, list[tuple[int, int]]] | None:
    """OSM opening_hours -> {weekday: [(open, close), ...]}, or None if it is not in the subset we read.

    A day no rule mentions is closed, as in OSM. A later rule replaces an earlier one for its days.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw == "24/7":
        return {d: [(0, 1440)] for d in range(7)}
    week: dict[int, list[tuple[int, int]]] = {}
    for rule in filter(None, (r.strip() for r in raw.split(";"))):
        days_text, times_text = _OSM_RULE.fullmatch(rule).groups()
        if days_text and days_text.upper().startswith("PH"):
            continue  # public holidays: ignored
        days = _osm_days(days_text) if days_text else list(range(7))
        if days is None:
            return None
        ranges = []
        if times_text.strip().lower() not in ("off", "closed"):
            for part in times_text.split(","):
                found = _OSM_RANGE.fullmatch(part.strip())
                if not found:
                    return None
                h1, m1, h2, m2 = map(int, found.groups())
                start, end = h1 * 60 + m1, h2 * 60 + m2
                ranges.append((start, end if end > start else end + 1440))  # "12:00-01:00" runs past midnight
        for d in days:
            week[d] = ranges
    return week


def osm_status_at(text: str | None, when: datetime) -> dict:
    """Do these OSM hours say the place is open at `when`? -> {"state", "reason"}, like status_at."""
    if (text or "").strip() == "24/7":
        return {"state": "open", "reason": "listed hours say open 24/7"}
    week = parse_osm_hours(text)
    if week is None:
        return {"state": "unclear", "reason": f"could not interpret the listed hours: {(text or '')[:60]!r}"}
    minute, day = when.hour * 60 + when.minute, when.weekday()
    # Today's ranges, then yesterday's ranges that run past midnight into today
    for start, end in week.get(day, []):
        if start <= minute < end:
            return {"state": "open", "reason": f"listed hours say open until {fmt(end)}"}
    for start, end in week.get((day - 1) % 7, []):
        if minute < end - 1440:
            return {"state": "open", "reason": f"listed hours say open until {fmt(end)}"}
    return {"state": "closed", "reason": "listed hours say closed at this time"}
