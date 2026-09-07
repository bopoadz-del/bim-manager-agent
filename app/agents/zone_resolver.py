"""ZoneResolver -- resolve the clashes inside one zone, on that zone's branch.

The loop is small and the discipline in it is the product:

1. Take the zone's clashes in the kit's resolution order -- gravity first,
   because a drain cannot be raised without losing its fall, so everything else
   must move around it rather than it around them.
2. Ask B4 for candidate moves, smallest displacement first.
3. Run all three monitors on each candidate. Not one, not the cheap two.
4. Commit only on a clean sweep, and record the monitor evidence either way.
5. Give up after three monitored attempts, and escalate with the alternatives
   that were tried and what objected to them.

The attempt cap is deliberately low. A resolver that grinds through forty
candidates on a clash nobody can solve is a resolver that spends its afternoon
on the hardest clash in the model and never reaches the ninety easy ones. Three
failures with named objections is a better thing to hand an engineer than a
fortieth rejected vector.

Boundary-owned clashes are never committed here. The element is shared with a
neighbouring zone, and two resolvers committing to their own branches would each
be right and the pair of them wrong.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from app.agents.results import ProposalOutcome, ZoneResult
from app.blocks import clash_resolver
from app.monitors import ALL_MONITORS, MonitorContext, run_all

log = logging.getLogger(__name__)

VERDICT_VERIFIED = "verified"
VERDICT_REJECTED = "rejected"
VERDICT_ESCALATED = "escalated"
VERDICT_FLAGGED = "flagged_unsourced"


Vector3 = tuple[float, float, float]


def as_vector3(values: Sequence[float]) -> Vector3:
    """A checked 3-tuple. A move vector is always exactly three numbers."""
    x, y, z = (float(v) for v in values)
    return (x, y, z)


def _magnitude(vector: Sequence[float]) -> float:
    return sum(v * v for v in vector) ** 0.5


def _dominant_axis(vector: Sequence[float]) -> int:
    return max(range(3), key=lambda i: abs(vector[i]))


class ZoneResolver:
    """Resolves one zone. Deterministic given the same model and branch heads."""

    def __init__(
        self,
        zone_id: str,
        zone_key: str,
        model: Any,
        zone_gids: list[str],
        buffer_gids: list[str],
        neighbour_zone_gids: dict[str, list[str]],
        rules: list[Any],
        access_rules: list[Any] | None = None,
        store: Any = None,
        stream: str | None = None,
        max_attempts: int = 3,
        monitors: list[Any] | None = None,
    ):
        self.zone_id = zone_id
        self.zone_key = zone_key
        self.model = model
        self.zone_gids = zone_gids
        self.buffer_gids = buffer_gids
        self.neighbour_zone_gids = neighbour_zone_gids
        self.rules = rules
        self.access_rules = access_rules or []
        self.store = store
        self.stream = stream
        self.max_attempts = max_attempts
        self.monitors = monitors if monitors is not None else ALL_MONITORS

    # -- ordering ---------------------------------------------------------
    def order_clashes(self, clashes: list[Any]) -> list[Any]:
        """Kit resolution order first, then worst-first within a rank."""
        order = clash_resolver_order()
        def rank(c):
            systems = list(getattr(c, "systems", []) or [])
            ranks = [
                clash_resolver_rank(s, order) for s in systems
            ] or [99]
            return (min(ranks), -float(getattr(c, "severity_mm", 0.0) or 0.0), c.clash_key)
        return sorted(clashes, key=rank)

    # -- one clash --------------------------------------------------------
    def resolve_clash(self, clash: Any) -> ProposalOutcome:
        movable_gid, element = self._pick_movable(clash)
        base = ProposalOutcome(
            clash_id=clash.id,
            clash_key=clash.clash_key,
            element_gid=movable_gid or "",
            move_type="none",
            vector_mm=(0.0, 0.0, 0.0),
            verdict=VERDICT_ESCALATED,
            attempt=0,
        )

        if element is None:
            base.verdict = VERDICT_ESCALATED
            base.alternatives = [{"reason": "neither element is present and movable in this zone"}]
            return base

        required_gap = float(getattr(clash, "required_gap_mm", 0.0) or 0.0)
        rule_id = getattr(clash, "rule_id", None)

        # A clearance clash with no rule behind it has no distance to move to.
        # It is flagged for an engineer, never dressed up as a proposal.
        if clash.kind == "clearance" and not rule_id:
            base.verdict = VERDICT_FLAGGED
            base.alternatives = [
                {"reason": "clearance finding carries no sourced rule; no authorised distance exists"}
            ]
            return base

        rule = next((r for r in self.rules if r.rule_id == rule_id), None)
        clause = f"{rule.source.doc} {rule.source.clause}" if rule else None

        partner_gid = clash.b_gid if clash.a_gid == element.global_id else clash.a_gid
        candidates = self._rank_candidates(
            clash_resolver.candidate_moves(clash, element, required_gap),
            element,
            self.model.element(partner_gid),
            required_gap,
        )
        blocked_axes: set[tuple[int, int]] = set()
        tried: list[dict] = []
        rejected_attempts: list[dict] = []
        attempt = 0

        for move_type, vector in candidates:
            if attempt >= self.max_attempts:
                break

            axis_sign = (_dominant_axis(vector), 1 if vector[_dominant_axis(vector)] > 0 else -1)
            if axis_sign in blocked_axes:
                # A previous attempt was rejected for pushing this way. Trying a
                # longer move along the same heading spends an attempt to be
                # told the same thing, harder.
                continue

            attempt += 1
            ctx = MonitorContext(
                model=self.model,
                element_gid=element.global_id,
                vector_mm=vector,
                zone_key=self.zone_key,
                zone_gids=self.zone_gids,
                buffer_gids=self.buffer_gids,
                neighbour_zone_gids=self.neighbour_zone_gids,
                rules=self.rules,
                access_rules=self.access_rules,
                store=self.store,
                stream=self.stream,
            )
            passed, results = run_all(self.monitors, ctx)
            monitors_payload = {name: r.as_dict() for name, r in results.items()}

            outcome = ProposalOutcome(
                clash_id=clash.id,
                clash_key=clash.clash_key,
                element_gid=element.global_id,
                move_type=move_type,
                vector_mm=as_vector3(vector),
                verdict=VERDICT_VERIFIED if passed else VERDICT_REJECTED,
                attempt=attempt,
                monitors=monitors_payload,
                rule_ids=[rule_id] if rule_id else [],
                clause_text=clause,
            )

            if passed:
                outcome.rejected_attempts = rejected_attempts
                if getattr(clash, "owner", "zone") == "coordinator":
                    # Verified locally, but the element is shared. The
                    # coordinator re-runs both zones before anything commits.
                    outcome.handed_to_coordinator = True
                    outcome.alternatives = tried
                    return outcome
                outcome.committed_as = self._commit(element.global_id, vector, clash)
                outcome.alternatives = tried
                return outcome

            objections = {
                name: r.reason for name, r in results.items() if not r.passed
            }
            rejected_attempts.append(
                {
                    "attempt": attempt,
                    "move_type": move_type,
                    "vector_mm": list(vector),
                    "monitors": monitors_payload,
                }
            )
            tried.append(
                {
                    "attempt": attempt,
                    "move_type": move_type,
                    "vector_mm": [round(v, 1) for v in vector],
                    "displacement_mm": round(_magnitude(vector), 1),
                    "objections": objections,
                }
            )
            if "boundary" in objections:
                blocked_axes.add(axis_sign)

        base.verdict = VERDICT_ESCALATED
        base.attempt = attempt
        base.rule_ids = [rule_id] if rule_id else []
        base.clause_text = clause
        base.alternatives = tried
        base.rejected_attempts = rejected_attempts
        base.element_gid = element.global_id
        return base

    # -- candidate ordering ----------------------------------------------
    @staticmethod
    def _box_gap(box_a: Sequence[float], box_b: Sequence[float]) -> float:
        """Separation between two axis-aligned boxes, in metres. 0 if they overlap."""
        gaps = [
            max(0.0, max(box_a[i] - box_b[i + 3], box_b[i] - box_a[i + 3])) for i in range(3)
        ]
        return sum(g * g for g in gaps) ** 0.5

    def _rank_candidates(
        self,
        candidates: list[tuple[str, Vector3]],
        element: Any,
        partner: Any,
        required_gap_mm: float,
    ) -> list[tuple[str, Vector3]]:
        """Order candidates by whether they can plausibly work, then by size.

        The kit orders by displacement alone, which is the right instinct and the
        wrong result under an attempt cap. A diagonal splits its displacement
        across two axes, so it separates less along the axis that is actually
        tight -- and because a unit diagonal is a hair shorter than a unit axis
        move, every diagonal sorts ahead of the axis move of the same size. With
        three monitored attempts, all three go to candidates that geometrically
        cannot achieve the gap, and a solvable clash escalates.

        So each candidate is scored with a cheap bounding-box prediction first,
        and the ones that cannot reach the required separation are tried last
        rather than first. The prediction is not a verdict: whatever survives
        this ordering still goes through all three monitors, and the exact
        engine still has the final say. This only decides what to spend an
        attempt on.
        """
        if partner is None or not element.bbox or not partner.bbox:
            return candidates

        # Everything in THIS zone the move could run into, boxes only.
        #
        # Deliberately not the buffer. Whether a move lands in a neighbouring
        # zone is the BoundaryMonitor's question, and scoring it here would
        # answer that question with a cheap bounding-box approximation and then
        # quietly rank the move last -- so the real check would never see the
        # case it exists for. A resolver may propose a move that crosses a
        # boundary. It may not commit one.
        scope = []
        for gid in self.zone_gids:
            if gid in (element.global_id, partner.global_id):
                continue
            other = self.model.element(gid)
            if other is not None and other.bbox:
                scope.append(other.bbox)

        required_m = float(required_gap_mm) / 1000.0
        scored = []
        for i, (move_type, vector) in enumerate(candidates):
            moved = [element.bbox[j] + vector[j] / 1000.0 for j in range(3)] + [
                element.bbox[j + 3] + vector[j] / 1000.0 for j in range(3)
            ]
            achieves = self._box_gap(moved, partner.bbox) + 1e-9 >= required_m
            # Cheap collision count against everything else in scope. A move
            # that solves this clash by landing inside four other elements is
            # not a better first guess than one that solves it in clear air, and
            # under a three-attempt cap the difference is the whole outcome.
            collisions = sum(1 for box in scope if self._box_gap(moved, box) <= 0.0)  # own zone
            scored.append(
                (0 if achieves else 1, collisions, _magnitude(vector), i, move_type, vector)
            )

        scored.sort()
        return [(move_type, vector) for *_, move_type, vector in scored]

    # -- helpers ----------------------------------------------------------
    def _pick_movable(self, clash: Any) -> tuple[str | None, Any]:
        """Prefer moving the element this zone owns, and never move structure."""
        owned = set(self.zone_gids)
        candidates = [clash.a_gid, clash.b_gid]
        ranked = sorted(
            candidates,
            key=lambda g: (
                g not in owned,
                getattr(self.model.element(g), "discipline", "") == "structural",
                getattr(self.model.element(g), "is_gravity", False),
            ),
        )
        for gid in ranked:
            el = self.model.element(gid)
            if el is not None and getattr(el, "discipline", "") != "structural":
                return gid, el
        return None, None

    def _commit(self, gid: str, vector: Sequence[float], clash: Any) -> str | None:
        if self.store is None or self.stream is None:
            return None
        self.store.ensure_branch(self.stream, self.zone_key)
        return self.store.commit(
            self.stream,
            self.zone_key,
            gid,
            as_vector3(vector),
            message=f"resolve {clash.clash_key}",
            meta={"clash_key": clash.clash_key, "rule_id": getattr(clash, "rule_id", None)},
        )

    # -- entry point ------------------------------------------------------
    def run(self, clashes: list[Any]) -> ZoneResult:
        branch = None
        if self.store is not None and self.stream is not None:
            branch = self.store.ensure_branch(self.stream, self.zone_key)

        result = ZoneResult(zone_id=self.zone_id, zone_key=self.zone_key, branch=branch)
        for clash in self.order_clashes(clashes):
            outcome = self.resolve_clash(clash)
            result.outcomes.append(outcome)
            result.clashes_seen += 1
            if outcome.handed_to_coordinator:
                result.handed_to_coordinator += 1
            elif outcome.verdict == VERDICT_VERIFIED:
                result.verified += 1
            elif outcome.verdict == VERDICT_FLAGGED:
                result.flagged_unsourced += 1
            else:
                result.escalated += 1
        return result


def clash_resolver_order():
    from app.blocks.clash_triage import load_order

    return load_order()


def clash_resolver_rank(system: str, order) -> int:
    from app.blocks.clash_triage import resolution_rank

    return resolution_rank(system, order)
