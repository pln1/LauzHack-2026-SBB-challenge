"""Upgrade timetable links stored by older versions (chat messages keep their widgets in the database).

Older builds wrote sbb.ch links as ``?von=..&nach=..&datum=..&zeit=..`` or ``?stops=[{"value":..,"type":"ID"..}]&date="..."&time="HH:MM"&moment="DEPARTURE"``
(sometimes percent-encoded twice). ``upgrade_timetable_url`` rebuilds them with the one shared builder
(``app.core.i18n.timetable_query``); links already in the current format are returned unchanged."""
from __future__ import annotations

import json
from datetime import datetime
from urllib.parse import parse_qs, unquote, urlsplit

from app.core.i18n import TZ, timetable_query


def _json(v: str):
    try:
        return json.loads(v)
    except ValueError:
        return None


def _moment(date: str | None, time: str | None) -> datetime | None:
    try:
        if date and "." in date:
            d = datetime.strptime(date, "%d.%m.%Y")
        elif date:
            d = datetime.strptime(date, "%Y-%m-%d")
        else:
            return None
        h, m = (time or "00:00").replace("_", ":").split(":")[:2]
        return d.replace(hour=int(h), minute=int(m), tzinfo=TZ)
    except ValueError:
        return None


def upgrade_timetable_url(url: str) -> str:
    parts = urlsplit(url)
    if "sbb.ch" not in parts.netloc or not parts.query:
        return url
    query = unquote(parts.query) if "%25" in parts.query else parts.query        # undo a second round of percent-encoding
    q = {k: v[0] for k, v in parse_qs(query).items()}
    origin = destination = oid = did = None
    if q.get("von") and q.get("nach"):
        origin, destination = q["von"], q["nach"]
        when = _moment(q.get("datum"), q.get("zeit"))
    elif q.get("stops", "").lstrip().startswith("["):
        try:
            a, b = json.loads(q["stops"])[:2]
        except (ValueError, TypeError):
            return url
        origin, destination = a.get("label") or a.get("value"), b.get("label") or b.get("value")
        oid = a.get("value") if a.get("type") == "ID" else None
        did = b.get("value") if b.get("type") == "ID" else None
        when = _moment(_json(q.get("date", "null")) if q.get("date") else None, _json(q.get("time", "null")) if q.get("time") else None)
    else:
        return url
    if not origin or not destination:
        return url
    return f"{parts.scheme}://{parts.netloc}{parts.path}?" + timetable_query(origin, destination, when, oid, did)
