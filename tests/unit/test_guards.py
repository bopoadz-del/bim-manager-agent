"""Guards that the mutation probes found nothing else testing.

Every test in this file exists because ``scripts/mutation_probes.py`` broke a
safety property and the suite stayed green. They are written against the
property directly rather than against a scenario that happens to exercise it,
because a scenario can stop exercising it without anyone noticing.
"""
from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta

import pytest

from app.agents.zone_resolver import ZoneResolver
from app.kit.engine import load_model
from app.ledger import history
from app.models import Clash, LedgerEvent
from app.monitors import Monitor, MonitorResult
from app.monitors.base import FAIL, Check
from tests.conftest import ALIASES, GENERATED


class AlwaysObjects(Monitor):
    name = "geometry"

    def __init__(self):
        self.calls = 0

    def run(self, ctx):
        self.calls += 1
        return MonitorResult.from_checks(self.name, [Check("no", FAIL, "never acceptable")])


# -- the retry cap ---------------------------------------------------------
@pytest.mark.parametrize("cap", [1, 2, 3])
def test_the_resolver_stops_at_the_retry_cap(mesh_cache, cap):
    """The cap is a budget, not a hint.

    Without it one unsolvable clash consumes the whole candidate search -- 72
    monitored evaluations on a real model -- while the solvable clashes behind
    it wait. The acceptance run escalates either way, so nothing there notices;
    this counts the attempts.
    """
    model = load_model(
        GENERATED / "gravity_trap.ifc", cache_dir=mesh_cache, aliases_path=str(ALIASES)
    )
    a, b = model.elements[0], model.elements[1]
    clash = Clash(
        model_version_id="mv", clash_key=f"{a.global_id}::{b.global_id}",
        a_gid=a.global_id, b_gid=b.global_id, kind="hard", required_gap_mm=100.0,
    )
    clash.id = "cap-test"

    monitor = AlwaysObjects()
    resolver = ZoneResolver(
        zone_id="z", zone_key="L0|0_0", model=model,
        zone_gids=[e.global_id for e in model.elements], buffer_gids=[],
        neighbour_zone_gids={}, rules=[], max_attempts=cap, monitors=[monitor],
    )
    outcome = resolver.resolve_clash(clash)

    assert outcome.verdict == "escalated"
    assert outcome.attempt == cap, f"expected exactly {cap} attempts, made {outcome.attempt}"
    assert monitor.calls == cap, f"the monitor ran {monitor.calls} times for a cap of {cap}"
    assert len(outcome.rejected_attempts) == cap


def test_an_escalated_clash_still_reports_what_was_tried(mesh_cache):
    model = load_model(
        GENERATED / "gravity_trap.ifc", cache_dir=mesh_cache, aliases_path=str(ALIASES)
    )
    a, b = model.elements[0], model.elements[1]
    clash = Clash(
        model_version_id="mv", clash_key="x::y", a_gid=a.global_id, b_gid=b.global_id,
        kind="hard", required_gap_mm=100.0,
    )
    clash.id = "alt-test"
    resolver = ZoneResolver(
        zone_id="z", zone_key="L0|0_0", model=model,
        zone_gids=[e.global_id for e in model.elements], buffer_gids=[],
        neighbour_zone_gids={}, rules=[], max_attempts=3, monitors=[AlwaysObjects()],
    )
    outcome = resolver.resolve_clash(clash)

    assert len(outcome.alternatives) == 3
    for alt in outcome.alternatives:
        assert alt["objections"], "an escalation must say what objected"
        assert alt["displacement_mm"] > 0


# -- ledger ordering -------------------------------------------------------
def test_history_is_ordered_by_sequence_not_by_clock(db):
    """A clock that steps backwards must not reorder history.

    Timestamps are not a safe sort key: several transitions routinely land in
    the same microsecond, and a clock correction can make a later event carry an
    earlier time. The sequence is monotonic by construction.
    """
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    for i, state in enumerate(["open", "proposed", "verified", "merged"]):
        db.add(
            LedgerEvent(
                entity="clash",
                entity_id="c1",
                to_state=state,
                # Deliberately going backwards in time.
                ts=base - timedelta(seconds=i),
            )
        )
        db.flush()

    assert [e.to_state for e in history(db, "clash", "c1")] == [
        "open", "proposed", "verified", "merged"
    ]


