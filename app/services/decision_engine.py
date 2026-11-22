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
    return sbb_link("timetable", ctx.lang, origin, destination)


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
T: dict[str, dict[str, str]] = {
    "ticket_valid": {
        "en": "**Your ticket remains valid:** during an official SBB disruption you may take the next available train on the same route.",
        "de": "**Ihr Billett bleibt gültig:** Bei einer offiziellen SBB-Störung dürfen Sie den nächsten verfügbaren Zug auf derselben Strecke nehmen.",
        "fr": "**Votre billet reste valable :** lors d’une perturbation officielle CFF, vous pouvez prendre le prochain train disponible sur le même trajet.",
        "it": "**Il tuo biglietto resta valido:** durante una perturbazione ufficiale FFS puoi prendere il prossimo treno disponibile sulla stessa tratta.",
    },
    "supersaver": {
        "en": "**Supersaver ticket:** the binding to a specific train is lifted during an official disruption — board the next train on the same route.",
        "de": "**Sparbillett:** Die Zugbindung ist während einer offiziellen Störung aufgehoben — nehmen Sie den nächsten Zug auf derselben Strecke.",
        "fr": "**Billet dégriffé :** l’obligation de prendre un train précis est levée pendant une perturbation officielle — prenez le prochain train sur le même trajet.",
        "it": "**Biglietto risparmio:** il vincolo al treno specifico decade durante una perturbazione ufficiale — prendi il prossimo treno sulla stessa tratta.",
    },
    "alternative": {
        "en": "**Alternative for {route}:** {action}", "de": "**Alternative für {route}:** {action}",
        "fr": "**Alternative pour {route} :** {action}", "it": "**Alternativa per {route}:** {action}",
    },
    "uncertain": {
        "en": "⚠️ **Forecast is uncertain** ({forecast}). The end time may move — do not wait at an intermediate station without a backup plan.",
        "de": "⚠️ **Prognose unsicher** ({forecast}). Das Ende kann sich verschieben — warten Sie nicht ohne Alternativplan an einem Zwischenbahnhof.",
        "fr": "⚠️ **Prévision incertaine** ({forecast}). L’heure de fin peut changer — n’attendez pas dans une gare intermédiaire sans plan B.",
        "it": "⚠️ **Previsione incerta** ({forecast}). L’orario di fine può cambiare — non attendere in una stazione intermedia senza un piano alternativo.",
    },
    "certain": {
        "en": "**Current forecast:** {forecast}. Check your onward connection in the SBB timetable before you set off.",
        "de": "**Aktuelle Prognose:** {forecast}. Prüfen Sie Ihren Anschluss im SBB Fahrplan, bevor Sie losfahren.",
        "fr": "**Prévision actuelle :** {forecast}. Vérifiez votre correspondance dans l’horaire CFF avant de partir.",
        "it": "**Previsione attuale:** {forecast}. Verifica la coincidenza nell’orario FFS prima di partire.",
    },
    "resumed": {
        "en": "✅ **Service has resumed.** Isolated delays are still possible; your planned connection is likely to work — check live times in the SBB timetable.",
        "de": "✅ **Der Betrieb läuft wieder.** Vereinzelt sind Verspätungen möglich; prüfen Sie die aktuellen Zeiten im SBB Fahrplan.",
        "fr": "✅ **Le trafic est rétabli.** Des retards isolés restent possibles ; vérifiez les horaires actuels dans l’horaire CFF.",
        "it": "✅ **La circolazione è ripristinata.** Sono possibili ritardi isolati; verifica gli orari attuali nell’orario FFS.",
    },
    "urgent": {
        "en": "**Time-critical trip?** If this is your last connection or you have a flight, ask staff at the station or the SBB Contact Center for onward travel assistance right away.",
        "de": "**Zeitkritische Reise?** Wenn dies Ihre letzte Verbindung ist oder Sie einen Flug haben, wenden Sie sich sofort an das Bahnhofpersonal oder das SBB Contact Center.",
        "fr": "**Trajet urgent ?** S’il s’agit de votre dernière correspondance ou d’un vol, adressez-vous tout de suite au personnel en gare ou au Contact Center CFF.",
        "it": "**Viaggio urgente?** Se è l’ultima coincidenza o hai un volo, rivolgiti subito al personale in stazione o al Contact Center FFS.",
    },
    "detour": {
        "en": "**Recommended detour:** {detour}", "de": "**Empfohlene Umleitung:** {detour}",
        "fr": "**Déviation recommandée :** {detour}", "it": "**Deviazione consigliata:** {detour}",
    },
    "bus_yes": {
        "en": "**Replacement bus (Bahnersatz):** follow the «Bahnersatz» signs outside the station. Your SBB ticket is valid on the bus; allow extra travel time.",
        "de": "**Bahnersatz:** Folgen Sie der Signalisation «Bahnersatz» beim Bahnhof. Ihr Billett gilt im Bus; rechnen Sie mit längerer Reisezeit.",
        "fr": "**Bus de remplacement :** suivez la signalétique «Bahnersatz» à la sortie de la gare. Votre billet est valable dans le bus ; prévoyez plus de temps.",
        "it": "**Bus sostitutivo (Bahnersatz):** segui la segnaletica «Bahnersatz» all’uscita della stazione. Il biglietto è valido sul bus; calcola più tempo.",
    },
    "bus_no": {
        "en": "**No replacement bus announced yet.** Use parallel rail lines or local public transport; this may change with the next update.",
        "de": "**Noch kein Bahnersatz angekündigt.** Nutzen Sie parallele Bahnlinien oder den Ortsverkehr; das kann sich mit dem nächsten Update ändern.",
        "fr": "**Pas encore de bus de remplacement annoncé.** Utilisez des lignes parallèles ou les transports locaux ; cela peut changer à la prochaine mise à jour.",
        "it": "**Nessun bus sostitutivo annunciato finora.** Usa linee parallele o i trasporti locali; può cambiare con il prossimo aggiornamento.",
    },
    "map_hint": {
        "en": "Use the button below to show the detour on the map, then confirm exact departures in the SBB timetable.",
        "de": "Mit der Schaltfläche unten sehen Sie die Umleitung auf der Karte; genaue Abfahrten finden Sie im SBB Fahrplan.",
        "fr": "Le bouton ci-dessous affiche la déviation sur la carte ; les départs exacts figurent dans l’horaire CFF.",
        "it": "Con il pulsante qui sotto vedi la deviazione sulla mappa; gli orari esatti sono nell’orario FFS.",
    },
    "refund_unused": {
        "en": "**Trip abandoned because of the disruption:** your unused ticket is refunded in full, without any fee.",
        "de": "**Reise wegen der Störung abgebrochen oder nicht angetreten:** Das unbenutzte Billett wird vollständig und gebührenfrei erstattet.",
        "fr": "**Voyage annulé à cause de la perturbation :** votre billet non utilisé est remboursé intégralement, sans frais.",
        "it": "**Viaggio annullato a causa della perturbazione:** il biglietto non utilizzato è rimborsato integralmente, senza spese.",
    },
    "refund_delay": {
        "en": "**Delay compensation:** arriving 60–119 min late = **25%** of the ticket price; 120 min or more = **50%**.",
        "de": "**Entschädigung bei Verspätung:** 60–119 Min. Ankunftsverspätung = **25%** des Billettpreises; ab 120 Min. = **50%**.",
        "fr": "**Indemnisation en cas de retard :** 60–119 min de retard à l’arrivée = **25%** du prix du billet ; 120 min ou plus = **50%**.",
        "it": "**Indennizzo per ritardo:** 60–119 min di ritardo all’arrivo = **25%** del prezzo del biglietto; 120 min o più = **50%**.",
    },
    "refund_you": {
        "en": "**Your case:** with an expected delay of {m} min you are entitled to **{pct}**.",
        "de": "**Ihr Fall:** Bei einer Verspätung von {m} Min. haben Sie Anspruch auf **{pct}**.",
        "fr": "**Votre cas :** avec un retard de {m} min, vous avez droit à **{pct}**.",
        "it": "**Il tuo caso:** con un ritardo di {m} min hai diritto a **{pct}**.",
    },
    "refund_none": {
        "en": "**Your case:** a delay of {m} min is below the 60-minute threshold, so no delay compensation applies.",
        "de": "**Ihr Fall:** {m} Min. Verspätung liegen unter der 60-Minuten-Schwelle; es gibt keine Verspätungsentschädigung.",
        "fr": "**Votre cas :** {m} min de retard restent sous le seuil de 60 minutes ; aucune indemnisation ne s’applique.",
        "it": "**Il tuo caso:** {m} min di ritardo sono sotto la soglia di 60 minuti; non spetta alcun indennizzo.",
    },
    "refund_how": {
        "en": "Keep your ticket (SBB Mobile or paper) and submit the claim online on the official SBB refunds page.",
        "de": "Bewahren Sie Ihr Billett auf (SBB Mobile oder Papier) und reichen Sie den Antrag online auf der offiziellen SBB-Seite ein.",
        "fr": "Conservez votre billet (CFF Mobile ou papier) et déposez la demande en ligne sur la page officielle des CFF.",
        "it": "Conserva il biglietto (FFS Mobile o cartaceo) e invia la richiesta online sulla pagina ufficiale FFS.",
    },
    "status_cause": {
        "en": "**Cause:** {reason}{lines}.", "de": "**Ursache:** {reason}{lines}.",
        "fr": "**Cause :** {reason}{lines}.", "it": "**Causa:** {reason}{lines}.",
    },
    "status_latest": {
        "en": "**Latest update ({label}, {time}):** {text}", "de": "**Letztes Update ({label}, {time}):** {text}",
        "fr": "**Dernière mise à jour ({label}, {time}) :** {text}", "it": "**Ultimo aggiornamento ({label}, {time}):** {text}",
    },
    "status_evolution": {
        "en": "This incident has been updated {n} times so far — SBB refines the information as it evolves.",
        "de": "Diese Meldung wurde bisher {n}-mal aktualisiert — die SBB präzisiert die Informationen laufend.",
        "fr": "Cet incident a déjà été mis à jour {n} fois — les CFF précisent l’information au fur et à mesure.",
        "it": "Questo evento è stato aggiornato {n} volte — le FFS precisano le informazioni man mano.",
    },
    "confirmed": {
        "en": "✅ The forecast has a concrete end time; keep monitoring the live updates.",
        "de": "✅ Die Prognose nennt ein konkretes Ende; verfolgen Sie die Live-Updates.",
        "fr": "✅ La prévision indique une heure de fin concrète ; suivez les mises à jour en direct.",
        "it": "✅ La previsione indica un orario di fine concreto; segui gli aggiornamenti in tempo reale.",
    },
}
TITLES = {
    "missed_connection": {"en": "Action plan for missed connection risk", "de": "Massnahmenplan bei Anschlussrisiko",
                          "fr": "Plan d’action en cas de correspondance menacée", "it": "Piano d’azione per coincidenza a rischio"},
    "detour_route": {"en": "Detour route & replacement transport", "de": "Umleitung & Ersatztransport",
                     "fr": "Itinéraire de déviation & transport de remplacement", "it": "Percorso alternativo e trasporto sostitutivo"},
    "refund_compensation": {"en": "SBB refund & compensation guidelines", "de": "SBB-Regeln für Rückerstattung & Entschädigung",
                            "fr": "Règles CFF de remboursement & d’indemnisation", "it": "Regole FFS per rimborsi e indennizzi"},
    "status_timeline": {"en": "Operational assessment & forecast reliability", "de": "Lagebeurteilung & Zuverlässigkeit der Prognose",
                        "fr": "Évaluation opérationnelle & fiabilité de la prévision", "it": "Valutazione operativa e affidabilità della previsione"},
    "general": {"en": "Your next move", "de": "Ihr nächster Schritt", "fr": "Votre prochaine étape", "it": "La tua prossima mossa"},
}
SUMMARIES = {
    "missed_connection": {"en": "Here is how to protect your connection on {route}.", "de": "So sichern Sie Ihren Anschluss auf {route}.",
                          "fr": "Voici comment sécuriser votre correspondance sur {route}.", "it": "Ecco come salvare la coincidenza su {route}."},
    "detour_route": {"en": "Here is the best way around the disruption on {route}.", "de": "So umfahren Sie die Störung auf {route}.",
                     "fr": "Voici comment contourner la perturbation sur {route}.", "it": "Ecco come aggirare la perturbazione su {route}."},
    "refund_compensation": {"en": "These are the official SBB rules for your ticket.", "de": "Das sind die offiziellen SBB-Regeln für Ihr Billett.",
                            "fr": "Voici les règles officielles des CFF pour votre billet.", "it": "Ecco le regole ufficiali FFS per il tuo biglietto."},
    "status_timeline": {"en": "What we know about {title} right now.", "de": "Das wissen wir derzeit über {title}.",
                        "fr": "Ce que nous savons actuellement sur {title}.", "it": "Ecco cosa sappiamo ora su {title}."},
    "general": {"en": "Based on your message, here is what I recommend for {route}.", "de": "Basierend auf Ihrer Nachricht empfehle ich für {route}:",
                "fr": "D’après votre message, voici ma recommandation pour {route}.", "it": "In base al tuo messaggio, ecco cosa consiglio per {route}."},
}
LINES_WORD = {"en": "lines", "de": "Linien", "fr": "lignes", "it": "linee"}
DETOUR_BTN = {"en": "🗺️ Display detour route on map", "de": "🗺️ Umleitungsroute auf Karte anzeigen",
              "fr": "🗺️ Afficher l’itinéraire de déviation sur la carte", "it": "🗺️ Visualizza percorso alternativo sulla mappa"}
