"""Boundary arbitration: both zones, or no commit.

These use a deliberate stand-in monitor. That is not a shortcut around the real
ones -- the real monitors are exercised end to end in the acceptance suite. What
is under test here is the *aggregation*: whether the coordinator commits on one
pass or insists on two. To test that, one zone has to say no while the other says
yes, and the cleanest way to arrange a disagreement is to have a monitor that
disagrees.
"""
from __future__ import annotations

import pytest

from app.agents.coordinator import Coordinator, load_project_rules
from app.kit.engine import load_model
from app.ledger import history
from app.models import Clash, Proposal
from app.monitors import Monitor, MonitorResult
from app.monitors.base import FAIL, PASS, Check
from app.pipeline import run_pipeline
from tests.conftest import ALIASES, GENERATED, make_model_version


class AlwaysPasses(Monitor):
    name = "geometry"

    def run(self, ctx):
        return MonitorResult.from_checks(self.name, [Check("ok", PASS, "fine")])


class PassesExceptInOneZone(Monitor):
    """Objects only when asked about a named zone."""

    name = "boundary"

    def __init__(self, objecting_zone: str):
        self.objecting_zone = objecting_zone

    def run(self, ctx):
        if ctx.zone_key == self.objecting_zone:
            return MonitorResult.from_checks(
                self.name, [Check("neighbour", FAIL, f"not acceptable in {ctx.zone_key}")]
            )
        return MonitorResult.from_checks(self.name, [Check("neighbour", PASS, "fine")])


class Integrity(Monitor):
    name = "integrity"

    def run(self, ctx):
        return MonitorResult.from_checks(self.name, [Check("ok", PASS, "fine")])


@pytest.fixture
def boundary_clash(db, project, settings, store):
    """A model version whose clash genuinely spans two zones."""
    # One element per zone, or the two adjacent cells merge into a single zone
    # and there is no boundary left to arbitrate across.
    settings.max_elements_per_zone = 1
    mv = make_model_version(db, project, GENERATED / "arbitration_trap.ifc")
    run_pipeline(db, mv, store=store, top_n=10, settings=settings)
    db.commit()

    shared = [
        c
        for c in db.query(Clash).filter(Clash.model_version_id == mv.id)
        if c.owner == "coordinator"
    ]
    assert shared, "the trap did not produce a clash shared between two zones"
    return mv, shared[0]


def _model(db, settings, store, mv):
    coordinator = Coordinator(db, settings=settings, store=store)
    return coordinator, load_model(
        mv.ifc_path, cache_dir=coordinator._cache_dir(), aliases_path=str(ALIASES)
    )


def test_arbitration_asks_both_zones(db, settings, store, boundary_clash):
    mv, clash = boundary_clash
    coordinator, model = _model(db, settings, store, mv)
    coordinator.monitors = [AlwaysPasses(), PassesExceptInOneZone("nowhere"), Integrity()]

    db.add(
        Proposal(
            clash_id=clash.id, element_gid=clash.a_gid, move_vector=[50.0, 0.0, 0.0],
            verdict="pending",
        )
    )
    db.flush()

    result = coordinator.arbitrate(clash, model, load_project_rules())
    assert len(result.zones_checked) == 2, (
        f"arbitration consulted {result.zones_checked}; a boundary decision needs both sides"
    )
    assert result.committed is True
    assert set(result.monitors) == set(result.zones_checked)


def test_a_single_zone_objection_blocks_the_commit(db, settings, store, boundary_clash):
    """One zone happy and the other not asked is the failure zoning introduces."""
    mv, clash = boundary_clash
    coordinator, model = _model(db, settings, store, mv)

    plans = coordinator._plans_for(mv.id)
    owning = [p for p in plans if clash.a_gid in p.element_gids or clash.b_gid in p.element_gids]
    objector = owning[-1].zone_key

    coordinator.monitors = [AlwaysPasses(), PassesExceptInOneZone(objector), Integrity()]
    db.add(
        Proposal(
            clash_id=clash.id, element_gid=clash.a_gid, move_vector=[50.0, 0.0, 0.0],
            verdict="pending",
        )
    )
    db.flush()

    result = coordinator.arbitrate(clash, model, load_project_rules())
    db.flush()

    assert result.committed is False, (
        "a proposal that one of the two zones rejected was committed anyway"
    )
    assert objector in result.reason
    assert clash.state == "escalated"

    events = history(db, "clash", clash.id)
    assert events[-1].actor == "coordinator"
    assert events[-1].payload["arbitration"] == "rejected"
    assert events[-1].payload["objections"]


def test_a_clash_that_is_not_actually_shared_is_not_arbitrated(
    db, project, settings, store
):
    mv = make_model_version(db, project, GENERATED / "gravity_trap.ifc")
    run_pipeline(db, mv, store=store, top_n=10, settings=settings)
    db.commit()

    clash = db.query(Clash).filter(Clash.model_version_id == mv.id).first()
    coordinator, model = _model(db, settings, store, mv)
    db.add(
        Proposal(clash_id=clash.id, element_gid=clash.a_gid, move_vector=[1.0, 0.0, 0.0])
    )
    db.flush()

    result = coordinator.arbitrate(clash, model, load_project_rules())
    assert result.committed is False
    assert "not actually shared" in result.reason


def test_arbitration_with_no_proposal_does_not_commit(db, settings, store, boundary_clash):
    mv, clash = boundary_clash
    coordinator, model = _model(db, settings, store, mv)
    db.query(Proposal).filter(Proposal.clash_id == clash.id).delete()
    db.flush()

    result = coordinator.arbitrate(clash, model, load_project_rules())
    assert result.committed is False
    assert "no proposal" in result.reason
