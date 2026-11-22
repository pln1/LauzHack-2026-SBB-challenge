"""Hybrid Decision Engine: deterministic SBB rules (Strategy pattern) + optional Gemini synthesis.

The deterministic layer always runs. It owns every policy fact (refund %, ticket validity,
forecast warnings, official links, map payload). Gemini, when available, rewrites the plan
into personal, numbered steps using the operational context. Its output is validated with
guardrails and discarded in favour of the deterministic plan if it breaks one.
"""
from __future__ import annotations

import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from app.core.geo import get_station_index
from app.core.i18n import is_uncertain, norm_lang, pick, sbb_link
from app.db.models import Incident
from app.services.gemini_service import AssistantDecisionSchema, get_gemini_service
from app.core.resources import load_resource

log = logging.getLogger("decision")


@dataclass
class DecisionContext:
    incident: Incident
    lang: str
    trip_context: dict
    reason: str
    forecast: str
    recommended_action: str = ""
    detour_summary: str = ""
    updates: list[dict] = field(default_factory=list)  # [{version, time, text, source}]
    operational_log: list[dict] = field(default_factory=list)  # [{time, source, text}]
    messages: list[dict] = field(default_factory=list)  # [{time, title, body}]
    chat: list[dict] = field(default_factory=list)

    @property
    def route(self) -> str:
        tc = self.trip_context
        if tc.get("selected_route"):
            return tc["selected_route"]
        if tc.get("origin") and tc.get("destination"):
            return f"{tc['origin']} → {tc['destination']}"
        return self.incident.title

    @property
    def route_keys(self) -> str:
        tc = self.trip_context
        return " → ".join(x for x in (self.route, tc.get("origin"), tc.get("destination")) if x)

    @property
    def uncertain(self) -> bool:
        return self.incident.status != "RESUMED" and is_uncertain(self.incident.duration_type)

    @property
    def detour_route(self) -> list | None:
        return (self.incident.map_geometry or {}).get("detourRoute") or None


def timetable_link(ctx: DecisionContext) -> dict:
    stations = ctx.incident.affected_stations or []
    origin = ctx.trip_context.get("origin") or (stations[0] if stations else None)
    destination = ctx.trip_context.get("destination") or (stations[-1] if len(stations) > 1 else None)
    ids = {st.get("name"): st.get("uic") for st in ((getattr(ctx.incident, "map_geometry", None) or {}).get("stations") or []) if isinstance(st, dict)}
    return sbb_link("timetable", ctx.lang, origin, destination, when=getattr(ctx.incident, "updated_at", None),
                    origin_id=ids.get(origin), destination_id=ids.get(destination))


@dataclass
class DecisionResult:
    scenario: str
    title: str
    steps: list[str]
    summary_text: str
    widgets: list[dict[str, Any]]
    forecast_warning: bool
    show_detour_map: bool
    policy_facts: list[str] = field(default_factory=list)
    engine: str = "deterministic"
    trip_context_patch: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- i18n templates
_R = load_resource("decision_texts")
T: dict[str, dict[str, str]] = _R["T"]
TITLES = _R["TITLES"]
SUMMARIES = _R["SUMMARIES"]
LINES_WORD = _R["LINES_WORD"]
DETOUR_BTN = _R["DETOUR_BTN"]
WARNING_BADGE = _R["WARNING_BADGE"]


def focus_guidance(text: str, route: str, max_sentences: int = 2) -> str:
    """Keep the guidance sentences that concern the passenger's route (official texts list many OD pairs)."""
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text or "") if x.strip()]
    if len(sentences) <= max_sentences:
        return text
    names = [n.strip().lower() for n in re.split(r"→|->| - | – ", route or "") if n.strip()]
    keys = {n.split("/")[0].split(" ")[0] for n in names if len(n) >= 3}
    relevant = [x for x in sentences if any(k in x.lower() for k in keys)]
    return " ".join((relevant or sentences)[:max_sentences])


def tr(key: str, lang: str, **kw) -> str:
    return T[key][lang].format(**kw)


def _forecast_warning_widget(ctx: DecisionContext) -> dict:
    return {"type": "forecast_warning", "level": "warning", "text": WARNING_BADGE[ctx.lang], "durationType": ctx.incident.duration_type}


def _detour_widget(ctx: DecisionContext) -> dict:
    return {"type": "detour_map", "label": DETOUR_BTN[ctx.lang], "route": ctx.detour_route,
            "summary": ctx.detour_summary}


def _route_chips(ctx: DecisionContext) -> dict:
    return {"type": "route_chips", "routes": list(ctx.incident.quick_routes or []), "selected": ctx.trip_context.get("selected_route")}