def test_sequence_numbers_increase_across_entities(db):
    db.add(LedgerEvent(entity="zone", entity_id="z1", to_state="queued"))
    db.flush()
    db.add(LedgerEvent(entity="clash", entity_id="c1", to_state="open"))
    db.flush()
    rows = db.query(LedgerEvent).order_by(LedgerEvent.seq).all()
    assert [r.seq for r in rows] == sorted(r.seq for r in rows)
    assert len(set(r.seq for r in rows)) == len(rows), "sequence numbers must be unique"


# -- vendor pin ------------------------------------------------------------
def test_the_vendor_check_actually_detects_an_edited_block(tmp_path, monkeypatch):
    """Running --check on a clean tree proves nothing about what it would catch."""
    import scripts.vendor_kit as vk

    dest = tmp_path / "mep_coordination"
    shutil.copytree(vk.DEST, dest)
    lock = tmp_path / "VENDOR.lock"
    shutil.copy(vk.LOCK, lock)

    monkeypatch.setattr(vk, "DEST", dest)
    monkeypatch.setattr(vk, "LOCK", lock)
    assert vk.check() == 0, "the copied tree should match its own lock"

    target = dest / "geometry_engine.py"
    target.write_text(
        target.read_text(encoding="utf-8") + "\n# a quiet local edit\n", encoding="utf-8"
    )
    assert vk.check() == 1, "an edited vendored block passed the pin check"


def test_the_vendor_check_detects_a_deleted_block(tmp_path, monkeypatch):
    import scripts.vendor_kit as vk

    dest = tmp_path / "mep_coordination"
    shutil.copytree(vk.DEST, dest)
    lock = tmp_path / "VENDOR.lock"
    shutil.copy(vk.LOCK, lock)
    monkeypatch.setattr(vk, "DEST", dest)
    monkeypatch.setattr(vk, "LOCK", lock)

    (dest / "connectivity.py").unlink()
    assert vk.check() == 1


def test_the_vendor_check_detects_an_added_file(tmp_path, monkeypatch):
    import scripts.vendor_kit as vk

    dest = tmp_path / "mep_coordination"
    shutil.copytree(vk.DEST, dest)
    lock = tmp_path / "VENDOR.lock"
    shutil.copy(vk.LOCK, lock)
    monkeypatch.setattr(vk, "DEST", dest)
    monkeypatch.setattr(vk, "LOCK", lock)

    (dest / "extra_block.py").write_text("# smuggled in\n", encoding="utf-8")
    assert vk.check() == 1


def test_the_vendor_check_ignores_generated_bytecode(tmp_path, monkeypatch):
    """Importing the kit writes __pycache__. That is not drift."""
    import scripts.vendor_kit as vk

    dest = tmp_path / "mep_coordination"
    shutil.copytree(vk.DEST, dest)
    lock = tmp_path / "VENDOR.lock"
    shutil.copy(vk.LOCK, lock)
    monkeypatch.setattr(vk, "DEST", dest)
    monkeypatch.setattr(vk, "LOCK", lock)

    cache = dest / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "geometry_engine.cpython-312.pyc").write_bytes(b"\x00\x01")
    assert vk.check() == 0


def test_the_lock_records_a_full_commit_sha():
    import scripts.vendor_kit as vk

    lock = json.loads(vk.LOCK.read_text(encoding="utf-8"))
    assert len(lock["sha"]) == 40, "an abbreviated sha is not a pin"
    assert lock["files"], "the lock lists no files"


# -- integrity paths that only run when the data exists --------------------
def test_connectivity_is_checked_when_the_model_carries_ports(mesh_cache):
    """The other half of the unprovable branch.

    Almost every model omits IfcDistributionPort, so the monitor almost always
    reports connectivity as unprovable -- and the code that runs when ports *are*
    present would otherwise never execute in a test.
    """
    from app.monitors import PASS, UNPROVABLE, MonitorContext
    from app.monitors.integrity import IntegrityMonitor

    model = load_model(GENERATED / "connected_trap.ifc", cache_dir=mesh_cache)
    assert model.graph.available, "the fixture lost its ports"

    a, b = model.elements[0], model.elements[1]
    gids = [a.global_id, b.global_id]

    def ctx(vector):
        return MonitorContext(
            model=model, element_gid=a.global_id, vector_mm=vector,
            zone_key="L0|0_0", zone_gids=gids, buffer_gids=[],
            neighbour_zone_gids={}, rules=[],
        )

    moved = IntegrityMonitor().run(ctx((500.0, 0.0, 0.0)))
    connected = next(c for c in moved.checks if c.name == "still_connected")
    assert connected.status not in (UNPROVABLE,), "ports are present; this is checkable"
    assert connected.failed, "moving one end of a joined pair away must break the joint"
    assert b.global_id in connected.data["partners"]

    still = IntegrityMonitor().run(ctx((0.1, 0.0, 0.0)))
    unmoved = next(c for c in still.checks if c.name == "still_connected")
    assert unmoved.status == PASS, "a sub-millimetre move is within joint tolerance"


