"""Scenario metadata."""
from __future__ import annotations

from fastapi import APIRouter, Query

from app.core.i18n import SCENARIOS, norm_lang, pick
from app.schemas.dtos import ScenarioDto

router = APIRouter(tags=["scenarios"])


@router.get("/api/scenarios", response_model=list[ScenarioDto], response_model_by_alias=True)
async def list_scenarios(lang: str = Query("en")):
    lang = norm_lang(lang)
    return [ScenarioDto(slug=s["slug"], icon=s["icon"], title=pick(s["title"], lang), description=pick(s["description"], lang))
            for s in SCENARIOS]