# --------------------------------------------------------------------------- strategies
class ScenarioStrategy(ABC):
    slug: str

    @abstractmethod
    def steps(self, ctx: DecisionContext) -> tuple[list[str], list[str]]:
        """Return (steps, policy_facts that must survive LLM rewriting)."""

    def links(self, ctx: DecisionContext) -> list[dict]:
        return [timetable_link(ctx)]

    def build(self, ctx: DecisionContext) -> DecisionResult:
        steps, facts = self.steps(ctx)
        widgets: list[dict] = []
        if ctx.uncertain:
            widgets.append(_forecast_warning_widget(ctx))
        show_map = bool(ctx.detour_route) and self.slug in ("missed_connection", "detour_route", "general")
        if show_map:
            widgets.append(_detour_widget(ctx))
        widgets += self.links(ctx)
        return DecisionResult(
            scenario=self.slug, title=TITLES[self.slug][ctx.lang], steps=steps,
            summary_text=SUMMARIES[self.slug][ctx.lang].format(route=ctx.route, title=ctx.incident.title),
            widgets=widgets, forecast_warning=ctx.uncertain, show_detour_map=show_map, policy_facts=facts,
        )


def _forecast_step(ctx: DecisionContext) -> str:
    if ctx.incident.status == "RESUMED":
        return tr("resumed", ctx.lang)
    if ctx.uncertain:
        return tr("uncertain", ctx.lang, forecast=ctx.forecast)
    return tr("certain", ctx.lang, forecast=ctx.forecast)


class MissedConnectionStrategy(ScenarioStrategy):
    slug = "missed_connection"

    def steps(self, ctx):
        lang = ctx.lang
        steps = [tr("supersaver" if ctx.trip_context.get("ticket_type") == "SUPERSAVER" else "ticket_valid", lang)]
        if ctx.incident.status != "RESUMED":
            steps.append(tr("alternative", lang, route=ctx.route, action=focus_guidance(ctx.recommended_action, ctx.route_keys)))
        steps.append(_forecast_step(ctx))
        if ctx.trip_context.get("urgency") == "HIGH":
            steps.append(tr("urgent", lang))
        return steps, [steps[0]]


class DetourRouteStrategy(ScenarioStrategy):
    slug = "detour_route"

    def steps(self, ctx):
        lang = ctx.lang
        steps = [tr("detour", lang, detour=focus_guidance(ctx.detour_summary, ctx.route_keys, 3))]
        if ctx.incident.status != "RESUMED":
            steps.append(tr("bus_yes" if ctx.incident.has_replacement_bus else "bus_no", lang))
        if ctx.uncertain:
            steps.append(tr("uncertain", lang, forecast=ctx.forecast))
        steps.append(tr("map_hint", lang) if ctx.detour_route else tr("certain", lang, forecast=ctx.forecast)
                     if ctx.incident.status != "RESUMED" else tr("resumed", lang))
        return steps, []

    def links(self, ctx):
        return [timetable_link(ctx), sbb_link("traffic", ctx.lang)]


class RefundCompensationStrategy(ScenarioStrategy):
    slug = "refund_compensation"

    @staticmethod
    def compensation_pct(delay_min: int | None) -> int | None:
        if delay_min is None:
            return None
        if delay_min >= 120:
            return 50
        if delay_min >= 60:
            return 25
        return 0

    def steps(self, ctx):
        lang = ctx.lang
        steps = [tr("refund_unused", lang), tr("refund_delay", lang)]
        delay = ctx.trip_context.get("expected_delay_minutes")
        pct = self.compensation_pct(delay)
        if pct:
            steps.append(tr("refund_you", lang, m=delay, pct=f"{pct}%"))
        elif pct == 0:
            steps.append(tr("refund_none", lang, m=delay))
        if ctx.trip_context.get("ticket_type") == "SUPERSAVER":
            steps.append(tr("supersaver", lang))
        steps.append(tr("refund_how", lang))
        return steps, steps[:2]

    def links(self, ctx):
        return [sbb_link("refund", ctx.lang)]


class StatusTimelineStrategy(ScenarioStrategy):
    slug = "status_timeline"

    def steps(self, ctx):
        lang = ctx.lang
        lines = ctx.incident.affected_lines or []
        lines_txt = f" ({LINES_WORD[lang]} {', '.join(lines[:8])})" if lines else ""
        steps = [tr("status_cause", lang, reason=ctx.reason, lines=lines_txt)]
        if ctx.updates:
            last = ctx.updates[-1]
            steps.append(tr("status_latest", lang, label=f"v{last['version']}", time=last["time"], text=last["text"]))
            if len(ctx.updates) > 1:
                steps.append(tr("status_evolution", lang, n=len(ctx.updates)))
        if ctx.incident.status == "RESUMED":
            steps.append(tr("resumed", lang))
        elif ctx.uncertain:
            steps.append(tr("uncertain", lang, forecast=ctx.forecast))
        else:
            steps.append(tr("certain", lang, forecast=ctx.forecast))
            steps.append(tr("confirmed", lang))
        return steps, []

    def links(self, ctx):
        return [sbb_link("traffic", ctx.lang)]


