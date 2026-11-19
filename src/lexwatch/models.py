"""SQLAlchemy 2 typed models.

Enumerations are plain strings guarded by CHECK constraints (no PG enums) so that adding a
value is a one-line migration. Allowed values are exported as tuples for reuse in code.
The schema is owned by Alembic (``migrations/versions``); keep both in sync.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

ROLES = ("admin", "user")
LANGUAGES = ("fr", "en")
SCORE_STATES = ("pending", "scored", "unscored")
RESCORE_STATUSES = ("idle", "pending", "running")
RUN_KINDS = ("daily", "manual", "rescore")
RUN_STATUSES = ("queued", "running", "success", "partial", "failed")

# Bill fields whose changes are recorded in bill_events (ingestion stream).
TRACKED_BILL_FIELDS = (
    "type",
    "title",
    "status",
    "filing_date",
    "closing_date",
    "author",
    "dossier_url",
    "summary",
)

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _in(column: str, values: tuple[str, ...]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{column} IN ({quoted})"


SEARCH_VECTOR_SQL = (
    "to_tsvector('french'::regconfig, "
    "number || ' ' || coalesce(title, '') || ' ' || coalesce(summary, ''))"
)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(_in("role", ROLES), name="role"),
        CheckConstraint(_in("language", LANGUAGES), name="language"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    oidc_sub: Mapped[str] = mapped_column(String(255), unique=True)
    email: Mapped[str | None] = mapped_column(String(320))
    name: Mapped[str | None] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default="user", server_default="user")
    language: Mapped[str] = mapped_column(String(2), default="fr", server_default="fr")
    last_visit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    previous_visit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    profiles: Mapped[list[Profile]] = relationship(back_populates="owner")

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"


class Bill(Base):
    __tablename__ = "bills"
    __table_args__ = (
        CheckConstraint(_in("score_state", SCORE_STATES), name="score_state"),
        Index("ix_bills_search_vector", "search_vector", postgresql_using="gin"),
        Index("ix_bills_filing_date", "filing_date"),
        Index("ix_bills_score_state", "score_state"),
    )

    number: Mapped[str] = mapped_column(String(32), primary_key=True)
    type: Mapped[str | None] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str | None] = mapped_column(Text)
    filing_date: Mapped[date | None] = mapped_column(Date)
    closing_date: Mapped[date | None] = mapped_column(Date)
    author: Mapped[str | None] = mapped_column(Text)
    dossier_url: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    scored_text: Mapped[str | None] = mapped_column(Text)
    score_state: Mapped[str] = mapped_column(
        String(16), default="pending", server_default="pending"
    )
    source: Mapped[str] = mapped_column(String(32))
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed(SEARCH_VECTOR_SQL, persisted=True), nullable=True, deferred=True
    )

    events: Mapped[list[BillEvent]] = relationship(
        back_populates="bill", order_by="BillEvent.changed_at"
    )


class BillEvent(Base):
    __tablename__ = "bill_events"
    __table_args__ = (Index("ix_bill_events_bill_number_changed_at", "bill_number", "changed_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bill_number: Mapped[str] = mapped_column(
        ForeignKey("bills.number", ondelete="CASCADE", onupdate="CASCADE")
    )
    field: Mapped[str] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    source: Mapped[str] = mapped_column(String(32))

    bill: Mapped[Bill] = relationship(back_populates="events")


class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = (
        CheckConstraint(_in("rescore_status", RESCORE_STATUSES), name="rescore_status"),
        CheckConstraint("threshold IS NULL OR (threshold >= 0 AND threshold <= 1)", name="threshold"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), index=True)
    threshold: Mapped[Decimal | None] = mapped_column(Numeric(4, 3))  # NULL = default
    recipients: Mapped[str] = mapped_column(Text, default="", server_default="")
    rescore_status: Mapped[str] = mapped_column(
        String(16), default="idle", server_default="idle"
    )
    last_digest_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped[User] = relationship(back_populates="profiles")
    rules: Mapped[list[Rule]] = relationship(
        back_populates="profile",
        order_by="Rule.position",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def effective_threshold(self, default: float) -> float:
        return float(self.threshold) if self.threshold is not None else default

    def recipient_list(self) -> list[str]:
        parts = self.recipients.replace("\n", ",").replace(";", ",").split(",")
        return [p.strip() for p in parts if p.strip()]


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("profiles.id", ondelete="CASCADE"), index=True
    )
    text: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer, default=0, server_default="0")

    profile: Mapped[Profile] = relationship(back_populates="rules")


class RuleScore(Base):
    __tablename__ = "rule_scores"
    __table_args__ = (UniqueConstraint("rule_id", "bill_number", name="uq_rule_scores_rule_bill"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("rules.id", ondelete="CASCADE"))
    bill_number: Mapped[str] = mapped_column(
        ForeignKey("bills.number", ondelete="CASCADE", onupdate="CASCADE"), index=True
    )
    score: Mapped[float] = mapped_column(Float)
    model: Mapped[str] = mapped_column(String(128))
    scored_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        UniqueConstraint("profile_id", "bill_number", name="uq_matches_profile_bill"),
        Index("ix_matches_profile_matched", "profile_id", "matched"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"))
    bill_number: Mapped[str] = mapped_column(
        ForeignKey("bills.number", ondelete="CASCADE", onupdate="CASCADE"), index=True
    )
    profile_score: Mapped[float] = mapped_column(Float)
    matched: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_in("kind", RUN_KINDS), name="kind"),
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        Index("ix_runs_status", "status"),
        Index("ix_runs_started_at", "started_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="queued", server_default="queued")
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    bills_added: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bills_changed: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bills_scored: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    bills_unscored: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    billed_units: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_message: Mapped[str | None] = mapped_column(Text)
    # Free-form per-step status, e.g. {"sources": {"chd": {"status": "failed", "error": "..."}}}
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default="{}"
    )
