"""GeometryMonitor -- does the moved element still fit where it now is?

Re-judges the moved element against every element in its own zone plus the
buffer, through the same ``geometry_engine`` that condemned the original clash.
Using the same engine is the point: a resolver that proposed moves against one
notion of "clash" and verified them against another would be marking its own
homework with a different rubric.

The test is comparative, not absolute. A congested zone can contain clashes this
move was never meant to fix, and failing a proposal for a problem it did not
create would stall the queue on the first crowded riser. So the monitor fails
only on findings that are *new or worse* than the baseline measured before the
move.
"""
from __future__ import annotations

from app.blocks import geometry_engine
from app.blocks.identity import clash_id
from app.kit.engine import translated
from app.monitors.base import FAIL, PASS, Check, Monitor, MonitorContext, MonitorResult

ACTIVE_KINDS = (geometry_engine.KIND_CLASH, geometry_engine.KIND_CLEARANCE)

# A move must not make an existing gap measurably tighter. The tolerance keeps
# floating-point noise in mesh translation from being reported as degradation.
WORSENING_TOLERANCE_MM = 1.0


def _rule_for(rules, a, b):
    from app.blocks.clearance_rules import find_applicable_rule

    return find_applicable_rule(rules, getattr(a, "system", None), getattr(b, "system", None), "any")


def judge_against(subject, others, rules) -> dict[str, object]:
    """Judge one element against many, keyed by canonical clash id."""
    out = {}
    for other in others:
        if other is None or other.global_id == subject.global_id:
            continue
        rule = _rule_for(rules, subject, other)
        finding = geometry_engine.judge_pair(
            subject.global_id,
            other.global_id,
            subject.mesh,
            other.mesh,
            required_clearance_m=(rule.min_gap_mm / 1000.0) if rule else None,
            rule_id=rule.rule_id if rule else None,
            category_a=getattr(subject, "system", None),
            category_b=getattr(other, "system", None),
        )
        out[clash_id(subject.global_id, other.global_id)] = finding
    return out


def _severity_mm(finding) -> float:
    """How bad this finding is, in millimetres, larger being worse.

    Penetration and shortfall are different measurements, so they are not
    compared against each other -- only ever against the same kind from the
    baseline for the same pair.
    """
    if finding.kind == geometry_engine.KIND_CLASH:
        vol = finding.penetration_volume_m3
        return float(vol * 1e9) if vol else 1.0
    if finding.kind == geometry_engine.KIND_CLEARANCE:
        required = finding.required_clearance_m or 0.0
        actual = finding.distance_m if finding.distance_m is not None else 0.0
        return max(0.0, (required - actual) * 1000.0)
    return 0.0


def compare(baseline: dict, after: dict) -> tuple[list[dict], list[dict]]:
    """Return (introduced, worsened) findings relative to the baseline."""
    introduced, worsened = [], []
    for cid, finding in after.items():
        if finding.kind not in ACTIVE_KINDS:
            continue
        before = baseline.get(cid)
        if before is None or before.kind not in ACTIVE_KINDS:
            introduced.append(
                {
                    "clash_id": cid,
                    "kind": finding.kind,
                    "method": finding.method,
                    "distance_m": finding.distance_m,
                    "penetration_volume_m3": finding.penetration_volume_m3,
                    "was": before.kind if before is not None else "not measured",
                }
            )
            continue
        if before.kind != finding.kind:
            # clearance -> clash is a degradation of kind, not of degree.
            if finding.kind == geometry_engine.KIND_CLASH:
                worsened.append({"clash_id": cid, "from": before.kind, "to": finding.kind})
            continue
        delta = _severity_mm(finding) - _severity_mm(before)
        if delta > WORSENING_TOLERANCE_MM:
            worsened.append(
                {"clash_id": cid, "kind": finding.kind, "worse_by_mm": round(delta, 3)}
            )
    return introduced, worsened


class GeometryMonitor(Monitor):
    name = "geometry"

    def run(self, ctx: MonitorContext) -> MonitorResult:
        element = ctx.element()
        if element is None:
            return MonitorResult.from_checks(
                self.name,
                [
                    Check(
                        "element_present",
                        FAIL,
                        f"{ctx.element_gid} is not in the loaded model",
                    )
                ],
            )

        scope_gids = [g for g in set(ctx.zone_gids) | set(ctx.buffer_gids) if g != ctx.element_gid]
        others = [ctx.model.element(g) for g in scope_gids]
        others = [o for o in others if o is not None]

        before = ctx.baseline.get("geometry")
        if before is None:
            before = judge_against(element, others, ctx.rules)

        moved = translated(element, ctx.vector_mm)
        after = judge_against(moved, others, ctx.rules)

        introduced, worsened = compare(before, after)

        checks = [
            Check(
                "no_new_clash",
                FAIL if introduced else PASS,
                (
                    f"move introduces {len(introduced)} finding(s) not present before"
                    if introduced
                    else f"no new finding against {len(others)} element(s) in zone + buffer"
                ),
                {"introduced": introduced[:10]},
            ),
            Check(
                "nothing_made_worse",
                FAIL if worsened else PASS,
                (
                    f"move tightens {len(worsened)} existing finding(s)"
                    if worsened
                    else "no existing finding degraded"
                ),
                {"worsened": worsened[:10]},
            ),
        ]

        return MonitorResult.from_checks(
            self.name,
            checks,
            {
                "scope_elements": len(others),
                "exact_backend": geometry_engine.exact_backend_available(),
                "vector_mm": list(ctx.vector_mm),
            },
        )
