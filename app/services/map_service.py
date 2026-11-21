"""GeoJSON for the map: incident markers, disruption segments, station points and detour routes.

Internally geometry is stored as ``[lat, lon]`` pairs (Leaflet order). GeoJSON uses ``[lon, lat]``, so every
coordinate is converted here. Incidents without usable coordinates are never dropped silently: they are
counted and listed under ``meta.unmappedIds`` so the UI can say so.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rail import dist_m, simplify
from app.schemas.dtos import IncidentDetailDto, IncidentSummaryDto
from app.services import incident_service as isvc

STATUS_COLORS = {"ACTIVE": "#EB0000", "RESTRICTED": "#F59E0B", "RESUMED": "#10B981"}
DETOUR_COLOR = "#059669"
SWITZERLAND_BOUNDS = [[45.80, 5.90], [47.85, 10.55]]  # [[south, west], [north, east]]


def _valid(p: Any) -> bool:
    return (isinstance(p, (list, tuple)) and len(p) == 2 and all(isinstance(v, (int, float)) and v == v and abs(v) < 1000 for v in p)
            and -90 <= p[0] <= 90 and -180 <= p[1] <= 180)


def valid_points(points: list | None) -> list[list[float]]:
    return [[float(p[0]), float(p[1])] for p in (points or []) if _valid(p)]


def _lonlat(p: list[float]) -> list[float]:
    return [round(p[1], 6), round(p[0], 6)]


def bounds_of(points: list[list[float]], pad: float = 0.0) -> list[list[float]] | None:
    """[[south, west], [north, east]] around the points, or None."""
    if not points:
        return None
    lats, lons = [p[0] for p in points], [p[1] for p in points]
    return [[round(min(lats) - pad, 5), round(min(lons) - pad, 5)], [round(max(lats) + pad, 5), round(max(lons) + pad, 5)]]


def _midpoint(points: list[list[float]]) -> list[float]:
    """Point halfway along the path length (so the marker sits ON the track, not beside a curve)."""
    if len(points) == 1:
        return points[0]
    seg = [dist_m((a[0], a[1]), (b[0], b[1])) for a, b in zip(points, points[1:])]
    half, run = sum(seg) / 2, 0.0
    for (a, b), w in zip(zip(points, points[1:]), seg):
        if run + w >= half and w > 0:
            t = (half - run) / w
            return [a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])]
        run += w
    return points[-1]


def _thin_for_overview(points: list[list[float]], limit: int = 60) -> list[list[float]]:
    tol = 30.0
    pts = points
    while len(pts) > limit and tol < 5000:
        pts = [list(p) for p in simplify([(p[0], p[1]) for p in points], tol)]
        tol *= 1.8
    return pts


def _feature(geometry: dict, **props: Any) -> dict:
    return {"type": "Feature", "geometry": geometry, "properties": props}


def _point(p: list[float], **props: Any) -> dict:
    return _feature({"type": "Point", "coordinates": _lonlat(p)}, **props)


def _line(pts: list[list[float]], **props: Any) -> dict:
    return _feature({"type": "LineString", "coordinates": [_lonlat(p) for p in pts]}, **props)


def _base_props(inc: IncidentSummaryDto | IncidentDetailDto) -> dict:
    return {"id": inc.id, "title": inc.title, "status": inc.status, "statusLabel": inc.status_label,
            "color": STATUS_COLORS.get(inc.status, "#64748B"), "affectedLines": list(inc.affected_lines),
            "version": inc.current_version, "hasReplacementBus": inc.has_replacement_bus, "canton": inc.canton,
            "forecastLabel": inc.forecast_label, "updatedAgo": inc.updated_ago}


def _track(geo) -> list[list[float]]:
    """The line to draw for the disrupted section: real track if known, else the straight station-to-station line."""
    t = valid_points(getattr(geo, "track", None))
    return t if len(t) >= 2 else valid_points(geo.waypoints)


def _detours(geo, inc, detailed: bool) -> list[dict]:
    out = []
    for i, d in enumerate(getattr(geo, "detours", None) or []):
        route = valid_points(d.get("route"))
        if len(route) < 2:
            continue
        if not detailed:
            route = _thin_for_overview(route)
        out.append(_line(route, kind="detour", index=i, color=DETOUR_COLOR, incidentId=inc.id, title=inc.title,
                         label=d.get("label", ""), detourKind=d.get("kind", "via"), via=d.get("via") or [],
                         approximate=bool(d.get("approximate")), lengthKm=d.get("lengthKm"), source=d.get("source", ""),
                         summary=inc.detour_summary, bounds=bounds_of(route, 0.02)))
    if out:
        return out
    detour = valid_points(geo.detour_route)  # older rows without the detours list
    if len(detour) >= 2:
        out.append(_line(detour, kind="detour", index=0, color=DETOUR_COLOR, incidentId=inc.id, title=inc.title, label="",
                         detourKind="via", via=[], approximate=True, lengthKm=None, source="straight",
                         summary=inc.detour_summary, bounds=bounds_of(detour, 0.02)))
    return out


def incident_features(inc: IncidentSummaryDto | IncidentDetailDto, with_detour: bool, detailed: bool = False) -> list[dict]:
    geo = inc.map_geometry
    pts = valid_points(geo.waypoints)
    if not pts:
        return []
    track = _track(geo)
    if not detailed:
        track = _thin_for_overview(track)
    base = _base_props(inc)
    box = bounds_of(track, 0.02)
    source = getattr(geo, "track_source", "straight")
    approx = bool(geo.approximate) or source != "rail"
    feats = []
    if len(track) >= 2:
        feats.append(_line(track, kind="segment", dashed=inc.status == "RESTRICTED", approximate=approx, trackSource=source,
                           lengthKm=getattr(geo, "track_length_km", None), bounds=box, **base))
    feats.append(_point(_midpoint(track), kind="incident", bounds=box, approximate=approx, trackSource=source, **base))
    if with_detour:
        feats.extend(_detours(geo, inc, detailed))
    return feats


def collection(features: list[dict], **meta: Any) -> dict:
    pts = [[f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0]] for f in features if f["geometry"]["type"] == "Point"]
    return {"type": "FeatureCollection", "features": features,
            "bbox": [min((p[1] for p in pts), default=SWITZERLAND_BOUNDS[0][1]), min((p[0] for p in pts), default=SWITZERLAND_BOUNDS[0][0]),
                     max((p[1] for p in pts), default=SWITZERLAND_BOUNDS[1][1]), max((p[0] for p in pts), default=SWITZERLAND_BOUNDS[1][0])],
            "meta": {"defaultBounds": SWITZERLAND_BOUNDS, **meta}}


async def overview(db: AsyncSession, lang: str, mode: str, status: str, has_bus: bool | None, search: str | None,
                   date: str | None, detours: bool, limit: int = 1000) -> dict:
    data = await isvc.list_incidents(db, lang, mode, status, has_bus, search, date, limit)
    feats: list[dict] = []
    unmapped: list[int] = []
    for inc in data.incidents:
        f = incident_features(inc, with_detour=detours)
        if f:
            feats.extend(f)
        else:
            unmapped.append(inc.id)
    return collection(feats, mode=data.mode, lang=data.lang, incidentCount=len(data.incidents),
                      mappedCount=len(data.incidents) - len(unmapped), unmappedCount=len(unmapped), unmappedIds=unmapped)


async def incident_map(db: AsyncSession, incident_id: int, lang: str) -> dict:
    inc = await isvc.get_detail(db, incident_id, lang)
    feats = incident_features(inc, with_detour=True, detailed=True)
    pts = valid_points(inc.map_geometry.waypoints)
    stations = [s for s in (inc.map_geometry.stations or []) if _valid([s.get("lat"), s.get("lng")])]
    for i, s in enumerate(stations):
        role = "from" if i == 0 else "to" if i == len(stations) - 1 and len(stations) > 1 else "station"
        feats.append(_point([float(s["lat"]), float(s["lng"])], kind="station", role=role, name=s.get("name", ""),
                            incidentId=inc.id, color=STATUS_COLORS.get(inc.status, "#64748B")))
    detour_feats = [f for f in feats if f["properties"].get("kind") == "detour"]
    detour: list[list[float]] = []
    for f in detour_feats:
        detour += [[c[1], c[0]] for c in f["geometry"]["coordinates"]]
    for f in detour_feats:
        cs = f["geometry"]["coordinates"]
        mid = cs[len(cs) // 2]
        feats.append(_point([mid[1], mid[0]], kind="detour_label", incidentId=inc.id, color=DETOUR_COLOR, summary=inc.detour_summary,
                            index=f["properties"]["index"], label=f["properties"].get("label", "")))
    out = collection(feats, incidentId=inc.id, lang=lang, hasCoordinates=bool(pts), hasDetour=bool(detour_feats),
                     trackSource=getattr(inc.map_geometry, "track_source", "straight"),
                     approximate=inc.map_geometry.approximate, version=inc.current_version, detourSummary=inc.detour_summary,
                     bounds=bounds_of(valid_points(inc.map_geometry.track) + pts + detour, 0.03))
    return out
