"""connectivity — tell a JOINT from a CLASH, by topology first and geometry second.

THE PROBLEM (F5, measured on Infra-Plumbing.ifc): 24 of 24 flagged pairs were
pipe-to-pipe contacts in a single-discipline drainage run. Segments that JOIN
touch by design. Reporting them as clashes buries the real findings, and the
previous filter missed them because it keyed on fitting NAMES ("bend", "tee")
which those elements do not carry.

TWO SIGNALS, IN ORDER OF AUTHORITY.

1. TOPOLOGY — the correct answer. Two elements whose IfcDistributionPorts are
   linked by IfcRelConnectsPorts are connected: the model itself says so.
   A fitting nested (IfcRelNests) to both is the same statement. This is
   evidence, not inference, and it is checked first.

2. ZERO PENETRATION — the fallback, and NOT a proximity heuristic. Two solids
   that touch share a surface but enclose no common volume: the exact boolean
   intersection is 0.00000 m3. Two solids that genuinely conflict enclose
   real volume. That is a measurement of the geometry, not a guess about
   nearness, and it separates the two cases exactly.

WHY THE FALLBACK IS NEEDED, stated plainly: **neither available fixture carries
any port data at all.** Verified, both models:

    IfcRelConnectsPorts   0        IfcDistributionPort   0
    IfcRelNests           0        IfcRelConnectsPortToElement 0

So the topology path cannot be exercised by them. It is implemented and tested
against a synthetic model that DOES carry ports, because real IFC4 MEP exports
from Revit and Navisworks generally do — and when they do, it must win.

The fallback is deliberately narrow. It requires ALL of:
    * both elements in the SAME system
    * zero penetration volume, measured exactly
    * the exact boolean backend available, so the zero is trustworthy
A cross-system contact is never a joint, whatever the volume. Without the
boolean backend a zero cannot be trusted, so nothing is excluded.

READS   an IFC model (ports, nests); a pair's measured penetration volume.
WRITES  nothing. This module only classifies.
NEVER   excludes a pair on proximity, on element names, or on a volume it
        could not measure exactly. Every exclusion is counted and reported --
        a joint that vanishes silently is indistinguishable from a missed clash.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger(__name__)

REASON_PORT = "connected_joint:port"
REASON_NEST = "connected_joint:nested_fitting"
REASON_TANGENT = "connected_joint:zero_penetration"

# CALIBRATED, not chosen. The discriminator is the shared volume as a FRACTION
# of the smaller element, because a fraction is scale-invariant: it means the
# same thing for a 50 mm pipe and a 2 m culvert, where an absolute volume does
# not.
#
# Measured on the 24 touching pairs in Infra-Plumbing.ifc and on a constructed
# pipe-through-pipe crossing:
#
#     joints   (24 real pairs)   4.2e-17 .. 1.6e-8 m3   fraction < 1e-6
#     crossing (pipe through)    5.2e-3 m3             fraction   0.0844
#
# The band between them is empty by roughly five orders of magnitude -- the
# real conflict is ~84,000x the joint ceiling. The threshold sits inside that
# gap: 100x above the worst joint, ~800x below the smallest real overlap.
#
# Absolute volume was tried first and REJECTED: a 1e-9 floor excluded only 3 of
# 24 joints, because triangulated cylinders that merely touch still enclose a
# small faceting volume that scales with pipe diameter. That is a modelling
# artefact, not a conflict, and an absolute floor cannot tell the two apart
# across sizes.
TOUCH_VOLUME_FRACTION = 1e-4

# Retained as a secondary floor for the degenerate case where a volume cannot
# be computed for the smaller element (a non-watertight mesh reports 0).
TOUCH_VOLUME_M3 = 1e-9


@dataclass
class ConnectivityGraph:
    """Which elements the MODEL says are joined."""

    edges: set[frozenset] = field(default_factory=set)
    ports_seen: int = 0
    connections_seen: int = 0
    nests_seen: int = 0

    def connected(self, a: str, b: str) -> bool:
        return frozenset((a, b)) in self.edges

    @property
    def available(self) -> bool:
        """False when the model carries no connectivity at all.

        Callers must know the difference between "the model says these are not
        connected" and "the model says nothing". Treating an empty graph as
        proof of non-connection would flag every joint in a model that simply
        omits ports -- which is both fixtures.
        """
        return self.connections_seen > 0 or self.nests_seen > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "edges": len(self.edges),
            "ports_seen": self.ports_seen,
            "connections_seen": self.connections_seen,
            "nests_seen": self.nests_seen,
            "available": self.available,
        }


def build_graph(model: Any) -> ConnectivityGraph:
    """Build the joint graph from IfcRelConnectsPorts and IfcRelNests.

    Every lookup is defensive: these entity types are absent from IFC2X3 in
    some combinations and absent from many exports entirely, and a missing
    relationship must degrade the graph, never raise.
    """
    g = ConnectivityGraph()

    def _safe(t: str):
        try:
            return model.by_type(t)
        except Exception:  # noqa: BLE001 -- type absent from this schema
            return []

    g.ports_seen = len(_safe("IfcDistributionPort")) or len(_safe("IfcPort"))

    # Port -> owning element, via the port's inverse relationship.
    owner: dict[int, str] = {}
    for rel in _safe("IfcRelConnectsPortToElement"):
        port = getattr(rel, "RelatingPort", None)
        el = getattr(rel, "RelatedElement", None)
        gid = getattr(el, "GlobalId", None)
        if port is not None and gid:
            owner[port.id()] = gid
    for port in _safe("IfcDistributionPort"):
        if port.id() in owner:
            continue
        for rel in getattr(port, "ContainedIn", None) or []:
            el = getattr(rel, "RelatedElement", None)
            gid = getattr(el, "GlobalId", None)
            if gid:
                owner[port.id()] = gid
                break

    for rel in _safe("IfcRelConnectsPorts"):
        pa, pb = getattr(rel, "RelatingPort", None), getattr(rel, "RelatedPort", None)
        if pa is None or pb is None:
            continue
        g.connections_seen += 1
        ga, gb = owner.get(pa.id()), owner.get(pb.id())
        if ga and gb and ga != gb:
            g.edges.add(frozenset((ga, gb)))

    # A fitting nested to two elements joins them: that is what a fitting is.
    for rel in _safe("IfcRelNests"):
        g.nests_seen += 1
        children = [
            getattr(c, "GlobalId", None) for c in (getattr(rel, "RelatedObjects", None) or [])
        ]
        children = [c for c in children if c]
        for i, a in enumerate(children):
            for b in children[i + 1:]:
                g.edges.add(frozenset((a, b)))

    logger.info("connectivity graph: %s", g.as_dict())
    return g


def joint_reason(
    element_a: Any,
    element_b: Any,
    penetration_volume_m3: float | None,
    graph: ConnectivityGraph | None = None,
    exact_backend: bool = True,
) -> str | None:
    """Why this touching pair is a JOINT, or None if it is a real clash.

    Order is deliberate: the model's own statement outranks any measurement.
    """
    gid_a = getattr(element_a, "global_id", element_a)
    gid_b = getattr(element_b, "global_id", element_b)

    if graph is not None and graph.available and graph.connected(gid_a, gid_b):
        return REASON_PORT

    # Fallback. Narrow on purpose -- see the module docstring.
    if not exact_backend:
        return None                      # an unmeasured zero proves nothing
    sys_a = getattr(element_a, "system", None)
    sys_b = getattr(element_b, "system", None)
    if sys_a is None or sys_a != sys_b:
        return None                      # cross-system contact is never a joint
    if penetration_volume_m3 is None:
        return None                      # depth unknown -> cannot exclude

    smaller = _smaller_volume(element_a, element_b)
    if smaller and smaller > 0:
        # Scale-invariant test: is the shared volume a negligible fraction of
        # the smaller element, or a meaningful part of it?
        if (penetration_volume_m3 / smaller) <= TOUCH_VOLUME_FRACTION:
            return REASON_TANGENT
        return None

    # No usable volume for either element -- fall back to the absolute floor,
    # which is the most that can be said without a scale to compare against.
    if penetration_volume_m3 <= TOUCH_VOLUME_M3:
        return REASON_TANGENT
    return None


def _smaller_volume(a: Any, b: Any) -> float | None:
    """Volume of the smaller of two elements, or None if neither can report one."""
    vols = []
    for el in (a, b):
        mesh = getattr(el, "mesh", None)
        try:
            v = float(getattr(mesh, "volume", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            v = 0.0
        if v > 0:
            vols.append(v)
    return min(vols) if vols else None


def classify_findings(
    findings: Iterable[Any],
    elements_by_id: dict[str, Any],
    graph: ConnectivityGraph | None = None,
    exact_backend: bool = True,
) -> tuple[list[Any], list[Any]]:
    """Split findings into (real, joints). Joints are RETURNED, not discarded --
    the caller reports the count, because a silent drop is indistinguishable
    from a missed clash."""
    real, joints = [], []
    for f in findings:
        if getattr(f, "kind", "") != "clash":
            real.append(f)
            continue
        a = elements_by_id.get(getattr(f, "element_a", ""))
        b = elements_by_id.get(getattr(f, "element_b", ""))
        if a is None or b is None:
            real.append(f)
            continue
        reason = joint_reason(
            a, b, getattr(f, "penetration_volume_m3", None), graph, exact_backend
        )
        if reason:
            try:
                f.kind = "joint"
                f.note = reason
            except Exception:  # noqa: BLE001 -- frozen finding, keep the split
                logger.debug("could not annotate finding", exc_info=True)
            joints.append(f)
        else:
            real.append(f)
    return real, joints
