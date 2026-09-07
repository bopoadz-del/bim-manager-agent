"""IntegrityMonitor -- is the moved element still a working part of a system?

Geometry and boundary both ask whether the element now occupies empty space.
Neither asks whether it still *works*. A drain relocated into a clear void with
its fall reversed passes both, and would be committed, and would be built.

Four questions, each failing loudly and separately:

* **connectivity** -- is the element still joined to what it was joined to?
* **fall** -- does a gravity system still run downhill?
* **clearance** -- are the sourced separations still satisfied afterwards?
* **access** -- is maintenance access preserved?

Two of these can be unprovable rather than passed. Most IFC exports of this
vintage carry no ``IfcRelConnectsPorts`` at all, and a project that has supplied
no access table has no access requirement to check against. Recording those as
``unprovable`` rather than ``pass`` is the difference between "we checked and it
is fine" and "we could not check" -- and only one of those two sentences is true.
"""
from __future__ import annotations

from app.blocks import clearance_rules
from app.blocks.clash_resolver import preserves_fall
from app.kit.engine import translated
from app.monitors.base import FAIL, PASS, UNPROVABLE, Check, Monitor, MonitorContext, MonitorResult
from app.monitors.geometry import in_range, judge_against, reach_m

# How far a joined element may move before the joint is considered broken. A
# fitting and its pipe are modelled coincident; any real separation is a break.
JOINT_TOLERANCE_MM = 1.0


def _connectivity_check(ctx: MonitorContext) -> Check:
    graph = getattr(ctx.model, "graph", None)
    if graph is None or not getattr(graph, "available", False):
        return Check(
            "still_connected",
            UNPROVABLE,
            "model carries no port or nesting relationships; connectivity cannot be checked",
            {"graph": graph.as_dict() if graph is not None else None},
        )

    partners = [
        other
        for edge in graph.edges
        if ctx.element_gid in edge
        for other in edge
        if other != ctx.element_gid
    ]
    if not partners:
        return Check(
            "still_connected",
            PASS,
            "model records no joints on this element; none can be broken",
            {"partners": []},
        )

    magnitude = sum(v * v for v in ctx.vector_mm) ** 0.5
    moving_partners = [p for p in partners if p in set(ctx.zone_gids) | set(ctx.buffer_gids)]
    if magnitude > JOINT_TOLERANCE_MM:
        return Check(
            "still_connected",
            FAIL,
            (
                f"element is joined to {len(partners)} element(s) that are not moving with it; "
                f"a {magnitude:.0f} mm displacement breaks the joint"
            ),
            {"partners": partners[:10], "displacement_mm": round(magnitude, 2)},
        )
    return Check(
        "still_connected",
        PASS,
        f"displacement {magnitude:.2f} mm is within joint tolerance",
        {"partners": partners[:10], "partners_in_scope": len(moving_partners)},
    )


def _fall_check(ctx: MonitorContext) -> Check:
    element = ctx.element()
    if not getattr(element, "is_gravity", False):
        return Check(
            "fall_preserved",
            PASS,
            f"{getattr(element, 'system', 'system')} is not a gravity system; fall does not apply",
        )
    if preserves_fall(element, ctx.vector_mm):
        return Check(
            "fall_preserved",
            PASS,
            "gravity element moved without altering its fall",
            {"vector_mm": list(ctx.vector_mm)},
        )
    return Check(
        "fall_preserved",
        FAIL,
        (
            f"move changes the invert of a gravity element by {ctx.vector_mm[2]:.0f} mm; "
            "a drain that runs uphill does not drain"
        ),
        {"vector_mm": list(ctx.vector_mm)},
    )


def _clearance_check(ctx: MonitorContext) -> Check:
    if not ctx.rules:
        return Check(
            "clearances_satisfied",
            UNPROVABLE,
            "no clearance rules loaded for this project; separations cannot be judged",
        )
    element = ctx.element()
    scope = [
        ctx.model.element(g)
        for g in set(ctx.zone_gids) | set(ctx.buffer_gids)
        if g != ctx.element_gid
    ]
    scope = [s for s in scope if s is not None]
    scope = in_range(element, scope, reach_m(ctx))
    moved = translated(element, ctx.vector_mm)
    findings = list(judge_against(moved, scope, ctx.rules).values())
    violations = clearance_rules.evaluate(findings, ctx.rules)
    if violations:
        worst = min(v.distance_mm - v.required_min_gap_mm for v in violations)
        return Check(
            "clearances_satisfied",
            FAIL,
            (
                f"{len(violations)} clearance violation(s) after the move; "
                f"worst shortfall {abs(worst):.0f} mm"
            ),
            {
                "violations": [
                    {
                        "with": v.element_b,
                        "rule_id": v.rule_id,
                        "required_mm": v.required_min_gap_mm,
                        "actual_mm": round(v.distance_mm, 1),
                        "clause": v.source_clause,
                    }
                    for v in violations[:10]
                ]
            },
        )
    return Check(
        "clearances_satisfied",
        PASS,
        f"all {len(ctx.rules)} sourced rule(s) satisfied against {len(scope)} element(s)",
    )


def _access_check(ctx: MonitorContext) -> Check:
    """Maintenance access, judged only against a sourced access table.

    When a project supplies no access rules there is no number to check against,
    and this monitor will not invent one. An access clearance pulled from
    nowhere is exactly the kind of unsourced figure the whole product refuses to
    put in front of an engineer.
    """
    if not ctx.access_rules:
        return Check(
            "access_preserved",
            UNPROVABLE,
            "project has supplied no sourced access-zone table; access cannot be judged",
        )
    element = ctx.element()
    scope = [
        ctx.model.element(g)
        for g in set(ctx.zone_gids) | set(ctx.buffer_gids)
        if g != ctx.element_gid
    ]
    scope = [s for s in scope if s is not None]
    scope = in_range(element, scope, reach_m(ctx))
    moved = translated(element, ctx.vector_mm)
    findings = list(judge_against(moved, scope, ctx.access_rules).values())
    violations = clearance_rules.evaluate(findings, ctx.access_rules)
    if violations:
        return Check(
            "access_preserved",
            FAIL,
            f"{len(violations)} access-zone breach(es) after the move",
            {
                "violations": [
                    {"with": v.element_b, "rule_id": v.rule_id, "required_mm": v.required_min_gap_mm}
                    for v in violations[:10]
                ]
            },
        )
    return Check(
        "access_preserved",
        PASS,
        f"all {len(ctx.access_rules)} sourced access rule(s) satisfied",
    )


class IntegrityMonitor(Monitor):
    name = "integrity"

    def run(self, ctx: MonitorContext) -> MonitorResult:
        element = ctx.element()
        if element is None:
            return MonitorResult.from_checks(
                self.name,
                [Check("element_present", FAIL, f"{ctx.element_gid} is not in the loaded model")],
            )
        checks = [
            _connectivity_check(ctx),
            _fall_check(ctx),
            _clearance_check(ctx),
            _access_check(ctx),
        ]
        return MonitorResult.from_checks(
            self.name,
            checks,
            {
                "system": getattr(element, "system", None),
                "is_gravity": bool(getattr(element, "is_gravity", False)),
                "rules_loaded": len(ctx.rules),
                "access_rules_loaded": len(ctx.access_rules),
            },
        )
