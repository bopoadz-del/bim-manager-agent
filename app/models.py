"""The ledger schema.

Two rules shape every table here:

1. State lives in one named column with a checked vocabulary, never in a pair of
   booleans that can express a contradiction such as "approved and rejected".
2. Every state change also writes an append-only ``ledger_event`` row. The state
   column answers "where is this now"; the ledger answers "how did it get here",
   which is the question an engineer asks when a proposal they rejected turns up
   in a change set.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _uuid() -> str:
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """Declarative base.

    ``LEDGER_FIELD`` names the column that holds this entity's state. The spec
    calls it ``status`` on a zone and ``state`` on a clash, and a ledger helper
    that guessed would work on one and raise on the other -- at runtime, in the
    middle of a resolver run. Each model states its own answer instead.
    """

    LEDGER_FIELD = "state"


ZONE_STATES = ("queued", "active", "awaiting_review", "merged")
CLASH_STATES = (
    "open",
    "proposed",
    "verified",
    "approved",
    "merged",
    "resolved",
    "regressed",
    "escalated",
)
CLASH_OWNERS = ("zone", "coordinator")
REVIEW_DECISIONS = ("approve", "reject", "edit")
PROPOSAL_VERDICTS = (
    "pending",
    "verified",
    "rejected",
    "flagged_unsourced",
    "escalated",
    "approved",
    "superseded",
)


class Project(Base):
    __tablename__ = "project"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ApiKey(Base):
    """Per-project key. Only the hash is stored; the plaintext is shown once."""

    __tablename__ = "api_key"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"), index=True)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(20), default="reviewer")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("role in ('reviewer', 'operator')", name="ck_api_key_role"),
    )


class ModelVersion(Base):
    __tablename__ = "model_version"
    LEDGER_FIELD = "status"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.id", ondelete="CASCADE"), index=True)
    ifc_sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ifc_path: Mapped[str] = mapped_column(Text, nullable=False)
    speckle_stream: Mapped[str | None] = mapped_column(String(120))
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    element_count: Mapped[int] = mapped_column(Integer, default=0)
    parent_id: Mapped[str | None] = mapped_column(ForeignKey("model_version.id"))
    status: Mapped[str] = mapped_column(String(20), default="ingesting")
    # Remembered so every later load reproduces the same element systems. A
    # review that re-meshed with a different alias table would be reviewing a
    # different model than the one that was judged.
    aliases_path: Mapped[str | None] = mapped_column(Text)
    programme_path: Mapped[str | None] = mapped_column(Text)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)

    zones: Mapped[list[Zone]] = relationship(
        back_populates="model_version", cascade="all, delete-orphan"
    )


class Zone(Base):
    __tablename__ = "zone"
    LEDGER_FIELD = "status"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_version.id", ondelete="CASCADE"), index=True
    )
    zone_key: Mapped[str] = mapped_column(String(120), nullable=False)
    level: Mapped[str] = mapped_column(String(120), default="")
    grid_cell: Mapped[str] = mapped_column(String(120), default="")
    buffer_m: Mapped[float] = mapped_column(Float, default=2.0)
    congestion: Mapped[float] = mapped_column(Float, default=0.0)
    programme_rank: Mapped[int] = mapped_column(Integer, default=0)
    priority: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    status: Mapped[str] = mapped_column(String(20), default="queued", index=True)
    element_count: Mapped[int] = mapped_column(Integer, default=0)
    element_gids: Mapped[list] = mapped_column(JSON, default=list)
    dedicated_reason: Mapped[str | None] = mapped_column(String(60))
    branch: Mapped[str | None] = mapped_column(String(160))

    model_version: Mapped[ModelVersion] = relationship(back_populates="zones")
    clashes: Mapped[list[Clash]] = relationship(
        back_populates="zone", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("model_version_id", "zone_key", name="uq_zone_key_per_version"),
        CheckConstraint(
            "status in ('queued', 'active', 'awaiting_review', 'merged')", name="ck_zone_status"
        ),
    )


class Clash(Base):
    __tablename__ = "clash"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    model_version_id: Mapped[str] = mapped_column(
        ForeignKey("model_version.id", ondelete="CASCADE"), index=True
    )
    zone_id: Mapped[str | None] = mapped_column(ForeignKey("zone.id", ondelete="CASCADE"), index=True)
    clash_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    a_gid: Mapped[str] = mapped_column(String(64), nullable=False)
    b_gid: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    depth_or_gap_mm: Mapped[float | None] = mapped_column(Float)
    systems: Mapped[list] = mapped_column(JSON, default=list)
    state: Mapped[str] = mapped_column(String(20), default="open", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    owner: Mapped[str] = mapped_column(String(20), default="zone")
    rule_id: Mapped[str | None] = mapped_column(String(80))
    required_gap_mm: Mapped[float | None] = mapped_column(Float)
    severity_mm: Mapped[float] = mapped_column(Float, default=0.0)
    resolution_rank: Mapped[int] = mapped_column(Integer, default=99)
    note: Mapped[str | None] = mapped_column(Text)

    zone: Mapped[Zone | None] = relationship(back_populates="clashes")
    proposals: Mapped[list[Proposal]] = relationship(
        back_populates="clash", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("model_version_id", "clash_key", name="uq_clash_key_per_version"),
        CheckConstraint(
            "state in ('open', 'proposed', 'verified', 'approved', 'merged', "
            "'resolved', 'regressed', 'escalated')",
            name="ck_clash_state",
        ),
        CheckConstraint("owner in ('zone', 'coordinator')", name="ck_clash_owner"),
        Index("ix_clash_zone_state", "zone_id", "state"),
    )


class Proposal(Base):
    __tablename__ = "proposal"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    clash_id: Mapped[str] = mapped_column(ForeignKey("clash.id", ondelete="CASCADE"), index=True)
    element_gid: Mapped[str] = mapped_column(String(64), nullable=False)
    move_type: Mapped[str] = mapped_column(String(40), default="offset")
    move_vector: Mapped[list] = mapped_column(JSON, default=list)
    rule_ids: Mapped[list] = mapped_column(JSON, default=list)
    clause_text: Mapped[str | None] = mapped_column(Text)
    monitor_geometry: Mapped[dict | None] = mapped_column(JSON)
    monitor_boundary: Mapped[dict | None] = mapped_column(JSON)
    monitor_integrity: Mapped[dict | None] = mapped_column(JSON)
    verdict: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    superseded: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    clash: Mapped[Clash] = relationship(back_populates="proposals")

    __table_args__ = (
        CheckConstraint(
            "verdict in ('pending', 'verified', 'rejected', 'flagged_unsourced', "
            "'escalated', 'approved', 'superseded')",
            name="ck_proposal_verdict",
        ),
    )


class ReviewDecision(Base):
    __tablename__ = "review_decision"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    zone_id: Mapped[str] = mapped_column(ForeignKey("zone.id", ondelete="CASCADE"), index=True)
    reviewer: Mapped[str] = mapped_column(String(120), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    change_set_ref: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("decision in ('approve', 'reject', 'edit')", name="ck_review_decision"),
    )


class LedgerEvent(Base):
    """Append-only. There is deliberately no update or delete path in the code.

    ``seq`` is the ordering, not ``ts``. Several transitions routinely land in
    the same microsecond -- a clash going proposed then verified inside one
    resolver call -- and ordering those by timestamp puts them in an arbitrary
    order. An arbitrary order is worse than no order here, because the whole
    value of this table is being able to say what happened *before* what.
    """

    __tablename__ = "ledger_event"

    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(32), unique=True, default=_uuid)
    entity: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    from_state: Mapped[str | None] = mapped_column(String(40))
    to_state: Mapped[str | None] = mapped_column(String(40))
    actor: Mapped[str] = mapped_column(String(120), default="system")
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
