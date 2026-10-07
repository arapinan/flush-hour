"""Turn the NYC restroom dataset's messy `hours_of_operation` text into answers.

Formats seen in the real data (check_apis.py output):
  "8am-4pm, Open later seasonally"      single daily range (643 rows)
  "9:00am-5:00pm", "6:00am-8:00pm Daily", "8am-8pm"
  "24 Hours"
  "Sunday: Closed \\nMonday: 10:00 am - 6:00 pm \\n..."   weekly table
  "Monday\\t10 am - 6 pm\\n...Sunday\\tCLOSED"             weekly table, tabs
  empty / missing (about 9% of rows)
  "7:30am - dusk", "6am - dusk"                             a light-dependent end (3 rows)
Some weekly tables contain typos like "Tuesday: 10:00 pm - 7:00 pm"; we flag
those as unclear instead of trusting them. Text with several different ranges whose days
we do not read ("Weekdays - 12pm-Dusk Weekends 11am-Dusk") is open or closed only when every
range agrees, and unclear otherwise.

Also reads OpenStreetMap's `opening_hours` format, used by community-listed places
(see the section at the end).

Pure standard library so it can be tested offline.
"""

import re
from datetime import datetime, timedelta

# --- The city's hours_of_operation text ---

DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]  # index = datetime.weekday()
_T = r"(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?"  # one clock time: hour, optional :minutes, am/pm
_SUN = r"(dawn|sunrise|dusk|sunset)"  # a time that moves with the seasons
_POINT = rf"(?:{_T}|{_SUN})"  # a clock time or a sun word
_RANGE = re.compile(rf"{_POINT}\s*(?:-|–|—|to)\s*{_POINT}", re.I)  # "8am-4pm", "10:00 am - 6 pm", "7:30am - dusk"
_DAY_LINE = re.compile(rf"^\s*({'|'.join(DAYS)})\b[\s:]*(.*)$", re.I)  # "Monday: ..." or "Monday<TAB>..."
SEASONAL_WINTER_MONTHS = (11, 12, 1, 2, 3)  # when seasonal sites are often shut
# The range a sun word covers in NYC over the year (minutes after midnight): sunset is
# 4:30 PM at the earliest and dusk ends by 9:15 PM; dawn starts by 4:45 AM, sunrise ends by 7:30 AM.
DUSK_RANGE = (16 * 60 + 30, 21 * 60 + 15)
DAWN_RANGE = (4 * 60 + 45, 7 * 60 + 30)

# Times of day below are minutes after midnight (480 = 8 AM). A time can also be a sun
# word ("dusk"), kept as text because its clock time changes with the season.


def _minutes(h: str, m: str | None, ap: str) -> int:
    """12-hour clock parts -> minutes after midnight (12 am is 0, 12 pm is 720)."""
    hour = int(h) % 12 + (12 if ap.lower() == "p" else 0)
    return hour * 60 + int(m or 0)


def fmt(minute: int) -> str:
    """480 -> '8 AM', 1230 -> '8:30 PM'."""
    h, m = divmod(minute % 1440, 60)
    suffix = "AM" if h < 12 else "PM"
    return f"{h % 12 or 12}{f':{m:02d}' if m else ''} {suffix}"


def _label(point: int | str) -> str:
    """A range end for people: 480 -> '8 AM', 'dusk' -> 'dusk'."""
    return point if isinstance(point, str) else fmt(point)


def _range(text: str) -> tuple | None:
    """The first 'open - close' range in text as (open, close), or None. Each end is minutes or a sun word."""
    found = _RANGE.search(text)
    if not found:
        return None
    h1, m1, a1, sun1, h2, m2, a2, sun2 = found.groups()
    return (sun1.lower() if sun1 else _minutes(h1, m1, a1)), (sun2.lower() if sun2 else _minutes(h2, m2, a2))


