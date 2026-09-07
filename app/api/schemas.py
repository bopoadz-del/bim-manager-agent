"""Request and response shapes. These are the OpenAPI contract."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class ProjectOut(BaseModel):
    id: str
    name: str
    created_at: datetime


class KeyIssued(BaseModel):
    """The plaintext appears here and nowhere else, ever again."""

    key: str
    role: str
    label: str
    note: str = "Store this now. It is not recoverable."


class ModelVersionOut(BaseModel):
    id: str
    project_id: str
    ifc_sha256: str
    element_count: int
    status: str
    speckle_stream: str | None = None
    ingested_at: datetime
    stats: dict[str, Any] = Field(default_factory=dict)


class IngestOut(BaseModel):
    model_version: ModelVersionOut
    zones_created: int
    clashes_created: int
    joints_excluded: int
    pairs_admitted: int
    boundary_owned: int


class ZoneOut(BaseModel):
    id: str
    zone_key: str
    level: str
    grid_cell: str
    buffer_m: float
    congestion: float
    programme_rank: int
    priority: float
    status: str
    element_count: int
    dedicated_reason: str | None = None
    branch: str | None = None


class ClashOut(BaseModel):
    id: str
    clash_key: str
    a_gid: str
    b_gid: str
    kind: str
    depth_or_gap_mm: float | None
    systems: list[str]
    state: str
    attempts: int
    owner: str
    rule_id: str | None
    required_gap_mm: float | None
    note: str | None


class ProposalOut(BaseModel):
    id: str
    clash_id: str
    element_gid: str
    move_type: str
    move_vector: list[float]
    rule_ids: list[str]
    clause_text: str | None
    verdict: str
    attempt: int
    monitor_geometry: dict[str, Any] | None
    monitor_boundary: dict[str, Any] | None
    monitor_integrity: dict[str, Any] | None
    created_at: datetime


class ReviewEdit(BaseModel):
    """A reviewer's own vector. It is re-monitored, never trusted on sight."""

    proposal_id: str
    move_vector: list[float] = Field(min_length=3, max_length=3)


class ReviewIn(BaseModel):
    decision: Literal["approve", "reject", "edit"]
    reviewer: str = Field(min_length=1, max_length=120)
    notes: str | None = None
    edits: list[ReviewEdit] = Field(default_factory=list)


class ReviewOut(BaseModel):
    zone_id: str
    decision: str
    zone_status: str
    clashes_affected: int
    change_set_ref: str | None
    re_monitored: list[dict[str, Any]] = Field(default_factory=list)


class DiffOut(BaseModel):
    model_version_id: str
    previous_id: str
    resolved: int
    regressed: int
    new: int
    persisting: int
    proposal_score: dict[str, Any]


class HealthOut(BaseModel):
    status: str
    build_sha: str
    database: str
    store: dict[str, Any]
    exact_geometry_backend: bool
    vendored_kit: dict[str, Any]
