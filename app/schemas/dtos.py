"""API data transfer objects (camelCase JSON)."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

Lang = Literal["en", "de", "fr", "it"]
Status = Literal["ACTIVE", "RESTRICTED", "RESUMED"]


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, from_attributes=True)


class MapGeometry(CamelModel):
    waypoints: list[list[float]] = []
    from_: list[float] | None = Field(default=None, alias="from")
    to: list[float] | None = None
    stations: list[dict[str, Any]] = []
    detour_route: list[list[float]] | None = None
    track: list[list[float]] | None = None          # path along the rail network between the affected stations
    track_source: str = "straight"                   # "rail" | "straight"
    track_length_km: float | None = None
    detours: list[dict[str, Any]] = []               # [{label, kind, via, route, approximate, lengthKm}]
    approximate: bool = False
    note: str | None = None


class IncidentSummaryDto(CamelModel):
    id: int
    external_id: str
    status: Status
    status_label: str
    canton: str | None
    title: str
    subtitle: str
    reason: str
    reason_code: str | None
    duration_type: str
    forecast_label: str
    forecast_warning: bool
    has_replacement_bus: bool
    affected_lines: list[str]
    current_version: int
    updated_at: datetime
    updated_ago: str
    map_geometry: MapGeometry
    quick_routes: list[str]
    recommended_action: str
    detour_summary: str
    data_mode: Literal["live", "dataset"]
    sources: list[str]
    has_pending_updates: bool = False


class KpiDto(CamelModel):
    active: int
    replacement_bus: int
    uncertain_forecast: int
    resumed: int


class DashboardResponseDto(CamelModel):
    kpis: KpiDto
    incidents: list[IncidentSummaryDto]
    total_count: int
    filtered_count: int
    mode: Literal["live", "dataset"]
    lang: Lang
    available_dates: list[str] = []


class TimelineEntryDto(CamelModel):
    version: int
    label: str
    published_at: datetime
    time: str
    text: str
    source: str
    status: str | None


class IncidentMessageDto(CamelModel):
    title: str
    body: str
    published_at: datetime
    time: str


class OperationalSummaryDto(CamelModel):
    total_entries: int
    sources: list[str]
    latest: list[dict[str, Any]]
    correlation: list[dict[str, Any]]
    note: str


class IncidentDetailDto(IncidentSummaryDto):
    description: str
    audit_timeline: list[TimelineEntryDto]
    messages: list[IncidentMessageDto]
    operational_summary: OperationalSummaryDto
    source_refs: dict[str, Any]


class ScenarioDto(CamelModel):
    slug: str
    icon: str
    title: str
    description: str


class TripContext(CamelModel):
    origin: str | None = None
    destination: str | None = None
    selected_route: str | None = None
    ticket_type: str | None = None  # e.g. SUPERSAVER, GA, HALF_FARE, POINT_TO_POINT, DAY_PASS
    urgency: str | None = None  # LOW | MEDIUM | HIGH
    expected_delay_minutes: int | None = None


class ChatMessageDto(CamelModel):
    id: UUID
    sender: Literal["USER", "ASSISTANT", "SYSTEM_ALERT"]
    content: str
    widgets: list[dict[str, Any]] = []
    created_at: datetime


class SessionDto(CamelModel):
    session_id: UUID
    incident_id: int
    lang: Lang
    trip_context: TripContext
    last_seen_incident_version: int
    incident_version: int
    expires_at: datetime
    messages: list[ChatMessageDto]
    new_alerts: list[ChatMessageDto] = []


class CreateSessionRequest(CamelModel):
    incident_id: int
    lang: str = "en"
    trip_context: TripContext | None = None


class ScenarioRequest(CamelModel):
    scenario_slug: str
    selected_route: str | None = None
    trip_context_patch: TripContext | None = None
    lang: str | None = None


class MessageRequest(CamelModel):
    content: str = Field(min_length=1, max_length=2000)
    selected_route: str | None = None
    lang: str | None = None


class AssistantTurnDto(CamelModel):
    session_id: UUID
    incident_version: int
    new_alerts: list[ChatMessageDto]
    messages: list[ChatMessageDto]  # messages created by this call (user + assistant)
    ui_widgets: list[dict[str, Any]]
    trip_context: TripContext
    engine: str  # "gemini" | "deterministic"


class SimulateUpdateResponse(CamelModel):
    incident: IncidentDetailDto
    applied: bool
    source: str
    change_summary: str