def parse_hours(text: str | None) -> dict:
    """Return {"kind", "days", "later_seasonally"}, plus "ranges" for kind "several".

    kind: always | daily | weekly | several | none | unparsed
    days: {0..6: (open, close) | "closed" | "bad"}  (0 = Monday); each end is minutes or a sun word
    ranges: for "several" only, every (open, close) in the text
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

    # Several different ranges without day names we read ("M-F 10:30am-5:15pm S-S 12:00pm-8:00pm",
    # "(indoor season) ... (outdoor season)"): we cannot tell which applies, so status_at checks them all
    ranges = list(dict.fromkeys(_range(m.group(0)) for m in _RANGE.finditer(raw)))
    if len(ranges) > 1:
        return {"kind": "several", "days": {}, "ranges": ranges, "later_seasonally": later}

    # No day names: a single range that applies every day
    single = _range(raw)
    if single:
        return {"kind": "daily", "days": {i: single for i in range(7)}, "later_seasonally": later}
    return {"kind": "unparsed", "days": {}, "later_seasonally": later}


def _sun_window_state(start: int | str, end: int | str, minute: int) -> tuple[str, str]:
    """(state, reason) for a window with a sun word at one end. The word's clock time is only
    known to lie in DAWN_RANGE / DUSK_RANGE, so only times outside that range are certain."""
    opens = DAWN_RANGE if isinstance(start, str) else (start, start)  # earliest and latest it could open
    closes = DUSK_RANGE if isinstance(end, str) else (end, end)
    if minute < opens[0]:
        return "closed", f"opens at {_label(start)}"
    if minute >= closes[1]:
        return "closed", f"closed since {_label(end)}"
    if opens[1] <= minute < closes[0]:
        return "open", f"open until {_label(end)}"
    word = start if minute < opens[1] else end
    return "unclear", f"listed hours are {_label(start)} – {_label(end)}, and {word} changes with the season"


def _window_state(window: tuple, minute: int, later: bool) -> tuple[str, str]:
    """(state, reason) for one day's (open, close) window at `minute` of that day."""
    start, end = window
    if isinstance(start, str) or isinstance(end, str):
        return _sun_window_state(start, end, minute)
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
    minute = when.hour * 60 + when.minute
    if kind == "several":
        # Certain only when every listed range gives the same answer
        states = {_window_state(r, minute, sched["later_seasonally"])[0] for r in sched["ranges"]}
        if len(states) == 1 and states != {"unclear"}:
            state = states.pop()
            return {"state": state, "reason": f"{state} under each of its listed hours: {(text or '')[:60]!r}"}
        reason = f"its listed hours give different times for different days or seasons: {(text or '')[:60]!r}"
        return {"state": "unclear", "reason": reason}

    day = when.weekday()
    entry = sched["days"].get(day)
    name = DAYS[day].capitalize()
    if entry is None:
        return {"state": "unclear", "reason": f"hours for {name} are not listed"}
    if entry == "closed":
        return {"state": "closed", "reason": f"closed on {name}s"}
    if entry == "bad":
        return {"state": "unclear", "reason": f"could not interpret the {name} hours"}
    state, reason = _window_state(entry, minute, sched["later_seasonally"])
    return {"state": state, "reason": reason}


def hours_for_day(text: str | None, weekday: int) -> str:
    """Short human string for one weekday, e.g. '10 AM – 6 PM' or 'Closed'."""
    sched = parse_hours(text)
    if sched["kind"] == "always":
        return "24 hours"
    if sched["kind"] in ("none", "unparsed", "several"):
        return "not listed" if sched["kind"] == "none" else (text or "")[:40]
    entry = sched["days"].get(weekday)
    if entry is None:
        return "not listed"
    if entry == "closed":
        return "Closed"
    if entry == "bad":
        return "unreadable"
    note = " (later in some seasons)" if sched["later_seasonally"] else ""
    return f"{_label(entry[0])} – {_label(entry[1])}{note}"


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
        if not isinstance(entry, tuple):
            continue  # closed or unreadable day
        start, end = entry
        if isinstance(start, str):  # opens at dawn: its clock time is unknown, so name the day only
            if offset or when.hour * 60 + when.minute < DAWN_RANGE[0]:
                return f"{day.strftime('%A')} at {start}"
            continue
        # Skip typo ranges (same rule as _window_state)
        if isinstance(end, int) and end <= start and end > 6 * 60:
            continue
        opens = day.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
        if opens > when:
            return f"{opens.strftime('%A')} at {fmt(start)}"
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
#   "Mo-Su 11:00-21:00, Fr-Sa 11:00-22:00"                  an extra rule after a comma
#   "10:00-17:00; We off; Nov Th[4] off; Dec 25 off"      holiday closures (the Met)
#   "Mo-Su 08:00-22:00+", "17:00+"                          open-ended: closing time not given
#   "\"Temporarily closed\""                                 a closure notice instead of hours
# Only this common subset is read. Anything else (month ranges, "sunset", notes in quotes) is unclear
# rather than guessed, except a rule that only closes the place: it can never make a closed
# place open, so the regular hours still decide "closed". Ranges here are minutes after
# midnight; a close past 1440 runs overnight.

