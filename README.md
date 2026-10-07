# Flush Hour

A chat agent that finds public restrooms in New York City that are **open when you get there**, and says so plainly when it isn't sure.

Most "nearest restroom" tools return the closest dot on a map. Flush Hour reads the city's messy opening-hours data, checks each site against the time you would actually arrive, remembers what you need (wheelchair access, a changing station, an all-gender restroom), and plans stops along a walk.

**Live agent:** see `submission.json` (Columbia accounts only, via Identity-Aware Proxy).

## Sample queries for grading

1. `I'm at Columbia University and need a wheelchair-accessible restroom with a changing station.`
   Calls `remember_needs` and `find_restrooms`. Results show walking time, open/closed status, and which needs the city's data could not confirm.
2. `I'm walking from Union Square to the Brooklyn Bridge. Where can I stop on the way?`
   Calls `restrooms_along_route`. Stops come back in walking order with the extra detour, and each site's hours are checked for the minute you would pass it.
3. `It’s 1am and I’m by the Met. What’s actually open?`
   Calls `find_restrooms` for a future time, then usually `fallback_options`. Expect an honest "very little is confirmed open", a suggestion to use the bathroom at home if possible, and a few backups under "Uncertain Options".

Follow-ups worth trying in the same chat: `Will the first one be open Sunday night?` (uses `check_open_status`), and turning on `Use my location` and asking without naming a place (`I need to pee`) to search from your GPS position.

## Tools

| Tool | What it does | Data |
|---|---|---|
| `find_restrooms` | Official NYC restrooms near a place, ranked by estimated walking time, skipping any that are closed at the requested time. Searches 800 m, widens to 2 km on its own if nothing is open, and respects a distance the user states. | NYC Open Data "Public Restrooms", NYC GeoSearch, OpenStreetMap Nominatim (business names) |
| `restrooms_along_route` | Restrooms near a walk from A to B, ordered along the route, with detour cost, hours checked at arrival time, and the longest stretch without a stop. If nothing is open on the way, it gives the arrival time at the end, so a follow-up search there checks hours for then, not now. | same |
| `check_open_status` | Weekly hours, open/closed/unclear at a given time, and the next opening time for one restroom. | same |
| `fallback_options` | When nothing official is open: sites with uncertain hours, volunteer-listed restrooms (with listing age and votes), and OpenStreetMap toilets. Only places with known opening hours are returned; volunteer listings get theirs from the same business on OpenStreetMap. Those hours are checked against the requested time, and places they say are closed are left out. A place listed more than once (the Met has two volunteer pins) is shown once. Saved needs are applied like in `find_restrooms`. Results are ordered: needs met, then hours that say open, then uncertain hours, then walking time. Each has a confidence level. | Refuge Restrooms, OpenStreetMap (Overpass, Nominatim) |
| `remember_needs` | Saves standing needs for the session so later searches apply them automatically. | session state |

Tool-writing choices (from the tool-calling lecture):
- Descriptions say what each tool is for and when not to use it; arguments have formats and examples.
- `needs` is an enum, so invalid requirements cannot be expressed.
- Tools that always run together are merged (geocode, search and hours check happen inside one call).
- The harness fills in what the model should not have to: the current NYC time is added to each user turn, and saved needs are read from session state.
- Place names that match more than one spot (a street in two boroughs, a bar with several branches) return a `needs_clarification` result; the agent asks which one, the page shows buttons, and the chosen option's coordinates are passed back so nothing is looked up twice.
- Errors are JSON with a next step ("retry with a larger `radius_m`", "ids come from earlier `find_restrooms` results; call one of those first"), never a stack trace.
- Arguments are checked before a tool runs, and unreadable ones come back as an error the model can retry from; a bug inside a tool is never blamed on the arguments.
- Each search names the result to suggest (`recommend`), picked in the same order the page shows its cards, so the reply and the first card agree; the model copies a name instead of ranking results itself.
- Results are small, focused JSON.

