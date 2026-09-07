"""A1-A9. Evidence, not assertions.

Every test here writes what it measured into ``artifacts/mep-judge/evidence/``
so ACCEPTANCE.md quotes numbers a reader can regenerate rather than numbers
somebody typed. No mocks: the monitors, the geometry engine and the model store
are the real ones throughout, and the IFC files are real IFC files.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.blocks.ifc_loader import model_sha256

from app.agents.coordinator import Coordinator, load_project_rules
from app.kit.engine import load_model
from app.ledger import history
from app.models import Clash, Proposal, Zone
from app.pipeline import run_pipeline
from tests.conftest import ALIASES, GENERATED, SCHEPENDOMLAAN, make_model_version, requires

EVIDENCE: dict[str, dict] = {}


#: Evidence is written into the repository, not the per-test temp directory, so
#: ACCEPTANCE.md can quote numbers a reader is able to regenerate and diff.
EVIDENCE_DIR = Path(__file__).resolve().parent.parent.parent / "artifacts" / "mep-judge" / "evidence"


def _evidence(settings, name: str, payload: dict) -> None:
    EVIDENCE[name] = payload
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    (EVIDENCE_DIR / f"{name}.json").write_text(
        json.dumps(payload, indent=2, default=str), encoding="utf-8"
    )


# -- A1 --------------------------------------------------------------------
@requires(SCHEPENDOMLAAN)
@pytest.mark.acceptance
@pytest.mark.slow
def test_A1_schependomlaan_through_the_full_pipeline(db, project, settings, store):
    """Zones created, queue ordered by congestion, a zone reaches review verified."""
    mv = make_model_version(db, project, SCHEPENDOMLAAN)
    result = run_pipeline(db, mv, store=store, top_n=8)
    db.commit()

    zones = db.query(Zone).filter(Zone.model_version_id == mv.id).all()
    assert zones, "no zones were created"

    dispatched = sorted(
        [z for z in zones if z.status != "queued"], key=lambda z: -z.priority
    )
    priorities = [z.priority for z in dispatched]
    assert priorities == sorted(priorities, reverse=True), "the queue was not worked in priority order"

    reviewed = [z for z in zones if z.status == "awaiting_review"]
    assert reviewed, "no zone reached awaiting_review"

    proposals = (
        db.query(Proposal)
        .join(Clash, Clash.id == Proposal.clash_id)
        .filter(Clash.model_version_id == mv.id, Proposal.superseded == 0)
        .all()
    )
    fully = [p for p in proposals if p.verdict == "verified"]
    conditional = [p for p in proposals if p.verdict == "verified_conditional"]
    assert conditional, "no proposal was accepted by all three monitors"

    # A02. This model carries no IfcDistributionPort and the project supplied no
    # access table, so integrity cannot answer two of its checks on any element.
    # Every acceptance here is therefore conditional, and none may be reported as
    # a full verification.
    assert not fully, (
        f"{len(fully)} proposals claim full verification on a model that cannot "
        f"answer connectivity or access for any element"
    )
    for proposal in conditional:
        for field in ("monitor_geometry", "monitor_boundary", "monitor_integrity"):
            evidence = getattr(proposal, field)
            assert evidence is not None, f"accepted proposal has no {field}"
            assert evidence["verdict"] != "fail", f"accepted proposal has a failing {field}"
        assert "integrity.still_connected" in proposal.unprovable_checks
        assert "integrity.access_preserved" in proposal.unprovable_checks

    _evidence(
        settings,
        "A1_schependomlaan",
        {
            "elements": mv.element_count,
            "ifc_sha256": mv.ifc_sha256,
            "zones": len(zones),
            "zones_awaiting_review": len(reviewed),
            "clashes": result.clashes_created,
            "joints_excluded": result.joints_excluded,
            "pairs_admitted": result.pairs_admitted,
            "pairs_possible": result.stats["pairs_possible"],
            "proposals_verified": len(fully),
            "proposals_conditional": len(conditional),
            "provable_check_ratio": result.provable_check_ratio,
            "unprovable_checks": sorted(
                {c for p in conditional for c in (p.unprovable_checks or [])}
            ),
            "boundary_owned": result.boundary_owned,
            "rules_never_applied": result.stats["rules_never_applied"],
            "top_zone_priorities": [
                {"zone": z.zone_key, "priority": z.priority, "congestion": z.congestion,
                 "elements": z.element_count, "status": z.status}
                for z in dispatched[:8]
            ],
        },
    )


# -- A2 --------------------------------------------------------------------
@pytest.mark.acceptance
def test_A2_boundary_trap_is_caught_by_the_boundary_monitor(db, project, settings, store):
    """The only local fix pushes into zone B. BoundaryMonitor must object."""
    mv = make_model_version(db, project, GENERATED / "boundary_trap.ifc")
    run_pipeline(db, mv, store=store, top_n=20, settings=_tight_zones(settings))
    db.commit()

    clashes = db.query(Clash).filter(Clash.model_version_id == mv.id).all()
    assert clashes, "the trap produced no clash at all"

    proposals = (
        db.query(Proposal)
        .join(Clash, Clash.id == Proposal.clash_id)
        .filter(Clash.model_version_id == mv.id)
        .all()
    )
    boundary_objections = [
        p for p in proposals
        if p.monitor_boundary is not None and p.monitor_boundary["passed"] is False
    ]
    escalated = [c for c in clashes if c.state == "escalated"]

    assert boundary_objections, (
        "the BoundaryMonitor never objected. The trap's only workable move crosses "
        "into zone B, so if nothing objected the monitor is not doing its job -- or "
        "the resolver found a local fix the trap was built to deny."
    )
    assert escalated, "the clash was neither resolved nor escalated"

    # Whatever happened is in the ledger, in order.
    sequences = {
        c.clash_key: [(e.from_state, e.to_state, e.actor) for e in history(db, "clash", c.id)]
        for c in clashes
    }
    assert all(seq for seq in sequences.values()), "a clash has no ledger history"

    _evidence(
        settings,
        "A2_boundary_trap",
        {
            "clashes": len(clashes),
            "proposals": len(proposals),
            "boundary_objections": len(boundary_objections),
            "escalated": len(escalated),
            "objection_reasons": [
                p.monitor_boundary["reason"] for p in boundary_objections[:5]
            ],
            "alternatives_tried": [
                {"clash": c.clash_key, "attempts": c.attempts, "state": c.state}
                for c in clashes
            ],
            "ledger": sequences,
        },
    )


def _tight_zones(settings):
    """Force several zones out of a small model so boundaries actually exist."""
    settings.max_elements_per_zone = 6
    settings.zone_buffer_m = 2.0
    return settings


# -- A3 --------------------------------------------------------------------
@pytest.mark.acceptance
def test_A3_a_move_that_reverses_fall_is_refused(db, project, settings, store):
    """Two guards, tested separately: B4 never offers Z, the monitor refuses it.

    The resolver alone would pass this test without the monitor existing,
    because ``candidate_moves`` withholds the vertical axis from a gravity
    element. That is exactly why the monitor is checked directly as well: it is
    the backstop, and a backstop nobody tests is a comment.
    """
    from app.blocks.clash_resolver import candidate_moves

    from app.monitors import MonitorContext
    from app.monitors.integrity import IntegrityMonitor

    mv = make_model_version(db, project, GENERATED / "gravity_trap.ifc")
    coordinator = Coordinator(db, settings=settings, store=store)
    rules = load_project_rules()
    model = load_model(mv.ifc_path, cache_dir=coordinator._cache_dir(), aliases_path=str(ALIASES))

    drain = next(e for e in model.elements if e.is_gravity)
    others = [e.global_id for e in model.elements]

    # Guard 1: no candidate the resolver would ever offer has a Z component.
    offered = candidate_moves(None, drain, 300.0)
    vertical = [v for _, v in offered if abs(v[2]) > 1e-9]
    assert not vertical, f"{len(vertical)} candidate moves would lift a gravity element"

    # Guard 2: the monitor refuses one anyway.
    ctx = MonitorContext(
        model=model,
        element_gid=drain.global_id,
        vector_mm=(0.0, 0.0, 250.0),
        zone_key="L0|test",
        zone_gids=others,
        buffer_gids=[],
        neighbour_zone_gids={},
        rules=rules,
    )
    result = IntegrityMonitor().run(ctx)
    fall = next(c for c in result.checks if c.name == "fall_preserved")

    assert result.passed is False, "IntegrityMonitor accepted a move that reverses fall"
    assert fall.status == "fail"

    # And a lateral move of the same size is fine, so the monitor is refusing
    # the vertical component rather than the displacement.
    lateral = IntegrityMonitor().run(
        MonitorContext(
            model=model, element_gid=drain.global_id, vector_mm=(250.0, 0.0, 0.0),
            zone_key="L0|test", zone_gids=others, buffer_gids=[],
            neighbour_zone_gids={}, rules=rules,
        )
    )
    lateral_fall = next(c for c in lateral.checks if c.name == "fall_preserved")
    assert lateral_fall.status == "pass"

    _evidence(
        settings,
        "A3_gravity_trap",
        {
            "element": drain.name,
            "system": drain.system,
            "candidates_offered": len(offered),
            "candidates_with_vertical_component": len(vertical),
            "vertical_move_verdict": fall.status,
            "vertical_move_reason": fall.detail,
            "lateral_move_verdict": lateral_fall.status,
        },
    )


# -- A4 --------------------------------------------------------------------
@pytest.mark.acceptance
def test_A4_a_move_that_needs_a_stripped_rule_is_flagged_not_proposed(
    db, project, settings, store, tmp_path
):
    """With the rule: a clearance violation with a clause. Without it: flagged.

    Stripping only the 400 mm gas-to-LV rule is not enough, and finding that out
    is part of the point: the 300 mm gas-to-anything wildcard still governs the
    pair, so the finding stays sourced and stays a violation. That is correct --
    a table with one rule removed is not a table with no rule. To reach the
    ungoverned state the trap is about, every rule that can govern the pair is
    removed, and then the resolver must refuse to attach a distance to it.
    """
    from app.blocks import clearance_rules

    ifc = GENERATED / "unsourced_trap.ifc"
    full = load_project_rules()
    assert any(r.rule_id == "MEP-GAS-LV-400" for r in full)

    # The same model, judged with the governing rule removed.
    stripped_path = tmp_path / "stripped_rules.json"
    governing = {"MEP-GAS-LV-400", "MEP-GAS-ANY-300"}
    kept = [
        {
            "rule_id": r.rule_id, "system_a": r.system_a, "system_b": r.system_b,
            "min_gap_mm": r.min_gap_mm, "axis": r.axis, "precedence": r.precedence,
            "source": {"doc": r.source.doc, "clause": r.source.clause,
                       "text_hash": r.source.text_hash},
        }
        for r in full if r.rule_id not in governing
    ]
    stripped_path.write_text(json.dumps(kept, indent=2), encoding="utf-8")
    stripped = clearance_rules.load_rules(str(stripped_path))
    assert not any(r.rule_id in governing for r in stripped)
    assert stripped, "the stripped table must still be a real table, not an empty one"

    coordinator = Coordinator(db, settings=settings, store=store)
    model = load_model(ifc, cache_dir=coordinator._cache_dir(), aliases_path=str(ALIASES))

    from app.kit.engine import full_pass

    with_rule = full_pass(model, full)
    without_rule = full_pass(model, stripped)

    governed = [f for f in with_rule["findings"] if f.rule_id == "MEP-GAS-LV-400"]
    assert governed, "the trap did not produce the finding the rule governs"

    unsourced_clearance = [
        f for f in without_rule["findings"] if f.kind == "clearance" and not f.rule_id
    ]
    assert not unsourced_clearance, "a clearance violation was reported with no rule behind it"
    assert not [f for f in without_rule["findings"] if f.kind == "clearance"], (
        "with every governing rule removed there is no authorised distance, "
        "so there must be no clearance violation to propose a move for"
    )

    # And the resolver refuses to turn an unsourced clearance into a proposal.
    from app.agents.zone_resolver import VERDICT_FLAGGED, ZoneResolver

    clash = Clash(
        model_version_id="x", zone_id=None, clash_key="a::b",
        a_gid=model.elements[0].global_id, b_gid=model.elements[1].global_id,
        kind="clearance", rule_id=None, required_gap_mm=None,
    )
    clash.id = "unsourced-test"
    resolver = ZoneResolver(
        zone_id="z", zone_key="L0|0_0", model=model,
        zone_gids=[e.global_id for e in model.elements], buffer_gids=[],
        neighbour_zone_gids={}, rules=stripped,
    )
    outcome = resolver.resolve_clash(clash)
    assert outcome.verdict == VERDICT_FLAGGED
    assert outcome.vector_mm == (0.0, 0.0, 0.0), "a flagged finding must carry no move"

    _evidence(
        settings,
        "A4_unsourced_trap",
        {
            "with_rule": {
                "rule_id": governed[0].rule_id,
                "kind": governed[0].kind,
                "distance_m": governed[0].distance_m,
                "required_m": governed[0].required_clearance_m,
            },
            "rules_stripped": sorted(governing),
            "without_rule": {
                "clearance_findings": sum(
                    1 for f in without_rule["findings"] if f.kind == "clearance"
                ),
                "resolver_verdict": outcome.verdict,
                "reason": outcome.alternatives,
            },
        },
    )


# -- A6 --------------------------------------------------------------------
@pytest.mark.acceptance
def test_A6_the_original_model_is_never_written_to(db, project, settings, store):
    """The read-only promise, as evidence rather than assertion."""
    ifc = GENERATED / "gravity_trap.ifc"
    before = model_sha256(ifc)
    before_bytes = Path(ifc).stat().st_size

    mv = make_model_version(db, project, ifc)
    run_pipeline(db, mv, store=store, top_n=20)
    db.commit()

    after = model_sha256(ifc)
    assert after == before, "the pipeline modified the model of record"
    assert Path(ifc).stat().st_size == before_bytes

    zone = db.query(Zone).filter(Zone.model_version_id == mv.id).first()
    coordinator = Coordinator(db, settings=settings, store=store)
    model = load_model(mv.ifc_path, cache_dir=coordinator._cache_dir(), aliases_path=str(ALIASES))
    package = coordinator.assemble_review_package(zone, model)

    assert package["original_ifc_sha256"] == before, (
        "the review package must show the reviewer the hash of the untouched original"
    )
    assert Path(package["change_set"]).exists()
    assert Path(package["bcf"]).exists()

    _evidence(
        settings,
        "A6_read_only",
        {
            "sha256_before": before,
            "sha256_after": after,
            "identical": after == before,
            "shown_in_review_package": package["original_ifc_sha256"],
            "change_set": package["change_set"],
            "bcf": package["bcf"],
        },
    )


# -- A7 --------------------------------------------------------------------
@pytest.mark.acceptance
def test_A7_diff_marks_the_planted_fix_and_the_planted_regression(
    db, project, settings, store
):
    v1 = make_model_version(db, project, GENERATED / "diff_v1.ifc")
    run_pipeline(db, v1, store=store, top_n=20)
    db.commit()

    v2 = make_model_version(db, project, GENERATED / "diff_v2.ifc")
    v2.parent_id = v1.id
    run_pipeline(db, v2, store=store, top_n=20)
    db.commit()

    coordinator = Coordinator(db, settings=settings, store=store)
    diff = coordinator.diff_against(v2, v1)
    db.commit()

    assert diff.resolved >= 1, "the planted fix was not recognised as resolved"
    assert diff.regressed >= 1, (
        "the planted regression was not recognised; a pair that was measured clear "
        "and is now a clash must not be filed as merely new"
    )

    _evidence(
        settings,
        "A7_version_diff",
        {
            "v1": v1.ifc_sha256, "v2": v2.ifc_sha256,
            "resolved": diff.resolved, "regressed": diff.regressed,
            "new": diff.new, "persisting": diff.persisting,
            "proposal_score": diff.proposal_score,
        },
    )


# -- A8 (the part that does not need a container) -------------------------
@pytest.mark.acceptance
def test_A8_health_reports_the_build_sha_and_what_is_degraded(client, settings):
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()

    assert body["build_sha"] == settings.build_sha
    assert body["database"] == "ok"
    assert body["vendored_kit"]["pinned"] is True
    assert len(body["vendored_kit"]["sha"]) == 40
    assert body["exact_geometry_backend"] is True
    # With no Speckle token the store must say local, and say why.
    assert body["store"]["backend"] == "local"
    assert body["store"]["reason"]

    _evidence(settings, "A8_health", body)


def test_A8_health_needs_no_api_key(client):
    """An operator's monitoring must not need a project credential."""
    response = client.get("/health", headers={"X-API-Key": ""})
    assert response.status_code == 200


