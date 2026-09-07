"""Monitor behaviour, including the parts that must refuse to claim a pass."""
from __future__ import annotations

import pytest

from app.agents.coordinator import load_project_rules
from app.kit.engine import load_model, translated
from app.monitors import (
    FAIL,
    PASS,
    UNPROVABLE,
    BoundaryMonitor,
    GeometryMonitor,
    IntegrityMonitor,
    MonitorContext,
    MonitorResult,
    run_all,
)
from app.monitors.base import Check
from app.store.base import BranchHead
from tests.conftest import ALIASES, GENERATED


@pytest.fixture
def model(mesh_cache):
    return load_model(
        GENERATED / "gravity_trap.ifc", cache_dir=mesh_cache, aliases_path=str(ALIASES)
    )


def _ctx(model, gid, vector, **kw):
    gids = [e.global_id for e in model.elements]
    defaults = dict(
        model=model,
        element_gid=gid,
        vector_mm=vector,
        zone_key="L0|0_0",
        zone_gids=gids,
        buffer_gids=[],
        neighbour_zone_gids={},
        rules=load_project_rules(),
    )
    defaults.update(kw)
    return MonitorContext(**defaults)


# -- the three-valued check -----------------------------------------------
def test_unprovable_is_not_a_pass():
    """The name was always right; the assertion used to contradict it.

    This test previously asserted `passed is True` for a monitor with an
    unanswered check -- which is precisely the collapse it claims to guard
    against. See tests/unit/test_conditional_verdict.py.
    """
    result = MonitorResult.from_checks(
        "x", [Check("a", PASS, "fine"), Check("b", UNPROVABLE, "no data")]
    )
    assert result.passed is False
    assert result.conditional is True
    assert result.acceptable is True
    assert result.unprovable_checks == ["b"]
    assert "cannot answer" in result.reason


def test_any_failure_fails_the_monitor():
    result = MonitorResult.from_checks(
        "x", [Check("a", PASS, "fine"), Check("b", FAIL, "broken"), Check("c", FAIL, "also")]
    )
    assert result.passed is False
    assert result.acceptable is False
    assert "broken" in result.reason and "also" in result.reason


def test_run_all_runs_every_monitor_even_after_one_fails(model):
    class Boom(GeometryMonitor):
        name = "boom"

        def run(self, ctx):
            return MonitorResult.from_checks(self.name, [Check("x", FAIL, "no")])

    calls = []

    class Counting(IntegrityMonitor):
        name = "counting"

        def run(self, ctx):
            calls.append(1)
            return super().run(ctx)

    gid = model.elements[0].global_id
    verdict, results = run_all([Boom(), Counting()], _ctx(model, gid, (10.0, 0.0, 0.0)))
    assert verdict == "fail"
    assert calls, (
        "a later monitor was skipped after an earlier failure; the resolver needs "
        "every objection at once or it burns attempts discovering them one by one"
    )
    assert set(results) == {"boom", "counting"}


# -- geometry --------------------------------------------------------------
def test_geometry_fails_a_move_into_an_occupied_space(model):
    """Move the drain onto an element it does not currently touch.

    The target has to be one the drain is clear of today, or the monitor
    correctly reports the collision as a worsening of something that was already
    there rather than as a new finding -- and this test would be asserting the
    wrong half of the comparison.
    """
    drain = next(e for e in model.elements if e.is_gravity)
    target = next(e for e in model.elements if "CT-01" in e.name)

    def centre(el):
        return [(el.bbox[i] + el.bbox[i + 3]) / 2.0 for i in range(3)]

    vector = tuple((centre(target)[i] - centre(drain)[i]) * 1000.0 for i in range(3))

    result = GeometryMonitor().run(_ctx(model, drain.global_id, vector))
    assert result.passed is False
    introduced = next(c for c in result.checks if c.name == "no_new_clash")
    assert introduced.failed, (
        f"expected a new clash; checks were {[(c.name, c.status) for c in result.checks]}"
    )


def test_geometry_does_not_blame_a_move_for_a_pre_existing_clash(model):
    """A zero move changes nothing, so nothing can be introduced or worsened."""
    gid = model.elements[0].global_id
    result = GeometryMonitor().run(_ctx(model, gid, (0.0, 0.0, 0.0)))
    assert result.passed is True


def test_geometry_reports_a_missing_element_rather_than_passing_it(model):
    result = GeometryMonitor().run(_ctx(model, "no-such-element", (10.0, 0.0, 0.0)))
    assert result.passed is False
    assert any(c.name == "element_present" for c in result.checks)


# -- boundary --------------------------------------------------------------
def test_boundary_passes_when_there_are_no_neighbours(model):
    gid = model.elements[0].global_id
    result = BoundaryMonitor().run(_ctx(model, gid, (50.0, 0.0, 0.0)))
    assert result.passed is True
    assert any("no neighbouring zones" in c.detail for c in result.checks)
    assert result.unprovable_checks == []


