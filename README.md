# SBB Disruption Companion (LauzHack 2026)

During a disruption, SBB messages say *what* happened. This app tells the passenger *what to do next*.
It merges three SBB data perspectives into one passenger-safe incident view, tracks how each incident
evolves version by version, and answers with a **Hybrid Decision Engine**: deterministic SBB rules plus
Google Gemini with Structured Outputs. When Gemini is unavailable, the rules answer on their own.

- **Backend:** FastAPI, async SQLAlchemy 2 with asyncpg, PostgreSQL 16, structured JSON logs
- **AI:** `google-genai`, `gemini-3.8-flash` by default, Pydantic `response_schema`, guardrails and fallback
- **Frontend:** the original `sbb_disruption_companion_en.html` design (Tailwind, Leaflet), now backed by the API: `static/index.html`
- **Languages:** EN (default), DE, FR, IT. **No login:** each anonymous session is a UUID kept in `sessionStorage`, with a TTL.

---

## Quickstart

### Option A: Docker (one command)

```bash
cp .env.example .env              # optional: put GEMINI_API_KEY=... in it
# Point DATEN_PATH at the SBB "Daten" folder (with extern-liveticker/, intern-liveticker/, Ereignismeldungen/)
DATEN_PATH=/path/to/Daten docker compose up --build
```

On Windows PowerShell:

```powershell
$env:DATEN_PATH="C:/Users/<you>/Downloads/Daten"; $env:GEMINI_API_KEY="..."; docker compose up --build
```

Then open http://localhost:8000. The API docs are at http://localhost:8000/docs.
The first start runs the ETL automatically (`AUTO_INGEST=true`), which takes about 5 seconds.

### Option B: Local Python (3.11+)

```bash
python -m venv .venv && . .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
docker compose up -d db                              # or use your own PostgreSQL 15+
cp .env.example .env                                 # set DATABASE_URL, DATA_DIR, GEMINI_API_KEY
python scripts/ingest_data.py --data-dir /path/to/Daten   # idempotent; add --reset to wipe incident and chat tables
uvicorn app.main:app --reload
```

### Gemini

Set `GEMINI_API_KEY` (from Google AI Studio) in `.env`. Defaults: `GEMINI_MODEL=gemini-3.8-flash`,
`GEMINI_FALLBACK_MODEL=gemini-3.5-flash` (comma separated list allowed), `GEMINI_THINKING_LEVEL=minimal`.

**Speed and resilience for live demos.** With `GEMINI_MODE=async` (default) the passenger gets the SBB rule-engine
answer in about 0.2 s. The card shows "Refining with Gemini…" and the browser polls
`GET /api/sessions/{id}/messages/{messageId}`. When Gemini has rephrased the plan (guardrails checked) the card is
replaced with the Gemini version; if Gemini is slow, overloaded or rejected, the rule-based card simply stays. So the demo
never waits on Google. `GEMINI_MODE=sync` waits for Gemini instead (use small timeouts such as 25 / 12 there).

Background calls may take up to `GEMINI_TIMEOUT_SECONDS=50` in total and `GEMINI_ATTEMPT_TIMEOUT_SECONDS=25` per
call (Google rejects deadlines under 10 s, so the app never uses less). On `503 high demand`, `429` or a 5xx the same
model is retried once; a timeout is not retried, the next model in `GEMINI_FALLBACK_MODEL` (comma separated) gets the turn.
A failed model is skipped for 30-60 s, a 404 (retired model) for 10 minutes. The prompt is kept small (freshest 3 updates,
5 log lines, 2 messages, 4 chat turns), the answer is capped at `GEMINI_MAX_OUTPUT_TOKENS=900`, and `GEMINI_THINKING_LEVEL=minimal`
keeps Gemini 3 from thinking for a long time. `GEMINI_OUTPUT_MODE` (`schema`, `jsonschema`, `json`) chooses how the JSON
structure is requested; a model that rejects the schema falls back to the simpler modes automatically.

Docker Compose reads `.env` from the project folder. After changing it, run `docker compose up -d --force-recreate app`.
`docker compose exec app python scripts/check_gemini.py` sends a **real-size request** (same prompt size, schema and
thinking level as the app) to every current Flash model your key lists, in all three output modes, **streaming** the
answer so it shows the time to the first token and the total time. It then prints the fastest combination as
`GEMINI_MODEL`, `GEMINI_FALLBACK_MODEL` and `GEMINI_OUTPUT_MODE` lines for your `.env`. Options: `--tiny` (quick liveness
check), `--runs 2` (median), `--models a,b` (only these), `--include-old` (retired 1.x/2.x models).
`GET /api/health` shows whether Gemini is configured, available, and its last error. Each assistant card shows which
engine answered: "AI-synthesised (Gemini)" or "SBB rule engine".

