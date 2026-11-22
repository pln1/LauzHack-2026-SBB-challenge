"""Anonymous chat sessions, SYSTEM_ALERT version observer and assistant turns."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.i18n import SCENARIO_SLUGS, STATUS_LABELS, TZ, norm_lang, pick
from app.db.models import ChatMessage, ChatSession, Incident, IncidentUpdate
from app.db.session import get_sessionmaker
from app.schemas.dtos import AssistantTurnDto, ChatMessageDto, SessionDto, TripContext
from app.services import incident_service as isvc
from app.services.decision_engine import DecisionContext, DecisionResult, get_engine
from app.core.legacy_links import upgrade_timetable_url
from app.core.resources import load_resource

log = logging.getLogger("sessions")
_bg_tasks: set[asyncio.Task] = set()  # keeps references so background refinements are not garbage collected

_R = load_resource("session_texts")
ALERT_HEAD = _R["ALERT_HEAD"]
ALERT_TIP = _R["ALERT_TIP"]
ROUTE_MSG = _R["ROUTE_MSG"]
SCENARIO_PROMPTS = _R["SCENARIO_PROMPTS"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _upgraded(widgets: list | None) -> list:
    """Chat messages keep their widgets in the DB: links written by older builds are rewritten to the current sbb.ch format."""
    out = []
    for w in widgets or []:
        if isinstance(w, dict) and w.get("type") == "sbb_link" and w.get("kind") == "timetable" and w.get("url"):
            w = {**w, "url": upgrade_timetable_url(w["url"])}
        out.append(w)
    return out


def msg_dto(m: ChatMessage) -> ChatMessageDto:
    return ChatMessageDto(id=m.id, sender=m.sender, content=m.content, widgets=_upgraded(m.widgets), created_at=m.created_at)


def _ttl() -> timedelta:
    return timedelta(hours=get_settings().session_ttl_hours)


async def create_session(db: AsyncSession, incident_id: int, lang: str, trip: TripContext | None) -> ChatSession:
    inc = await isvc.get_incident(db, incident_id)
    sess = ChatSession(id=uuid.uuid4(), incident_id=inc.id, lang=norm_lang(lang),
                       trip_context=(trip.model_dump(exclude_none=True) if trip else {}),
                       last_seen_incident_version=inc.current_version, created_at=_now(), expires_at=_now() + _ttl())
    db.add(sess)
    await db.commit()
    await db.refresh(sess)
    return sess


async def load_session(db: AsyncSession, session_id: uuid.UUID) -> ChatSession:
    sess = await db.get(ChatSession, session_id)
    if sess is None:
        raise HTTPException(404, "Session not found")
    if sess.expires_at < _now():
        raise HTTPException(410, "Session expired")
    sess.expires_at = _now() + _ttl()  # sliding expiry
    return sess


async def check_version(db: AsyncSession, sess: ChatSession, inc: Incident, lang: str) -> list[ChatMessage]:
    """Observer: if the incident moved on since the passenger last looked, persist a SYSTEM_ALERT."""
    if inc.current_version < sess.last_seen_incident_version:
        sess.last_seen_incident_version = inc.current_version  # live demo was rewound
        return []
    if inc.current_version <= sess.last_seen_incident_version:
        return []
    q = (select(IncidentUpdate).where(IncidentUpdate.incident_id == inc.id, IncidentUpdate.is_applied.is_(True),
                                      IncidentUpdate.version > sess.last_seen_incident_version)
         .order_by(IncidentUpdate.version))
    ups = list((await db.execute(q)).scalars().all())
    lang = norm_lang(lang)
    lines = []
    for u in ups:
        ts = isvc.shifted(inc, u.published_at).astimezone(TZ).strftime("%H:%M")
        lines.append(f"v{u.version} ({ts}): {isvc.update_text(inc, u, lang)}")
    content = f"⚡ {ALERT_HEAD[lang]} — {inc.title}\n" + "\n".join(lines[-3:]) + f"\n{ALERT_TIP[lang]}"
    widgets = [{"type": "system_alert", "fromVersion": sess.last_seen_incident_version, "toVersion": inc.current_version,
                "status": inc.status, "statusLabel": STATUS_LABELS[inc.status][lang],
                "forecast": pick(isvc.effective_forecast(inc), lang), "hasReplacementBus": inc.has_replacement_bus}]
    alert = ChatMessage(id=uuid.uuid4(), session_id=sess.id, sender="SYSTEM_ALERT", content=content, widgets=widgets, created_at=_now())
    db.add(alert)
    sess.last_seen_incident_version = inc.current_version
    log.info("system alert", extra={"session": str(sess.id), "incident_id": inc.id, "version": inc.current_version})
    return [alert]


async def _all_messages(db: AsyncSession, sess: ChatSession) -> list[ChatMessage]:
    q = select(ChatMessage).where(ChatMessage.session_id == sess.id).order_by(ChatMessage.created_at)
    return list((await db.execute(q)).scalars().all())


async def session_dto(db: AsyncSession, sess: ChatSession, inc: Incident, alerts: list[ChatMessage]) -> SessionDto:
    return SessionDto(session_id=sess.id, incident_id=sess.incident_id, lang=sess.lang,
                      trip_context=TripContext.model_validate(sess.trip_context or {}),
                      last_seen_incident_version=sess.last_seen_incident_version, incident_version=inc.current_version,
                      expires_at=sess.expires_at, messages=[msg_dto(m) for m in await _all_messages(db, sess)],
                      new_alerts=[msg_dto(a) for a in alerts])


async def get_session_state(db: AsyncSession, session_id: uuid.UUID, lang: str | None) -> SessionDto:
    sess = await load_session(db, session_id)
    if lang:
        sess.lang = norm_lang(lang)
    inc = await isvc.get_incident(db, sess.incident_id)
    alerts = await check_version(db, sess, inc, sess.lang)
    await db.commit()
    return await session_dto(db, sess, inc, alerts)


async def _decision_context(db: AsyncSession, sess: ChatSession, inc: Incident, exclude_id: uuid.UUID | None = None) -> DecisionContext:
    lang = sess.lang
    fmt = "%H:%M" if inc.data_mode == "live" else "%d.%m. %H:%M"
    updates = await isvc.applied_updates(db, inc)
    logs = await isvc.visible_logs(db, inc, limit=25)
    msgs = await isvc.visible_messages(db, inc)
    chat = [m for m in await _all_messages(db, sess) if m.id != exclude_id]
    return DecisionContext(
        incident=inc, lang=lang, trip_context=dict(sess.trip_context or {}),
        reason=pick(isvc.reason_i18n(inc), lang, "—"), forecast=pick(isvc.effective_forecast(inc), lang),
        recommended_action=isvc.shift_text(inc, pick(inc.recommended_action_i18n, lang)),
        detour_summary=isvc.shift_text(inc, pick(inc.detour_summary_i18n, lang)),
        updates=[{"version": u.version, "time": isvc.shifted(inc, u.published_at).astimezone(TZ).strftime(fmt),
                  "text": isvc.update_text(inc, u, lang), "source": u.source} for u in updates],
        operational_log=[{"time": isvc.shifted(inc, l.timestamp).astimezone(TZ).strftime(fmt), "source": l.source_type,
                          "text": isvc.shift_text(inc, l.sanitized_message)} for l in logs],
        messages=[{"time": isvc.shifted(inc, m.published_at).astimezone(TZ).strftime(fmt), "title": isvc.shift_text(inc, pick(m.title_i18n, lang)),
                   "body": isvc.shift_text(inc, pick(m.body_i18n, lang))[:500]} for m in msgs],
        chat=[{"sender": m.sender, "content": m.content[:400]} for m in chat[-10:]],
    )


def _widgets(result: DecisionResult, inc: Incident, ai_pending: bool = False) -> list[dict]:
    card = {"type": "decision_card", "title": result.title, "steps": result.steps, "canton": inc.canton,
            "scenario": result.scenario, "engine": result.engine, "forecastWarning": result.forecast_warning,
            "showDetourMap": result.show_detour_map}
    if ai_pending:
        card["aiPending"] = True  # the frontend polls this message until Gemini has refined it
    return [card] + result.widgets


def _assistant_message(sess: ChatSession, result: DecisionResult, inc: Incident, ai_pending: bool = False) -> ChatMessage:
    widgets = _widgets(result, inc, ai_pending)
    return ChatMessage(id=uuid.uuid4(), session_id=sess.id, sender="ASSISTANT", content=result.summary_text,
                       widgets=widgets, created_at=_now() + timedelta(milliseconds=2))


async def _run_turn(db: AsyncSession, sess: ChatSession, user_text: str, scenario: str | None,
                    free_text: bool) -> AssistantTurnDto:
    inc = await isvc.get_incident(db, sess.incident_id)
    alerts = await check_version(db, sess, inc, sess.lang)
    user_msg = ChatMessage(id=uuid.uuid4(), session_id=sess.id, sender="USER", content=user_text, widgets=[],
                           created_at=_now() + timedelta(milliseconds=1))
    db.add(user_msg)
    await db.flush()
    ctx = await _decision_context(db, sess, inc)
    engine = get_engine()
    text = user_text if free_text else None
    if get_settings().gemini_mode == "async" and engine.gemini.available:
        # Answer instantly from the SBB rules; Gemini refines this message in the background.
        det, fb = engine.prepare(ctx, scenario, text)
        if fb:
            sess.trip_context = {**(sess.trip_context or {}), **{k: v for k, v in fb.items() if v is not None}}
        assistant = _assistant_message(sess, det, inc, ai_pending=True)
        db.add(assistant)
        await db.commit()
        task = asyncio.create_task(_refine_message(assistant.id, sess.id, scenario, text))
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)
        return AssistantTurnDto(session_id=sess.id, incident_version=inc.current_version, new_alerts=[msg_dto(a) for a in alerts],
                                messages=[msg_dto(user_msg), msg_dto(assistant)], ui_widgets=assistant.widgets,
                                trip_context=TripContext.model_validate(sess.trip_context or {}), engine="deterministic")
    result = await engine.decide(ctx, scenario, text)
    if result.trip_context_patch:
        sess.trip_context = {**(sess.trip_context or {}), **result.trip_context_patch}
    assistant = _assistant_message(sess, result, inc)
    db.add(assistant)
    await db.commit()
    return AssistantTurnDto(session_id=sess.id, incident_version=inc.current_version, new_alerts=[msg_dto(a) for a in alerts],
                            messages=[msg_dto(user_msg), msg_dto(assistant)], ui_widgets=assistant.widgets,
                            trip_context=TripContext.model_validate(sess.trip_context or {}), engine=result.engine)


async def _refine_message(message_id: uuid.UUID, session_id: uuid.UUID, scenario: str | None, text: str | None) -> None:
    """Background: let Gemini rephrase the instant answer. On any failure the rule-based answer simply stays."""
    try:
        async with get_sessionmaker()() as db:
            sess = await db.get(ChatSession, session_id)
            msg = await db.get(ChatMessage, message_id)
            if sess is None or msg is None:
                return
            inc = await isvc.get_incident(db, sess.incident_id)
            ctx = await _decision_context(db, sess, inc, exclude_id=message_id)
            engine = get_engine()
            det, fb = engine.prepare(ctx, scenario, text)
            result = await engine.refine(ctx, det, fb, scenario, text)
            if result.engine == "gemini":
                msg.content = result.summary_text
                msg.widgets = _widgets(result, inc)
                if result.trip_context_patch:
                    sess.trip_context = {**(sess.trip_context or {}), **result.trip_context_patch}
            else:
                widgets = [dict(w) for w in (msg.widgets or [])]
                for w in widgets:
                    if w.get("type") == "decision_card":
                        w.pop("aiPending", None)
                        w["aiFailed"] = True
                msg.widgets = widgets
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("background refinement failed")
        try:
            async with get_sessionmaker()() as db:
                msg = await db.get(ChatMessage, message_id)
                if msg is not None:
                    msg.widgets = [{k: v for k, v in w.items() if k != "aiPending"} for w in (msg.widgets or [])]
                    await db.commit()
        except Exception:  # noqa: BLE001
            log.exception("could not clear aiPending flag")


async def get_message(db: AsyncSession, session_id: uuid.UUID, message_id: uuid.UUID) -> ChatMessageDto:
    await load_session(db, session_id)
    msg = await db.get(ChatMessage, message_id)
    if msg is None or msg.session_id != session_id:
        raise HTTPException(404, "Message not found")
    return msg_dto(msg)


async def run_scenario(db: AsyncSession, session_id: uuid.UUID, slug: str, selected_route: str | None,
                       patch: TripContext | None, lang: str | None) -> AssistantTurnDto:
    if slug not in SCENARIO_SLUGS:
        raise HTTPException(422, f"Unknown scenario '{slug}'. Valid: {sorted(SCENARIO_SLUGS)}")
    sess = await load_session(db, session_id)
    if lang:
        sess.lang = norm_lang(lang)
    tc = dict(sess.trip_context or {})
    if patch:
        tc.update(patch.model_dump(exclude_none=True))
    if selected_route:
        tc["selected_route"] = selected_route
    sess.trip_context = tc
    return await _run_turn(db, sess, SCENARIO_PROMPTS[slug][sess.lang], slug, free_text=False)


async def post_message(db: AsyncSession, session_id: uuid.UUID, content: str, selected_route: str | None,
                       lang: str | None) -> AssistantTurnDto:
    sess = await load_session(db, session_id)
    if lang:
        sess.lang = norm_lang(lang)
    if selected_route:
        sess.trip_context = {**(sess.trip_context or {}), "selected_route": selected_route}
    return await _run_turn(db, sess, content.strip(), None, free_text=True)


async def cleanup_expired(db: AsyncSession) -> int:
    res = await db.execute(delete(ChatSession).where(ChatSession.expires_at < _now()))
    await db.commit()
    return res.rowcount or 0
