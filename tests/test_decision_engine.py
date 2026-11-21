import asyncio
from types import SimpleNamespace

import pytest

from app.services import decision_engine as de
from app.services.gemini_service import AssistantDecisionSchema, TripContextPatch


def make_incident(**kw):
    base = dict(id=1, title="Genève – Annemasse", status="ACTIVE", duration_type="UNKNOWN", has_replacement_bus=False,
                affected_lines=["RE33", "RL1"], affected_stations=["Genève", "Annemasse"], canton="GE / FR",
                map_geometry={"detourRoute": [[46.2, 6.14], [46.19, 6.23]]}, quick_routes=["Genève → Annemasse"],
                recommended_action_i18n={"en": "Use tram 12."}, detour_summary_i18n={"en": "Tram 12 via Moillesulaz."},
                current_version=2)
    base.update(kw)
    return SimpleNamespace(**base)


def ctx(inc=None, lang="en", trip=None):
    return de.DecisionContext(incident=inc or make_incident(), lang=lang, trip_context=trip or {}, reason="Engineering work",
                              forecast="End not yet known", recommended_action="Use tram 12.",
                              detour_summary="Tram 12 via Moillesulaz.",
                              updates=[{"version": 1, "time": "07:10", "text": "Interrupted", "source": "PUBLIC"}])


@pytest.mark.parametrize("delay,pct", [(30, 0), (60, 25), (119, 25), (120, 50), (300, 50), (None, None)])
def test_compensation_thresholds(delay, pct):
    assert de.RefundCompensationStrategy.compensation_pct(delay) == pct


def test_uncertain_forecast_guardrail_and_detour_payload():
    r = de.STRATEGIES["missed_connection"].build(ctx())
    assert r.forecast_warning is True
    assert any(w["type"] == "forecast_warning" for w in r.widgets)
    assert r.show_detour_map and any(w["type"] == "detour_map" and w["route"] for w in r.widgets)
    assert any("intermediate station" in s for s in r.steps)


def test_supersaver_rule_and_localized_links():
    r = de.STRATEGIES["missed_connection"].build(ctx(lang="de", trip={"ticket_type": "SUPERSAVER"}))
    assert "Sparbillett" in r.steps[0]
    link = next(w for w in r.widgets if w["type"] == "sbb_link")
    assert link["url"].startswith("https://www.sbb.ch/de")


def test_refund_strategy_uses_trip_delay():
    r = de.STRATEGIES["refund_compensation"].build(ctx(trip={"expected_delay_minutes": 75}))
    assert any("25%" in s for s in r.steps) and any("50%" in s for s in r.steps)
    assert any("75 min" in s for s in r.steps)
    assert r.widgets[-1]["kind"] == "refund"


def test_fallback_nlu():
    assert de.detect_intent("Can I get a refund?") == "refund_compensation"
    assert de.detect_intent("Gibt es einen Bahnersatz Bus?") == "detour_route"
    patch = de.extract_trip_context("Heading to Lausanne from Genève with a Supersaver, delay 70 min, my flight is soon")
    assert patch["ticket_type"] == "SUPERSAVER" and patch["urgency"] == "HIGH" and patch["expected_delay_minutes"] == 70
    assert patch["destination"] == "Lausanne" and patch["origin"] == "Genève"


class FakeGemini:
    def __init__(self, out):
        self.out = out

    async def generate_decision(self, **kw):
        return self.out


def run(engine, c, scenario, text=None):
    return asyncio.run(engine.decide(c, scenario, text))


def test_engine_falls_back_without_gemini():
    engine = de.HybridDecisionEngine()
    engine.gemini = FakeGemini(None)
    r = run(engine, ctx(), "status_timeline")
    assert r.engine == "deterministic" and r.steps


def test_engine_accepts_valid_gemini_output():
    out = AssistantDecisionSchema(intent="detour_route", decision_title="Take tram 12", steps=["Walk to tram 12.", "Check the SBB timetable."],
                                  summary_text="Tram 12 is your best option.", trip_context_patch=TripContextPatch(destination="Annemasse"))
    engine = de.HybridDecisionEngine()
    engine.gemini = FakeGemini(out)
    r = run(engine, ctx(), None, "How do I get to Annemasse?")
    assert r.engine == "gemini" and r.title == "Take tram 12"
    assert r.trip_context_patch["destination"] == "Annemasse"
    assert any(w["type"] == "sbb_link" for w in r.widgets)  # widgets always deterministic


def test_engine_rejects_invented_departure_times():
    out = AssistantDecisionSchema(intent="missed_connection", decision_title="Next train", steps=["Take the IR at 08:47 from platform 3."],
                                  summary_text="Hurry.", trip_context_patch=TripContextPatch())
    engine = de.HybridDecisionEngine()
    engine.gemini = FakeGemini(out)
    r = run(engine, ctx(), "missed_connection")
    assert r.engine == "deterministic"


def test_engine_rejects_refund_without_policy_numbers():
    out = AssistantDecisionSchema(intent="refund_compensation", decision_title="Refund", steps=["You get some money back."],
                                  summary_text="ok", trip_context_patch=TripContextPatch())
    engine = de.HybridDecisionEngine()
    engine.gemini = FakeGemini(out)
    assert run(engine, ctx(), "refund_compensation").engine == "deterministic"