class GeneralStrategy(ScenarioStrategy):
    slug = "general"

    def steps(self, ctx):
        lang = ctx.lang
        steps = []
        if ctx.incident.status != "RESUMED":
            steps.append(tr("alternative", lang, route=ctx.route, action=focus_guidance(ctx.recommended_action, ctx.route_keys)))
        steps.append(_forecast_step(ctx))
        if ctx.trip_context.get("ticket_type") == "SUPERSAVER":
            steps.append(tr("supersaver", lang))
        if ctx.trip_context.get("urgency") == "HIGH":
            steps.append(tr("urgent", lang))
        return steps, []


STRATEGIES: dict[str, ScenarioStrategy] = {
    s.slug: s for s in (MissedConnectionStrategy(), DetourRouteStrategy(), RefundCompensationStrategy(),
                        StatusTimelineStrategy(), GeneralStrategy())
}


# --------------------------------------------------------------------------- free-text fallback NLU
INTENT_KEYWORDS = _R["INTENT_KEYWORDS"]
TICKET_KEYWORDS = _R["TICKET_KEYWORDS"]
URGENCY_KEYWORDS = tuple(_R["URGENCY_KEYWORDS"])
DEST_MARKERS = r"(?:to|towards|heading to|going to|nach|zum|zur|richtung|à|a|pour|vers|verso|per|fino a)"
ORIGIN_MARKERS = r"(?:from|von|ab|de|depuis|da|dalla|dal)"


def detect_intent(text: str) -> str:
    low = f" {text.lower()} "
    scores = {k: sum(1 for kw in kws if kw in low) for k, kws in INTENT_KEYWORDS.items()}
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "general"


def extract_trip_context(text: str) -> dict:
    """Deterministic trip-context extraction (used when Gemini is unavailable)."""
    patch: dict = {}
    low = f" {text.lower()} "
    for ticket, kws in TICKET_KEYWORDS:
        if any(k in low for k in kws):
            patch["ticket_type"] = ticket
            break
    if any(k in low for k in URGENCY_KEYWORDS):
        patch["urgency"] = "HIGH"
    m = re.search(r"(\d{2,3})\s?(?:min|minutes|minuten|minuti)", low)
    if m:
        patch["expected_delay_minutes"] = int(m.group(1))
    idx = get_station_index()
    stations = idx.find_in_text(text)
    if stations:
        names = [s.name for s in stations]
        origin = dest = None
        for st in stations:
            n = re.escape(st.name.lower().split("/")[0].split(" ")[0])
            if re.search(DEST_MARKERS + r"\s+(?:the\s+)?" + n, low):
                dest = dest or st.name
            elif re.search(ORIGIN_MARKERS + r"\s+" + n, low):
                origin = origin or st.name
        if len(names) >= 2:
            origin = origin or (names[0] if names[0] != dest else names[1])
            dest = dest or next((n for n in names if n != origin), None)
        elif not dest and not origin:
            dest = names[0]
        if origin:
            patch["origin"] = origin
        if dest:
            patch["destination"] = dest
    return patch


# --------------------------------------------------------------------------- guardrails
TIME_RE = re.compile(r"\b([01]?\d|2[0-3])[:.h]([0-5]\d)\b")


def _times(text: str) -> set[str]:
    return {f"{int(h):02d}:{m}" for h, m in TIME_RE.findall(text or "")}


def validate_llm(out: AssistantDecisionSchema, det: DecisionResult, ctx_text: str) -> str | None:
    """Return a reason string if the LLM output must be rejected."""
    if not out.steps or len(out.steps) > 6 or not out.decision_title.strip():
        return "empty_or_too_many_steps"
    joined = " ".join(out.steps) + " " + out.summary_text
    allowed = _times(ctx_text)
    invented = _times(joined) - allowed
    if invented:
        return f"invented_times:{sorted(invented)}"
    if det.scenario == "refund_compensation" and not ("25" in joined and "50" in joined):
        return "refund_policy_missing"
    if re.search(r"\+?\d[\d ]{8,}\d", joined):
        return "phone_number_in_output"
    return None