OSM_DAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]  # index = datetime.weekday()
OSM_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_OSM_RULE = re.compile(r"(?:([A-Za-z]{2}(?:\s*[-,]\s*[A-Za-z]{2})*)\s+)?(.+)")  # optional days, then times
# "12:00-01:00", "08:00-22:00+" (open until at least 10 PM), or "17:00+" (closing time not given)
_OSM_RANGE = re.compile(r"(\d{1,2}):(\d{2})(?:\s*-\s*(\d{1,2}):(\d{2}))?(\+)?")
_OSM_NOTICE = re.compile(r'"\s*(temporarily|permanently)\s+closed\s*"', re.I)  # the whole listing is a closure notice
# After an open-ended range, how long it may still be open: NYC bars must close by 4 AM
LAST_CALL = 4 * 60
_OSM_CLOSURE = re.compile(r"(.+?)\s+(?:off|closed)", re.I)  # "Dec 25 off"
# A comma right after a time and before a day name starts an additional rule, which adds hours instead
# of replacing them: "Mo-Su 11:00-21:00, Fr-Sa 11:00-22:00". Commas in "Mo,We" or "08:00-12:00,13:00-17:00" do not.
_OSM_ADDITIONAL = re.compile(rf"(?<=[\d+]),\s*(?=(?:{'|'.join(OSM_DAYS)}|PH)\b)")  # after a time or "+"
# One date: "Dec 25", or the nth weekday of a month, "Nov Th[4]" (Thanksgiving) or "May Mo[-1]" (the last)
_OSM_DATE = re.compile(rf"({'|'.join(OSM_MONTHS)})\s+(?:(\d{{1,2}})|({'|'.join(OSM_DAYS)})\[(-?[1-5])\])")


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


def _on_date(closure: tuple, day: datetime) -> bool:
    """Does a closure from _OSM_DATE (month, day of month, weekday, nth) fall on `day`?"""
    month, day_of_month, weekday, nth = closure
    if day.month != OSM_MONTHS.index(month) + 1:
        return False
    if day_of_month:
        return day.day == int(day_of_month)
    if day.weekday() != OSM_DAYS.index(weekday):
        return False
    n = int(nth)
    if n < 0:  # [-1]: the last one in the month, so a week later is next month
        return (day + timedelta(days=7)).month != day.month
    return (day.day - 1) // 7 + 1 == n


