"""Rules a model proposed, and the human step between proposing and applying.

``app/llm/rule_extraction.py`` can read a project specification and come back
with candidate clearance rules. This module is what stands between a candidate
and a number an engineer will move a duct to satisfy.

A candidate is inert. It is stored, it is listed, it can be read — and it cannot
reach the rule table until a named person approves that specific rule, by id.
Approving a batch, approving by count, approving anything the reviewer has not
looked at individually: none of those exist here, because a clearance figure is
exactly the kind of thing that gets waved through when the interface makes it
easy to.

Every approval and every rejection is a ledger row. The question an engineer will
eventually ask about a rule is not "is it approved" but "who approved it, when,
and against what text", and only the ledger can answer that.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATUS_PENDING = "pending_approval"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


class NotApproved(Exception):
    """Raised when unapproved candidates are asked to behave like rules."""


@dataclass
class Candidate:
    rule_id: str
    payload: dict[str, Any]
    status: str = STATUS_PENDING
    decided_by: str | None = None
    decided_at: str | None = None
    note: str | None = None

    @property
    def approved(self) -> bool:
        return self.status == STATUS_APPROVED

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "status": self.status,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "note": self.note,
            "rule": self.payload,
        }


@dataclass
class CandidateSet:
    """Candidates extracted from one document, and their decisions."""

    project_id: str
    source_doc: str
    candidates: list[Candidate] = field(default_factory=list)

    def by_id(self, rule_id: str) -> Candidate:
        for c in self.candidates:
            if c.rule_id == rule_id:
                return c
        raise KeyError(rule_id)

    @property
    def pending(self) -> list[Candidate]:
        return [c for c in self.candidates if c.status == STATUS_PENDING]

    @property
    def approved(self) -> list[Candidate]:
        return [c for c in self.candidates if c.approved]

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "source_doc": self.source_doc,
            "candidates": [c.as_dict() for c in self.candidates],
        }


def from_extraction(project_id: str, source_doc: str, extracted: list[dict]) -> CandidateSet:
    """Wrap extractor output. Everything arrives pending; nothing arrives applied."""
    return CandidateSet(
        project_id=project_id,
        source_doc=source_doc,
        candidates=[
            Candidate(rule_id=str(r.get("rule_id") or f"CANDIDATE-{i}"), payload=dict(r))
            for i, r in enumerate(extracted)
        ],
    )


def decide(
    candidates: CandidateSet,
    rule_id: str,
    approve: bool,
    reviewer: str,
    session: Any = None,
    note: str | None = None,
) -> Candidate:
    """Approve or reject exactly one candidate, and write it to the ledger.

    One at a time, by id, with a name attached. There is deliberately no
    ``approve_all``: the reviewer has to have looked at the clause.
    """
    if not reviewer or not reviewer.strip():
        raise NotApproved("an approval must name the person making it")

    candidate = candidates.by_id(rule_id)
    candidate.status = STATUS_APPROVED if approve else STATUS_REJECTED
    candidate.decided_by = reviewer.strip()
    candidate.decided_at = datetime.now(UTC).isoformat()
    candidate.note = note

    if session is not None:
        from app.ledger import record

        record(
            session,
            entity="rule_candidate",
            entity_id=rule_id[:32],
            from_state=STATUS_PENDING,
            to_state=candidate.status,
            actor=candidate.decided_by,
            payload={
                "project_id": candidates.project_id,
                "source_doc": candidates.source_doc,
                "rule_id": rule_id,
                "clause": candidate.payload.get("source_clause")
                or candidate.payload.get("source", {}).get("clause"),
                "min_gap_mm": candidate.payload.get("min_gap_mm"),
                "note": note,
            },
        )
    return candidate


def approved_rules(candidates: CandidateSet) -> list[dict[str, Any]]:
    """The approved candidates, in the shape the kit's loader accepts."""
    out = []
    for candidate in candidates.approved:
        payload = candidate.payload
        source = payload.get("source") or {
            "doc": payload.get("source_doc", ""),
            "clause": payload.get("source_clause", ""),
            "text_hash": payload.get("source_text_hash", ""),
        }
        out.append(
            {
                "rule_id": candidate.rule_id,
                "system_a": payload["system_a"],
                "system_b": payload["system_b"],
                "min_gap_mm": float(payload["min_gap_mm"]),
                "axis": payload.get("axis", "any"),
                "precedence": payload.get("precedence", "project_spec"),
                "source": source,
            }
        )
    return out


def write_pending(candidates: CandidateSet, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(candidates.as_dict(), indent=2), encoding="utf-8")
    return p


# --------------------------------------------------------------------------
def acceptance_probe() -> tuple[bool, str]:
    """A08: candidates are inert until approved one at a time, and it is logged."""
    from app.ledger import history
    from app.llm.rule_extraction import _validate

    source = "Gas mains shall be separated from LV cables by not less than 400 mm."
    extracted = _validate(
        [
            {
                "rule_id": "SPEC-GAS-LV-400",
                "system_a": "gas_main",
                "system_b": "electrical_lv",
                "min_gap_mm": 400,
                "axis": "any",
                "precedence": "project_spec",
                "source_doc": "spec.pdf",
                "source_clause": "3.1",
                "quote": source,
            }
        ],
        source,
    )
    if not extracted:
        return False, "the extractor rejected a candidate whose quote is in the source"

    candidates = from_extraction("p1", "spec.pdf", extracted)
    inert = not approved_rules(candidates) and len(candidates.pending) == 1

    # A dedicated database. The probe used to reset the global engine, which
    # locked the file out from under the acceptance run that already held a
    # session on it -- a test harness must not disturb what it is measuring.
    import tempfile

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.models import Base

    db_path = Path(tempfile.mkdtemp()) / "approval_probe.db"
    engine = create_engine(f"sqlite+pysqlite:///{db_path.as_posix()}", future=True)
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()
    try:
        decide(candidates, "SPEC-GAS-LV-400", approve=True, reviewer="an engineer",
               session=session, note="checked against the drawing")
        session.flush()
        applied = approved_rules(candidates)
        events = history(session, "rule_candidate", "SPEC-GAS-LV-400"[:32])
    finally:
        session.close()
        engine.dispose()

    anonymous_refused = False
    try:
        decide(candidates, "SPEC-GAS-LV-400", approve=True, reviewer="  ")
    except NotApproved:
        anonymous_refused = True

    ok = (
        inert
        and len(applied) == 1
        and applied[0]["source"]["clause"] == "3.1"
        and len(events) == 1
        and events[0].actor == "an engineer"
        and anonymous_refused
    )
    return ok, (
        f"inert before approval={inert} applied after={len(applied)} "
        f"ledger rows={len(events)} actor={events[0].actor if events else None} "
        f"anonymous approval refused={anonymous_refused}"
    )
