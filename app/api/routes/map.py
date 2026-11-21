"""Map API: GeoJSON FeatureCollections for the Leaflet map."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.services import map_service

router = APIRouter(tags=["map"])

LangQ = Query("en", pattern="(?i)^(en|de|fr|it)$", description="Language of titles and labels")


@router.get("/api/map", summary="GeoJSON for all incidents of the current dashboard filter")
async def map_overview(
    lang: str = LangQ,
    mode: Literal["live", "dataset"] = "live",
    status: Literal["ALL", "ACTIVE", "RESTRICTED", "RESUMED", "BUS"] = "ALL",
    has_replacement_bus: bool | None = Query(None, alias="hasReplacementBus"),
    search: str | None = Query(None, max_length=100),
    date: str | None = Query(None, description="Dataset mode only: YYYY-MM-DD"),
    detours: bool = Query(False, description="Also include every incident's detour route"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Per incident: a ``segment`` LineString (colour = status), an ``incident`` marker with id, title, status,
    affected lines, version and bounds, and optionally a ``detour`` LineString. Coordinates are GeoJSON ``[lon, lat]``.
    ``meta.unmappedIds`` lists incidents without coordinates."""
    return await map_service.overview(db, lang, mode, status, has_replacement_bus, search, date, detours)


@router.get("/api/incidents/{incident_id}/map", summary="GeoJSON for one incident incl. stations and detour")
async def incident_map(incident_id: int, lang: str = LangQ, db: AsyncSession = Depends(get_db)) -> dict:
    return await map_service.incident_map(db, incident_id, lang)
