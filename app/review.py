"""Reviewer decisions.

Approve merges the zone branch and exports the change set. Reject sends the
zone's clashes back to the queue with the reviewer's note attached. Edit is the
interesting one: a reviewer supplies their own vector, and it is re-run through
all three monitors before it is accepted.

Re-running a human's vector is not distrust of the human. It is the same
courtesy the resolver gets -- every proposal in this system, whoever authored
it, carries the evidence of having been checked. A reviewer's move that skipped
the monitors would be the only change in the change set with nothing behind it,
and it would be indistinguishable from the ones that were checked.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import BadRequest, NotFound
from app.api.schemas import ReviewIn, ReviewOut
from app.config import get_settings
from app.ledger import record, transition
from app.models import CLASH_STATES, ZONE_STATES, Clash, ModelVersion, Proposal, ReviewDecision, Zone

log = logging.getLogger(__name__)


def _zone_clashes(db: Session, zone: Zone) -> list[Clash]:
    return list(db.execute(select(Clash).where(Clash.zone_id == zone.id)).scalars())


def _re_monitor(db: Session, zone: Zone, edits: list[Any]) -> list[dict[str, Any]]:
    """Run the reviewer's own vectors through the full monitor set."""
    from app.agents.coordinator import Coordinator, load_project_rules
    from app.agents.zone_resolver import as_vector3
    from app.agents.zoning import buffer_gids, neighbours_of
    from app.kit.engine import load_model
    from app.monitors import MonitorContext, run_all

    coordinator = Coordinator(db)
    mv = db.get(ModelVersion, zone.model_version_id)
    if mv is None:
        raise NotFound("model version for this zone no longer exists")

    rules = load_project_rules()
    model = load_model(mv.ifc_path, cache_dir=coordinator._cache_dir(), aliases_path=mv.aliases_path)
    plans = coordinator._plans_for(zone.model_version_id)
    plan = next((p for p in plans if p.zone_key == zone.zone_key), None)
    if plan is None:
        raise NotFound("zone plan is not reproducible from the stored model version")

    buf = buffer_gids(plan, model.by_id, zone.buffer_m)
    neighbours = neighbours_of(plan, plans, buf)

    out: list[dict[str, Any]] = []
    for edit in edits:
        proposal = db.get(Proposal, edit.proposal_id)
        if proposal is None:
            raise NotFound(f"proposal {edit.proposal_id} does not exist")
        clash = db.get(Clash, proposal.clash_id)
        if clash is None or clash.zone_id != zone.id:
            raise BadRequest(
                "proposal does not belong to this zone",
                {"proposal_id": edit.proposal_id, "zone_id": zone.id},
            )

        vector = as_vector3(edit.move_vector)
        ctx = MonitorContext(
            model=model,
            element_gid=proposal.element_gid,
            vector_mm=vector,
            zone_key=zone.zone_key,
            zone_gids=list(zone.element_gids),
            buffer_gids=buf,
            neighbour_zone_gids=neighbours,
            rules=rules,
            store=coordinator.store,
            stream=mv.speckle_stream,
        )
        passed, results = run_all(coordinator.monitors, ctx)

        proposal.superseded = 1
        edited = Proposal(
            clash_id=clash.id,
            element_gid=proposal.element_gid,
            move_type="reviewer_edit",
            move_vector=list(vector),
            rule_ids=list(proposal.rule_ids or []),
            clause_text=proposal.clause_text,
            monitor_geometry=results["geometry"].as_dict(),
            monitor_boundary=results["boundary"].as_dict(),
            monitor_integrity=results["integrity"].as_dict(),
            verdict="verified" if passed else "rejected",
            attempt=proposal.attempt + 1,
        )
        db.add(edited)
        db.flush()

        transition(
            db,
            clash,
            entity="clash",
            to_state="verified" if passed else "escalated",
            allowed=CLASH_STATES,
            actor="reviewer",
            payload={
                "reviewer_edit": True,
                "vector_mm": list(vector),
                "passed": passed,
                "objections": [f"{n}: {r.reason}" for n, r in results.items() if not r.passed],
            },
        )
        out.append(
            {
                "proposal_id": edited.id,
                "replaces": proposal.id,
                "clash_key": clash.clash_key,
                "passed": passed,
                "objections": [f"{n}: {r.reason}" for n, r in results.items() if not r.passed],
            }
        )
    return out


def apply_review(db: Session, zone: Zone, body: ReviewIn, actor: str) -> ReviewOut:
    re_monitored: list[dict[str, Any]] = []
    change_set_ref: str | None = None

    if body.decision == "edit":
        if not body.edits:
            raise BadRequest("an edit decision must carry at least one edit")
        re_monitored = _re_monitor(db, zone, body.edits)

    clashes = _zone_clashes(db, zone)
    affected = 0

    if body.decision == "approve":
        path = Path(get_settings().artifacts_dir) / "review" / zone.id / "change_set.json"
        change_set_ref = str(path) if path.exists() else None
        for clash in clashes:
            if clash.state != "verified":
                continue
            transition(
                db, clash, entity="clash", to_state="approved", allowed=CLASH_STATES,
                actor=actor, payload={"review": "approve"},
            )
            transition(
                db, clash, entity="clash", to_state="merged", allowed=CLASH_STATES,
                actor=actor, payload={"review": "approve", "branch": zone.branch},
            )
            affected += 1
        transition(
            db, zone, entity="zone", to_state="merged", allowed=ZONE_STATES, actor=actor,
            payload={"decision": "approve", "clashes_merged": affected, "change_set": change_set_ref},
        )

    elif body.decision == "reject":
        for clash in clashes:
            if clash.state not in ("verified", "proposed"):
                continue
            transition(
                db, clash, entity="clash", to_state="open", allowed=CLASH_STATES, actor=actor,
                payload={"review": "reject", "notes": body.notes},
            )
            affected += 1
        transition(
            db, zone, entity="zone", to_state="queued", allowed=ZONE_STATES, actor=actor,
            payload={"decision": "reject", "clashes_reopened": affected, "notes": body.notes},
        )

    else:  # edit
        affected = len(re_monitored)
        if zone.status != "awaiting_review":
            transition(
                db, zone, entity="zone", to_state="awaiting_review", allowed=ZONE_STATES,
                actor=actor, payload={"decision": "edit", "edits": affected},
            )

    decision = ReviewDecision(
        zone_id=zone.id,
        reviewer=body.reviewer,
        decision=body.decision,
        change_set_ref=change_set_ref,
        notes=body.notes,
    )
    db.add(decision)
    db.flush()
    record(
        db,
        entity="review_decision",
        entity_id=decision.id,
        to_state=body.decision,
        actor=actor,
        payload={"zone_id": zone.id, "reviewer": body.reviewer, "affected": affected},
    )

    return ReviewOut(
        zone_id=zone.id,
        decision=body.decision,
        zone_status=zone.status,
        clashes_affected=affected,
        change_set_ref=change_set_ref,
        re_monitored=re_monitored,
    )
