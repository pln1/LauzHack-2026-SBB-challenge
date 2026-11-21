"""Health / readiness."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ChatSession, Incident
from app.db.session import get_db
from app.services.gemini_service import get_gemini_service

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health(db: AsyncSession = Depends(get_db)) -> dict:
    db_ok, counts, error = True, {}, None
    try:
        await db.execute(text("SELECT 1"))
        rows = (await db.execute(select(Incident.data_mode, func.count()).group_by(Incident.data_mode))).all()
        counts = {mode: n for mode, n in rows}
        counts["sessions"] = (await db.execute(select(func.count()).select_from(ChatSession))).scalar_one()
    except Exception as exc:  # noqa: BLE001
        db_ok, error = False, str(exc)[:200]
    gem = get_gemini_service().status()
    return {
        "status": "ok" if db_ok else "degraded",
        "database": {"ok": db_ok, "counts": counts, "error": error},
        "gemini": gem,
        "decisionEngine": "hybrid (gemini + deterministic)" if gem["available"] else "deterministic fallback",
    }
