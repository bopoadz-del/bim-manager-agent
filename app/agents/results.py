"""Typed results returned by the agents.

Each agent has one ``run()`` and returns one of these. They are plain data: an
agent that returned a live database object would tempt callers into lazy-loading
across a closed session, and an agent that returned a dict would let a typo in a
key name read as a missing value rather than an error.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ProposalOutcome:
    """One monitored attempt at one clash."""

    clash_id: str
    clash_key: str
    element_gid: str
    move_type: str
    vector_mm: tuple[float, float, float]
    verdict: str
    attempt: int
    monitors: dict[str, dict] = field(default_factory=dict)
    rule_ids: list[str] = field(default_factory=list)
    clause_text: str | None = None
    committed_as: str | None = None
    handed_to_coordinator: bool = False
    #: Checks no monitor could answer from this model, as "monitor.check".
    #: Persisted with the proposal, because a reviewer approving a conditional
    #: verification is accepting exactly this list.
    unprovable_checks: list[str] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)
    #: Every monitored attempt that was rejected, with its full monitor evidence.
    #: Kept because "which monitor objected, and to what" is the question a
    #: reviewer asks about an escalated clash, and a one-line reason in a ledger
    #: payload cannot answer it.
    rejected_attempts: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["vector_mm"] = list(self.vector_mm)
        return d


@dataclass
class ZoneResult:
    zone_id: str
    zone_key: str
    branch: str | None
    clashes_seen: int = 0
    verified: int = 0
    #: Accepted by every monitor with at least one check the model could not
    #: answer. Counted apart from `verified` on purpose: summing them would
    #: rebuild the very conflation the three-valued verdict removes.
    verified_conditional: int = 0
    escalated: int = 0
    flagged_unsourced: int = 0
    handed_to_coordinator: int = 0
    outcomes: list[ProposalOutcome] = field(default_factory=list)
    status: str = "awaiting_review"
    note: str | None = None

    @property
    def accepted(self) -> int:
        """Proposals no monitor objected to, fully or conditionally verified."""
        return self.verified + self.verified_conditional

    @property
    def resolve_rate(self) -> float:
        """Fraction of clashes with an accepted proposal.

        Counts conditionals: they are real proposals a reviewer can act on. The
        breakdown beside it says how many carry unanswered checks, so the rate
        never has to be read as a claim about verification depth.
        """
        return round(self.accepted / self.clashes_seen, 4) if self.clashes_seen else 0.0

    @property
    def fully_verified_rate(self) -> float:
        """Fraction whose proposal passed every check outright."""
        return round(self.verified / self.clashes_seen, 4) if self.clashes_seen else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "zone_id": self.zone_id,
            "zone_key": self.zone_key,
            "branch": self.branch,
            "clashes_seen": self.clashes_seen,
            "verified": self.verified,
            "verified_conditional": self.verified_conditional,
            "escalated": self.escalated,
            "flagged_unsourced": self.flagged_unsourced,
            "handed_to_coordinator": self.handed_to_coordinator,
            "resolve_rate": self.resolve_rate,
            "fully_verified_rate": self.fully_verified_rate,
            "status": self.status,
            "note": self.note,
            "outcomes": [o.as_dict() for o in self.outcomes],
        }


@dataclass
class IngestResult:
    model_version_id: str
    ifc_sha256: str
    element_count: int
    zones_created: int
    clashes_created: int
    joints_excluded: int
    pairs_admitted: int
    boundary_owned: int
    proposals_verified: int = 0
    proposals_conditional: int = 0
    #: Of every check every monitor ran, the fraction this model could answer.
    #: 1.0 means nothing was taken on trust; a low number means most of what the
    #: report calls "accepted" rests on checks that never ran.
    provable_check_ratio: float | None = None
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ArbitrationResult:
    """The coordinator's verdict on a proposal that crosses a zone boundary."""

    clash_id: str
    committed: bool
    reason: str
    zones_checked: list[str] = field(default_factory=list)
    monitors: dict[str, dict] = field(default_factory=dict)
    rebase_enqueued: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DiffResult:
    model_version_id: str
    previous_id: str
    resolved: int
    regressed: int
    new: int
    persisting: int
    proposal_score: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
