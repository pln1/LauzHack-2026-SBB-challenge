"""Localized labels, official sbb.ch links and forecast formatting (EN/DE/FR/IT)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, quote_plus, urlencode
from zoneinfo import ZoneInfo
from app.core.resources import load_resource

LANGS = ("en", "de", "fr", "it")
TZ = ZoneInfo("Europe/Zurich")


def norm_lang(lang: str | None) -> str:
    lang = (lang or "en").lower()[:2]
    return lang if lang in LANGS else "en"


def pick(i18n: dict | None, lang: str, default: str = "") -> str:
    if not i18n:
        return default
    return i18n.get(lang) or i18n.get("en") or i18n.get("de") or next(iter(i18n.values()), default) or default


_R = load_resource("i18n")
STATUS_LABELS = _R["STATUS_LABELS"]

SBB_LINKS = _R["SBB_LINKS"]

LINK_LABELS = _R["LINK_LABELS"]


def timetable_moment(when: datetime | None, now: datetime | None = None) -> tuple[str, str, str] | None:
    """(date to plan, time, original day) to pre-fill on sbb.ch. SBB's planner only accepts today or later, so a past
    moment (dataset incidents belong to Sept 2026) keeps its time of day but is planned for today; a future moment is kept.
    The third value is always the incident's own day."""
    if when is None:
        return None
    local, today = when.astimezone(TZ), (now or datetime.now(TZ)).astimezone(TZ)
    day = local.date() if local.date() >= today.date() else today.date()
    return day.strftime("%Y-%m-%d"), local.strftime("%H:%M"), local.strftime("%Y-%m-%d")


def timetable_query(origin: str, destination: str, when: datetime | None = None, origin_id: str | None = None,
                    destination_id: str | None = None) -> str:
    """Query string in the format sbb.ch writes itself, e.g.
    ``stops=Bern+Wankdorf_I8516161~G%C3%BCmligen_I8507003&date=%222026-11-22%22&time=09_00&moment=dep&day=2026-09-01``.
    Each stop is ``Name_I<UIC>`` (just the name when the id is unknown), joined by ``~``; everything is percent-encoded once."""
    def stop(name: str, uic: str | None) -> str:
        return quote_plus(name) + (f"_I{uic}" if uic else "")
    parts = [f"stops={stop(origin, origin_id)}~{stop(destination, destination_id)}"]
    m = timetable_moment(when)
    if m:
        parts += [f"date={quote(chr(34) + m[0] + chr(34))}", f"time={m[1].replace(':', '_')}", "moment=dep", f"day={m[2]}"]
    return "&".join(parts)


def sbb_link(kind: str, lang: str, origin: str | None = None, destination: str | None = None,
             when: datetime | None = None, origin_id: str | None = None, destination_id: str | None = None) -> dict:
    """Official sbb.ch link. A timetable link carries both stations and the incident's date/time (see ``timetable_moment``)
    so SBB.ch opens on that connection instead of the browser's "now"."""
    lang = norm_lang(lang)
    url = SBB_LINKS[lang][kind]
    if kind == "timetable" and origin and destination:
        url += ("&" if "?" in url else "?") + timetable_query(origin, destination, when, origin_id, destination_id)
    return {"type": "sbb_link", "kind": kind, "url": url, "label": LINK_LABELS[kind][lang]}


UNCERTAIN_DURATIONS = {"UNKNOWN", "UNTIL_AT_LEAST", "UNTIL_AT_LEAST_DATE_TIME", "NONE"}


def is_uncertain(duration_type: str | None) -> bool:
    return (duration_type or "UNKNOWN") in UNCERTAIN_DURATIONS


def _fmt_time(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%H:%M") if dt else "?"


def _fmt_dt(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%d.%m. %H:%M") if dt else "?"


def forecast_label(duration_type: str | None, duration_from: datetime | None, duration_to: datetime | None,
                   at_least: str | None, with_date: bool = False, ref: datetime | None = None) -> dict:
    """Human readable expected end of the disruption, in all four languages.

    Live incidents are replayed "today", so a bare clock time is unambiguous. Dataset incidents belong to their own day
    (Sept 2026): with ``with_date`` every time carries its date (``01.09. 09:00``); ``ref`` supplies the date for an
    "at least until HH:MM" value that has none of its own."""
    d = duration_type or "UNKNOWN"
    t_to, dt_to, t_from = _fmt_time(duration_to), _fmt_dt(duration_to), _fmt_time(duration_from)
    at = (at_least or "")[:5] or t_to
    if with_date:
        t_to, t_from = dt_to, _fmt_dt(duration_from)
        day = next((x for x in (duration_to, duration_from, ref) if x), None)
        if at_least and day:
            at = f"{day.astimezone(TZ).strftime('%d.%m.')} {at}"
        elif not at_least:
            at = dt_to
    table = {k: {l: v.format(t_to=t_to, dt_to=dt_to, t_from=t_from, at=at) for l, v in tpl.items()} for k, tpl in _R["FORECAST_LABELS"].items()}
    if d in ("UNTIL", "FROM_TO", "UNTIL_DATE", "UNTIL_APPROX_DATE_TIME", "UNTIL_AT_LEAST_DATE_TIME") and duration_to is None:
        d = "UNKNOWN"
    if d == "UNTIL_AT_LEAST" and not (at_least or duration_to):
        d = "UNKNOWN"
    return table.get(d, table["UNKNOWN"])


def updated_ago(ts: datetime | None, lang: str, now: datetime | None = None) -> str:
    lang = norm_lang(lang)
    if ts is None:
        return ""
    now = now or datetime.now(timezone.utc)
    delta = now - ts
    if delta < timedelta(seconds=0):
        delta = timedelta(0)
    mins = int(delta.total_seconds() // 60)
    u = _R["UPDATED_AGO"]
    if mins < 1:
        return u["just"][lang]
    if mins < 60:
        return u["minutes"][lang].format(n=mins)
    if mins < 24 * 60:
        return u["hours"][lang].format(n=mins // 60)
    return u["stamp"][lang].format(n=ts.astimezone(TZ).strftime("%d.%m.%Y %H:%M"))


SCENARIOS = _R["SCENARIOS"]
SCENARIO_SLUGS = {s["slug"] for s in SCENARIOS}