## How it handles messy data

The hours column has several formats (single daily range, weekly tables with colons or tabs, "24 Hours", blanks) and some typos such as `Tuesday: 10:00 pm - 7:00 pm`. `hours.py` parses all of them and returns **open**, **closed**, or **unclear** with a reason, instead of guessing. `7:30am - dusk` is read using the earliest and latest dusk in NYC, so it is closed at 1 AM and unclear only in the evening. Text with several ranges whose days it cannot tell apart (`M-F 10:30am-5:15pm S-S 12:00pm-8:00pm`) counts as open or closed only when every range agrees. Hours that just say `Temp Closed` count as closed. Seasonal sites are flagged in winter months.

Community and OpenStreetMap places use OSM's `opening_hours` format (`Mo-Th 12:00-24:00; Fr-Sa 12:00-01:00`). `hours.py` reads its common forms, including overnight ranges, days off, extra rules after a comma (`Mo-Su 11:00-21:00, Fr-Sa 11:00-22:00`), holiday closures like `Nov Th[4] off` (Thanksgiving), open-ended hours like `08:00-22:00+` (unclear after 10 PM until 4 AM, NYC's last call), and a listing that just says `"Temporarily closed"`; anything else is unclear. A closure rule it cannot date never makes a place look open: the regular hours still decide when it is closed. On 5,409 real Manhattan listings, about 1% are left unread.

Walking time uses Manhattan's rotated street grid (about 29 degrees) rather than a straight line; it is an estimate, and the agent says so.

## Files

- `app.py`: FastAPI server, agent loop, sessions. `/chat` returns `response`, `session_id`, `tool_calls`.
- `tools.py`: the five tools and their JSON schemas.
- `hours.py`, `geo.py`: opening-hours parsing and distance math (standard library only).
- `index.html`: the interface. Results confirmed open are shown first; anything with uncertain hours goes under "Uncertain Options", in the same order as `fallback_options` (at most 3 cards each, or up to 5 confirmed-open stops for a walking route). An explanation every card shares is shown once above them. If nothing is confirmed open, the page (not the model) suggests using the bathroom at home. With "Use my location" on, a place typed in the message still wins over GPS, and a "Searched near" line then says where the search ran. Each answer has a "How I found this" panel listing every tool call; tool errors appear only there, since they are written for the model, which retries or explains them in its reply.
- `test_core.py`, `test_tools.py`: offline tests using real rows from the dataset.
- `pyproject.toml`, `uv.lock`: dependencies; `uv sync` installs the exact versions.
- `submission.json`: the deployed URL and the authors' UNIs, for grading.
- `check_apis.py`, `probe_geocoding.py`: developer scripts, not used by the app. `check_apis.py` prints a live response from each data source (the tests and the hours parser were written against its output); `probe_geocoding.py` shows how the location lookup handles tricky place names. Both need network access.

## Run locally

```bash
gcloud auth application-default login   # Gemini via Vertex AI, as in the course setup guide
uv sync
uv run app.py                            # http://localhost:8000
uv run python test_core.py && uv run python test_tools.py
```

## Deploy

Cloud Run with continuous deploy from GitHub (Developer Connect), buildpack, entrypoint `uvicorn app:app --host 0.0.0.0 --port $PORT`, and IAP with principal `columbia.edu`. Set **maximum instances to 1**: sessions are held in memory, so a second instance would not know the first one's chats.

## Limitations

- Official data is updated about twice a year; hours and status can be out of date.
- Community listings can be years old (the agent shows listing age and votes), and their hours come from OpenStreetMap, not the business or the city. Listings with no hours on OpenStreetMap are left out, which is most of them.
- OpenStreetMap's public servers are often slow; that source is optional, and when it fails those results are simply missing.
- Each search returns at most 3 places (5 stops for a walking route).
- Walking times and routes are grid estimates, not turn-by-turn directions.
