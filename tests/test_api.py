"""End-to-end API flow against a real PostgreSQL that has been loaded with scripts/ingest_data.py.

Skipped unless DATABASE_URL points to a reachable, ingested database.
"""
import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

pytestmark = pytest.mark.skipif(not os.environ.get("DATABASE_URL"), reason="needs DATABASE_URL with ingested data")


def client():
    from app.main import app
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def _flow():
    from app.db.session import dispose_engine
    async with client() as c:
        h = (await c.get("/api/health")).json()
        assert h["database"]["ok"] and h["database"]["counts"].get("live", 0) > 0

        reset = await c.post("/api/incidents/live/reset")
        assert reset.status_code == 200

        dash = (await c.get("/api/incidents", params={"mode": "live", "lang": "de"})).json()
        assert dash["totalCount"] >= 1 and set(dash["kpis"]) == {"active", "replacementBus", "uncertainForecast", "resumed"}
        target = next(i for i in dash["incidents"] if i["hasPendingUpdates"])

        detail = (await c.get(f"/api/incidents/{target['id']}", params={"lang": "fr"})).json()
        assert detail["auditTimeline"] and detail["auditTimeline"][-1]["version"] == detail["currentVersion"]
        assert "operationalSummary" in detail

        s = (await c.post("/api/sessions", json={"incidentId": target["id"], "lang": "en"})).json()
        sid = s["sessionId"]
        assert s["lastSeenIncidentVersion"] == detail["currentVersion"]

        turn = (await c.post(f"/api/sessions/{sid}/scenario", json={"scenarioSlug": "refund_compensation"})).json()
        assert [m["sender"] for m in turn["messages"]] == ["USER", "ASSISTANT"]
        assert any(w["type"] == "sbb_link" and "sbb.ch/en" in w["url"] for w in turn["uiWidgets"])

        sim = (await c.post(f"/api/incidents/{target['id']}/simulate-update")).json()
        assert sim["applied"] and sim["incident"]["currentVersion"] == detail["currentVersion"] + 1

        state = (await c.get(f"/api/sessions/{sid}")).json()
        assert len(state["newAlerts"]) == 1 and state["newAlerts"][0]["sender"] == "SYSTEM_ALERT"
        again = (await c.get(f"/api/sessions/{sid}")).json()
        assert again["newAlerts"] == []  # alert is emitted once per version

        msg = (await c.post(f"/api/sessions/{sid}/messages", json={"content": "I travel to Lausanne with a supersaver ticket"})).json()
        assert msg["tripContext"]["ticketType"] == "SUPERSAVER"

        bad = await c.post(f"/api/sessions/{sid}/scenario", json={"scenarioSlug": "nope"})
        assert bad.status_code == 422

        ds = (await c.get("/api/incidents", params={"mode": "dataset", "search": "RE33"})).json()
        assert all("RE33" in " ".join(i["affectedLines"]) or "RE33" in i["title"] for i in ds["incidents"]) or ds["incidents"] == []
        day = (await c.get("/api/incidents", params={"mode": "dataset", "date": "2026-09-01"})).json()
        assert day["filteredCount"] >= 1

        # expiry
        from sqlalchemy import update
        from app.db.models import ChatSession
        from app.db.session import get_sessionmaker
        async with get_sessionmaker()() as db:
            await db.execute(update(ChatSession).where(ChatSession.id == sid).values(expires_at=datetime.now(timezone.utc) - timedelta(minutes=1)))
            await db.commit()
        assert (await c.get(f"/api/sessions/{sid}")).status_code == 410
        assert (await c.delete("/api/sessions/expired")).json()["deleted"] >= 1
        assert (await c.post("/api/incidents/live/reset")).status_code == 200
    await dispose_engine()


def test_full_flow():
    asyncio.run(_flow())
