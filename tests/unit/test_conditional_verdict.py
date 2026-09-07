"""G1 — "nothing failed" is not "everything passed".

Every test here fails on the version before this one, where
``MonitorResult.from_checks`` returned ``passed=True`` whenever no check had
failed. On the only real building model available, every element lacks ports and
no access table exists, so integrity's connectivity and access checks are
unprovable on every proposal. That version reported all seven A1 proposals as
`verified`, and the distinction survived only as a sentence in `reason` that
nothing read.
"""
from __future__ import annotations

import pytest

from app.api.schemas import ReviewIn
from app.models import Clash, Proposal
from app.monitors import (
    VERDICT_CONDITIONAL,
    VERDICT_FAIL,
    VERDICT_PASS,
    Check,
    Monitor,
    MonitorResult,
    aggregate,
    run_all,
    unprovable_across,
)
from app.monitors.base import FAIL, PASS, UNPROVABLE
from app.review import apply_review, unacknowledged


# -- the verdict itself ----------------------------------------------------
def test_an_unprovable_check_does_not_make_a_pass():
    result = MonitorResult.from_checks(
        "integrity", [Check("a", PASS, "fine"), Check("b", UNPROVABLE, "no ports")]
    )
    assert result.verdict == VERDICT_CONDITIONAL
    assert result.passed is False, (
        "a monitor with an unanswered check reported a pass; that is the exact "
        "substitution the three-valued check exists to prevent"
    )
    assert result.conditional is True
    assert result.acceptable is True
    assert result.unprovable_checks == ["b"]


def test_all_checks_passing_is_a_full_pass():
    result = MonitorResult.from_checks("geometry", [Check("a", PASS, "fine")])
    assert result.verdict == VERDICT_PASS
    assert result.passed is True
    assert result.conditional is False


def test_a_failure_outranks_an_unprovable():
    result = MonitorResult.from_checks(
        "integrity", [Check("a", FAIL, "broken"), Check("b", UNPROVABLE, "no data")]
    )
    assert result.verdict == VERDICT_FAIL
    assert result.acceptable is False


def test_the_reason_names_what_could_not_be_answered():
    result = MonitorResult.from_checks(
        "integrity",
        [Check("still_connected", UNPROVABLE, "no ports"),
         Check("access_preserved", UNPROVABLE, "no table")],
    )
    assert "cannot answer" in result.reason
    assert "still_connected" in result.reason and "access_preserved" in result.reason


# -- aggregation across monitors -------------------------------------------
def _result(monitor: str, verdict: str) -> MonitorResult:
    status = {VERDICT_PASS: PASS, VERDICT_CONDITIONAL: UNPROVABLE, VERDICT_FAIL: FAIL}[verdict]
    return MonitorResult.from_checks(monitor, [Check("c", status, "d")])


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ((VERDICT_PASS, VERDICT_PASS, VERDICT_PASS), VERDICT_PASS),
        ((VERDICT_PASS, VERDICT_PASS, VERDICT_CONDITIONAL), VERDICT_CONDITIONAL),
        ((VERDICT_CONDITIONAL, VERDICT_CONDITIONAL, VERDICT_CONDITIONAL), VERDICT_CONDITIONAL),
        ((VERDICT_PASS, VERDICT_FAIL, VERDICT_CONDITIONAL), VERDICT_FAIL),
        ((VERDICT_FAIL, VERDICT_FAIL, VERDICT_FAIL), VERDICT_FAIL),
    ],
)
def test_aggregate_never_rounds_a_conditional_up(verdicts, expected):
    results = {f"m{i}": _result(f"m{i}", v) for i, v in enumerate(verdicts)}
    assert aggregate(results) == expected


def test_unprovable_across_names_the_monitor_and_the_check():
    results = {
        "integrity": MonitorResult.from_checks(
            "integrity",
            [Check("still_connected", UNPROVABLE, "x"), Check("fall_preserved", PASS, "y")],
        ),
        "boundary": MonitorResult.from_checks(
            "boundary", [Check("neighbour_heads_current", UNPROVABLE, "z")]
        ),
    }
    assert unprovable_across(results) == [
        "boundary.neighbour_heads_current",
        "integrity.still_connected",
    ]