# -- A5 --------------------------------------------------------------------
@pytest.mark.acceptance
def test_A5_a_neighbour_commit_sends_a_verified_proposal_back_to_proposed(
    db, project, settings, store
):
    """The verdict was true when it was made. Something else moved.

    Zone B verifies a move against the neighbourhood as it stands. Zone A then
    commits a move into the space B just claimed. Nothing about B changed, and
    B's verdict is now wrong -- so it must not survive. The monitors are re-run
    for real; a proposal that still holds keeps its verdict, and only one that
    no longer passes is sent back.
    """
    settings.max_elements_per_zone = 2
    settings.zone_buffer_m = 2.0

    mv = make_model_version(db, project, GENERATED / "rebase_trap.ifc")
    run_pipeline(db, mv, store=store, top_n=10, settings=settings)
    db.commit()

    zones = {z.zone_key: z for z in db.query(Zone).filter(Zone.model_version_id == mv.id)}
    assert len(zones) >= 2, f"expected two zones so a boundary exists, got {list(zones)}"

    verified = [
        c
        for c in db.query(Clash).filter(Clash.model_version_id == mv.id)
        if c.state in ("verified", "verified_conditional")
    ]
    assert verified, "zone B accepted nothing, so there is nothing for a rebase to invalidate"
    target = verified[0]
    zone_b = zones[next(k for k, z in zones.items() if z.id == target.zone_id)]

    coordinator = Coordinator(db, settings=settings, store=store)
    rules = load_project_rules()
    model = load_model(mv.ifc_path, cache_dir=coordinator._cache_dir(), aliases_path=str(ALIASES))

    # The neighbour's element, and the zone that owns it.
    aa = next(e for e in model.elements if "AA-01" in e.name)
    zone_a_key = next(k for k, z in zones.items() if aa.global_id in z.element_gids)
    assert zone_a_key != zone_b.zone_key, "the trap put both elements in one zone"

    before = [e.to_state for e in history(db, "clash", target.id)]
    assert before[-1] in ("verified", "verified_conditional")

    # Aim zone A's commit at the position B actually claimed, whichever
    # direction B chose. Predicting the direction would make the test a
    # restatement of the resolver's search order rather than a test of the rebase.
    proposal = coordinator._latest_proposal(target.id)
    assert proposal is not None
    assert proposal.verdict in ("verified", "verified_conditional")
    moved_gid = proposal.element_gid
    moved_element = model.element(moved_gid)
    vector = tuple(float(v) for v in proposal.move_vector)
    footprint_mm = max(
        moved_element.bbox[i + 3] - moved_element.bbox[i] for i in range(3)
    ) * 1000.0
    assert sum(v * v for v in vector) ** 0.5 > footprint_mm, (
        "the trap needs a move larger than the element itself, or its old and new "
        "footprints overlap and a neighbour landing on one is already touching the other"
    )

    def _centre(el):
        b = el.bbox
        return [(b[i] + b[i + 3]) / 2.0 for i in range(3)]

    claimed = [
        c + v / 1000.0 for c, v in zip(_centre(moved_element), vector, strict=True)
    ]
    aa_commit = tuple((claimed[i] - _centre(aa)[i]) * 1000.0 for i in range(3))

    store.ensure_branch(mv.speckle_stream, zone_a_key)
    store.commit(
        mv.speckle_stream, zone_a_key, aa.global_id, aa_commit,
        message="zone A takes the space B claimed", meta={"acceptance": "A5"},
    )

    rebased = coordinator.enqueue_rebase(
        mv.id, [zone_a_key], aa.global_id, model=model, rules=rules
    )
    db.commit()
    db.refresh(target)

    assert zone_b.zone_key in rebased, (
        f"zone {zone_b.zone_key} was not rebased after its neighbour committed; got {rebased}"
    )
    assert target.state == "proposed", (
        f"a verified proposal survived a neighbour commit that invalidates it "
        f"(state is {target.state})"
    )

    after = history(db, "clash", target.id)
    last = after[-1]
    assert last.from_state in ("verified", "verified_conditional")
    assert last.to_state == "proposed"
    assert last.payload["rebase"] is True
    assert last.payload["objections"], "the rebase recorded no reason"

    _evidence(
        settings,
        "A5_rebase",
        {
            "zone_a": zone_a_key,
            "zone_b": zone_b.zone_key,
            "clash": target.clash_key,
            "state_path": [e.to_state for e in after],
            "rebased_zones": rebased,
            "objections": last.payload["objections"],
            "trigger": last.payload["trigger"],
            "verified_move_mm": [round(v, 1) for v in vector],
            "neighbour_commit_mm": [round(v, 1) for v in aa_commit],
        },
    )