WARNING_BADGE = {"en": "Forecast uncertain — end time may change", "de": "Prognose unsicher — Ende kann sich verschieben",
                 "fr": "Prévision incertaine — l’heure de fin peut changer", "it": "Previsione incerta — l’orario di fine può cambiare"}


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
INTENT_KEYWORDS = {
    "refund_compensation": ("refund", "compensat", "money", "erstatt", "entschädig", "geld", "rembours", "indemn",
                            "rimbors", "indenniz", "soldi"),
    "detour_route": ("detour", "bus", "bahnersatz", "umleit", "umfahr", "alternative", "déviation", "deviation", "contourn",
                     "deviazion", "percorso alternativo", "another way", "other route"),
    "missed_connection": ("connection", "miss", "anschluss", "verpass", "correspondance", "rater", "coincidenz", "perder",
                          "flight", "flug", "vol ", "volo", "next train", "nächste zug"),
    "status_timeline": ("how long", "when", "what happened", "wie lange", "wann", "was ist passiert", "combien de temps",
                        "quand", "que s", "quanto", "quando", "cosa è successo", "forecast", "prognose", "prévision", "previsione"),
}
TICKET_KEYWORDS = [
    ("SUPERSAVER", ("supersaver", "super saver", "sparbillett", "spar-billett", "billet dégriffé", "dégriffé", "biglietto risparmio", "saver")),
    ("GA", (" ga ", "generalabonnement", "abonnement général", "abbonamento generale", "travelcard")),
    ("HALF_FARE", ("halbtax", "half fare", "half-fare", "demi-tarif", "metà prezzo")),
    ("DAY_PASS", ("tageskarte", "day pass", "carte journalière", "carta giornaliera")),
]
URGENCY_KEYWORDS = ("urgent", "asap", "flight", "flug", "vol ", "volo", "airport", "flughafen", "aéroport", "aeroport",
                    "last train", "letzter zug", "dernier train", "ultimo treno", "dringend", "subito", "immédiatement")
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