### Tests

```bash
pytest -q                                            # unit tests (no DB needed)
DATABASE_URL=postgresql+asyncpg://sbb:sbb@localhost:5432/sbb pytest -q   # also runs the end-to-end API flow on an ingested DB
```

---

## Live demo script (about 3 minutes)

1. **Live: Today.** Six real incidents from Sept/Oct 2026 are *replayed* as if they were happening now.
   The KPIs, map and cards come from `GET /api/incidents?mode=live`.
2. Click **What to do now** on an incident. The drawer shows the **audit timeline** (v1, v2, …), the
   **official messages and sanitized operational context** (internal ticker and VIA), the 4 scenarios and the chat.
3. Pick a route chip, then **How do I take a detour?** You get numbered steps, a **Display detour on map** button,
   official `sbb.ch/{lang}` links, and a forecast warning when the end time is uncertain.
4. Type free text, for example *"Heading to Lausanne with a Supersaver ticket, 75 min late"*. The trip context
   (destination, ticket type, delay) is extracted and the refund rule answers *25%*.
5. Click **⚡ Test: new update in chat** (or **Simulate update** in the header). The next real audit
   snapshot is replayed (`POST /api/incidents/{id}/simulate-update`). The toast, KPIs and timeline update,
   and the chat receives a **SYSTEM_ALERT** with the delta. The next answer uses the new state.
6. Switch the language to DE, FR or IT, or switch to **Dataset: Sept 2026** to browse all 225 unified incidents by day.
7. To repeat the demo, `POST /api/incidents/live/reset` rewinds the live incidents.

---

## Architecture

```
Daten/ ──► scripts/ingest_data.py (app/etl) ──► PostgreSQL ◄── FastAPI (app/api) ◄── static/index.html
                                                   ▲
                       Hybrid Decision Engine ─────┘
                       ├─ deterministic strategies (policy facts, links, map, warnings)
                       └─ Gemini structured output (+ guardrails, fallback)
```

| Path | Purpose |
|---|---|
| `app/core/config.py` | Settings from env / `.env` |
| `app/core/geo.py`, `data/stations.csv` | Station names and UIC numbers to coordinates, canton estimate |
| `app/core/i18n.py` | Labels, forecast text, official sbb.ch links, scenario metadata (EN/DE/FR/IT) |
| `app/db/models.py` | Schema: `stations`, `disruption_reasons`, `incidents`, `incident_updates`, `incident_messages`, `operational_logs`, `chat_sessions`, `chat_messages` |
| `app/etl/` | Loaders (encoding detection) and the correlation and enrichment pipeline |
| `app/services/decision_engine.py` | Strategy pattern: `missed_connection`, `detour_route`, `refund_compensation`, `status_timeline` (+ `general`) |
| `app/services/gemini_service.py` | `google.genai.Client`, `response_schema=AssistantDecisionSchema`, timeout and circuit breaker |
| `app/services/session_service.py` | Anonymous sessions, SYSTEM_ALERT observer, assistant turns |
| `app/services/incident_service.py` | Dashboard, detail, live replay, `simulate-update` |

### Decision engine rules (deterministic, always applied)

- Ticket unused because of the disruption: full refund with no fee. Arrival delay of 60–119 min: 25%. 120 min or more: 50%.
- Supersaver: the binding to a specific train is lifted during an official disruption.
- `UNKNOWN`, `UNTIL_AT_LEAST`, `UNTIL_AT_LEAST_DATE_TIME`: `forecastWarning = true`, plus advice not to wait at an intermediate station.
- `showDetourMap = true` with coordinates whenever a detour route exists.
- Links: `sbb.ch/{lang}` timetable, live traffic info and refunds.

Gemini gets the incident, the versioned updates, the sanitized operational logs, the public messages, the
trip context, the recent chat and the deterministic plan. Its answer is **rejected**, and the rule-based plan used
instead, if it contains clock times that do not appear in the source data (no invented departures), drops the
refund percentages, or contains a phone number. Widgets and links always come from the rules.

