"""Incident queries, DTO mapping, live-mode projection and update simulation / replay."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import Date, and_, cast, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.i18n import (
    LANGS, STATUS_LABELS, TZ, forecast_label, is_uncertain, norm_lang, pick, updated_ago,
)
from app.core.timeshift import aligned_shift, shift_ts, unshift_ts
from app.db.models import Incident, IncidentMessage, IncidentUpdate, OperationalLog
from app.schemas.dtos import (
    DashboardResponseDto, IncidentDetailDto, IncidentMessageDto, IncidentSummaryDto, KpiDto, MapGeometry,
    OperationalSummaryDto, TimelineEntryDto,
)

log = logging.getLogger("incidents")

LIVE_STALE_AFTER = timedelta(hours=6)

BUS_DETOUR = {
    "en": "Replacement buses (Bahnersatz) run between {a} and {b}. Follow the «Bahnersatz» signs at the station; your ticket is valid on the bus.",
    "de": "Bahnersatzbusse verkehren zwischen {a} und {b}. Folgen Sie der Signalisation «Bahnersatz» am Bahnhof; Ihr Billett gilt im Bus.",
    "fr": "Des bus de remplacement circulent entre {a} et {b}. Suivez la signalétique «Bahnersatz» en gare ; votre billet est valable dans le bus.",
    "it": "Bus sostitutivi (Bahnersatz) circolano tra {a} e {b}. Segui la segnaletica «Bahnersatz» in stazione; il biglietto è valido sul bus.",
}
RESUMED_ACTION = {
    "en": "Service has resumed. Isolated delays are still possible; travel as planned.",
    "de": "Der Betrieb ist wieder aufgenommen. Vereinzelt sind Verspätungen möglich; reisen Sie wie geplant.",
    "fr": "Le trafic est rétabli. Des retards isolés restent possibles ; voyagez comme prévu.",
    "it": "La circolazione è ripristinata. Sono ancora possibili ritardi isolati; viaggia come previsto.",
}


_CLOCK_RE = re.compile(r"\b([01]?\d|2[0-3])([:h.])([0-5]\d)\b")


def shifted(inc: Incident, ts: datetime | None) -> datetime | None:
    return shift_ts(ts, inc.time_shift_seconds or 0)


def shift_text(inc: Incident, text: str | None) -> str:
    """Live mode replays history "today": move clock times (08:00, 7h05, 14.42) in texts by the same shift."""
    if not text or inc.data_mode != "live" or not inc.time_shift_seconds:
        return text or ""
    minutes = (inc.time_shift_seconds // 60) % 1440

    def repl(m: re.Match) -> str:
        total = (int(m.group(1)) * 60 + int(m.group(3)) + minutes) % 1440
        return f"{total // 60:02d}{m.group(2)}{total % 60:02d}"

    return _CLOCK_RE.sub(repl, text)


def shifted_i18n(inc: Incident, d: dict | None) -> dict:
    return {k: shift_text(inc, v) for k, v in (d or {}).items()}


def _at_least_shifted(inc: Incident, at_least: str | None) -> str | None:
    if not at_least or inc.data_mode != "live":
        return at_least
    try:
        h, m = int(at_least[:2]), int(at_least[3:5])
    except ValueError:
        return at_least
    total = (h * 60 + m + (inc.time_shift_seconds // 60)) % 1440
    return f"{total // 60:02d}:{total % 60:02d}"


def effective_forecast(inc: Incident, snap: dict | None = None) -> dict:
    """Forecast label; recomputed with shifted times for live-mode incidents."""
    if inc.data_mode != "live" and snap is None:
        return inc.forecast_label_i18n or forecast_label(inc.duration_type, inc.duration_from, inc.duration_to, inc.duration_at_least)
    src = snap or {}
    dtype = src.get("duration_type", inc.duration_type)
    d_from = src.get("duration_from") if snap else inc.duration_from
    d_to = src.get("duration_to") if snap else inc.duration_to
    if isinstance(d_from, str):
        d_from = datetime.fromisoformat(d_from)
    if isinstance(d_to, str):
        d_to = datetime.fromisoformat(d_to)
    at_least = src.get("duration_at_least") if snap else inc.duration_at_least
    if inc.data_mode != "live":
        return forecast_label(dtype, d_from, d_to, at_least)
    return forecast_label(dtype, shifted(inc, d_from), shifted(inc, d_to), _at_least_shifted(inc, at_least))


BUS_SUFFIX = {"en": "🚌 Replacement bus", "de": "🚌 Bahnersatz", "fr": "🚌 Bus de remplacement", "it": "🚌 Bus sostitutivo"}


def update_text(inc: Incident, u: IncidentUpdate, lang: str) -> str:
    if inc.data_mode != "live" or u.source == "SIMULATION" or not u.snapshot:
        return shift_text(inc, pick(u.summary_i18n, lang))
    snap = u.snapshot
    parts = [pick(snap.get("subtitle_i18n") or {}, lang, inc.title)]
    if snap.get("status") != "RESUMED":
        parts.append(pick(effective_forecast(inc, snap), lang))
    if snap.get("has_replacement_bus"):
        parts.append(BUS_SUFFIX[lang])
    return " — ".join(p for p in parts if p)


def _visible_cutoff(inc: Incident) -> datetime | None:
    return inc.replay_cursor if inc.data_mode == "live" else None


def reason_i18n(inc: Incident) -> dict:
    if inc.reason is None:
        return {lang: "—" for lang in LANGS}
    r = inc.reason
    return {"en": r.reason_en, "de": r.reason_de, "fr": r.reason_fr, "it": r.reason_it}


def to_summary(inc: Incident, lang: str, now: datetime | None = None, pending: bool = False) -> IncidentSummaryDto:
    lang = norm_lang(lang)
    upd = inc.updated_at
    geo = dict(inc.map_geometry or {})
    return IncidentSummaryDto(
        id=inc.id, external_id=inc.external_id, status=inc.status,
        status_label=STATUS_LABELS[inc.status][lang], canton=inc.canton, title=inc.title,
        subtitle=pick(inc.subtitle_i18n, lang, inc.title), reason=pick(reason_i18n(inc), lang, "—"),
        reason_code=inc.reason_id, duration_type=inc.duration_type,
        forecast_label=pick(effective_forecast(inc), lang),
        forecast_warning=inc.status != "RESUMED" and is_uncertain(inc.duration_type),
        has_replacement_bus=inc.has_replacement_bus, affected_lines=list(inc.affected_lines or []),
        current_version=inc.current_version, updated_at=upd, updated_ago=updated_ago(upd, lang, now),
        map_geometry=MapGeometry.model_validate(geo), quick_routes=list(inc.quick_routes or []),
        recommended_action=shift_text(inc, pick(inc.recommended_action_i18n, lang)),
        detour_summary=shift_text(inc, pick(inc.detour_summary_i18n, lang)),
        data_mode=inc.data_mode, sources=list(inc.sources or []), has_pending_updates=pending,
    )


async def refresh_live_projection(db: AsyncSession) -> None:
    """Keep the live demo "today": re-anchor live incidents whose latest update is older than 6 h."""
    now = datetime.now(timezone.utc)
    rows = (await db.execute(select(Incident).where(Incident.data_mode == "live"))).scalars().all()
    changed = False
    for n, inc in enumerate(sorted(rows, key=lambda i: i.id)):
        if inc.replay_cursor and now - inc.updated_at > LIVE_STALE_AFTER:
            inc.time_shift_seconds = aligned_shift(now, inc.replay_cursor, 5 * n)
            inc.updated_at = shifted(inc, inc.replay_cursor)
            changed = True
    if changed:
        await db.commit()


async def _pending_ids(db: AsyncSession, ids: list[int]) -> set[int]:
    if not ids:
        return set()
    q = select(IncidentUpdate.incident_id).where(IncidentUpdate.incident_id.in_(ids), IncidentUpdate.is_applied.is_(False)).distinct()
    return set((await db.execute(q)).scalars().all())


STATUS_ORDER = {"ACTIVE": 0, "RESTRICTED": 1, "RESUMED": 2}


async def list_incidents(db: AsyncSession, lang: str, mode: str, status: str = "ALL", has_bus: bool | None = None,
                         search: str | None = None, date: str | None = None, limit: int = 300) -> DashboardResponseDto:
    lang = norm_lang(lang)
    mode = mode if mode in ("live", "dataset") else "live"
    if mode == "live":
        await refresh_live_projection(db)
    base = [Incident.data_mode == mode]
    local_date = cast(func.timezone("Europe/Zurich", Incident.started_at), Date)
    if date and mode == "dataset":
        try:
            base.append(local_date == datetime.strptime(date, "%Y-%m-%d").date())
        except ValueError:
            raise HTTPException(422, "date must be YYYY-MM-DD")

    all_rows = (await db.execute(select(Incident).where(*base))).scalars().all()
    kpis = KpiDto(
        active=sum(1 for i in all_rows if i.status in ("ACTIVE", "RESTRICTED")),
        replacement_bus=sum(1 for i in all_rows if i.has_replacement_bus),
        uncertain_forecast=sum(1 for i in all_rows if i.status != "RESUMED" and is_uncertain(i.duration_type)),
        resumed=sum(1 for i in all_rows if i.status == "RESUMED"),
    )

    conds = list(base)
    st = (status or "ALL").upper()
    if st in ("ACTIVE", "RESTRICTED", "RESUMED"):
        conds.append(Incident.status == st)
    elif st == "BUS":
        has_bus = True
    if has_bus is not None:
        conds.append(Incident.has_replacement_bus.is_(has_bus))
    if search and search.strip():
        q = f"%{search.strip()}%"
        conds.append(or_(
            Incident.title.ilike(q), Incident.canton.ilike(q),
            func.array_to_string(Incident.affected_lines, " ").ilike(q),
            func.array_to_string(Incident.affected_stations, " ").ilike(q),
            Incident.subtitle_i18n[lang].astext.ilike(q), Incident.external_id.ilike(q),
        ))
    rows = (await db.execute(select(Incident).where(*conds))).scalars().all()
    if mode == "live":
        rows = sorted(rows, key=lambda i: (STATUS_ORDER[i.status], -i.updated_at.timestamp()))
    else:
        rows = sorted(rows, key=lambda i: -(i.started_at or i.updated_at).timestamp())
    rows = rows[:limit]
    pending = await _pending_ids(db, [r.id for r in rows])
    now = datetime.now(timezone.utc)

    dates: list[str] = []
    if mode == "dataset":
        dq = select(local_date).where(Incident.data_mode == "dataset").distinct().order_by(local_date)
        dates = [d.isoformat() for d in (await db.execute(dq)).scalars().all() if d]
    return DashboardResponseDto(
        kpis=kpis, incidents=[to_summary(r, lang, now, r.id in pending) for r in rows],
        total_count=len(all_rows), filtered_count=len(rows), mode=mode, lang=lang, available_dates=dates,
    )


async def get_incident(db: AsyncSession, incident_id: int) -> Incident:
    inc = await db.get(Incident, incident_id)
    if inc is None:
        raise HTTPException(404, f"Incident {incident_id} not found")
    return inc


async def applied_updates(db: AsyncSession, inc: Incident) -> list[IncidentUpdate]:
    q = select(IncidentUpdate).where(IncidentUpdate.incident_id == inc.id, IncidentUpdate.is_applied.is_(True)).order_by(IncidentUpdate.version)
    return list((await db.execute(q)).scalars().all())


def _timeline_dto(inc: Incident, u: IncidentUpdate, lang: str) -> TimelineEntryDto:
    ts = shifted(inc, u.published_at)
    return TimelineEntryDto(version=u.version, label=f"v{u.version}", published_at=ts,
                            time=ts.astimezone(TZ).strftime("%H:%M") if inc.data_mode == "live" else ts.astimezone(TZ).strftime("%d.%m. %H:%M"),
                            text=update_text(inc, u, lang), source=u.source, status=u.status)


async def timeline(db: AsyncSession, incident_id: int, lang: str) -> list[TimelineEntryDto]:
    inc = await get_incident(db, incident_id)
    return [_timeline_dto(inc, u, norm_lang(lang)) for u in await applied_updates(db, inc)]


async def visible_logs(db: AsyncSession, inc: Incident, limit: int | None = None) -> list[OperationalLog]:
    conds = [OperationalLog.incident_id == inc.id]
    cutoff = _visible_cutoff(inc)
    if cutoff:
        conds.append(OperationalLog.timestamp <= cutoff)
    q = select(OperationalLog).where(*conds).order_by(OperationalLog.timestamp.desc())
    if limit:
        q = q.limit(limit)
    return list((await db.execute(q)).scalars().all())


async def visible_messages(db: AsyncSession, inc: Incident) -> list[IncidentMessage]:
    conds = [IncidentMessage.incident_id == inc.id]
    cutoff = _visible_cutoff(inc)
    if cutoff:
        conds.append(IncidentMessage.published_at <= cutoff)
    q = select(IncidentMessage).where(*conds).order_by(IncidentMessage.published_at)
    return list((await db.execute(q)).scalars().all())


async def get_detail(db: AsyncSession, incident_id: int, lang: str) -> IncidentDetailDto:
    lang = norm_lang(lang)
    inc = await get_incident(db, incident_id)
    pending = bool(await _pending_ids(db, [inc.id]))
    updates = await applied_updates(db, inc)
    msgs = await visible_messages(db, inc)
    logs = await visible_logs(db, inc)
    snapshot = updates[-1].snapshot if updates else {}
    summary = to_summary(inc, lang, pending=pending)
    fmt = "%H:%M" if inc.data_mode == "live" else "%d.%m. %H:%M"
    return IncidentDetailDto(
        **summary.model_dump(),
        description=shift_text(inc, pick(snapshot.get("description_i18n") or {}, lang)),
        audit_timeline=[_timeline_dto(inc, u, lang) for u in updates],
        messages=[IncidentMessageDto(title=shift_text(inc, pick(m.title_i18n, lang)), body=shift_text(inc, pick(m.body_i18n, lang)),
                                     published_at=shifted(inc, m.published_at),
                                     time=shifted(inc, m.published_at).astimezone(TZ).strftime(fmt)) for m in msgs],
        operational_summary=OperationalSummaryDto(
            total_entries=len(logs), sources=sorted({l.source_type for l in logs}),
            latest=[{"time": shifted(inc, l.timestamp).astimezone(TZ).strftime(fmt), "source": l.source_type,
                     "text": shift_text(inc, l.sanitized_message)} for l in logs[:8]],
            correlation=(inc.source_refs or {}).get("correlation", []),
            note="Internal entries are sanitized: staff names, phone numbers, e-mails, staff ids and internal links are removed. "
                 "KULT entries are linked by a time/reason/location heuristic (see correlation.confidence).",
        ),
        source_refs=inc.source_refs or {},
    )


# ----------------------------------------------------------------------------- simulation / replay
def _apply_snapshot(inc: Incident, snap: dict) -> None:
    for key in ("status", "title", "subtitle_i18n", "reason_id", "duration_type", "duration_at_least", "forecast_label_i18n",
                "has_replacement_bus", "affected_lines", "affected_stations", "canton", "map_geometry", "quick_routes",
                "recommended_action_i18n", "detour_summary_i18n"):
        if key in snap:
            setattr(inc, key, snap[key])
    for key in ("duration_from", "duration_to"):
        if key in snap:
            setattr(inc, key, datetime.fromisoformat(snap[key]) if snap[key] else None)


def _synthetic_update(inc: Incident, now: datetime) -> tuple[dict, dict] | None:
    """A realistic next operational step when the real history is exhausted."""
    if inc.status == "RESUMED":
        return None
    stations = list(inc.affected_stations or [])
    a, b = (stations[0], stations[-1]) if len(stations) >= 2 else (stations[0] if stations else inc.title, inc.title)
    snap: dict = {}
    if inc.status == "ACTIVE" and not inc.has_replacement_bus:
        geo = dict(inc.map_geometry or {})
        track = geo.get("track") or ([geo["from"], geo["to"]] if geo.get("from") and geo.get("to") else None)
        if not geo.get("detours") and track:
            # replacement buses run along the disrupted corridor: draw the (rail-routed) track as the bus line
            geo["detours"] = [{"label": f"{a} → {b}", "kind": "bus", "via": [], "from": a, "to": b, "route": track,
                               "source": geo.get("trackSource", "straight"), "approximate": geo.get("trackSource") != "rail",
                               "lengthKm": geo.get("trackLengthKm")}]
        if not geo.get("detourRoute") and track:
            geo["detourRoute"] = track[:: max(1, len(track) // 70)] if len(track) > 70 else track
        snap = {"has_replacement_bus": True, "map_geometry": geo,
                "detour_summary_i18n": {l: BUS_DETOUR[l].format(a=a, b=b) for l in LANGS},
                "recommended_action_i18n": {l: BUS_DETOUR[l].format(a=a, b=b) for l in LANGS}}
        text = {"en": f"TCC update: replacement buses (Bahnersatz) now operating between {a} and {b}.",
                "de": f"Update TCC: Bahnersatzbusse verkehren jetzt zwischen {a} und {b}.",
                "fr": f"Mise à jour TCC : des bus de remplacement circulent désormais entre {a} et {b}.",
                "it": f"Aggiornamento TCC: bus sostitutivi (Bahnersatz) ora in servizio tra {a} e {b}."}
    elif is_uncertain(inc.duration_type):
        to = (now + timedelta(minutes=75)).replace(second=0, microsecond=0)
        to = to - timedelta(minutes=to.minute % 15)
        to_src = unshift_ts(to, inc.time_shift_seconds or 0)  # stored in source time; shifted on read
        snap = {"duration_type": "UNTIL_APPROX_DATE_TIME", "duration_to": to_src.isoformat(), "duration_from": None,
                "duration_at_least": None, "forecast_label_i18n": forecast_label("UNTIL_APPROX_DATE_TIME", None, to_src, None)}
        hhmm = to.astimezone(TZ).strftime("%H:%M")
        text = {"en": f"Forecast refined: service expected to resume at approx. {hhmm}.",
                "de": f"Prognose präzisiert: Wiederaufnahme voraussichtlich ca. {hhmm}.",
                "fr": f"Prévision affinée : reprise prévue vers {hhmm}.",
                "it": f"Previsione aggiornata: ripresa prevista verso le {hhmm}."}
    else:
        snap = {"status": "RESUMED", "duration_type": "ENDED", "has_replacement_bus": False,
                "forecast_label_i18n": forecast_label("ENDED", None, None, None), "recommended_action_i18n": RESUMED_ACTION,
                "subtitle_i18n": {l: f"{STATUS_LABELS['RESUMED'][l]}: {inc.title}" for l in LANGS}}
        text = {l: f"{STATUS_LABELS['RESUMED'][l]}: {inc.title}" for l in LANGS}
    return snap, text


async def simulate_update(db: AsyncSession, incident_id: int, lang: str) -> tuple[Incident, bool, str, str]:
    lang = norm_lang(lang)
    inc = await get_incident(db, incident_id)
    now = datetime.now(timezone.utc)
    q = (select(IncidentUpdate).where(IncidentUpdate.incident_id == inc.id, IncidentUpdate.is_applied.is_(False))
         .order_by(IncidentUpdate.version).limit(1))
    nxt = (await db.execute(q)).scalar_one_or_none()
    if nxt is not None:
        _apply_snapshot(inc, nxt.snapshot or {})
        nxt.is_applied = True
        inc.current_version = nxt.version
        inc.replay_cursor = nxt.published_at
        inc.time_shift_seconds = aligned_shift(now, nxt.published_at)
        inc.updated_at = shifted(inc, nxt.published_at)
        source, summary = f"REPLAY:{nxt.source}", update_text(inc, nxt, lang)
    else:
        synth = _synthetic_update(inc, now)
        if synth is None:
            return inc, False, "NONE", {"en": "No further updates: service has already resumed.",
                                        "de": "Keine weiteren Updates: Der Betrieb läuft wieder.",
                                        "fr": "Aucune mise à jour : le trafic est déjà rétabli.",
                                        "it": "Nessun aggiornamento: la circolazione è già ripristinata."}[lang]
        snap, text = synth
        _apply_snapshot(inc, snap)
        version = inc.current_version + 1
        src_ts = unshift_ts(now, inc.time_shift_seconds or 0)
        full_snapshot = {**snap, "status": inc.status, "title": inc.title}
        db.add(IncidentUpdate(incident_id=inc.id, version=version, published_at=src_ts, summary_i18n=text,
                              source="SIMULATION", status=inc.status, snapshot=full_snapshot, is_applied=True))
        inc.current_version = version
        if inc.data_mode == "live":
            inc.replay_cursor = src_ts
        inc.updated_at = now
        source, summary = "SIMULATION", text[lang]
    await db.commit()
    await db.refresh(inc)
    log.info("incident updated", extra={"incident_id": inc.id, "version": inc.current_version, "source": source})
    return inc, True, source, summary


async def reset_live(db: AsyncSession) -> int:
    """Rewind every live incident to the version it started with (for repeated demos)."""
    rows = (await db.execute(select(Incident).where(Incident.data_mode == "live"))).scalars().all()
    now = datetime.now(timezone.utc)
    for n, inc in enumerate(sorted(rows, key=lambda i: i.id)):
        initial = ((inc.source_refs or {}).get("replay") or {}).get("initial_version", 1)
        await db.execute(delete(IncidentUpdate).where(IncidentUpdate.incident_id == inc.id, IncidentUpdate.source == "SIMULATION"))
        await db.execute(update(IncidentUpdate).where(IncidentUpdate.incident_id == inc.id)
                         .values(is_applied=IncidentUpdate.version <= initial))
        snap_row = (await db.execute(select(IncidentUpdate).where(
            and_(IncidentUpdate.incident_id == inc.id, IncidentUpdate.version == initial)))).scalar_one_or_none()
        if snap_row:
            _apply_snapshot(inc, snap_row.snapshot or {})
            inc.replay_cursor = snap_row.published_at
            inc.time_shift_seconds = aligned_shift(now, snap_row.published_at, 5 * n)
            inc.updated_at = shifted(inc, snap_row.published_at)
        inc.current_version = initial
    await db.commit()
    return len(rows)
