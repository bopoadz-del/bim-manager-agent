"""H2 — a model may propose a clearance figure; only a person may apply one."""
from __future__ import annotations

import pytest

from app.ledger import history
from app.rules.approval import (
    STATUS_APPROVED,
    STATUS_PENDING,
    STATUS_REJECTED,
    NotApproved,
    approved_rules,
    decide,
    from_extraction,
    write_pending,
)

EXTRACTED = [
    {
        "rule_id": "SPEC-A",
        "system_a": "gas_main",
        "system_b": "electrical_lv",
        "min_gap_mm": 400,
        "axis": "any",
        "precedence": "project_spec",
        "source_doc": "spec.pdf",
        "source_clause": "3.1",
        "source_text_hash": "abc",
    },
    {
        "rule_id": "SPEC-B",
        "system_a": "gas_main",
        "system_b": "*",
        "min_gap_mm": 300,
        "axis": "any",
        "precedence": "project_spec",
        "source_doc": "spec.pdf",
        "source_clause": "3.2",
        "source_text_hash": "def",
    },
]


@pytest.fixture
def candidates():
    return from_extraction("p1", "spec.pdf", EXTRACTED)


def test_everything_arrives_pending(candidates):
    assert [c.status for c in candidates.candidates] == [STATUS_PENDING, STATUS_PENDING]
    assert approved_rules(candidates) == [], (
        "an extracted candidate reached the rule table without anyone approving it"
    )


def test_approving_one_does_not_approve_the_other(candidates, db):
    decide(candidates, "SPEC-A", approve=True, reviewer="an engineer", session=db)
    db.flush()

    applied = approved_rules(candidates)
    assert [r["rule_id"] for r in applied] == ["SPEC-A"]
    assert candidates.by_id("SPEC-B").status == STATUS_PENDING


def test_a_rejection_is_recorded_as_a_decision_not_as_silence(candidates, db):
    decide(candidates, "SPEC-B", approve=False, reviewer="an engineer",
           session=db, note="wrong edition")
    db.flush()

    assert candidates.by_id("SPEC-B").status == STATUS_REJECTED
    events = history(db, "rule_candidate", "SPEC-B")
    assert len(events) == 1
    assert events[0].to_state == STATUS_REJECTED
    assert events[0].payload["note"] == "wrong edition"


def test_an_approval_must_name_somebody(candidates):
    with pytest.raises(NotApproved):
        decide(candidates, "SPEC-A", approve=True, reviewer="")
    with pytest.raises(NotApproved):
        decide(candidates, "SPEC-A", approve=True, reviewer="   ")
    assert candidates.by_id("SPEC-A").status == STATUS_PENDING


def test_the_ledger_records_who_what_and_against_which_clause(candidates, db):
    decide(candidates, "SPEC-A", approve=True, reviewer="A Reviewer", session=db)
    db.flush()

    event = history(db, "rule_candidate", "SPEC-A")[0]
    assert event.actor == "A Reviewer"
    assert event.from_state == STATUS_PENDING
    assert event.to_state == STATUS_APPROVED
    assert event.payload["clause"] == "3.1"
    assert event.payload["min_gap_mm"] == 400
    assert event.payload["source_doc"] == "spec.pdf"


def test_approved_rules_come_out_in_the_shape_the_kit_loader_accepts(candidates, db):
    decide(candidates, "SPEC-A", approve=True, reviewer="e", session=db)
    db.flush()

    import json
    import tempfile
    from pathlib import Path

    from app.blocks.clearance_rules import load_rules  # type: ignore[attr-defined]

    path = Path(tempfile.mkdtemp()) / "approved.json"
    path.write_text(json.dumps(approved_rules(candidates)), encoding="utf-8")

    loaded = load_rules(str(path))
    assert len(loaded) == 1
    assert loaded[0].source.clause == "3.1"


def test_there_is_no_approve_everything(candidates):
    """The interface has to make the careless thing impossible, not just rude."""
    import app.rules.approval as approval

    forbidden = [n for n in dir(approval) if "all" in n.lower() and "approve" in n.lower()]
    assert not forbidden, f"bulk approval helpers exist: {forbidden}"


def test_pending_candidates_can_be_written_out_for_review(candidates, tmp_path):
    path = write_pending(candidates, tmp_path / "pending.json")
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["source_doc"] == "spec.pdf"
    assert all(c["status"] == STATUS_PENDING for c in payload["candidates"])


def test_the_acceptance_probe_agrees(db):
    from app.rules.approval import acceptance_probe

    ok, evidence = acceptance_probe()
    assert ok, evidence