def test_an_element_with_no_recorded_joints_passes_connectivity(mesh_cache):
    from app.monitors import PASS, MonitorContext
    from app.monitors.integrity import IntegrityMonitor

    model = load_model(GENERATED / "connected_trap.ifc", cache_dir=mesh_cache)
    lone = "not-in-the-graph"

    ctx = MonitorContext(
        model=model, element_gid=model.elements[0].global_id, vector_mm=(0.0, 0.0, 0.0),
        zone_key="z", zone_gids=[e.global_id for e in model.elements], buffer_gids=[],
        neighbour_zone_gids={}, rules=[],
    )
    result = IntegrityMonitor().run(ctx)
    assert next(c for c in result.checks if c.name == "still_connected").status == PASS
    assert lone not in str(result.as_dict())


def test_access_zones_are_checked_when_a_sourced_table_is_supplied(mesh_cache):
    """With a table the check runs; the fixture table says so about itself."""
    from app.blocks import clearance_rules
    from app.monitors import FAIL, PASS, MonitorContext
    from app.monitors.integrity import IntegrityMonitor
    from tests.conftest import FIXTURE_DIR

    access = clearance_rules.load_rules(str(FIXTURE_DIR / "access_rules.json"))
    assert access, "the fixture access table failed to load"

    model = load_model(GENERATED / "connected_trap.ifc", cache_dir=mesh_cache)
    a, b = model.elements[0], model.elements[1]

    def ctx(vector):
        return MonitorContext(
            model=model, element_gid=a.global_id, vector_mm=vector,
            zone_key="z", zone_gids=[a.global_id, b.global_id], buffer_gids=[],
            neighbour_zone_gids={}, rules=[], access_rules=access,
        )

    close = IntegrityMonitor().run(ctx((0.0, 0.0, 0.0)))
    breach = next(c for c in close.checks if c.name == "access_preserved")
    assert breach.status == FAIL, "touching ducts cannot satisfy a 600 mm access rule"
    assert breach.data["violations"][0]["rule_id"] == "TEST-ACCESS-DUCT-600"

    far = IntegrityMonitor().run(ctx((0.0, 5000.0, 0.0)))
    assert next(c for c in far.checks if c.name == "access_preserved").status == PASS


def test_a_malformed_alias_table_raises_rather_than_being_ignored(tmp_path):
    from app.kit.systems import InvalidAliasTable, load_system_aliases

    bad_json = tmp_path / "bad.json"
    bad_json.write_text("{not json", encoding="utf-8")
    with pytest.raises(InvalidAliasTable):
        load_system_aliases(bad_json)

    wrong_shape = tmp_path / "shape.json"
    wrong_shape.write_text(json.dumps({"aliases": "nope"}), encoding="utf-8")
    with pytest.raises(InvalidAliasTable):
        load_system_aliases(wrong_shape)

    bad_regex = tmp_path / "regex.json"
    bad_regex.write_text(
        json.dumps({"aliases": [{"pattern": "([unclosed", "system": "x"}]}), encoding="utf-8"
    )
    with pytest.raises(InvalidAliasTable):
        load_system_aliases(bad_regex)

    missing_key = tmp_path / "missing.json"
    missing_key.write_text(json.dumps({"aliases": [{"pattern": "x"}]}), encoding="utf-8")
    with pytest.raises(InvalidAliasTable):
        load_system_aliases(missing_key)


def test_an_absent_alias_table_is_simply_no_aliases(tmp_path):
    from app.kit.systems import load_system_aliases

    assert load_system_aliases(None) == []
    assert load_system_aliases(tmp_path / "nope.json") == []
