"""BoundaryMonitor -- does this move push a problem into somebody else's zone?

Zoning is what makes the work parallel, and it is also what makes it dangerous.
Each ZoneResolver sees its own elements and judges moves against them; a move
that shifts a duct two metres sideways is, from inside zone A, a clean fix. From
zone B it is a duct arriving through the wall.

So this monitor judges the moved element against the *neighbouring* zones as
they are right now -- fetched from the store's current branch heads, not from
the model as loaded at the start of the run. That distinction is the whole
monitor. Zone B may have committed three moves since zone A began work, and
checking against stale positions would verify a move against a building nobody
is building any more.
"""
from __future__ import annotations

from app.kit.engine import translated
from app.monitors.base import FAIL, PASS, UNPROVABLE, Check, Monitor, MonitorContext, MonitorResult
from app.monitors.geometry import compare, in_range, judge_against, reach_m


class BoundaryMonitor(Monitor):
    name = "boundary"

    def run(self, ctx: MonitorContext) -> MonitorResult:
        element = ctx.element()
        if element is None:
            return MonitorResult.from_checks(
                self.name,
                [Check("element_present", FAIL, f"{ctx.element_gid} is not in the loaded model")],
            )

        neighbour_keys = sorted(ctx.neighbour_zone_gids)
        if not neighbour_keys:
            return MonitorResult.from_checks(
                self.name,
                [
                    Check(
                        "neighbours_checked",
                        PASS,
                        "zone has no neighbouring zones; nothing can be pushed anywhere",
                    )
                ],
                {"neighbour_zones": []},
            )

        heads = {}
        head_source = "store"
        if ctx.store is not None and ctx.stream is not None:
            heads = ctx.store.heads(ctx.stream, neighbour_keys)
        else:
            head_source = "none"

        # Build the neighbourhood as it stands NOW: every neighbour element in
        # its committed position, not its as-loaded one.
        neighbours = []
        displaced = 0
        for zone_key in neighbour_keys:
            head = heads.get(zone_key)
            for gid in ctx.neighbour_zone_gids[zone_key]:
                if gid == ctx.element_gid:
                    continue
                el = ctx.model.element(gid)
                if el is None:
                    continue
                offset = head.offset_for(gid) if head is not None else (0.0, 0.0, 0.0)
                if any(offset):
                    el = translated(el, offset)
                    displaced += 1
                neighbours.append(el)

        if head_source == "none":
            unprovable = Check(
                "neighbour_heads_current",
                UNPROVABLE,
                "no model store configured; neighbours judged at their as-loaded positions",
            )
        else:
            unprovable = Check(
                "neighbour_heads_current",
                PASS,
                f"fetched {len(heads)} branch head(s); {displaced} neighbour element(s) already moved",
                {"commits": {k: h.commit_count for k, h in heads.items()}},
            )

        neighbours = in_range(element, neighbours, reach_m(ctx))
        before = judge_against(element, neighbours, ctx.rules)
        moved = translated(element, ctx.vector_mm)
        after = judge_against(moved, neighbours, ctx.rules)
        introduced, worsened = compare(before, after)

        checks = [
            unprovable,
            Check(
                "nothing_pushed_into_neighbour",
                FAIL if introduced else PASS,
                (
                    f"move creates {len(introduced)} finding(s) in neighbouring zones "
                    f"{', '.join(neighbour_keys[:4])}"
                    if introduced
                    else f"no new finding against {len(neighbours)} neighbouring element(s)"
                ),
                {"introduced": introduced[:10]},
            ),
            Check(
                "no_neighbour_finding_worsened",
                FAIL if worsened else PASS,
                (
                    f"move tightens {len(worsened)} finding(s) across the boundary"
                    if worsened
                    else "no neighbouring finding degraded"
                ),
                {"worsened": worsened[:10]},
            ),
        ]

        return MonitorResult.from_checks(
            self.name,
            checks,
            {
                "neighbour_zones": neighbour_keys,
                "neighbour_elements": len(neighbours),
                "neighbour_elements_displaced": displaced,
                "head_source": head_source,
            },
        )