# --------------------------------------------------------------------------- engine
class HybridDecisionEngine:
    def __init__(self) -> None:
        self.gemini = get_gemini_service()

    def deterministic(self, ctx: DecisionContext, scenario: str) -> DecisionResult:
        return STRATEGIES.get(scenario, STRATEGIES["general"]).build(ctx)

    def prepare(self, ctx: DecisionContext, scenario: str | None, user_text: str | None = None) -> tuple[DecisionResult, dict]:
        """Instant, deterministic answer (always available) plus the passenger facts extracted by keywords."""
        ctx.lang = norm_lang(ctx.lang)
        fallback_patch = extract_trip_context(user_text) if user_text else {}
        if user_text and fallback_patch:
            ctx.trip_context = {**ctx.trip_context, **{k: v for k, v in fallback_patch.items() if v is not None}}
        chosen = scenario or (detect_intent(user_text) if user_text else "general")
        det = self.deterministic(ctx, chosen)
        det.trip_context_patch = fallback_patch
        return det, fallback_patch

    async def decide(self, ctx: DecisionContext, scenario: str | None, user_text: str | None = None) -> DecisionResult:
        det, fallback_patch = self.prepare(ctx, scenario, user_text)
        return await self.refine(ctx, det, fallback_patch, scenario, user_text)

    async def refine(self, ctx: DecisionContext, det: DecisionResult, fallback_patch: dict, scenario: str | None,
                     user_text: str | None) -> DecisionResult:
        """Ask Gemini to phrase the plan; returns ``det`` unchanged if Gemini is unavailable or breaks a guardrail."""
        chosen = det.scenario
        llm_ctx = self._llm_context(ctx, det)
        out = await self.gemini.generate_decision(lang=ctx.lang, context=llm_ctx, passenger_message=user_text, scenario=scenario)
        if out is None:
            return det
        patch = {k: v for k, v in out.trip_context_patch.model_dump().items() if v is not None}
        if not scenario and out.intent != chosen and out.intent in STRATEGIES:
            # Gemini understood the intent better than the keyword matcher: rebuild the deterministic facts
            ctx.trip_context = {**ctx.trip_context, **patch}
            det = self.deterministic(ctx, out.intent)
            llm_ctx = self._llm_context(ctx, det)
        reason = validate_llm(out, det, json_text(llm_ctx) + " " + (user_text or ""))
        if reason:
            log.warning("gemini output rejected by guardrail", extra={"reason": reason, "scenario": det.scenario})
            det.trip_context_patch = {**fallback_patch, **patch}
            return det
        steps = list(out.steps)
        # Policy facts are re-attached verbatim if the model paraphrased them away
        for fact in det.policy_facts:
            if fact not in steps and det.scenario == "refund_compensation":
                steps = det.policy_facts + [s for s in steps if s not in det.policy_facts]
                break
        return DecisionResult(
            scenario=det.scenario, title=out.decision_title.strip()[:90], steps=steps[:6], summary_text=out.summary_text.strip(),
            widgets=det.widgets, forecast_warning=det.forecast_warning, show_detour_map=det.show_detour_map,
            policy_facts=det.policy_facts, engine="gemini", trip_context_patch={**fallback_patch, **patch},
        )

    @staticmethod
    def _llm_context(ctx: DecisionContext, det: DecisionResult) -> dict:
        """Compact prompt: a few hundred tokens of the freshest facts. Long prompts make Gemini slower, not better."""
        inc = ctx.incident

        def clip(text, n):
            text = str(text or "")
            return text if len(text) <= n else text[: n - 1] + "…"

        return {
            "incident": {
                "title": inc.title, "status": inc.status, "reason": ctx.reason, "affected_lines": (inc.affected_lines or [])[:8],
                "affected_stations": (inc.affected_stations or [])[:8], "duration_type": inc.duration_type, "forecast": ctx.forecast,
                "forecast_warning": ctx.uncertain, "has_replacement_bus": inc.has_replacement_bus,
                "recommended_action": clip(ctx.recommended_action, 300), "detour_summary": clip(ctx.detour_summary, 300),
                "version": inc.current_version,
            },
            "updates": [{**u, "text": clip(u.get("text"), 220)} for u in ctx.updates[-3:]],
            "operational_log": [{**l, "text": clip(l.get("text"), 220)} for l in ctx.operational_log[:5]],
            "messages": [{**m, "body": clip(m.get("body"), 260)} for m in ctx.messages[-2:]],
            "trip_context": ctx.trip_context,
            "chat": [{**c, "content": clip(c.get("content"), 160)} for c in ctx.chat[-4:]],
            "deterministic": {"scenario": det.scenario, "title": det.title, "steps": [clip(x, 260) for x in det.steps],
                              "policy_facts": det.policy_facts, "forecast_warning": det.forecast_warning},
        }


def json_text(obj) -> str:
    import json

    return json.dumps(obj, ensure_ascii=False, default=str)


_engine: HybridDecisionEngine | None = None


def get_engine() -> HybridDecisionEngine:
    global _engine
    if _engine is None:
        _engine = HybridDecisionEngine()
    return _engine
