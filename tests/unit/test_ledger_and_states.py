"""The ledger, and the state machine it guards."""
from __future__ import annotations

import pytest

from app.ledger import IllegalTransition, history, record, transition
from app.models import CLASH_STATES, ZONE_STATES, Clash, ModelVersion, Project, Zone


def _seed(db):
    project = Project(name="p")
    db.add(project)
    db.flush()
    mv = ModelVersion(project_id=project.id, ifc_sha256="a" * 64, ifc_path="/tmp/x.ifc")
    db.add(mv)
    db.flush()
    zone = Zone(model_version_id=mv.id, zone_key="L0|1_1", element_gids=["g1", "g2"])
    db.add(zone)
    db.flush()
    clash = Clash(
        model_version_id=mv.id, zone_id=zone.id, clash_key="g1::g2",
        a_gid="g1", b_gid="g2", kind="clash",
    )
    db.add(clash)
    db.flush()
    return mv, zone, clash


def test_transition_writes_state_and_event_together(db):
    _, zone, _ = _seed(db)
    transition(db, zone, entity="zone", to_state="active", allowed=ZONE_STATES)
    db.flush()

    assert zone.status == "active"
    events = history(db, "zone", zone.id)
    assert [(e.from_state, e.to_state) for e in events] == [("queued", "active")]


def test_an_unknown_target_state_raises_rather_than_being_coerced(db):
    _, zone, _ = _seed(db)
    with pytest.raises(IllegalTransition) as exc:
        transition(db, zone, entity="zone", to_state="merge", allowed=ZONE_STATES)
    assert "merge" in str(exc.value)
    assert zone.status == "queued", "a rejected transition must not have written anything"


def test_the_ledger_records_the_whole_path_not_just_the_destination(db):
    _, _, clash = _seed(db)
    for state in ("proposed", "verified", "approved", "merged"):
        transition(db, clash, entity="clash", to_state=state, allowed=CLASH_STATES)
    db.flush()

    path = [e.to_state for e in history(db, "clash", clash.id)]
    assert path == ["proposed", "verified", "approved", "merged"]


def test_a_rebase_is_visible_as_a_step_backwards(db):
    """The sequence A5 asks for: verified, then back to proposed, with a reason."""
    _, _, clash = _seed(db)
    transition(db, clash, entity="clash", to_state="proposed", allowed=CLASH_STATES)
    transition(db, clash, entity="clash", to_state="verified", allowed=CLASH_STATES)
    transition(
        db, clash, entity="clash", to_state="proposed", allowed=CLASH_STATES,
        actor="coordinator", payload={"rebase": True, "trigger": {"zones": ["L0|2_2"]}},
    )
    db.flush()

    events = history(db, "clash", clash.id)
    assert [e.to_state for e in events] == ["proposed", "verified", "proposed"]
    last = events[-1]
    assert last.from_state == "verified"
    assert last.payload["rebase"] is True
    assert last.actor == "coordinator"


def test_database_rejects_a_state_outside_the_vocabulary(db):
    """The check constraint is the backstop for anything that bypasses transition()."""
    from sqlalchemy.exc import IntegrityError

    _, zone, _ = _seed(db)
    zone.status = "nonsense"
    with pytest.raises(IntegrityError):
        db.flush()
    db.rollback()


def test_record_captures_non_transitions_too(db):
    mv, _, _ = _seed(db)
    record(db, entity="model_version", entity_id=mv.id, to_state="meshed",
           payload={"elements": 1437})
    db.flush()
    events = history(db, "model_version", mv.id)
    assert events[-1].payload["elements"] == 1437