### Data correlation: what is exact and what is a heuristic

| Link | Method | Confidence |
|---|---|---|
| Public ticker ↔ VIA | `Ereignismeldungen.alea_fall_id == incident.id` (both are the VIA event id) | **exact** |
| KULT internal ticker ↔ public/VIA | **Heuristic (my guess):** creation time within ±90 min, same EG reason code, and station distance (≤15 km strong, ≤40 km weak) between `zug_nr_strecke` and the incident stations | stored per match in `incidents.source_refs.correlation` as `high`, `medium` or `low`, with Δt and km |
| Cantons | **Estimate:** nearest of about 150 reference towns. It can be wrong right at canton borders | approximate |
| Station coordinates | `data/stations.csv`, extracted from the open [trainline-eu stations](https://github.com/trainline-eu/stations) list (ODbL). 255 of the 257 VIA station numbers resolve | exact where found |

Result on the provided export: 225 unified incidents in the dataset window. 18 have public ticker history
(14 of them with VIA rows), 154 exist only in VIA, and 53 exist only in KULT. 84 of the 137 KULT events
were linked heuristically. 3 incidents have no coordinates and appear as cards only, so the map never breaks.

Other interpretation choices:
- Public versions are ordered by `last_modified_date`, as the README of the export says. Consecutive snapshots with no
  passenger-visible change are collapsed, and a trailing `DELETED` housekeeping row is dropped.
- Status comes from the summary prefix (`Unterbruch`/`Interrupted` → ACTIVE, `Einschränkung`/`Limited` → RESTRICTED,
  `Aufgehoben`/`Service resumed` or `ENDED` → RESUMED).
- VIA-only incidents with no `zeitpunkt_globale_endmeldung` stay RESTRICTED with "end not communicated".
- **Live mode** replays real histories "today". All timestamps move by a wall-clock shift that is a multiple of 5 minutes,
  and clock times inside texts (08:00, 7h05) move by the same amount, so labels stay consistent. The live view is
  marked as a replay in `source_refs.replay`.

### One time reference for all dates

Live mode replays incidents "today": every stored time is shifted by the incident's own `time_shift_seconds`, so cards, "updated X ago", forecasts, timeline, chat answers and the sbb.ch timetable link all show the same moment. Dataset incidents keep their real day: their forecasts and times carry the date (`01.09. 09:00`), the "Dataset: ..." badge is built from the days really in the data, and the timetable link opens sbb.ch on the incident's own date and time (`datum`, `zeit`). SBB.ch itself only shows timetables for today and the future, so a dataset link may be adjusted by SBB.

### Movable incident list

The list on the left is its own panel: drag the splitter to change its width, or press "Detach panel" to float it as a window you can move (drag its header) and resize (corner grip). "Dock panel" puts it back; the choice is remembered.

### Text and settings live in resource files

No wording is hard-coded in the code any more:

- **Frontend text:** `static/i18n/en.json`, `de.json`, `fr.json`, `it.json` (flat key to text, identical keys in all four). `static/js/app.js` loads them at start. Edit a file to change wording or add keys; a test checks the four files stay in sync.
- **Backend text:** `app/resources/*.json` holds the assistant and decision-engine wording (`decision_texts.json`), status and forecast labels and sbb.ch links (`i18n.json`), ticker/ETL wording (`pipeline_texts.json`), incident and session texts, and the Gemini system instruction (`gemini_texts.json`). Each is read once through `app/core/resources.py`.
- **Tunable settings:** `app/resources/map_settings.json` (status colours, default map bounds) and `routing_settings.json` (detour limits, bus offset).
- Changing a resource file needs a container rebuild (`docker compose up -d --build`); detour limits also need a re-ingest.

### Map

The base map needs **no API key**. It uses the free **swisstopo** pixel map (`wmts.geo.admin.ch`, grey by default,
colour selectable) on top of **OpenStreetMap** tiles. OSM fills the area outside Switzerland and shows through if
swisstopo is unreachable, so the map is never blank. Attribution for both is shown on the map. Please keep OSM usage
light (demo only); for production use a tile provider or your own tile server.

Everything on the map is drawn from the API: `GET /api/map` gives the coloured disruption segments and markers
(red interrupted, amber dashed restricted, green resumed). Clicking a marker or line selects the matching card (and
switches an open assistant drawer to that incident). The selected incident's station names come from
`GET /api/incidents/{id}/map`. **Display detour route on map** fetches the detour geometry from that endpoint,
draws it in green and zooms to disruption and detour together. **All Switzerland** clears the detour and resets the view.
The map reloads with the dashboard, so it updates after **Simulate update** and every 20 s. Incidents without
coordinates (3 in the dataset window) get a card but no marker; the map shows a note with their count and never breaks.
**Geometry along the real track.** At ingest (`app/etl/pipeline.py` + `app/core/rail.py`) the affected stations are
snapped to a rail graph (`data/rail_ch.json`) and the disrupted section is routed along the track (Dijkstra). Detours are
routed on the same graph with the disrupted edges **blocked**, so a detour is a real alternative path over other lines,
never one straight line; bus detours are routed unblocked. The result is stored in `incidents.map_geometry` (`track`,
`detours`, `trackSource`), so the API only reads it. Anything that cannot be routed (station farther than 3 km from any
track, no path, or a path more than 3x/4x longer than the straight distance) falls back to a straight line and is flagged
`approximate`: it is drawn dotted and the popup says so. Overlapping markers are spread on a small ring with leader lines
and dissolve when you zoom in.

Data quality: the shipped graph is **Natural Earth 10m railroads** (public domain, generalised). It is good for main lines
(Lausanne–Yverdon, Genève–Bern, Zürich–Bern ...) but coarse and incomplete for regional and narrow-gauge lines, so some
incidents fall back to straight lines. For track-accurate geometry rebuild the graph from OpenStreetMap on a machine
with internet access (not testable in the build sandbox):

```
docker compose exec app python scripts/build_rail_network.py --source overpass
docker compose exec app python scripts/ingest_data.py --reset
```
Station coordinates were audited (Chavornay, Cossonay-Penthalaz, Ependes VD and others resolve to the right Swiss
stations; ambiguous names prefer Switzerland).

### Privacy

The ETL never reads the KULT columns `name`, `vorname`, `telefonnummer` or the log `user_name`. The sender becomes a role
(TCC, Station or Staff). Message text is stripped of HTML and scrubbed of phone numbers, e-mails, personnel numbers
(`u123456`), contact lines and internal links before it is stored in `operational_logs`. Only sanitized text
reaches the API and Gemini.

---

## REST API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/health` | DB counts, Gemini readiness |
| GET | `/api/incidents` | `lang`, `mode=live\|dataset`, `status=ALL\|ACTIVE\|RESTRICTED\|RESUMED\|BUS`, `hasReplacementBus`, `search`, `date` (dataset) → `kpis`, `incidents`, `totalCount` |
| GET | `/api/incidents/{id}` | Detail: `auditTimeline`, `messages`, `operationalSummary`, `mapGeometry`, `quickRoutes`, `sourceRefs` |
| GET | `/api/incidents/{id}/timeline` | Applied versions |
| POST | `/api/incidents/{id}/simulate-update` | Replay the next real snapshot, else a realistic step (Bahnersatz → forecast refined → resumed) |
| POST | `/api/incidents/live/reset` | Rewind the live demo |
| GET | `/api/map` | **GeoJSON** FeatureCollection for the map. Same filters as `/api/incidents` (`lang`, `mode`, `status`, `hasReplacementBus`, `search`, `date`) plus `detours=true`. Features: `segment` (LineString, `color` = status, `dashed` for restricted), `incident` (Point with `id`, `title`, `status`, `statusLabel`, `affectedLines`, `version`, `bounds`), optional `detour`. `meta.unmappedIds` lists incidents without coordinates |
| GET | `/api/incidents/{id}/map` | GeoJSON for one incident: segment, marker, `station` points (role from/to), `detour` LineString and `detour_label`; `meta` has `bounds`, `hasCoordinates`, `hasDetour`, `detourSummary` |
| GET | `/api/scenarios` | Localized scenario metadata |
| POST | `/api/sessions` | `{incidentId, lang, tripContext?}` |
| GET | `/api/sessions/{id}` | History and the version check (`newAlerts`) |
| POST | `/api/sessions/{id}/scenario` | `{scenarioSlug, selectedRoute?, tripContextPatch?, lang?}` |
| POST | `/api/sessions/{id}/messages` | `{content, selectedRoute?, lang?}` |
| DELETE | `/api/sessions/expired` | Cleanup. A background task also runs it every 10 minutes |

Expired sessions return `410`, and the frontend then creates a new one.
