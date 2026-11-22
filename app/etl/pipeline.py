"""ETL: ingest the three SBB data perspectives into one passenger-safe incident model.

Sources
-------
1. PUBLIC   extern-liveticker   incident (+ _audit), message (+ _audit), reason
2. VIA      Ereignismeldungen Via.xlsx   (alea_fall_id == public incident id == VIA event id)
3. INTERNAL intern-liveticker (KULT)   Ereignisfaelle, Kategorien, Liveticker messages, Logs

Correlation
-----------
* PUBLIC <-> VIA is an exact key match (``alea_fall_id`` is the VIA event id, which is also the
  public incident ``id``).
* INTERNAL <-> (PUBLIC/VIA) has no shared key. It is a HEURISTIC match on
  time proximity (+-90 min), same EG reason code and geographic distance between the stations
  named in ``zug_nr_strecke`` and the incident stations. Every match stores its evidence in
  ``incidents.source_refs.correlation`` with a confidence label.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from sqlalchemy import delete, func, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.rail import offset_line, overlap_share, road_route, dist_m, get_network, order_stations, path_length_m, simplify
from app.core.geo import Station, StationIndex, distance_km
from app.core.timeshift import aligned_shift, shift_ts
from app.core.i18n import LANGS, STATUS_LABELS, forecast_label, is_uncertain
from app.db.models import (
    ChatSession, DisruptionReason, Incident, IncidentMessage, IncidentUpdate, OperationalLog,
)
from app.db.models import Station as StationRow
from app.etl.loaders import find_one, parse_ts, read_csv, to_int, to_py
from app.services.text_utils import (
    extract_lines, mentions_bus, sanitize_operational, split_line_list, strip_html,
)
from app.core.resources import load_resource

log = logging.getLogger("etl")

LIVE_ID_OFFSET = 10_000_000_000_000  # live copies of a dataset incident
KULT_ID_OFFSET = 3_000_000_000  # incidents that only exist in the internal KULT ticker

# English names for internal category codes that are not in the public reason table
_R = load_resource("pipeline_texts")
EXTRA_REASON_EN = _R["EXTRA_REASON_EN"]

INFOBOARD_TITLES = _R["INFOBOARD_TITLES"]

SPLIT_STATIONS_RE = re.compile(r"\s+[-–]\s+|\s+(?:and|und|et|e)\s+")


# --------------------------------------------------------------------------- data classes
@dataclass
class PublicVersion:
    ts: datetime
    lifecycle: str
    duration_type: str
    d_from: datetime | None
    d_to: datetime | None
    at_least: str | None
    reason: str | None
    summary: dict
    description: dict
    status: str


@dataclass
class Cluster:
    key: str
    kind: str  # PUBLIC | VIA | INTERNAL
    public_id: str | None = None
    versions: list[PublicVersion] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    via_rows: list[dict] = field(default_factory=list)
    intern: list[dict] = field(default_factory=list)
    correlation: list[dict] = field(default_factory=list)
    stations: list[Station] = field(default_factory=list)
    start_ts: datetime | None = None

    @property
    def sources(self) -> list[str]:
        out = []
        if self.versions:
            out.append("PUBLIC")
        if self.via_rows:
            out.append("VIA")
        if self.intern:
            out.append("INTERNAL")
        return out


# --------------------------------------------------------------------------- helpers
def _i18n(row: dict, prefix: str) -> dict:
    return {lang: (row.get(f"{prefix}_{lang}") or "").strip() for lang in LANGS}


def _status_from_public(summary_en: str, summary_de: str, lifecycle: str) -> str:
    s_en, s_de = (summary_en or "").lower(), (summary_de or "").lower()
    if lifecycle == "ENDED" or s_en.startswith("service resumed") or s_de.startswith("aufgehoben"):
        return "RESUMED"
    if s_en.startswith("interrupted") or s_de.startswith("unterbruch"):
        return "ACTIVE"
    if s_en.startswith("limited") or s_en.startswith("restriction") or s_de.startswith("einschränkung"):
        return "RESTRICTED"
    return "RESTRICTED"


def _station_part(summary_en: str, summary_de: str) -> str:
    if summary_en and ":" in summary_en:
        return summary_en.split(":", 1)[1].strip()
    if summary_de and " " in summary_de:
        return summary_de.split(" ", 1)[1].strip()
    return summary_en or summary_de or ""


def _resolve_names(names, idx: StationIndex) -> list[Station]:
    out: list[Station] = []
    for n in names:
        st = idx.lookup(n)
        if st and st not in out:
            out.append(st)
    return out


def _stations_from_text(part: str, idx: StationIndex) -> list[Station]:
    return _resolve_names([p for p in SPLIT_STATIONS_RE.split(part or "") if p.strip()], idx)


def _canton_label(stations: list[Station]) -> str:
    cantons: list[str] = []
    for st in stations:
        c = st.canton
        if c not in cantons:
            cantons.append(c)
    return " / ".join(cantons[:3]) if cantons else "CH"


def _stable_hash(s: str) -> int:
    return int(hashlib.sha1(s.encode()).hexdigest()[:8], 16)


TRAVEL_RE = re.compile(
    r"Passengers travelling between (?P<a>.+?) and (?P<b>.+?) (?:(?:travel|go) via (?P<via>[^.]+)|(?P<bus>use the replacement bus))",
    re.I,
)
GUIDANCE_PREFIX = {l: tuple(v) for l, v in _R["GUIDANCE_PREFIX"].items()}  # str.startswith needs a tuple


def _guidance_lines(description: dict) -> dict:
    """Pick the passenger guidance sentences ("Passengers travelling between ... travel via ...")
    from the description in each language."""
    out = {}
    en_lines = [l.strip() for l in (description.get("en") or "").splitlines() if l.strip()]
    en_idx = [i for i, l in enumerate(en_lines) if l.lower().startswith(GUIDANCE_PREFIX["en"])]
    for lang in LANGS:
        lines = [l.strip() for l in (description.get(lang) or "").splitlines() if l.strip()]
        picked = [l for l in lines if l.lower().startswith(GUIDANCE_PREFIX[lang])]
        if not picked and len(lines) == len(en_lines) and en_idx:
            picked = [lines[i] for i in en_idx]
        out[lang] = " ".join(picked)
    return out


GENERIC_ACTION = _R["GENERIC_ACTION"]
NO_DETOUR = _R["NO_DETOUR"]
BUS_DETOUR = _R["BUS_DETOUR"]


# --------------------------------------------------------------------------- loading
def load_reasons(folder: Path) -> dict[str, dict]:
    path = find_one(folder, "reason_*.csv")
    reasons: dict[str, dict] = {}
    if path:
        for r in read_csv(path).to_dict("records"):
            if r.get("id"):
                reasons[r["id"]] = {lang: r.get(f"reason_{lang}") or r.get("reason_de") or r["id"] for lang in LANGS}
    return reasons


def load_public(folder: Path, start: datetime, end: datetime) -> dict[str, Cluster]:
    inc_path, aud_path = find_one(folder, "incident_2*.csv"), find_one(folder, "incident_audit_*.csv")
    if not inc_path:
        log.warning("public incident file missing", extra={"folder": str(folder)})
        return {}
    rows = read_csv(inc_path).to_dict("records")
    rows_audit = read_csv(aud_path).to_dict("records") if aud_path else []
    by_id: dict[str, list[dict]] = defaultdict(list)
    for r in rows_audit + rows:
        if r.get("id"):
            by_id[str(r["id"])].append(r)

    clusters: dict[str, Cluster] = {}
    for iid, versions in by_id.items():
        parsed = []
        for r in versions:
            ts = parse_ts(r.get("last_modified_date")) or parse_ts(r.get("updated_at_source"))
            if ts is None:
                continue
            summary, desc = _i18n(r, "summary"), _i18n(r, "description")
            lifecycle = r.get("lifecycle") or "INFO"
            parsed.append(PublicVersion(
                ts=to_py(ts), lifecycle=lifecycle, duration_type=r.get("duration_type") or "UNKNOWN",
                d_from=to_py(parse_ts(r.get("duration_from"))), d_to=to_py(parse_ts(r.get("duration_to"))),
                at_least=r.get("duration_at_least"), reason=r.get("reason_id_fk"), summary=summary, description=desc,
                status=_status_from_public(summary["en"], summary["de"], lifecycle),
            ))
        if not parsed:
            continue
        parsed.sort(key=lambda v: v.ts)
        first_src = min((parse_ts(r.get("created_at_source")) for r in versions if r.get("created_at_source")), default=None)
        start_ts = to_py(first_src) if first_src is not None else parsed[0].ts
        if not (start <= start_ts < end):
            continue
        # Deduplicate consecutive snapshots that carry no passenger-visible change
        dedup: list[PublicVersion] = []
        for v in parsed:
            key = (v.summary["en"], v.description["en"], v.duration_type, v.d_to, v.at_least, v.status, v.reason)
            if dedup:
                p = dedup[-1]
                if key == (p.summary["en"], p.description["en"], p.duration_type, p.d_to, p.at_least, p.status, p.reason):
                    continue
            dedup.append(v)
        # A DELETED tail after "resumed" is housekeeping, not a passenger-visible version
        while len(dedup) > 1 and dedup[-1].lifecycle == "DELETED" and dedup[-1].summary["en"] == dedup[-2].summary["en"]:
            dedup.pop()
        if dedup[-1].lifecycle == "DELETED":
            dedup[-1].status = "RESUMED"
        clusters[iid] = Cluster(key=iid, kind="PUBLIC", public_id=iid, versions=dedup, start_ts=start_ts)

    _attach_public_messages(folder, clusters)
    return clusters


def _attach_public_messages(folder: Path, clusters: dict[str, Cluster]) -> None:
    msg_path, aud_path = find_one(folder, "message_2*.csv"), find_one(folder, "message_audit_*.csv")
    if not msg_path:
        return
    current = read_csv(msg_path).to_dict("records")
    audit = read_csv(aud_path).to_dict("records") if aud_path else []
    edits: dict[str, int] = defaultdict(int)
    last_audit: dict[str, dict] = {}
    for r in audit:
        mid = r.get("id")
        if not mid:
            continue
        edits[mid] += 1
        prev = last_audit.get(mid)
        if prev is None or (r.get("last_modified_date") or "") > (prev.get("last_modified_date") or ""):
            last_audit[mid] = r
    seen = set()
    for r in current + [r for mid, r in last_audit.items()]:
        mid = r.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        iid = str(r.get("incident_id_fk") or "")
        if iid not in clusters or (r.get("status") or "PUBLISHED") != "PUBLISHED":
            continue
        ts = parse_ts(r.get("created_date"))
        if ts is None:
            continue
        clusters[iid].messages.append({
            "source_id": mid,
            "published_at": to_py(ts),
            "title": {lang: strip_html(r.get(f"title_{lang}")) for lang in LANGS},
            "body": {lang: strip_html(r.get(f"message_{lang}")) for lang in LANGS},
            "edit_count": edits.get(mid, 0),
        })
    for c in clusters.values():
        c.messages.sort(key=lambda m: m["published_at"])


def load_via(folder: Path, start: datetime, end: datetime, idx: StationIndex) -> dict[str, list[dict]]:
    path = find_one(folder, "*.xlsx")
    if not path:
        log.warning("VIA xlsx missing", extra={"folder": str(folder)})
        return {}
    xl = pd.ExcelFile(path)
    sheet = next((s for s in xl.sheet_names if s.lower() != "schema"), xl.sheet_names[0])
    df = xl.parse(sheet, dtype=object)
    df = df.astype(object).where(pd.notna(df), None)
    out: dict[str, list[dict]] = defaultdict(list)
    for r in df.to_dict("records"):
        fid = to_int(r.get("alea_fall_id"))
        ts = parse_ts(r.get("modified")) or parse_ts(r.get("logging_date_time"))
        if fid is None or ts is None:
            continue
        codes = [to_int(r.get(c)) for c in ("stoerungs_ort1", "stoerungs_ort2")]
        line_codes = [to_int(r.get(c)) for c in ("stoerungs_linie_von", "stoerungs_linie_bis")]
        out[str(fid)].append({
            "ts": to_py(ts),
            "created": to_py(parse_ts(r.get("alea_erstellungszeitpunkt"))),
            "ort": r.get("alea_ort_strecke"),
            "reason_text_de": r.get("alea_ereignisgrund"),
            "eg": r.get("ereignisgrund"),
            "stations": [s for s in (idx.by_uic(c) for c in codes if c) if s],
            "line_ends": [s for s in (idx.by_uic(c) for c in line_codes if c) if s],
            "unresolved_codes": [c for c in codes + line_codes if c and not idx.by_uic(c)],
            "lines": split_line_list(r.get("stoerungs_liniennummer")),
            "bus_ts": to_py(parse_ts(r.get("zeitpunkt_erstpublikation_bahnersatz"))),
            "reco_ts": to_py(parse_ts(r.get("zeitpunkt_erstpublikation_reiseempfehlung"))),
            "end_ts": to_py(parse_ts(r.get("zeitpunkt_globale_endmeldung"))),
            "first_pub": to_py(parse_ts(r.get("zeitpunkt_erstpublikation"))),
        })
    for rows in out.values():
        rows.sort(key=lambda x: x["ts"])
    return {k: v for k, v in out.items() if start <= (v[0]["created"] or v[0]["ts"]) < end}


def load_internal(folder: Path, idx: StationIndex) -> tuple[list[dict], dict[str, dict]]:
    ev_path = find_one(folder, "1__*.csv")
    if not ev_path:
        log.warning("KULT export missing", extra={"folder": str(folder)})
        return [], {}
    cats_path, msg_path, logs_path = find_one(folder, "2__*.csv"), find_one(folder, "3_*.csv"), find_one(folder, "4__*.csv")
    cats = {}
    if cats_path:
        for r in read_csv(cats_path).to_dict("records"):
            cats[str(to_int(r["id"]))] = {"code": r.get("name"), "de": r.get("name_de"), "fr": r.get("name_fr"), "it": r.get("name_it")}

    ticker: dict[int, list[dict]] = defaultdict(list)
    if msg_path:
        for r in read_csv(msg_path).to_dict("records"):
            eid, ts = to_int(r.get("ereignisfall_id")), parse_ts(r.get("timestamp"))
            text_ = sanitize_operational(r.get("message"))
            # PRIVACY: name / vorname / telefonnummer columns are dropped here on purpose,
            # and the sender is reduced to an organisational role.
            sender = (r.get("sender") or "").strip()
            role = "TCC" if sender.upper().startswith("TCC") else ("Station" if "bhf" in sender.lower() else "Staff")
            if eid is None or ts is None or not text_:
                continue
            ticker[eid].append({"ts": to_py(ts), "text": f"[{role}] {text_}", "raw": strip_html(r.get("message"))})

    oplogs: dict[int, list[dict]] = defaultdict(list)
    lifecycle: dict[int, list[tuple[datetime, str]]] = defaultdict(list)
    if logs_path:
        seen_text: set[tuple[int, str]] = set()
        for r in read_csv(logs_path).to_dict("records"):
            eid, ts = to_int(r.get("ereignisfall_id")), parse_ts(r.get("created_at"))
            if eid is None or ts is None:
                continue
            entity, action, data = r.get("entity") or "", r.get("action") or "", r.get("data") or ""
            # PRIVACY: user_name (staff id) is never read.
            if entity == "Ereignisfall" and action in ("ACTIVATE", "ARCHIVE", "REACTIVATE", "CREATE"):
                lifecycle[eid].append((to_py(ts), action))
            elif entity == "InfoboardModule" and action in ("UPDATE", "CREATE"):
                m_title = re.search(r"titel='([^']*)'", data)
                m_text = re.search(r"text='(.*?)', textVia=", data, re.S)
                body = sanitize_operational(m_text.group(1) if m_text else "", max_len=500)
                if not body or "XXX" in body:
                    continue
                title = (m_title.group(1) if m_title else "").strip()
                label = INFOBOARD_TITLES.get(title, title if title and not title.startswith("INFOBOARD_") else "Infoboard")
                key = (eid, body)
                if key in seen_text:
                    continue
                seen_text.add(key)
                oplogs[eid].append({"ts": to_py(ts), "text": f"[Infoboard · {label}] {body}"})
            elif entity == "Kubeeinsatz" and action == "CREATE":
                oplogs[eid].append({"ts": to_py(ts), "text": "[KUBE] Customer support staff (KUBE) deployed to stations."})

    events = []
    for r in read_csv(ev_path).to_dict("records"):
        eid = to_int(r.get("id"))
        created = parse_ts(r.get("created_at"))
        if eid is None or created is None:
            continue
        cat = cats.get(str(to_int(r.get("ereigniskategorie_id"))), {})
        strecke = (r.get("zug_nr_strecke") or "").strip()
        stations = _stations_from_text(strecke, idx)
        events.append({
            "id": eid, "created": to_py(created), "eg": cat.get("code"), "category": cat, "strecke": strecke,
            "stations": stations, "ticker": ticker.get(eid, []), "logs": oplogs.get(eid, []),
            "lifecycle": sorted(lifecycle.get(eid, [])), "archived": str(r.get("archived")).lower() == "true",
            "description": sanitize_operational(r.get("beschreibung")),
        })
    return events, cats


# --------------------------------------------------------------------------- correlation
def correlate_internal(events: list[dict], clusters: dict[str, Cluster]) -> list[dict]:
    """Attach KULT events to PUBLIC/VIA clusters. Returns the events that did not match."""
    unmatched = []
    for ev in events:
        best, best_score, best_ev = None, 0.0, None
        for c in clusters.values():
            if c.start_ts is None:
                continue
            dt_min = abs((c.start_ts - ev["created"]).total_seconds()) / 60
            if dt_min > 90:
                continue
            c_egs = {v.reason for v in c.versions} | {r["eg"] for r in c.via_rows}
            eg_match = ev["eg"] in c_egs if ev["eg"] else False
            dist = None
            if ev["stations"] and c.stations:
                dist = min(distance_km(a.coords, b.coords) for a in ev["stations"] for b in c.stations)
            geo = 3.0 if dist is not None and dist <= 15 else (1.0 if dist is not None and dist <= 40 else 0.0)
            score = (3.0 if eg_match else 0.0) + geo + (1 - dt_min / 90)
            accepted = (geo >= 3 and (eg_match or dt_min <= 45)) or (eg_match and geo >= 1) or (eg_match and dist is None and dt_min <= 30)
            if accepted and score > best_score:
                conf = "high" if (eg_match and geo >= 3) else "medium" if (eg_match or geo >= 3) else "low"
                best, best_score = c, score
                best_ev = {"kult_id": ev["id"], "method": "heuristic: time window +-90min, EG reason code, station distance",
                           "time_delta_min": round(dt_min, 1), "reason_match": eg_match,
                           "distance_km": round(dist, 1) if dist is not None else None, "confidence": conf}
        if best is not None:
            best.intern.append(ev)
            best.correlation.append(best_ev)
        else:
            unmatched.append(ev)
    return unmatched


# --------------------------------------------------------------------------- state derivation
def _detour_and_routes(description: dict, stations: list[Station], idx: StationIndex, has_bus: bool):
    """Parse 'Passengers travelling between A and B travel via X' / 'use the replacement bus'."""
    routes: list[str] = []
    detour: list[list[float]] | None = None
    bus_pair: tuple[str, str] | None = None
    specs: list[dict] = []  # every "A and B travel via X" / "use the replacement bus" sentence, for rail routing
    for m in TRAVEL_RE.finditer(description.get("en") or ""):
        a_st = _resolve_names([x.strip() for x in m.group("a").split(",")], idx)
        b_st = _resolve_names([x.strip() for x in m.group("b").split(",")], idx)
        if not a_st or not b_st:
            continue
        a, b = a_st[0], b_st[0]
        route = f"{a.name} → {b.name}"
        if route not in routes:
            routes.append(route)
        via = _resolve_names([x.strip() for x in re.split(r",| and ", m.group("via"))], idx) if m.group("via") else []
        if via and not any(sp["label"] == route and sp["kind"] == "via" for sp in specs):
            specs.append({"label": route, "kind": "via", "names": [a.name] + [v.name for v in via] + [b.name],
                          "points": [a.coords] + [v.coords for v in via] + [b.coords]})
        if m.group("bus") and not any(sp["label"] == route and sp["kind"] == "bus" for sp in specs):
            specs.append({"label": route, "kind": "bus", "names": [a.name, b.name], "points": [a.coords, b.coords]})
        if m.group("via") and detour is None:
            if via:
                detour = [a.coords] + [v.coords for v in via] + [b.coords]
        if m.group("bus") and bus_pair is None:
            bus_pair = (a.name, b.name)
            if detour is None:
                detour = [a.coords, b.coords]
    if len(stations) >= 2:
        seg = f"{stations[0].name} → {stations[-1].name}"
        if seg not in routes:
            routes.insert(0, seg)
        rev = f"{stations[-1].name} → {stations[0].name}"
        if rev not in routes:
            routes.append(rev)
    if has_bus and len(stations) >= 2 and not any(sp["kind"] == "bus" for sp in specs):
        if detour is None:
            detour = [stations[0].coords, stations[-1].coords]
        bus_pair = bus_pair or (stations[0].name, stations[-1].name)
        specs.append({"label": f"{stations[0].name} → {stations[-1].name}", "kind": "bus",
                      "names": [stations[0].name, stations[-1].name], "points": [stations[0].coords, stations[-1].coords]})
    specs.sort(key=lambda sp: sp["kind"] == "bus")  # rail alternatives first, bus last; none is cut by the cap
    return routes[:4], detour, bus_pair, specs[:8]


_RS = load_resource("routing_settings")
MAX_TRACK_DETOUR = _RS["MAX_TRACK_DETOUR"]
MAX_DETOUR_FACTOR = _RS["MAX_DETOUR_FACTOR"]


def _thin(points: list, limit: int) -> list:
    """Douglas-Peucker with growing tolerance until at most ``limit`` points remain."""
    tol = 20.0
    pts = [tuple(p) for p in points]
    while len(pts) > limit:
        pts = simplify(pts, tol)
        tol *= 1.6
    return [[round(p[0], 5), round(p[1], 5)] for p in pts]


MAX_VIA_KM = _RS["MAX_VIA_KM"]
MAX_DETOUR_KM = _RS["MAX_DETOUR_KM"]
BUS_OFFSET_M = _RS["BUS_OFFSET_M"]
MAX_DETOURS = _RS["MAX_DETOURS"]


def _local_detours(net, ordered: list, blocked: set, specs: list[dict], track: list | None = None) -> list[dict]:
    """Detours for the disrupted section itself: they start and end at the disruption's end stations.

    The texts describe passengers' whole journeys ("Genève-Aéroport and Biel travel via Bern"); drawing those would put long
    routes to far cities on the map. Here only via stations near the disruption count, the route must stay within a sane
    factor of the direct distance, and unroutable (straight, dotted) rail detours are dropped. One rail alternative at most
    plus the replacement bus."""
    A, B = ordered[0], ordered[-1]
    direct = max(dist_m(A, B), 500.0)
    mid = ((A[0] + B[0]) / 2, (A[1] + B[1]) / 2)
    via_opts: list[dict] = []
    bus: dict | None = None
    for sp in specs:
        names, pts = sp["names"], [tuple(p) for p in sp["points"]]
        if sp["kind"] == "bus":
            if bus is None:
                road = road_route(A, B)
                route, src = (road, "road") if road else (offset_line(track or [A, B], BUS_OFFSET_M), "offset")
                bus = {"label": sp["label"], "kind": "bus", "via": [], "from": names[0], "to": names[-1],
                       "route": _thin(route, 260), "source": src, "approximate": src != "road",
                       "lengthKm": round(path_length_m(route) / 1000, 1), "stops": [list(route[0]), list(route[-1])]}
            continue
        keep = [(n, p) for n, p in zip(names[1:-1], pts[1:-1]) if dist_m(p, mid) / 1000 <= max(MAX_VIA_KM, direct / 1000 * 3)]
        if not keep:
            continue
        wps = [A] + [p for _, p in keep] + [B]
        r = net.route_through(wps, blocked)
        if not r.ok or r.length_m > min(max(10 * direct, 60_000), MAX_DETOUR_KM * 1000):
            continue
        if track and overlap_share(r.points, track) > 0.3:   # a detour must not run on the disrupted line
            continue
        via_opts.append({"label": sp["label"], "kind": "via", "via": [n for n, _ in keep], "from": names[0], "to": names[-1],
                         "route": _thin(r.points, 260), "source": "rail", "approximate": False, "lengthKm": round(r.length_m / 1000, 1)})
    via_opts.sort(key=lambda d: d["lengthKm"])
    out = via_opts[:1]
    if bus and len(out) < MAX_DETOURS:
        out.append(bus)
    return out[:MAX_DETOURS]


def _rail_geometry(pts: list[list[float]], specs: list[dict] | None) -> dict:
    """Track path along the rail network for the affected stations, plus detours routed around the blocked track."""
    net = get_network()
    out: dict = {"trackSource": "straight", "track": [list(p) for p in pts], "detours": []}
    if net is None or len(pts) < 2:
        return out
    ordered = order_stations([tuple(p) for p in pts])
    straight = path_length_m(ordered)
    r = net.route_through(ordered)
    blocked = set()
    if r.ok and r.length_m <= max(MAX_TRACK_DETOUR * straight, 2000):
        out["track"] = _thin(r.points, 220)
        out["trackSource"] = "rail"
        out["trackLengthKm"] = round(r.length_m / 1000, 1)
        out["trackSnapMeters"] = round(r.snap_m)
        blocked = net.edges_of(r.nodes)
    out["detours"] = _local_detours(net, ordered, blocked, specs or [], [tuple(p) for p in out["track"]])
    return out


def _map_geometry(stations: list[Station], detour, approx_note: str | None = None, specs: list[dict] | None = None) -> dict:
    pts = [s.coords for s in stations]
    geo = {
        "waypoints": pts,
        "from": pts[0] if pts else None,
        "to": pts[-1] if pts else None,
        "stations": [{"name": s.name, "uic": s.code, "lat": s.lat, "lng": s.lng} for s in stations],
        "detourRoute": detour,
        "approximate": False,
    }
    if pts:
        geo.update(_rail_geometry(pts, specs))
        if geo["detours"]:
            geo["detourRoute"] = _thin(geo["detours"][0]["route"], 70)
    else:
        geo["approximate"] = True
        geo["note"] = approx_note or "No coordinates for this location; shown without a map line."
    return geo


def _bus_until(c: Cluster, upto: datetime | None, extra_texts: list[str]) -> bool:
    texts = list(extra_texts)
    for m in c.messages:
        if upto is None or m["published_at"] <= upto:
            texts += [m["body"].get("de", ""), m["body"].get("en", ""), m["title"].get("de", "")]
    for r in c.via_rows:
        if r["bus_ts"] and (upto is None or r["bus_ts"] <= upto):
            return True
    for ev in c.intern:
        for t in ev["ticker"]:
            if upto is None or t["ts"] <= upto:
                texts.append(t["raw"])
    return mentions_bus(*texts)


def derive_public_state(c: Cluster, v: PublicVersion, idx: StationIndex, reasons: dict, upto: datetime | None) -> dict:
    part = _station_part(v.summary["en"], v.summary["de"])
    stations = _stations_from_text(part, idx)
    via_upto = [r for r in c.via_rows if upto is None or r["ts"] <= upto]
    for r in via_upto[-1:]:
        for s in r["stations"]:
            if s not in stations:
                stations.append(s)
    lines: list[str] = []
    for l in extract_lines(v.description["en"], v.description["de"]):
        if l not in lines:
            lines.append(l)
    for r in via_upto[-1:]:
        for l in r["lines"]:
            if l not in lines:
                lines.append(l)
    has_bus = v.status != "RESUMED" and _bus_until(c, upto, [v.description["en"], v.description["de"]])
    routes, detour, bus_pair, specs = _detour_and_routes(v.description, stations, idx, has_bus)
    guidance = _guidance_lines(v.description)
    if v.status == "RESUMED":
        action = GENERIC_ACTION["RESUMED"]
    else:
        action = {lang: guidance[lang] or GENERIC_ACTION[v.status][lang] for lang in LANGS}
    if guidance["en"]:
        detour_summary = dict(guidance)
    elif has_bus and bus_pair:
        detour_summary = {lang: BUS_DETOUR[lang].format(a=bus_pair[0], b=bus_pair[1]) for lang in LANGS}
    else:
        detour_summary = dict(NO_DETOUR)
    dtype = "ENDED" if v.status == "RESUMED" else v.duration_type
    return {
        "status": v.status,
        "title": part.replace(" - ", " – ") or v.summary["en"],
        "subtitle_i18n": v.summary,
        "description_i18n": v.description,
        "reason_id": v.reason if v.reason in reasons else None,
        "duration_type": dtype,
        "duration_type_raw": v.duration_type,
        "duration_from": v.d_from.isoformat() if v.d_from else None,
        "duration_to": v.d_to.isoformat() if v.d_to else None,
        "duration_at_least": v.at_least,
        "forecast_label_i18n": forecast_label(dtype, v.d_from, v.d_to, v.at_least, with_date=True, ref=v.ts),
        "has_replacement_bus": bool(has_bus),
        "affected_lines": lines,
        "affected_stations": [s.name for s in stations],
        "canton": _canton_label(stations),
        "map_geometry": _map_geometry(stations, detour, None, specs),
        "quick_routes": routes,
        "recommended_action_i18n": action,
        "detour_summary_i18n": detour_summary,
        "lifecycle": v.lifecycle,
    }


def _reason_label(reasons: dict, eg: str | None, fallback_de: str | None = None) -> dict:
    if eg and eg in reasons:
        return reasons[eg]
    return {lang: fallback_de or "—" for lang in LANGS}


def _synthetic_summary(status: str, title: str, reason: dict) -> dict:
    return {lang: f"{STATUS_LABELS[status][lang]}: {title} ({reason[lang]})" for lang in LANGS}


def derive_via_state(c: Cluster, upto_idx: int, reasons: dict) -> dict:
    rows = c.via_rows[: upto_idx + 1]
    last = rows[-1]
    stations = list(last["stations"]) or [s for r in rows for s in r["stations"]][:2]
    ended = any(r["end_ts"] for r in rows)
    status = "RESUMED" if ended else "RESTRICTED"
    lines: list[str] = []
    for r in rows:
        for l in r["lines"]:
            if l not in lines:
                lines.append(l)
    has_bus = any(r["bus_ts"] for r in rows) and not ended
    title = " – ".join(s.name for s in stations) or (last["ort"] or f"VIA {c.key}")
    reason = _reason_label(reasons, last["eg"], last["reason_text_de"])
    routes, detour, bus_pair, specs = _detour_and_routes({}, stations, None, has_bus)  # type: ignore[arg-type]
    dtype = "ENDED" if ended else "UNKNOWN"
    if has_bus and bus_pair:
        detour_summary = {lang: BUS_DETOUR[lang].format(a=bus_pair[0], b=bus_pair[1]) for lang in LANGS}
    else:
        detour_summary = dict(NO_DETOUR)
    return {
        "status": status,
        "title": title,
        "subtitle_i18n": _synthetic_summary(status, title, reason),
        "description_i18n": {},
        "reason_id": last["eg"] if last["eg"] in reasons else None,
        "duration_type": dtype,
        "duration_type_raw": None,
        "duration_from": None, "duration_to": None, "duration_at_least": None,
        "forecast_label_i18n": forecast_label(dtype, None, None, None) if ended else _R["END_NOT_COMMUNICATED_VIA"],
        "has_replacement_bus": has_bus,
        "affected_lines": lines,
        "affected_stations": [s.name for s in stations],
        "canton": _canton_label(stations),
        "map_geometry": _map_geometry(stations, detour, "VIA station number(s) not in the station table", specs),
        "quick_routes": routes,
        "recommended_action_i18n": GENERIC_ACTION[status],
        "detour_summary_i18n": detour_summary,
        "lifecycle": "ENDED" if ended else "INFO",
    }


def derive_internal_state(ev: dict, reasons: dict, closed: bool) -> dict:
    stations = ev["stations"]
    texts = " ".join(t["raw"] for t in ev["ticker"]).lower()
    interrupted = any(k in texts for k in ("unterbruch", "interruption", "interruzione", "gesperrt"))
    status = "RESUMED" if closed else ("ACTIVE" if interrupted else "RESTRICTED")
    title = " – ".join(s.name for s in stations) or ev["strecke"] or f"KULT {ev['id']}"
    cat = ev["category"] or {}
    reason = reasons.get(cat.get("code") or "") or {
        "en": EXTRA_REASON_EN.get(cat.get("code") or "", cat.get("de") or "—"),
        "de": cat.get("de") or "—", "fr": cat.get("fr") or "—", "it": cat.get("it") or "—"}
    has_bus = (not closed) and mentions_bus(texts)
    routes, detour, bus_pair, specs = _detour_and_routes({}, stations, None, has_bus)  # type: ignore[arg-type]
    dtype = "ENDED" if closed else "UNKNOWN"
    return {
        "status": status,
        "title": title,
        "subtitle_i18n": _synthetic_summary(status, title, reason),
        "description_i18n": {},
        "reason_id": cat.get("code"),
        "duration_type": dtype, "duration_type_raw": None,
        "duration_from": None, "duration_to": None, "duration_at_least": None,
        "forecast_label_i18n": forecast_label(dtype, None, None, None),
        "has_replacement_bus": has_bus,
        "affected_lines": extract_lines(" ".join(t["raw"] for t in ev["ticker"]))[:8],
        "affected_stations": [s.name for s in stations],
        "canton": _canton_label(stations),
        "map_geometry": _map_geometry(stations, detour, f"Location '{ev['strecke']}' could not be geocoded", specs),
        "quick_routes": routes,
        "recommended_action_i18n": GENERIC_ACTION[status],
        "detour_summary_i18n": {lang: BUS_DETOUR[lang].format(a=bus_pair[0], b=bus_pair[1]) for lang in LANGS}
        if (has_bus and bus_pair) else dict(NO_DETOUR),
        "lifecycle": "ENDED" if closed else "INFO",
    }


def _timeline_text(state: dict, prev: dict | None) -> dict:
    out = {}
    for lang in LANGS:
        parts = [state["subtitle_i18n"].get(lang) or state["subtitle_i18n"].get("en") or state["title"]]
        if state["status"] != "RESUMED":
            parts.append(state["forecast_label_i18n"][lang])
        if state["has_replacement_bus"] and not (prev and prev["has_replacement_bus"]):
            parts.append(_R["BUS_ANNOUNCED"][lang])
        if prev and state["affected_lines"] != prev["affected_lines"] and state["affected_lines"]:
            parts.append(_R["LINES_WORD"][lang] + ": " + ", ".join(state["affected_lines"][:8]))
        out[lang] = " — ".join(p for p in parts if p)
    return out


# --------------------------------------------------------------------------- build rows
def _incident_row(iid: int, external_id: str, mode: str, state: dict, version: int, c: Cluster, started: datetime,
                  updated: datetime, shift: int = 0, cursor: datetime | None = None, extra_refs: dict | None = None) -> dict:
    refs = {
        "public_incident_id": c.public_id,
        "via_event_id": c.key if c.via_rows else None,
        "kult_ids": [ev["id"] for ev in c.intern],
        "correlation": c.correlation,
        "message_count": len(c.messages),
        "unresolved_station_codes": sorted({code for r in c.via_rows for code in r["unresolved_codes"]}),
    }
    if extra_refs:
        refs.update(extra_refs)
    return {
        "id": iid, "external_id": external_id, "data_mode": mode, "status": state["status"],
        "canton": state["canton"], "title": state["title"], "subtitle_i18n": state["subtitle_i18n"],
        "reason_id": state["reason_id"], "duration_type": state["duration_type"],
        "duration_from": datetime.fromisoformat(state["duration_from"]) if state["duration_from"] else None,
        "duration_to": datetime.fromisoformat(state["duration_to"]) if state["duration_to"] else None,
        "duration_at_least": state["duration_at_least"], "forecast_label_i18n": state["forecast_label_i18n"],
        "has_replacement_bus": state["has_replacement_bus"], "affected_lines": state["affected_lines"],
        "affected_stations": state["affected_stations"], "current_version": version,
        "map_geometry": state["map_geometry"], "quick_routes": state["quick_routes"],
        "recommended_action_i18n": state["recommended_action_i18n"], "detour_summary_i18n": state["detour_summary_i18n"],
        "sources": c.sources, "source_refs": refs, "time_shift_seconds": shift, "replay_cursor": cursor,
        "started_at": started, "updated_at": updated,
    }


def build_versions(c: Cluster, idx: StationIndex, reasons: dict) -> list[tuple[datetime, dict, str]]:
    """Return [(source_ts, state, source)] for every passenger-visible version of a cluster."""
    out: list[tuple[datetime, dict, str]] = []
    if c.kind == "PUBLIC":
        for v in c.versions:
            out.append((v.ts, derive_public_state(c, v, idx, reasons, v.ts), "PUBLIC"))
    elif c.kind == "VIA":
        prev_sig = None
        for i, r in enumerate(c.via_rows):
            st = derive_via_state(c, i, reasons)
            sig = (st["status"], tuple(st["affected_lines"]), tuple(st["affected_stations"]), st["has_replacement_bus"], st["reason_id"])
            if sig != prev_sig:
                out.append((r["ts"], st, "VIA"))
                prev_sig = sig
    else:  # INTERNAL only
        ev = c.intern[0]
        out.append((ev["created"], derive_internal_state(ev, reasons, closed=False), "INTERNAL"))
        archive = [ts for ts, a in ev["lifecycle"] if a == "ARCHIVE"]
        last_tick = ev["ticker"][-1]["ts"] if ev["ticker"] else None
        end_ts = archive[-1] if archive else last_tick
        if ev["archived"] and end_ts and end_ts > ev["created"]:
            out.append((end_ts, derive_internal_state(ev, reasons, closed=True), "INTERNAL"))
    return out


def oplogs_for(c: Cluster) -> list[dict]:
    logs: list[dict] = []
    prev = None
    for r in c.via_rows:
        msg_parts = []
        if r["stations"]:
            msg_parts.append("location " + " – ".join(s.name for s in r["stations"]))
        if r["lines"]:
            msg_parts.append("lines " + ", ".join(r["lines"][:12]))
        if r["bus_ts"]:
            msg_parts.append("rail replacement (Bahnersatz) published")
        if r["reco_ts"]:
            msg_parts.append("travel recommendation published")
        if r["end_ts"]:
            msg_parts.append("global end message sent")
        text_ = f"[VIA] Event updated and published ({r['reason_text_de'] or r['eg'] or 'n/a'}): " + "; ".join(msg_parts)
        if text_ != prev:
            logs.append({"timestamp": r["ts"], "sanitized_message": text_, "source_type": "VIA"})
            prev = text_
    for ev in c.intern:
        for t in ev["ticker"] + ev["logs"]:
            logs.append({"timestamp": t["ts"], "sanitized_message": t["text"], "source_type": "KULT_INTERNAL"})
        if ev["description"]:
            logs.append({"timestamp": ev["created"], "sanitized_message": f"[KULT] {ev['description']}", "source_type": "KULT_INTERNAL"})
    logs.sort(key=lambda x: x["timestamp"])
    return logs


def choose_live(clusters: list[Cluster], versions: dict[str, list], n: int) -> list[Cluster]:
    scored = []
    for c in clusters:
        vs = versions[c.key]
        if c.kind != "PUBLIC" or len(vs) < 3 or not any(s["status"] != "RESUMED" for _, s, _ in vs):
            continue
        any_bus = any(s["has_replacement_bus"] for _, s, _ in vs)
        any_detour = any(s["map_geometry"].get("detourRoute") for _, s, _ in vs)
        any_unc = any(is_uncertain(s["duration_type"]) for _, s, _ in vs)
        has_geo = bool(vs[-1][1]["map_geometry"]["waypoints"])
        score = min(len(vs), 10) + 3 * any_bus + 2 * any_detour + 2 * any_unc + 2 * bool(c.intern) + 0.5 * min(len(c.messages), 6) + 5 * has_geo
        scored.append((score, c))
    scored.sort(key=lambda x: -x[0])
    picked, titles = [], set()
    for _, c in scored:
        t = versions[c.key][-1][1]["title"]
        if t in titles:
            continue
        titles.add(t)
        picked.append(c)
        if len(picked) >= n:
            break
    return picked


# --------------------------------------------------------------------------- main entry
async def run_ingest(session: AsyncSession, settings: Settings, idx: StationIndex, reset: bool = False) -> dict:
    data_dir = Path(settings.data_dir)
    if not data_dir.exists():
        raise FileNotFoundError(f"Data directory not found: {data_dir}")
    start = datetime.fromisoformat(settings.dataset_from).replace(tzinfo=timezone.utc) - timedelta(hours=2)
    end = datetime.fromisoformat(settings.dataset_to).replace(tzinfo=timezone.utc)

    pub_dir, int_dir, via_dir = data_dir / "extern-liveticker", data_dir / "intern-liveticker", data_dir / "Ereignismeldungen"
    reasons = load_reasons(pub_dir)
    clusters = load_public(pub_dir, start, end)
    via = load_via(via_dir, start, end, idx)
    events, cats = load_internal(int_dir, idx)
    for code_id, cat in cats.items():
        code = cat.get("code")
        if code and code not in reasons:
            reasons[code] = {"en": EXTRA_REASON_EN.get(code, cat.get("de") or code), "de": cat.get("de") or code,
                             "fr": cat.get("fr") or code, "it": cat.get("it") or code}

    # PUBLIC <-> VIA: exact key
    for fid, rows in via.items():
        if fid in clusters:
            clusters[fid].via_rows = rows
            clusters[fid].correlation.append({"via_event_id": fid, "method": "exact id match (alea_fall_id = incident.id)", "confidence": "exact"})
        else:
            clusters[fid] = Cluster(key=fid, kind="VIA", via_rows=rows, start_ts=rows[0]["created"] or rows[0]["ts"])
    for c in clusters.values():
        if c.kind == "PUBLIC":
            v = c.versions[-1]
            c.stations = _stations_from_text(_station_part(v.summary["en"], v.summary["de"]), idx)
        for r in c.via_rows:
            for s in r["stations"]:
                if s not in c.stations:
                    c.stations.append(s)

    unmatched = correlate_internal(events, clusters)
    for ev in unmatched:
        key = f"KULT-{ev['id']}"
        clusters[key] = Cluster(key=key, kind="INTERNAL", intern=[ev], start_ts=ev["created"], stations=ev["stations"])

    # ---------------- write
    if reset:
        await session.execute(text("TRUNCATE chat_messages, chat_sessions, operational_logs, incident_messages, incident_updates, incidents RESTART IDENTITY CASCADE"))

    station_rows = {}
    for c in clusters.values():
        for s in c.stations + [s for ev in c.intern for s in ev["stations"]]:
            station_rows[s.code] = {"uic": s.code, "name": s.name, "canton": s.canton, "lat": s.lat, "lng": s.lng}
    if station_rows:
        stmt = pg_insert(StationRow).values(list(station_rows.values()))
        await session.execute(stmt.on_conflict_do_update(index_elements=["uic"], set_={
            "name": stmt.excluded.name, "canton": stmt.excluded.canton, "lat": stmt.excluded.lat, "lng": stmt.excluded.lng}))
    if reasons:
        stmt = pg_insert(DisruptionReason).values([
            {"id": k, "reason_en": v["en"], "reason_de": v["de"], "reason_fr": v["fr"], "reason_it": v["it"]} for k, v in reasons.items()])
        await session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_={
            "reason_en": stmt.excluded.reason_en, "reason_de": stmt.excluded.reason_de,
            "reason_fr": stmt.excluded.reason_fr, "reason_it": stmt.excluded.reason_it}))

    versions = {k: build_versions(c, idx, reasons) for k, c in clusters.items()}
    incident_rows, update_rows, message_rows, log_rows = [], [], [], []

    def numeric_id(c: Cluster) -> int:
        if c.kind == "INTERNAL":
            return KULT_ID_OFFSET + c.intern[0]["id"]
        return int(c.key) if c.key.isdigit() else KULT_ID_OFFSET + _stable_hash(c.key)

    def add_children(iid: int, c: Cluster, vs, applied_upto: int):
        prev = None
        for i, (ts, st, src) in enumerate(vs):
            update_rows.append({
                "incident_id": iid, "version": i + 1, "published_at": ts, "summary_i18n": _timeline_text(st, prev),
                "source": src, "status": st["status"], "snapshot": st, "is_applied": i <= applied_upto})
            prev = st
        for m in c.messages:
            message_rows.append({"incident_id": iid, "source_id": m["source_id"], "title_i18n": m["title"],
                                 "body_i18n": m["body"], "published_at": m["published_at"], "edit_count": m["edit_count"]})
        for lg in oplogs_for(c):
            log_rows.append({"incident_id": iid, **lg})

    for key, c in clusters.items():
        vs = versions[key]
        if not vs:
            continue
        iid = numeric_id(c)
        last_ts, last_state, _ = vs[-1]
        incident_rows.append(_incident_row(iid, key, "dataset", last_state, len(vs), c, c.start_ts or vs[0][0], last_ts))
        add_children(iid, c, vs, len(vs) - 1)

    # ---------------- live mode: curated incidents projected to "today" for the replay demo
    now = datetime.now(timezone.utc)
    live = choose_live([clusters[k] for k in clusters], versions, settings.live_incident_count)
    for n, c in enumerate(live):
        vs = versions[c.key]
        # start part-way through the real history so that "simulate update" replays the rest;
        # the last pick is fully replayed (a resolved incident for the "Resumed" KPI)
        if n == len(live) - 1 and len(live) >= 4:
            start_idx = len(vs) - 1
        else:
            first_open = next((i for i, (_, s, _) in enumerate(vs) if s["status"] != "RESUMED"), 0)
            start_idx = min(first_open + (n % 3), len(vs) - 2)
        cursor_ts = vs[start_idx][0]
        shift = aligned_shift(now, cursor_ts, 5 * n)  # wall-clock, 5-min aligned, staggered
        iid = LIVE_ID_OFFSET + numeric_id(c)
        st = vs[start_idx][1]
        incident_rows.append(_incident_row(
            iid, c.key, "live", st, start_idx + 1, c, (c.start_ts or vs[0][0]), shift_ts(cursor_ts, shift),
            shift=shift, cursor=cursor_ts, extra_refs={"replay": {"initial_version": start_idx + 1, "total_versions": len(vs)}}))
        add_children(iid, c, vs, start_idx)

    ids = [r["id"] for r in incident_rows]
    # idempotent: replace children of re-ingested incidents, upsert incidents, drop stale ones
    if ids:
        for model in (IncidentUpdate, IncidentMessage, OperationalLog):
            await session.execute(delete(model).where(model.incident_id.in_(ids)))
        stale = select(Incident.id).where(Incident.id.not_in(ids))
        await session.execute(delete(ChatSession).where(ChatSession.incident_id.in_(stale)))
        await session.execute(delete(Incident).where(Incident.id.not_in(ids)))
        for chunk in _chunks(incident_rows, 200):
            stmt = pg_insert(Incident).values(chunk)
            cols = {c.name: getattr(stmt.excluded, c.name) for c in Incident.__table__.columns if c.name != "id"}
            await session.execute(stmt.on_conflict_do_update(index_elements=["id"], set_=cols))
    for model, rows in ((IncidentUpdate, update_rows), (IncidentMessage, message_rows), (OperationalLog, log_rows)):
        for chunk in _chunks(rows, 500):
            await session.execute(pg_insert(model).values(chunk))
    await session.commit()

    stats = {
        "incidents_dataset": sum(1 for r in incident_rows if r["data_mode"] == "dataset"),
        "incidents_live": sum(1 for r in incident_rows if r["data_mode"] == "live"),
        "public_clusters": sum(1 for c in clusters.values() if c.kind == "PUBLIC"),
        "via_only_clusters": sum(1 for c in clusters.values() if c.kind == "VIA"),
        "kult_only_clusters": sum(1 for c in clusters.values() if c.kind == "INTERNAL"),
        "kult_events_correlated": len(events) - len(unmatched),
        "kult_events_total": len(events),
        "updates": len(update_rows), "messages": len(message_rows), "operational_logs": len(log_rows),
        "stations": len(station_rows), "reasons": len(reasons),
        "incidents_without_coordinates": sum(1 for r in incident_rows if not r["map_geometry"]["waypoints"]),
    }
    log.info("ingest complete", extra=stats)
    return stats


def _chunks(rows: list, n: int):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]


async def incident_count(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Incident))).scalar_one()
