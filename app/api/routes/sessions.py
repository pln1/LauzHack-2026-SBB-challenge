"""Anonymous decision-assistant sessions."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.schemas.dtos import AssistantTurnDto, ChatMessageDto, CreateSessionRequest, MessageRequest, ScenarioRequest, SessionDto
from app.services import incident_service as isvc
from app.services import session_service as svc

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionDto, response_model_by_alias=True, status_code=201)
async def create_session(body: CreateSessionRequest, db: AsyncSession = Depends(get_db)):
    sess = await svc.create_session(db, body.incident_id, body.lang, body.trip_context)
    inc = await isvc.get_incident(db, sess.incident_id)
    return await svc.session_dto(db, sess, inc, [])


@router.delete("/expired")
async def cleanup_expired(db: AsyncSession = Depends(get_db)) -> dict:
    return {"deleted": await svc.cleanup_expired(db)}


@router.get("/{session_id}", response_model=SessionDto, response_model_by_alias=True)
async def get_session(session_id: uuid.UUID, lang: str | None = Query(None), db: AsyncSession = Depends(get_db)):
    return await svc.get_session_state(db, session_id, lang)


@router.post("/{session_id}/scenario", response_model=AssistantTurnDto, response_model_by_alias=True)
async def run_scenario(session_id: uuid.UUID, body: ScenarioRequest, db: AsyncSession = Depends(get_db)):
    return await svc.run_scenario(db, session_id, body.scenario_slug, body.selected_route, body.trip_context_patch, body.lang)


@router.post("/{session_id}/messages", response_model=AssistantTurnDto, response_model_by_alias=True)
async def post_message(session_id: uuid.UUID, body: MessageRequest, db: AsyncSession = Depends(get_db)):
    return await svc.post_message(db, session_id, body.content, body.selected_route, body.lang)


@router.get("/{session_id}/messages/{message_id}", response_model=ChatMessageDto, response_model_by_alias=True)
async def get_message(session_id: uuid.UUID, message_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """Poll one assistant message: its card carries ``aiPending: true`` until Gemini has refined it."""
    return await svc.get_message(db, session_id, message_id)