def test_boundary_marks_head_currency_unprovable_without_a_store(model):
    a, b = model.elements[0], model.elements[1]
    result = BoundaryMonitor().run(
        _ctx(model, a.global_id, (10.0, 0.0, 0.0), neighbour_zone_gids={"L0|1_0": [b.global_id]})
    )
    check = next(c for c in result.checks if c.name == "neighbour_heads_current")
    assert check.status == UNPROVABLE, (
        "with no store the monitor is judging stale positions and must say so"
    )


def test_boundary_judges_neighbours_at_their_committed_positions(model, tmp_path):
    """The distinguishing behaviour: neighbours are read from the branch head."""
    from app.store.local import LocalModelStore

    store = LocalModelStore(tmp_path / "streams")
    stream = store.ensure_stream("mv1", "test")

    a, b = model.elements[0], model.elements[1]
    # Push the neighbour far away, so a move that would have hit it no longer does.
    store.commit(stream, "L0|1_0", b.global_id, (100000.0, 0.0, 0.0), "moved away")

    head = store.branch_head(stream, "L0|1_0")
    assert head.offset_for(b.global_id) == (100000.0, 0.0, 0.0)

    result = BoundaryMonitor().run(
        _ctx(
            model,
            a.global_id,
            (10.0, 0.0, 0.0),
            neighbour_zone_gids={"L0|1_0": [b.global_id]},
            store=store,
            stream=stream,
        )
    )
    check = next(c for c in result.checks if c.name == "neighbour_heads_current")
    assert check.status == PASS
    assert result.evidence["neighbour_elements_displaced"] == 1
    assert result.acceptable is True


# -- integrity -------------------------------------------------------------
def test_integrity_refuses_a_vertical_move_on_a_gravity_element(model):
    drain = next(e for e in model.elements if e.is_gravity)
    result = IntegrityMonitor().run(_ctx(model, drain.global_id, (0.0, 0.0, 100.0)))
    assert result.passed is False
    fall = next(c for c in result.checks if c.name == "fall_preserved")
    assert fall.status == FAIL


def test_integrity_allows_a_vertical_move_on_a_pressurised_element(model):
    duct = next(e for e in model.elements if not e.is_gravity)
    result = IntegrityMonitor().run(_ctx(model, duct.global_id, (0.0, 0.0, 5.0)))
    fall = next(c for c in result.checks if c.name == "fall_preserved")
    assert fall.status == PASS


def test_integrity_will_not_claim_connectivity_it_cannot_check(model):
    """These fixtures carry no ports. That is 'unprovable', never 'fine'."""
    gid = model.elements[0].global_id
    result = IntegrityMonitor().run(_ctx(model, gid, (5.0, 0.0, 0.0)))
    connected = next(c for c in result.checks if c.name == "still_connected")
    assert connected.status == UNPROVABLE
    assert "no port" in connected.detail


def test_integrity_will_not_invent_an_access_clearance(model):
    """With no sourced access table there is no number, so there is no verdict."""
    gid = model.elements[0].global_id
    result = IntegrityMonitor().run(_ctx(model, gid, (5.0, 0.0, 0.0)))
    access = next(c for c in result.checks if c.name == "access_preserved")
    assert access.status == UNPROVABLE
    assert "no sourced access-zone table" in access.detail


def test_integrity_reports_no_rules_as_unprovable_not_satisfied(model):
    gid = model.elements[0].global_id
    result = IntegrityMonitor().run(_ctx(model, gid, (5.0, 0.0, 0.0), rules=[]))
    clearances = next(c for c in result.checks if c.name == "clearances_satisfied")
    assert clearances.status == UNPROVABLE


# -- the shared translation ------------------------------------------------
def test_translating_an_element_does_not_move_the_original(model):
    element = model.elements[0]
    before_bounds = element.mesh.bounds.copy()
    before_box = element.bbox

    moved = translated(element, (1000.0, 0.0, 0.0))

    assert (element.mesh.bounds == before_bounds).all(), (
        "the shared model was mutated; every later judgement in this run would be "
        "measuring a building that does not exist"
    )
    assert element.bbox == before_box
    assert moved.bbox[0] == pytest.approx(before_box[0] + 1.0)


def test_branch_head_accumulates_repeated_moves_on_one_element(tmp_path):
    from app.store.local import LocalModelStore

    store = LocalModelStore(tmp_path / "s")
    stream = store.ensure_stream("mv", "n")
    store.commit(stream, "z", "el", (100.0, 0.0, 0.0), "first")
    store.commit(stream, "z", "el", (50.0, 25.0, 0.0), "second")

    head: BranchHead = store.branch_head(stream, "z")
    assert head.offset_for("el") == (150.0, 25.0, 0.0), (
        "a second move on the same element is a further displacement, not a replacement"
    )
    assert head.commit_count == 2
    assert head.offset_for("never-moved") == (0.0, 0.0, 0.0)