def test_run_all_returns_a_verdict_not_a_boolean(mesh_cache):
    """The boolean signature is what let every caller inherit the conflation."""
    from app.kit.engine import load_model
    from app.monitors import MonitorContext
    from tests.conftest import ALIASES, GENERATED

    model = load_model(
        GENERATED / "gravity_trap.ifc", cache_dir=mesh_cache, aliases_path=str(ALIASES)
    )
    ctx = MonitorContext(
        model=model,
        element_gid=model.elements[0].global_id,
        vector_mm=(1.0, 0.0, 0.0),
        zone_key="z",
        zone_gids=[e.global_id for e in model.elements],
        buffer_gids=[],
        neighbour_zone_gids={},
        rules=[],
    )

    class Passer(Monitor):
        name = "geometry"

        def run(self, _ctx):
            return MonitorResult.from_checks(self.name, [Check("a", PASS, "fine")])

    verdict, results = run_all([Passer()], ctx)
    assert verdict == VERDICT_PASS
    assert isinstance(verdict, str), "a boolean verdict cannot express 'conditional'"


# -- the resolver ----------------------------------------------------------
class Conditional(Monitor):
    def __init__(self, name):
        self.name = name

    def run(self, ctx):
        return MonitorResult.from_checks(
            self.name, [Check("ok", PASS, "fine"), Check("unknowable", UNPROVABLE, "no data")]
        )


class FullPass(Monitor):
    def __init__(self, name):
        self.name = name

    def run(self, ctx):
        return MonitorResult.from_checks(self.name, [Check("ok", PASS, "fine")])


def _clash(model):
    a, b = model.elements[0], model.elements[1]
    clash = Clash(
        model_version_id="mv", clash_key=f"{a.global_id}::{b.global_id}",
        a_gid=a.global_id, b_gid=b.global_id, kind="hard", required_gap_mm=10.0,
    )
    clash.id = "conditional-test"
    return clash


def _resolver(model, monitors):
    from app.agents.zone_resolver import ZoneResolver

    return ZoneResolver(
        zone_id="z", zone_key="L0|0_0", model=model,
        zone_gids=[e.global_id for e in model.elements], buffer_gids=[],
        neighbour_zone_gids={}, rules=[], max_attempts=3, monitors=monitors,
    )


@pytest.fixture
def trap_model(mesh_cache):
    from app.kit.engine import load_model
    from tests.conftest import ALIASES, GENERATED

    return load_model(
        GENERATED / "gravity_trap.ifc", cache_dir=mesh_cache, aliases_path=str(ALIASES)
    )


def test_the_resolver_marks_an_unprovable_sweep_conditional(trap_model):
    monitors = [Conditional(n) for n in ("geometry", "boundary", "integrity")]
    outcome = _resolver(trap_model, monitors).resolve_clash(_clash(trap_model))

    assert outcome.verdict == "verified_conditional", (
        "three monitors that each could not answer a check produced a full verification"
    )
    assert outcome.unprovable_checks == [
        "boundary.unknowable", "geometry.unknowable", "integrity.unknowable"
    ]


def test_the_resolver_still_fully_verifies_when_everything_is_checkable(trap_model):
    monitors = [FullPass(n) for n in ("geometry", "boundary", "integrity")]
    outcome = _resolver(trap_model, monitors).resolve_clash(_clash(trap_model))

    assert outcome.verdict == "verified"
    assert outcome.unprovable_checks == []


def test_one_conditional_monitor_is_enough_to_make_the_whole_thing_conditional(trap_model):
    monitors = [FullPass("geometry"), FullPass("boundary"), Conditional("integrity")]
    outcome = _resolver(trap_model, monitors).resolve_clash(_clash(trap_model))

    assert outcome.verdict == "verified_conditional"
    assert outcome.unprovable_checks == ["integrity.unknowable"]


def test_a_conditional_proposal_is_still_committed(trap_model):
    """It is a real proposal. It is not a verified one."""
    monitors = [Conditional(n) for n in ("geometry", "boundary", "integrity")]
    outcome = _resolver(trap_model, monitors).resolve_clash(_clash(trap_model))
    assert outcome.verdict == "verified_conditional"
    assert any(abs(v) > 0 for v in outcome.vector_mm), "a conditional must carry a real move"


def test_zone_results_count_conditionals_apart_from_verified():
    from app.agents.results import ZoneResult

    result = ZoneResult(zone_id="z", zone_key="k", branch=None)
    result.clashes_seen = 4
    result.verified = 1
    result.verified_conditional = 2
    result.escalated = 1

    assert result.accepted == 3
    assert result.resolve_rate == 0.75
    assert result.fully_verified_rate == 0.25, (
        "the fully-verified rate must not absorb conditionals"
    )
    assert result.as_dict()["verified_conditional"] == 2


