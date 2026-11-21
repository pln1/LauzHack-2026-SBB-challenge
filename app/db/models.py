"""PostgreSQL schema (SQLAlchemy 2.0 ORM)."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Station(Base):
    __tablename__ = "stations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    uic: Mapped[str | None] = mapped_column(String(16), unique=True)
    name: Mapped[str] = mapped_column(String(200), index=True)
    canton: Mapped[str | None] = mapped_column(String(4))
    lat: Mapped[float | None] = mapped_column(Float)
    lng: Mapped[float | None] = mapped_column(Float)


class DisruptionReason(Base):
    __tablename__ = "disruption_reasons"
    id: Mapped[str] = mapped_column(String(32), primary_key=True)  # e.g. EG_047
    reason_en: Mapped[str] = mapped_column(Text)
    reason_de: Mapped[str] = mapped_column(Text)
    reason_fr: Mapped[str] = mapped_column(Text)
    reason_it: Mapped[str] = mapped_column(Text)


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("external_id", "data_mode", name="uq_incident_external_mode"),
        Index("ix_incidents_mode_status", "data_mode", "status"),
        Index("ix_incidents_lines", "affected_lines", postgresql_using="gin"),
    )

    # Deterministic ids (derived from the VIA event id / KULT id) so that re-running the ETL
    # keeps ids stable and anonymous sessions survive a re-ingest.
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    external_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))  # ACTIVE | RESTRICTED | RESUMED
    canton: Mapped[str | None] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(Text)
    subtitle_i18n: Mapped[dict] = mapped_column(JSONB, default=dict)
    reason_id: Mapped[str | None] = mapped_column(ForeignKey("disruption_reasons.id"))
    duration_type: Mapped[str] = mapped_column(String(32), default="UNKNOWN")
    duration_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_at_least: Mapped[str | None] = mapped_column(String(16))
    forecast_label_i18n: Mapped[dict] = mapped_column(JSONB, default=dict)
    has_replacement_bus: Mapped[bool] = mapped_column(Boolean, default=False)
    affected_lines: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    affected_stations: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    current_version: Mapped[int] = mapped_column(Integer, default=1)
    map_geometry: Mapped[dict] = mapped_column(JSONB, default=dict)
    quick_routes: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    recommended_action_i18n: Mapped[dict] = mapped_column(JSONB, default=dict)
    detour_summary_i18n: Mapped[dict] = mapped_column(JSONB, default=dict)
    data_mode: Mapped[str] = mapped_column(String(8), index=True)  # live | dataset
    sources: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)  # PUBLIC / VIA / INTERNAL
    source_refs: Mapped[dict] = mapped_column(JSONB, default=dict)  # ids + correlation evidence
    # Replay support (live mode): source timestamps are shifted by this many seconds
    time_shift_seconds: Mapped[int] = mapped_column(Integer, default=0)
    replay_cursor: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # source time of last applied snapshot
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    reason: Mapped[DisruptionReason | None] = relationship(lazy="joined")


class IncidentUpdate(Base):
    """One version of an incident. Rows with ``is_applied = false`` are queued snapshots that
    ``simulate-update`` replays (live mode)."""

    __tablename__ = "incident_updates"
    __table_args__ = (Index("ix_updates_incident_version", "incident_id", "version"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # source time (unshifted)
    summary_i18n: Mapped[dict] = mapped_column(JSONB, default=dict)
    source: Mapped[str] = mapped_column(String(16))  # PUBLIC | INTERNAL | VIA | SIMULATION
    status: Mapped[str | None] = mapped_column(String(16))
    snapshot: Mapped[dict] = mapped_column(JSONB, default=dict)  # incident fields at this version
    is_applied: Mapped[bool] = mapped_column(Boolean, default=True)


class IncidentMessage(Base):
    __tablename__ = "incident_messages"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    source_id: Mapped[str | None] = mapped_column(String(64))
    title_i18n: Mapped[dict] = mapped_column(JSONB, default=dict)
    body_i18n: Mapped[dict] = mapped_column(JSONB, default=dict)
    published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    edit_count: Mapped[int] = mapped_column(Integer, default=0)


class OperationalLog(Base):
    __tablename__ = "operational_logs"
    __table_args__ = (Index("ix_oplogs_incident_ts", "incident_id", "timestamp"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sanitized_message: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(16))  # KULT_INTERNAL | VIA


class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    incident_id: Mapped[int] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    lang: Mapped[str] = mapped_column(String(2), default="en")
    trip_context: Mapped[dict] = mapped_column(JSONB, default=dict)
    last_seen_incident_version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)

    messages: Mapped[list["ChatMessage"]] = relationship(
        back_populates="session", order_by="ChatMessage.created_at", cascade="all, delete-orphan", lazy="selectin"
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"), index=True)
    sender: Mapped[str] = mapped_column(String(16))  # USER | ASSISTANT | SYSTEM_ALERT
    content: Mapped[str] = mapped_column(Text)
    widgets: Mapped[list | dict] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped[ChatSession] = relationship(back_populates="messages")
