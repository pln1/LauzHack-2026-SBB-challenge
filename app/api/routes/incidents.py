"""Incident endpoints."""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.dtos import DashboardResponseDto, IncidentDetailDto, SimulateUpdateResponse, TimelineEntryDto
from app.services import incident_service as svc

router = APIRouter(prefix="/api/incidents", tags=["incidents"])

LangQ = Query("en", pattern="(?i)^(en|de|fr|it)$", description="Response language")


@router.get("", response_model=DashboardResponseDto, response_model_by_alias=True)
async def list_incidents(
    lang: str = LangQ,
    mode: Literal["live", "dataset"] = "live",
    status: Literal["ALL", "ACTIVE", "RESTRICTED", "RESUMED", "BUS"] = "ALL",
    has_replacement_bus: bool | None = Query(None, alias="hasReplacementBus"),
    search: str | None = Query(None, max_length=100, description="Station, canton, line or title"),
    date: str | None = Query(None, description="Dataset mode only: YYYY-MM-DD (Europe/Zurich)"),
    limit: int = Query(300, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    return await svc.list_incidents(db, lang, mode, status, has_replacement_bus, search, date, limit)


@router.post("/live/reset", tags=["demo"])
async def reset_live(db: AsyncSession = Depends(get_db)) -> dict:
    """Rewind the live demo incidents to their starting version."""
    return {"reset": await svc.reset_live(db)}


@router.get("/{incident_id}", response_model=IncidentDetailDto, response_model_by_alias=True)
async def get_incident(incident_id: int, lang: str = LangQ, db: AsyncSession = Depends(get_db)):
    return await svc.get_detail(db, incident_id, lang)


@router.get("/{incident_id}/timeline", response_model=list[TimelineEntryDto], response_model_by_alias=True)
async def get_timeline(incident_id: int, lang: str = LangQ, db: AsyncSession = Depends(get_db)):
    return await svc.timeline(db, incident_id, lang)


@router.post("/{incident_id}/simulate-update", response_model=SimulateUpdateResponse, response_model_by_alias=True)
async def simulate_update(incident_id: int, lang: str = LangQ, db: AsyncSession = Depends(get_db)):
    """Advance the incident: replay the next real audit snapshot if one is queued, otherwise apply a
    realistic operational step (Bahnersatz deployed -> forecast refined -> service resumed)."""
    inc, applied, source, summary = await svc.simulate_update(db, incident_id, lang)
    detail = await svc.get_detail(db, inc.id, lang)
    return SimulateUpdateResponse(incident=detail, applied=applied, source=source, change_summary=summary)