# -- the review gate -------------------------------------------------------
def _zone_with_conditional(db, project, unprovable):
    from app.models import ModelVersion, Zone

    mv = ModelVersion(project_id=project.id, ifc_sha256="a" * 64, ifc_path="/x.ifc")
    db.add(mv)
    db.flush()
    zone = Zone(model_version_id=mv.id, zone_key="L0|0_0", status="awaiting_review")
    db.add(zone)
    db.flush()
    clash = Clash(
        model_version_id=mv.id, zone_id=zone.id, clash_key="a::b",
        a_gid="a", b_gid="b", kind="hard", state="verified_conditional",
    )
    db.add(clash)
    db.flush()
    db.add(
        Proposal(
            clash_id=clash.id, element_gid="a", move_vector=[10.0, 0.0, 0.0],
            verdict="verified_conditional", unprovable_checks=unprovable,
        )
    )
    db.flush()
    return zone, clash


def test_approving_a_conditional_without_acknowledgement_is_refused(db, project):
    from app.api.errors import AcknowledgementRequired

    zone, _ = _zone_with_conditional(db, project, ["integrity.still_connected"])
    with pytest.raises(AcknowledgementRequired) as exc:
        apply_review(
            db, zone, ReviewIn(decision="approve", reviewer="an engineer"), actor="k"
        )
    assert exc.value.status_code == 422
    assert exc.value.detail["unacknowledged"] == ["integrity.still_connected"]


def test_a_partial_acknowledgement_is_also_refused(db, project):
    from app.api.errors import AcknowledgementRequired

    zone, _ = _zone_with_conditional(
        db, project, ["integrity.still_connected", "integrity.access_preserved"]
    )
    with pytest.raises(AcknowledgementRequired) as exc:
        apply_review(
            db,
            zone,
            ReviewIn(
                decision="approve",
                reviewer="an engineer",
                acknowledge_unprovable=["integrity.still_connected"],
            ),
            actor="k",
        )
    assert exc.value.detail["unacknowledged"] == ["integrity.access_preserved"]


def test_acknowledging_every_check_lets_the_approval_through(db, project):
    checks = ["integrity.access_preserved", "integrity.still_connected"]
    zone, clash = _zone_with_conditional(db, project, checks)

    out = apply_review(
        db,
        zone,
        ReviewIn(decision="approve", reviewer="an engineer", acknowledge_unprovable=checks),
        actor="k",
    )
    db.flush()

    assert out.approved_conditionally == 1
    assert out.approved_fully == 0
    assert out.acknowledged == checks
    assert clash.state == "merged"


def test_a_fully_verified_zone_needs_no_acknowledgement(db, project):
    from app.models import ModelVersion, Zone

    mv = ModelVersion(project_id=project.id, ifc_sha256="a" * 64, ifc_path="/x.ifc")
    db.add(mv)
    db.flush()
    zone = Zone(model_version_id=mv.id, zone_key="L0|0_0", status="awaiting_review")
    db.add(zone)
    db.flush()
    clash = Clash(
        model_version_id=mv.id, zone_id=zone.id, clash_key="a::b",
        a_gid="a", b_gid="b", kind="hard", state="verified",
    )
    db.add(clash)
    db.flush()
    db.add(Proposal(clash_id=clash.id, element_gid="a", verdict="verified"))
    db.flush()

    out = apply_review(db, zone, ReviewIn(decision="approve", reviewer="e"), actor="k")
    assert out.approved_fully == 1
    assert out.approved_conditionally == 0


def test_unacknowledged_ignores_superseded_proposals(db, project):
    zone, clash = _zone_with_conditional(db, project, ["integrity.still_connected"])
    for p in db.query(Proposal).filter(Proposal.clash_id == clash.id):
        p.superseded = 1
    db.add(
        Proposal(
            clash_id=clash.id, element_gid="a", verdict="verified_conditional",
            unprovable_checks=["integrity.access_preserved"],
        )
    )
    db.flush()
    assert unacknowledged(db, zone, []) == ["integrity.access_preserved"]


# -- the deliverables ------------------------------------------------------
def test_the_change_set_says_which_entries_were_only_conditionally_verified(tmp_path):
    import json

    from app.review_package import CONDITIONAL, enrich_change_set

    path = tmp_path / "change_set.json"
    path.write_text(
        json.dumps({"entries": [{"clash_id": "a::b"}, {"clash_id": "c::d"}]}),
        encoding="utf-8",
    )
    enrich_change_set(
        path,
        {"a::b": {"verification": CONDITIONAL, "unprovable_checks": ["integrity.access_preserved"]}},
    )
    payload = json.loads(path.read_text(encoding="utf-8"))

    first, second = payload["entries"]
    assert first["verification"] == "conditional"
    assert first["unprovable_checks"] == ["integrity.access_preserved"]
    assert "not passes" in first["verification_note"]
    assert second["verification"] == "full"
    assert payload["verification_summary"] == {
        "entries": 2,
        "fully_verified": 1,
        "conditionally_verified": 1,
        "note": payload["verification_summary"]["note"],
    }
