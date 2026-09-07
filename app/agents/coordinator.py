"""Coordinator -- owns the model, the zones, and every decision that spans two.

The division of labour is the design. A ZoneResolver knows one zone very well
and is allowed to commit inside it. The Coordinator knows all of them and is the
only thing allowed to decide anything that crosses a boundary, because a
boundary question has two right answers from the inside and one from above.

It also owns the two operations that are meaningless at zone scope: cutting the
model into zones in the first place, and comparing one model version against the
next to find out whether the fixes were actually made.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.results import ArbitrationResult, DiffResult, IngestResult, ZoneResult
from app.agents.zone_resolver import ZoneResolver, as_vector3
from app.agents.zoning import ZonePlan, buffer_gids, neighbours_of, plan_zones
from app.blocks import (  # type: ignore[attr-defined]
    VENDOR_DIR,
    bcf_export,
    clash_triage,
    clearance_rules,
    model_clone,
    version_diff,
)  # app.blocks.__path__ is set at runtime; see app/blocks/__init__.py
from app.blocks.identity import clash_id as canonical_clash_id
from app.config import Settings, get_settings
from app.kit.engine import full_pass, load_model
from app.kit.systems import unreachable_rules
from app.ledger import record, transition
from app.models import (
    CLASH_STATES,
    ZONE_STATES,
    Clash,
    ModelVersion,
    Proposal,
    Zone,
)
from app.monitors import ALL_MONITORS, MonitorContext, run_all

log = logging.getLogger(__name__)

# app.blocks mounts the vendored kit via __path__, so the seed table is resolved
# through that mount rather than through a directory that does not exist on disk.
KIT_SEED_RULES = VENDOR_DIR / "seed_rules.json"
KIT_ORDER = VENDOR_DIR / "order.yaml"


def load_project_rules(extra_path: str | Path | None = None) -> list[Any]:
    """Kit seed rules plus any the project supplied.

    ``load_rules`` refuses a rule without a citation, so an unsourced entry in a
    project file fails the load rather than entering the table. That is the
    intended behaviour: a table that silently drops bad rules and keeps going is
    a table nobody can trust the contents of.
    """
    rules = list(clearance_rules.load_rules(str(KIT_SEED_RULES)))
    if extra_path and Path(extra_path).exists():
        rules.extend(clearance_rules.load_rules(str(extra_path)))
    return rules


class Coordinator:
    def __init__(
        self,
        session: Session,
        settings: Settings | None = None,
        store: Any = None,
        monitors: list[Any] | None = None,
    ):
        self.session = session
        self.settings = settings or get_settings()
        self.monitors = monitors if monitors is not None else ALL_MONITORS
        if store is None:
            from app.store import get_store

            store = get_store(self.settings)
        self.store = store

    # -- paths ------------------------------------------------------------
    @property
    def artifacts(self) -> Path:
        p = Path(self.settings.artifacts_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _cache_dir(self) -> Path:
        d = self.artifacts / "mesh_cache"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def findings_path(self, model_version_id: str) -> Path:
        d = self.artifacts / "findings"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{model_version_id}.json"

    def package_dir(self, zone_id: str) -> Path:
        d = self.artifacts / "review" / zone_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- step 1-4: ingest -------------------------------------------------
    def ingest(
        self,
        model_version: ModelVersion,
        programme_csv: str | Path | None = None,
        rules_path: str | Path | None = None,
        aliases_path: str | Path | None = None,
    ) -> IngestResult:
        """Mesh, judge, zone, triage, and write the queue. Steps 1 to 4."""
        rules = load_project_rules(rules_path)
        # The alias table is applied inside load_model so that every later load
        # of this version -- resolver, review, worker -- sees the same systems.
        # An argument overrides; otherwise keep whatever the row already carries.
        # Blanking a stored path here would make the resolver re-load the model
        # under a different vocabulary than the ingest judged it with.
        if aliases_path:
            model_version.aliases_path = str(aliases_path)
        if programme_csv:
            model_version.programme_path = str(programme_csv)
        aliases_path = model_version.aliases_path
        programme_csv = programme_csv or model_version.programme_path
        model = load_model(
            model_version.ifc_path, cache_dir=self._cache_dir(), aliases_path=aliases_path
        )
        alias_report = model.alias_report
        never_applied = unreachable_rules(rules, model.elements)
        if never_applied:
            log.warning(
                "%d rule(s) reference systems absent from this model and were never applied: %s",
                len(never_applied),
                ", ".join(r["rule_id"] for r in never_applied),
            )

        record(
            self.session,
            entity="model_version",
            entity_id=model_version.id,
            to_state="meshed",
            payload={"elements": len(model.elements), "sha256": model.sha256},
        )

        result = full_pass(model, rules)
        findings = result["findings"]

        programme = {}
        if programme_csv and Path(programme_csv).exists():
            programme = clash_triage.load_programme(programme_csv)

        triaged = clash_triage.triage(
            findings,
            elements_by_id=model.by_id,
            programme=programme,
            cell_m=self.settings.grid_fallback_m,
        )

        plans = plan_zones(
            model.elements,
            max_elements=self.settings.max_elements_per_zone,
            cell_m=self.settings.grid_fallback_m,
        )
        zone_of: dict[str, str] = {}
        for plan in plans:
            for gid in plan.element_gids:
                zone_of[gid] = plan.zone_key

        congestion = triaged.zones or {}
        programme_rank = self._programme_ranks(plans, model.by_id, programme)

        stream = self.store.ensure_stream(model_version.id, f"mep-judge {model_version.id}")
        model_version.speckle_stream = stream

        zone_rows = self._write_zones(model_version, plans, congestion, programme_rank)
        clash_rows, boundary_owned = self._write_clashes(
            model_version, triaged, zone_of, zone_rows, model
        )

        self.findings_path(model_version.id).write_text(
            json.dumps([f.as_dict() for f in findings], indent=2), encoding="utf-8"
        )

        model_version.element_count = len(model.elements)
        model_version.status = "queued"
        model_version.stats = {
            "pairs_admitted": result["pairs_admitted"],
            "pairs_possible": result["pairs_possible"],
            "joints_excluded": len(result["joints"]),
            "pad_m": result["pad_m"],
            "exact_backend": result["exact_backend"],
            "graph": result["graph"],
            "dropped_workflow": triaged.dropped_workflow,
            "deduped": triaged.deduped,
            "rules_loaded": len(rules),
            "system_aliases": alias_report,
            "rules_never_applied": never_applied,
        }
        transition(
            self.session,
            model_version,
            entity="model_version",
            to_state="queued",
            allowed=("ingesting", "meshed", "queued", "resolving", "reviewed"),
            payload=model_version.stats,
        )
        self.session.flush()

        return IngestResult(
            model_version_id=model_version.id,
            ifc_sha256=model.sha256,
            element_count=len(model.elements),
            zones_created=len(zone_rows),
            clashes_created=len(clash_rows),
            joints_excluded=len(result["joints"]),
            pairs_admitted=result["pairs_admitted"],
            boundary_owned=boundary_owned,
            stats=model_version.stats,
        )

    def _programme_ranks(
        self, plans: list[ZonePlan], by_id: dict[str, Any], programme: dict[str, str]
    ) -> dict[str, int]:
        """Earlier programme date means lower rank, meaning resolved sooner.

        Zones with no programme data rank last rather than first: an unknown
        date is not an early one, and letting it sort to the front would put
        undated work ahead of work with a real deadline.
        """
        if not programme:
            return {p.zone_key: 0 for p in plans}
        dates: dict[str, str] = {}
        for plan in plans:
            found = [
                programme[getattr(by_id[g], "system", "")]
                for g in plan.element_gids
                if g in by_id and getattr(by_id[g], "system", "") in programme
            ]
            if found:
                dates[plan.zone_key] = min(found)
        ordered = sorted(dates, key=lambda k: dates[k])
        ranks = {key: i + 1 for i, key in enumerate(ordered)}
        return {p.zone_key: ranks.get(p.zone_key, len(ordered) + 1) for p in plans}

    def _write_zones(
        self,
        model_version: ModelVersion,
        plans: list[ZonePlan],
        congestion: dict[str, float],
        programme_rank: dict[str, int],
    ) -> dict[str, Zone]:
        rows: dict[str, Zone] = {}
        for plan in plans:
            # Add the merged cells back up. A merged zone's name is not a key
            # the kit's per-cell congestion map has ever seen.
            cells = plan.member_cells or [plan.zone_key]
            cong = sum(congestion.get(c, 0.0) for c in cells)
            rank = programme_rank.get(plan.zone_key, 0)
            zone = Zone(
                model_version_id=model_version.id,
                zone_key=plan.zone_key,
                level=plan.level,
                grid_cell=plan.grid_cell,
                buffer_m=self.settings.zone_buffer_m,
                congestion=float(cong),
                programme_rank=rank,
                priority=self._priority(cong, rank, plan.element_count),
                element_count=plan.element_count,
                element_gids=plan.element_gids,
                dedicated_reason=plan.dedicated_reason,
                status="queued",
            )
            self.session.add(zone)
            self.session.flush()
            rows[plan.zone_key] = zone
            record(
                self.session,
                entity="zone",
                entity_id=zone.id,
                to_state="queued",
                payload={
                    "zone_key": zone.zone_key,
                    "elements": zone.element_count,
                    "congestion": zone.congestion,
                    "programme_rank": rank,
                    "dedicated": plan.dedicated_reason,
                },
            )
        return rows

    @staticmethod
    def _priority(congestion: float, programme_rank: int, element_count: int) -> float:
        """Congestion dominates; programme breaks ties; size breaks those.

        Congestion first because a congested zone is where the resolver's work
        compounds -- one move there clears several findings, and leaving it late
        means every other zone gets rebased around it.
        """
        rank_penalty = 0.0 if not programme_rank else 1.0 / (1.0 + programme_rank)
        return round(float(congestion) * 100.0 + rank_penalty * 10.0 + element_count * 0.001, 6)

    def _write_clashes(
        self,
        model_version: ModelVersion,
        triaged: Any,
        zone_of: dict[str, str],
        zone_rows: dict[str, Zone],
        model: Any,
    ) -> tuple[list[Clash], int]:
        rows: list[Clash] = []
        boundary_owned = 0
        for item in triaged.queue:
            za, zb = zone_of.get(item.element_a), zone_of.get(item.element_b)
            home = za or zb
            zone = zone_rows.get(home) if home else None
            # An element pair split across two zones is nobody's to move alone.
            owner = "coordinator" if (za and zb and za != zb) else "zone"
            if owner == "coordinator":
                boundary_owned += 1
            clash = Clash(
                model_version_id=model_version.id,
                zone_id=zone.id if zone is not None else None,
                clash_key=item.clash_id,
                a_gid=item.element_a,
                b_gid=item.element_b,
                kind=item.kind,
                depth_or_gap_mm=item.severity_mm,
                systems=[item.system_a, item.system_b],
                state="open",
                owner=owner,
                rule_id=item.rule_id,
                severity_mm=item.severity_mm,
                resolution_rank=item.resolution_rank,
                note=item.note,
            )
            clash.required_gap_mm = self._required_gap(
                item, model.element(item.element_a), model.element(item.element_b)
            )
            self.session.add(clash)
            rows.append(clash)
        self.session.flush()
        for clash in rows:
            record(
                self.session,
                entity="clash",
                entity_id=clash.id,
                to_state="open",
                payload={"clash_key": clash.clash_key, "owner": clash.owner, "kind": clash.kind},
            )
        return rows, boundary_owned

    @staticmethod
    def _backout_mm(a: Any, b: Any) -> float:
        """Least distance that separates two overlapping boxes, in millimetres.

        For a hard clash this is the number the move has to beat. The triaged
        severity cannot serve: for a clash it is derived from penetration
        *volume*, which is a useful ranking score and not a length. Feeding it to
        the resolver as a distance would ask a 2 cubic-metre overlap to be
        solved by a 2 metre move and a thin deep one by almost nothing.

        Taken along the axis of least overlap, because that is the cheapest
        direction out and the resolver searches every direction anyway.
        """
        if a is None or b is None or not a.bbox or not b.bbox:
            return 0.0
        overlaps = [
            min(a.bbox[i + 3], b.bbox[i + 3]) - max(a.bbox[i], b.bbox[i]) for i in range(3)
        ]
        if any(o <= 0 for o in overlaps):
            return 0.0
        return min(overlaps) * 1000.0

    @classmethod
    def _required_gap(cls, item: Any, element_a: Any = None, element_b: Any = None) -> float | None:
        """The separation the move has to achieve, in millimetres."""
        if item.kind == "clearance":
            # Severity for a clearance finding is the shortfall: how much more
            # room the rule wanted than it got.
            return float(item.severity_mm or 0.0)
        if item.kind == "hard":
            return cls._backout_mm(element_a, element_b)
        return None

    # -- step 5: dispatch -------------------------------------------------
    def zones_by_priority(self, model_version_id: str, limit: int | None = None) -> list[Zone]:
        stmt = (
            select(Zone)
            .where(Zone.model_version_id == model_version_id, Zone.status == "queued")
            .order_by(Zone.priority.desc(), Zone.zone_key)
        )
        zones = list(self.session.execute(stmt).scalars())
        return zones[:limit] if limit else zones

    def resolve_zone(self, zone: Zone, model: Any, rules: list[Any]) -> ZoneResult:
        """Run one ZoneResolver and persist everything it decided."""
        plans = self._plans_for(zone.model_version_id)
        plan = next((p for p in plans if p.zone_key == zone.zone_key), None)
        if plan is None:
            return ZoneResult(
                zone_id=zone.id,
                zone_key=zone.zone_key,
                branch=None,
                status="awaiting_review",
                note="zone plan not reproducible from the current model",
            )

        by_id = model.by_id
        buf = buffer_gids(plan, by_id, zone.buffer_m)
        neighbours = neighbours_of(plan, plans, buf)

        transition(
            self.session,
            zone,
            entity="zone",
            to_state="active",
            allowed=ZONE_STATES,
            payload={"buffer_elements": len(buf), "neighbours": list(neighbours)},
        )
        self.session.flush()

        clashes = list(
            self.session.execute(
                select(Clash).where(Clash.zone_id == zone.id, Clash.state.in_(("open", "proposed")))
            ).scalars()
        )

        mv = self.session.get(ModelVersion, zone.model_version_id)
        resolver = ZoneResolver(
            zone_id=zone.id,
            zone_key=zone.zone_key,
            model=model,
            zone_gids=list(zone.element_gids),
            buffer_gids=buf,
            neighbour_zone_gids=neighbours,
            rules=rules,
            store=self.store,
            stream=mv.speckle_stream if mv else None,
            max_attempts=self.settings.max_attempts_per_clash,
            monitors=self.monitors,
        )
        result = resolver.run(clashes)
        zone.branch = result.branch

        by_clash = {c.id: c for c in clashes}
        for outcome in result.outcomes:
            self._persist_outcome(by_clash.get(outcome.clash_id), outcome)

        transition(
            self.session,
            zone,
            entity="zone",
            to_state="awaiting_review",
            allowed=ZONE_STATES,
            payload={
                "verified": result.verified,
                "escalated": result.escalated,
                "handed_to_coordinator": result.handed_to_coordinator,
                "resolve_rate": result.resolve_rate,
            },
        )
        self.session.flush()
        return result

    def _persist_outcome(self, clash: Clash | None, outcome: Any) -> Proposal | None:
        if clash is None:
            return None

        # Every monitored attempt is a row, not just the one that stuck. An
        # escalated clash is only useful to a reviewer if they can see what was
        # tried and which monitor refused it.
        for rejected in getattr(outcome, "rejected_attempts", []):
            monitors = rejected["monitors"]
            self.session.add(
                Proposal(
                    clash_id=clash.id,
                    element_gid=outcome.element_gid,
                    move_type=rejected["move_type"],
                    move_vector=list(rejected["vector_mm"]),
                    rule_ids=outcome.rule_ids,
                    clause_text=outcome.clause_text,
                    monitor_geometry=monitors.get("geometry"),
                    monitor_boundary=monitors.get("boundary"),
                    monitor_integrity=monitors.get("integrity"),
                    verdict="rejected",
                    attempt=rejected["attempt"],
                    superseded=1,
                )
            )

        proposal = Proposal(
            clash_id=clash.id,
            element_gid=outcome.element_gid,
            move_type=outcome.move_type,
            move_vector=list(outcome.vector_mm),
            rule_ids=outcome.rule_ids,
            clause_text=outcome.clause_text,
            monitor_geometry=outcome.monitors.get("geometry"),
            monitor_boundary=outcome.monitors.get("boundary"),
            monitor_integrity=outcome.monitors.get("integrity"),
            verdict=outcome.verdict,
            attempt=outcome.attempt,
        )
        self.session.add(proposal)
        clash.attempts = max(clash.attempts, outcome.attempt)

        if outcome.handed_to_coordinator:
            target = "proposed"
        elif outcome.verdict == "verified":
            target = "verified"
        elif outcome.verdict == "flagged_unsourced":
            target = "escalated"
        else:
            target = "escalated"

        transition(
            self.session,
            clash,
            entity="clash",
            to_state=target,
            allowed=CLASH_STATES,
            payload={
                "verdict": outcome.verdict,
                "attempt": outcome.attempt,
                "vector_mm": list(outcome.vector_mm),
                "committed_as": outcome.committed_as,
                "alternatives": outcome.alternatives,
                "monitors": {
                    k: {"passed": v.get("passed"), "reason": v.get("reason")}
                    for k, v in outcome.monitors.items()
                },
            },
        )
        self.session.flush()
        return proposal

    def _plans_for(self, model_version_id: str) -> list[ZonePlan]:
        """Rebuild the zone plans from the stored element lists.

        Reading them back rather than recomputing keeps a zone stable for the
        life of a model version even if the zoning parameters change underneath
        it -- a reviewer's zone must not silently change shape between visits.
        """
        zones = list(
            self.session.execute(
                select(Zone).where(Zone.model_version_id == model_version_id).order_by(Zone.zone_key)
            ).scalars()
        )
        return [
            ZonePlan(
                zone_key=z.zone_key,
                level=z.level,
                grid_cell=z.grid_cell,
                element_gids=list(z.element_gids),
                member_cells=[],
                dedicated_reason=z.dedicated_reason,
                congestion=z.congestion,
                programme_rank=z.programme_rank,
                priority=z.priority,
            )
            for z in zones
        ]

    # -- step 6: boundary arbitration ------------------------------------
    def arbitrate(self, clash: Clash, model: Any, rules: list[Any]) -> ArbitrationResult:
        """Decide a proposal whose element is shared between two zones.

        Both zones' monitor sets run against the same proposed vector. Only a
        double pass commits. A single pass means one zone is happy and the other
        has not been asked -- which is precisely the failure the zoning
        introduced and this step exists to close.
        """
        proposal = self._latest_proposal(clash.id)
        if proposal is None:
            return ArbitrationResult(clash.id, False, "no proposal to arbitrate")

        plans = self._plans_for(clash.model_version_id)
        owning = [p for p in plans if clash.a_gid in p.element_gids or clash.b_gid in p.element_gids]
        if len(owning) < 2:
            return ArbitrationResult(
                clash.id, False, "clash is not actually shared between two zones",
                zones_checked=[p.zone_key for p in owning],
            )

        mv = self.session.get(ModelVersion, clash.model_version_id)
        if mv is None:
            return ArbitrationResult(
                clash.id, False, "the model version this clash belongs to no longer exists"
            )
        vector = as_vector3(proposal.move_vector)
        monitors_payload: dict[str, dict] = {}
        all_passed = True

        for plan in owning:
            buf = buffer_gids(plan, model.by_id, self.settings.zone_buffer_m)
            ctx = MonitorContext(
                model=model,
                element_gid=proposal.element_gid,
                vector_mm=vector,
                zone_key=plan.zone_key,
                zone_gids=plan.element_gids,
                buffer_gids=buf,
                neighbour_zone_gids=neighbours_of(plan, plans, buf),
                rules=rules,
                store=self.store,
                stream=mv.speckle_stream if mv else None,
            )
            passed, results = run_all(self.monitors, ctx)
            all_passed = all_passed and passed
            monitors_payload[plan.zone_key] = {
                name: r.as_dict() for name, r in results.items()
            }

        zones_checked = [p.zone_key for p in owning]
        if not all_passed:
            objections = [
                f"{zk}/{name}: {res['reason']}"
                for zk, per_zone in monitors_payload.items()
                for name, res in per_zone.items()
                if not res["passed"]
            ]
            proposal.verdict = "rejected"
            transition(
                self.session,
                clash,
                entity="clash",
                to_state="escalated",
                allowed=CLASH_STATES,
                actor="coordinator",
                payload={"arbitration": "rejected", "objections": objections},
            )
            self.session.flush()
            return ArbitrationResult(
                clash.id, False, "; ".join(objections[:4]), zones_checked, monitors_payload
            )

        commit_zone = owning[0].zone_key
        commit_id = self.store.commit(
            mv.speckle_stream,
            commit_zone,
            proposal.element_gid,
            vector,
            message=f"arbitrated {clash.clash_key}",
            meta={"arbitrated_across": zones_checked},
        )
        proposal.verdict = "verified"
        transition(
            self.session,
            clash,
            entity="clash",
            to_state="verified",
            allowed=CLASH_STATES,
            actor="coordinator",
            payload={"arbitration": "committed", "commit": commit_id, "zones": zones_checked},
        )

        rebased = self.enqueue_rebase(
            clash.model_version_id, zones_checked, proposal.element_gid, model=model, rules=rules
        )
        self.session.flush()
        return ArbitrationResult(
            clash.id, True, "double pass", zones_checked, monitors_payload, rebased
        )

    def _latest_proposal(self, clash_id: str) -> Proposal | None:
        stmt = (
            select(Proposal)
            .where(Proposal.clash_id == clash_id, Proposal.superseded == 0)
            .order_by(Proposal.created_at.desc(), Proposal.attempt.desc())
        )
        return self.session.execute(stmt).scalars().first()

    # -- rebase ------------------------------------------------------------
    def affected_zones(
        self, model_version_id: str, changed_zones: list[str], element_gid: str, model: Any
    ) -> list[Any]:
        """Zones whose verified work might no longer hold after a commit.

        A zone is affected when the moved element sits inside it or inside its
        buffer -- that is exactly the set of zones whose monitors looked at that
        element when they said yes.
        """
        plans = self._plans_for(model_version_id)
        out = []
        for plan in plans:
            if plan.zone_key in changed_zones:
                continue
            if element_gid in plan.element_gids:
                out.append(plan)
                continue
            if element_gid in buffer_gids(plan, model.by_id, self.settings.zone_buffer_m):
                out.append(plan)
        return out

    def enqueue_rebase(
        self,
        model_version_id: str,
        changed_zones: list[str],
        element_gid: str,
        model: Any = None,
        rules: list[Any] | None = None,
    ) -> list[str]:
        """A committed boundary move can invalidate verified work next door.

        Every affected zone's verified proposals are re-run through the full
        monitor set against the neighbourhood as it stands *now*. Only the ones
        that no longer pass go back to ``proposed``.

        Re-running rather than blanket-demoting matters in both directions. A
        proposal that still holds should not cost a reviewer a second look, and
        one that no longer holds must never keep a verdict it earned against a
        building that has since moved -- that is the most expensive lie this
        system could tell, because it is the one an engineer would have no
        reason to check.
        """
        if model is None:
            record(
                self.session,
                entity="model_version",
                entity_id=model_version_id,
                to_state="rebase_deferred",
                payload={
                    "reason": "no meshed model supplied; cannot re-run monitors",
                    "changed_zones": changed_zones,
                    "element_gid": element_gid,
                },
            )
            return []

        rules = rules if rules is not None else load_project_rules()
        mv = self.session.get(ModelVersion, model_version_id)
        plans = self._plans_for(model_version_id)
        rebased: list[str] = []

        for plan in self.affected_zones(model_version_id, changed_zones, element_gid, model):
            zone = self.session.execute(
                select(Zone).where(
                    Zone.model_version_id == model_version_id, Zone.zone_key == plan.zone_key
                )
            ).scalars().first()
            if zone is None:
                continue

            verified = list(
                self.session.execute(
                    select(Clash).where(Clash.zone_id == zone.id, Clash.state == "verified")
                ).scalars()
            )
            if not verified:
                continue

            buf = buffer_gids(plan, model.by_id, zone.buffer_m)
            neighbours = neighbours_of(plan, plans, buf)
            invalidated = 0

            for clash in verified:
                proposal = self._latest_proposal(clash.id)
                if proposal is None:
                    continue
                ctx = MonitorContext(
                    model=model,
                    element_gid=proposal.element_gid,
                    vector_mm=as_vector3(proposal.move_vector),
                    zone_key=zone.zone_key,
                    zone_gids=list(zone.element_gids),
                    buffer_gids=buf,
                    neighbour_zone_gids=neighbours,
                    rules=rules,
                    store=self.store,
                    stream=mv.speckle_stream if mv else None,
                )
                passed, results = run_all(self.monitors, ctx)
                if passed:
                    continue
                invalidated += 1
                proposal.monitor_geometry = results["geometry"].as_dict()
                proposal.monitor_boundary = results["boundary"].as_dict()
                proposal.monitor_integrity = results["integrity"].as_dict()
                proposal.verdict = "rejected"
                transition(
                    self.session,
                    clash,
                    entity="clash",
                    to_state="proposed",
                    allowed=CLASH_STATES,
                    actor="coordinator",
                    payload={
                        "rebase": True,
                        "trigger": {"zones": changed_zones, "element": element_gid},
                        "objections": [
                            f"{n}: {r.reason}" for n, r in results.items() if not r.passed
                        ],
                    },
                )

            if invalidated:
                if zone.status != "queued":
                    transition(
                        self.session,
                        zone,
                        entity="zone",
                        to_state="queued",
                        allowed=ZONE_STATES,
                        actor="coordinator",
                        payload={"rebase": True, "clashes_reopened": invalidated},
                    )
                rebased.append(zone.zone_key)

        self.session.flush()
        return rebased

    # -- step 7: review package -------------------------------------------
    def assemble_review_package(self, zone: Zone, model: Any) -> dict[str, Any]:
        """change_set.json + BCF + the monitor evidence behind every verdict."""
        out = self.package_dir(zone.id)
        clashes = list(
            self.session.execute(select(Clash).where(Clash.zone_id == zone.id)).scalars()
        )
        proposals: list[Proposal] = []
        for clash in clashes:
            latest = self._latest_proposal(clash.id)
            if latest is not None:
                proposals.append(latest)

        kit_proposals = [
            _KitProposal(
                clash_id=c.clash_key,
                element=p.element_gid,
                move_type=p.move_type,
                move_vector_mm=tuple(p.move_vector),
                status="proposed" if p.verdict == "verified" else p.verdict,
                attempts=p.attempt,
                rule_ids=list(p.rule_ids or []),
                clause_text=p.clause_text,
            )
            for c, p in ((c, self._latest_proposal(c.id)) for c in clashes)
            if p is not None
        ]
        change_set = model_clone.write_change_set(kit_proposals, out / "change_set.json")

        findings = self._findings_for_zone(zone, model)
        bcf_path = out / "issues.bcfzip"
        rule_lookup = {
            p.rule_ids[0]: p.clause_text
            for p in proposals
            if p.rule_ids and p.clause_text
        }
        bcf_export.export_bcf(
            findings, bcf_path, model_name=Path(model.path).name, rule_lookup=rule_lookup
        )

        evidence = {
            "zone_key": zone.zone_key,
            "branch": zone.branch,
            "original_ifc_sha256": model.sha256,
            "monitors": [
                {
                    "clash_key": c.clash_key,
                    "state": c.state,
                    "verdict": p.verdict if p else None,
                    "vector_mm": list(p.move_vector) if p else None,
                    "clause": p.clause_text if p else None,
                    "geometry": p.monitor_geometry if p else None,
                    "boundary": p.monitor_boundary if p else None,
                    "integrity": p.monitor_integrity if p else None,
                }
                for c, p in ((c, self._latest_proposal(c.id)) for c in clashes)
            ],
        }
        (out / "monitor_evidence.json").write_text(
            json.dumps(evidence, indent=2, default=str), encoding="utf-8"
        )

        record(
            self.session,
            entity="zone",
            entity_id=zone.id,
            to_state=zone.status,
            payload={"review_package": str(out)},
        )
        return {
            "dir": str(out),
            "change_set": str(change_set),
            "bcf": str(bcf_path),
            "evidence": str(out / "monitor_evidence.json"),
            "original_ifc_sha256": model.sha256,
        }

    def _findings_for_zone(self, zone: Zone, model: Any) -> list[Any]:
        path = self.findings_path(zone.model_version_id)
        if not path.exists():
            return []
        from app.blocks.geometry_engine import Finding

        gids = set(zone.element_gids)
        out = []
        for raw in json.loads(path.read_text(encoding="utf-8")):
            if raw["element_a"] in gids or raw["element_b"] in gids:
                out.append(Finding(**raw))
        return out

    # -- step 8: version diff ---------------------------------------------
    def diff_against(self, current: ModelVersion, previous: ModelVersion) -> DiffResult:
        """Compare two runs and score the proposals that claimed to fix things."""
        from app.blocks.geometry_engine import Finding

        def _load(mv: ModelVersion) -> list[Any]:
            path = self.findings_path(mv.id)
            if not path.exists():
                return []
            return [Finding(**raw) for raw in json.loads(path.read_text(encoding="utf-8"))]

        v1, v2 = _load(previous), _load(current)
        diff = version_diff.diff_versions(v1, v2)

        prior = list(
            self.session.execute(
                select(Proposal)
                .join(Clash, Clash.id == Proposal.clash_id)
                .where(Clash.model_version_id == previous.id, Proposal.verdict == "verified")
            ).scalars()
        )
        clash_keys: dict[str, str] = {}
        for proposal in prior:
            owning = self.session.get(Clash, proposal.clash_id)
            if owning is not None:
                clash_keys[proposal.id] = owning.clash_key
        scored = version_diff.score_proposals(
            [
                _KitProposal(clash_id=clash_keys[p.id], element=p.element_gid)
                for p in prior
                if p.id in clash_keys
            ],
            diff,
        )

        resolved_ids = {e["clash_id"] for e in diff["resolved"]}
        regressed_ids = {e["clash_id"] for e in diff["regressed"]}

        for clash in self.session.execute(
            select(Clash).where(Clash.model_version_id == previous.id)
        ).scalars():
            if clash.clash_key in resolved_ids and clash.state != "resolved":
                transition(
                    self.session, clash, entity="clash", to_state="resolved",
                    allowed=CLASH_STATES, actor="coordinator",
                    payload={"diff_from": previous.id, "diff_to": current.id},
                )
            elif clash.clash_key in regressed_ids and clash.state != "regressed":
                transition(
                    self.session, clash, entity="clash", to_state="regressed",
                    allowed=CLASH_STATES, actor="coordinator",
                    payload={"diff_from": previous.id, "diff_to": current.id},
                )

        record(
            self.session,
            entity="model_version",
            entity_id=current.id,
            to_state="diffed",
            payload={
                "previous": previous.id,
                "counts": {k: len(v) for k, v in diff.items()},
                "score": scored,
            },
        )
        self.session.flush()

        return DiffResult(
            model_version_id=current.id,
            previous_id=previous.id,
            resolved=len(diff["resolved"]),
            regressed=len(diff["regressed"]),
            new=len(diff["new"]),
            persisting=len(diff["persisting"]),
            proposal_score=scored,
        )


class _KitProposal:
    """Shape the kit's exporters expect, built from a stored proposal row."""

    def __init__(
        self,
        clash_id: str,
        element: str,
        move_type: str = "offset",
        move_vector_mm: tuple[float, float, float] = (0.0, 0.0, 0.0),
        status: str = "proposed",
        attempts: int = 1,
        rule_ids: list[str] | None = None,
        clause_text: str | None = None,
    ):
        self.clash_id = clash_id
        self.element = element
        self.move_type = move_type
        self.move_vector_mm = move_vector_mm
        self.status = status
        self.attempts = attempts
        self.rule_ids = rule_ids or []
        self.clause_text = clause_text
        self.rejected: list[str] = []
        self.note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["move_vector_mm"] = list(self.move_vector_mm)
        return d


__all__ = ["Coordinator", "canonical_clash_id", "load_project_rules", "asdict"]
