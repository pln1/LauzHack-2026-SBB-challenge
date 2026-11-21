"""Localized labels, official sbb.ch links and forecast formatting (EN/DE/FR/IT)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

LANGS = ("en", "de", "fr", "it")
TZ = ZoneInfo("Europe/Zurich")


def norm_lang(lang: str | None) -> str:
    lang = (lang or "en").lower()[:2]
    return lang if lang in LANGS else "en"


def pick(i18n: dict | None, lang: str, default: str = "") -> str:
    if not i18n:
        return default
    return i18n.get(lang) or i18n.get("en") or i18n.get("de") or next(iter(i18n.values()), default) or default


STATUS_LABELS = {
    "ACTIVE": {"en": "Interrupted service", "de": "Unterbruch", "fr": "Interruption", "it": "Interruzione"},
    "RESTRICTED": {"en": "Limited service", "de": "Einschränkung", "fr": "Trafic restreint", "it": "Limitazioni"},
    "RESUMED": {"en": "Service resumed", "de": "Betrieb wiederaufgenommen", "fr": "Trafic rétabli", "it": "Circolazione ripristinata"},
}

SBB_LINKS = {
    "en": {
        "timetable": "https://www.sbb.ch/en",
        "traffic": "https://www.sbb.ch/en/travel-information/rail-traffic-information.html",
        "refund": "https://www.sbb.ch/en/help-and-contact/refunds-compensation.html",
    },
    "de": {
        "timetable": "https://www.sbb.ch/de",
        "traffic": "https://www.sbb.ch/de/reiseinformationen/bahnverkehrsinformationen.html",
        "refund": "https://www.sbb.ch/de/hilfe-und-kontakt/erstattung-entschaedigung.html",
    },
    "fr": {
        "timetable": "https://www.sbb.ch/fr",
        "traffic": "https://www.sbb.ch/fr/informations-voyages/informations-trafic-ferroviaire.html",
        "refund": "https://www.sbb.ch/fr/aide-et-contact/remboursement-indemnisation.html",
    },
    "it": {
        "timetable": "https://www.sbb.ch/it",
        "traffic": "https://www.sbb.ch/it/informazioni-di-viaggio/informazioni-sul-traffico-ferroviario.html",
        "refund": "https://www.sbb.ch/it/aiuto-e-contatto/rimborso-indennizzo.html",
    },
}

LINK_LABELS = {
    "timetable": {"en": "Check SBB Timetable ↗", "de": "Im SBB Fahrplan prüfen ↗", "fr": "Vérifier dans l’horaire CFF ↗", "it": "Controlla nell’orario FFS ↗"},
    "traffic": {"en": "Official SBB live status ↗", "de": "Offizielle SBB Betriebslage ↗", "fr": "Info trafic officielle CFF ↗", "it": "Info traffico ufficiale FFS ↗"},
    "refund": {"en": "Claim refund on SBB.ch ↗", "de": "Rückerstattung auf SBB.ch ↗", "fr": "Demander un remboursement CFF ↗", "it": "Richiedi rimborso su FFS.ch ↗"},
}


def sbb_link(kind: str, lang: str) -> dict:
    lang = norm_lang(lang)
    return {"type": "sbb_link", "kind": kind, "url": SBB_LINKS[lang][kind], "label": LINK_LABELS[kind][lang]}


UNCERTAIN_DURATIONS = {"UNKNOWN", "UNTIL_AT_LEAST", "UNTIL_AT_LEAST_DATE_TIME", "NONE"}


def is_uncertain(duration_type: str | None) -> bool:
    return (duration_type or "UNKNOWN") in UNCERTAIN_DURATIONS


def _fmt_time(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%H:%M") if dt else "?"


def _fmt_dt(dt: datetime | None) -> str:
    return dt.astimezone(TZ).strftime("%d.%m. %H:%M") if dt else "?"


def forecast_label(duration_type: str | None, duration_from: datetime | None, duration_to: datetime | None,
                   at_least: str | None) -> dict:
    """Human readable expected end of the disruption, in all four languages."""
    d = duration_type or "UNKNOWN"
    t_to, dt_to, t_from = _fmt_time(duration_to), _fmt_dt(duration_to), _fmt_time(duration_from)
    at = (at_least or "")[:5] or t_to
    table = {
        "ENDED": {"en": "Service resumed — residual delays possible", "de": "Betrieb wiederaufgenommen — vereinzelt Verspätungen möglich",
                  "fr": "Trafic rétabli — retards résiduels possibles", "it": "Circolazione ripristinata — possibili ritardi residui"},
        "UNKNOWN": {"en": "End not yet known (under assessment)", "de": "Dauer unbekannt (wird abgeklärt)",
                    "fr": "Durée inconnue (en cours d’évaluation)", "it": "Durata sconosciuta (in valutazione)"},
        "NONE": {"en": "No end time communicated", "de": "Keine Dauer kommuniziert", "fr": "Aucune durée communiquée", "it": "Nessuna durata comunicata"},
        "UNTIL": {"en": f"Until {t_to}", "de": f"Bis {t_to}", "fr": f"Jusqu’à {t_to}", "it": f"Fino alle {t_to}"},
        "UNTIL_DATE": {"en": f"Until {dt_to}", "de": f"Bis {dt_to}", "fr": f"Jusqu’au {dt_to}", "it": f"Fino al {dt_to}"},
        "UNTIL_AT_LEAST": {"en": f"At least until {at} (extension possible)", "de": f"Mindestens bis {at} (Verlängerung möglich)",
                           "fr": f"Au moins jusqu’à {at} (prolongation possible)", "it": f"Almeno fino alle {at} (possibile proroga)"},
        "UNTIL_AT_LEAST_DATE_TIME": {"en": f"At least until {dt_to} (extension possible)", "de": f"Mindestens bis {dt_to} (Verlängerung möglich)",
                                     "fr": f"Au moins jusqu’au {dt_to} (prolongation possible)", "it": f"Almeno fino al {dt_to} (possibile proroga)"},
        "UNTIL_APPROX_DATE_TIME": {"en": f"Approx. until {t_to} (forecast may change)", "de": f"Bis ca. {t_to} (Prognose kann ändern)",
                                   "fr": f"Jusqu’à env. {t_to} (prévision susceptible d’évoluer)", "it": f"Fino alle {t_to} circa (previsione soggetta a modifiche)"},
        "FROM_TO": {"en": f"From {t_from} to {t_to}", "de": f"Von {t_from} bis {t_to}", "fr": f"De {t_from} à {t_to}", "it": f"Dalle {t_from} alle {t_to}"},
    }
    if d in ("UNTIL", "FROM_TO", "UNTIL_DATE", "UNTIL_APPROX_DATE_TIME", "UNTIL_AT_LEAST_DATE_TIME") and duration_to is None:
        d = "UNKNOWN"
    if d == "UNTIL_AT_LEAST" and not (at_least or duration_to):
        d = "UNKNOWN"
    return table.get(d, table["UNKNOWN"])


def updated_ago(ts: datetime | None, lang: str, now: datetime | None = None) -> str:
    lang = norm_lang(lang)
    if ts is None:
        return ""
    now = now or datetime.now(timezone.utc)
    delta = now - ts
    if delta < timedelta(seconds=0):
        delta = timedelta(0)
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return {"en": "Just updated", "de": "Soeben aktualisiert", "fr": "Mis à jour à l’instant", "it": "Appena aggiornato"}[lang]
    if mins < 60:
        return {"en": f"Updated {mins} min ago", "de": f"Vor {mins} Min. aktualisiert", "fr": f"Mis à jour il y a {mins} min", "it": f"Aggiornato {mins} min fa"}[lang]
    if mins < 24 * 60:
        h = mins // 60
        return {"en": f"Updated {h} h ago", "de": f"Vor {h} Std. aktualisiert", "fr": f"Mis à jour il y a {h} h", "it": f"Aggiornato {h} h fa"}[lang]
    stamp = ts.astimezone(TZ).strftime("%d.%m.%Y %H:%M")
    return {"en": f"Updated {stamp}", "de": f"Aktualisiert {stamp}", "fr": f"Mis à jour le {stamp}", "it": f"Aggiornato il {stamp}"}[lang]


SCENARIOS = [
    {"slug": "missed_connection", "icon": "⏱️",
     "title": {"en": "I might miss my connection", "de": "Ich verpasse meinen Anschluss", "fr": "Je risque de rater ma correspondance", "it": "Rischio di perdere la coincidenza"},
     "description": {"en": "Connection risk check, next train & ticket validity", "de": "Anschlussprüfung, nächster Zug & Ticketgültigkeit",
                     "fr": "Vérification de correspondance, train suivant & billet", "it": "Verifica coincidenza, prossimo treno e validità biglietto"}},
    {"slug": "detour_route", "icon": "🧭",
     "title": {"en": "How do I take a detour?", "de": "Wie kann ich die Störung umfahren?", "fr": "Comment contourner la perturbation ?", "it": "Come posso fare una deviazione?"},
     "description": {"en": "Alternative line, Bahnersatz bus & route on map", "de": "Alternativstrecke, Bahnersatzbus & Karte",
                     "fr": "Ligne alternative, bus de remplacement & carte", "it": "Linea alternativa, bus sostitutivo e percorso su mappa"}},
    {"slug": "refund_compensation", "icon": "💳",
     "title": {"en": "Refunds & compensation", "de": "Rückerstattung & Entschädigung", "fr": "Remboursement & indemnisation", "it": "Rimborsi e indennizzi"},
     "description": {"en": "Ticket refund rules & 60+ min delay claims", "de": "Bedingungen für Erstattung & Verspätung ab 60 Min.",
                     "fr": "Règles de remboursement & retard de 60+ min", "it": "Regole di rimborso e indennizzo per ritardi 60+ min"}},
    {"slug": "status_timeline", "icon": "📡",
     "title": {"en": "What happened & how long?", "de": "Was ist passiert und wie lange?", "fr": "Que s’est-il passé et pour combien de temps ?", "it": "Cosa è successo e quanto durerà?"},
     "description": {"en": "Forecast reliability & whether to wait at the station", "de": "Prognose-Zuverlässigkeit & Empfehlung am Bahnhof",
                     "fr": "Fiabilité des prévisions & conseil en gare", "it": "Affidabilità della previsione e consigli in stazione"}},
]
SCENARIO_SLUGS = {s["slug"] for s in SCENARIOS}