def parse_osm_hours(text: str | None) -> dict | None:
    """OSM opening_hours -> {"week", "open_ended", "closed_dates", "unread_closures"}, or None if we cannot read it.

    week: {weekday: [(open, close), ...]}. A day no rule mentions is closed, as in OSM. A later
    rule replaces an earlier one for its days; an additional rule (after a comma) adds to them.
    open_ended: {(weekday, close), ...} for ranges with "+": it may stay open after `close`.
    closed_dates: holiday closures we can check (_OSM_DATE groups).
    unread_closures: True if some other rule closes the place on dates we cannot work out.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw == "24/7":
        always = {d: [(0, 1440)] for d in range(7)}
        return {"week": always, "open_ended": set(), "closed_dates": [], "unread_closures": False}
    week: dict[int, list[tuple[int, int]]] = {}
    open_ended: set[tuple[int, int]] = set()
    closed_dates, unread_closures = [], False
    rules = [(rule.strip(), i > 0) for group in raw.split(";") for i, rule in enumerate(_OSM_ADDITIONAL.split(group))]
    for rule, additional in rules:
        if not rule:
            continue
        if rule.upper().startswith("PH"):
            continue  # public holidays: ignored
        # A closure on dates rather than weekdays ("Dec 25 off", "Easter off")
        closure = _OSM_CLOSURE.fullmatch(rule)
        if closure and _osm_days(closure.group(1)) is None:
            date = _OSM_DATE.fullmatch(closure.group(1))
            if date:
                closed_dates.append(date.groups())
            else:
                unread_closures = True
            continue
        days_text, times_text = _OSM_RULE.fullmatch(rule).groups()
        days = _osm_days(days_text) if days_text else list(range(7))
        if days is None:
            return None
        ranges = []
        if times_text.strip().lower() not in ("off", "closed"):
            for part in times_text.split(","):
                found = _OSM_RANGE.fullmatch(part.strip())
                if not found:
                    return None
                h1, m1, h2, m2, plus = found.groups()
                if h2 is None and not plus:
                    return None  # a lone time like "17:00" is not a range
                start = int(h1) * 60 + int(m1)
                end = start if h2 is None else int(h2) * 60 + int(m2)  # "17:00+": open from 5 PM, no close given
                if h2 is not None and end <= start:
                    end += 1440  # "12:00-01:00" runs past midnight
                ranges.append((start, end))
                if plus:
                    open_ended.update((d, end) for d in days)
        for d in days:
            week[d] = week.get(d, []) + ranges if additional and ranges else ranges
    return {"week": week, "open_ended": open_ended, "closed_dates": closed_dates, "unread_closures": unread_closures}


def osm_status_at(text: str | None, when: datetime) -> dict:
    """Do these OSM hours say the place is open at `when`? -> {"state", "reason"}, like status_at."""
    if (text or "").strip() == "24/7":
        return {"state": "open", "reason": "listed hours say open 24/7"}
    if _OSM_NOTICE.fullmatch((text or "").strip()):
        return {"state": "closed", "reason": f"listing says {text.strip().strip(chr(34)).lower()}"}
    hours = parse_osm_hours(text)
    if hours is None:
        return {"state": "unclear", "reason": f"could not interpret the listed hours: {(text or '')[:60]!r}"}
    week = hours["week"]

    def open_on(day: datetime, end: int, weekday: int) -> dict:
        """The verdict for a range that covers `when` and belongs to `day`, after the date closures."""
        if any(_on_date(c, day) for c in hours["closed_dates"]):
            return {"state": "closed", "reason": "listed hours say closed on this date"}
        if hours["unread_closures"]:
            reason = f"listed hours say open until {fmt(end)}, but some closing dates could not be checked"
            return {"state": "unclear", "reason": reason}
        at_least = "at least " if (weekday, end) in hours["open_ended"] else ""
        return {"state": "open", "reason": f"listed hours say open until {at_least}{fmt(end)}"}

    def maybe_later(start: int, close: int) -> dict:
        """After the end of a range marked "+": it may still be open, up to last call."""
        if start == close:  # "17:00+"
            return {"state": "unclear", "reason": f"listed hours say it opens at {fmt(start)}, with no closing time"}
        reason = f"listed hours say open until at least {fmt(close)}, with no closing time"
        return {"state": "unclear", "reason": reason}

    minute, day = when.hour * 60 + when.minute, when.weekday()
    # Today's ranges, then yesterday's ranges that run past midnight into today
    for start, end in week.get(day, []):
        if start <= minute < end:
            return open_on(when, end, day)
    yesterday = when - timedelta(days=1)
    for start, end in week.get((day - 1) % 7, []):
        if minute < end - 1440:
            return open_on(yesterday, end, (day - 1) % 7)
    # Past the end of a range marked "+": unclear until last call, then closed
    for d, ranges in ((day, week.get(day, [])), ((day - 1) % 7, week.get((day - 1) % 7, []))):
        for start, end in ranges:
            if (d, end) not in hours["open_ended"]:
                continue
            now = minute if d == day else minute + 1440  # minutes after that day's midnight
            if start <= now and end <= now and (d == day or minute < max(LAST_CALL, end - 1440)):
                return maybe_later(start, end)
    return {"state": "closed", "reason": "listed hours say closed at this time"}
