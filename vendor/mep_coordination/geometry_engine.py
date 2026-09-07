"""geometry_engine — exact clash and clearance from one pass over IFC meshes.

WHY THIS EXISTS
The Fork's existing detector compares axis-aligned bounding boxes. Its own
output says so:

    "basic (bounding-box - not geometric intersection. Verify critical
     clashes in Navisworks Clash Detective or Solibri)"

An AABB is the smallest upright box containing a shape. For a diagonal pipe
or a sloped duct the box is mostly empty air, so two services whose boxes
overlap frequently do not touch. That is a false positive, and a coordination
report full of them is worse than none: the engineer stops reading it.

This block replaces the DECISION while keeping the AABB as a cheap pre-filter.
Boxes are used to discard pairs that provably cannot touch; every surviving
pair is then tested on real triangle meshes.

ONE ENGINE, TWO OUTPUTS -- the order's requirement, and it falls out of the
geometry rather than being bolted on:

    overlap volume > 0        -> CLASH        (solids interpenetrate)
    overlap volume == 0 and
      min distance < rule     -> CLEARANCE    (no touch, too close to build,
                                               maintain, or insulate)

Both come from the same mesh pair, so a clash and a clearance violation can
never disagree about where the elements are.

READS   IFC file path; element pairs; a clearance rule table (block 2).
WRITES  a findings list -- one record per pair, with the measurement that
        produced it and the rule that judged it.
NEVER   writes to the IFC model, or to any authoring tool. Read-only on
        geometry, always. The original file is opened and closed; nothing
        is saved back. Resolution belongs to model_clone (block 5).

HONEST LIMITS, stated because a clash engine that overstates itself is the
thing this replaces:
  * IfcOpenShell must produce a mesh. Elements with no geometry
    (annotations, spaces, some proxies) are reported as skipped, not as
    clean. Silence is not a pass.
  * Exact boolean intersection needs a watertight mesh and the manifold3d
    backend. Where either is missing the pair falls back to a surface
    intersection test, which detects touching but cannot measure penetration
    depth. The record says which method judged it -- never assume.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

# Method labels. These travel in the output so a reader always knows how a
# finding was judged, and can discount it accordingly.
METHOD_EXACT = "exact_boolean"          # penetration volume measured
METHOD_SURFACE = "surface_intersection"  # touch proven, depth unknown
METHOD_DISTANCE = "min_distance"         # no touch; separation measured
METHOD_SKIPPED = "no_geometry"           # could not be judged at all

KIND_CLASH = "clash"
KIND_CLEARANCE = "clearance"
KIND_CLEAR = "clear"
KIND_UNJUDGED = "unjudged"


@dataclass
class Finding:
    """One judged pair. ``rule_id`` is None for a hard clash: solids that
    interpenetrate are a clash under any rule, so none is cited."""

    element_a: str
    element_b: str
    kind: str
    method: str
    distance_m: float | None = None
    penetration_volume_m3: float | None = None
    rule_id: str | None = None
    required_clearance_m: float | None = None
    category_a: str | None = None
    category_b: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class GeometryResult:
    findings: list[Finding] = field(default_factory=list)
    pairs_tested: int = 0
    pairs_prefiltered: int = 0
    elements_without_geometry: list[str] = field(default_factory=list)
    exact_backend_available: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.as_dict() for f in self.findings],
            "pairs_tested": self.pairs_tested,
            "pairs_prefiltered": self.pairs_prefiltered,
            "elements_without_geometry": self.elements_without_geometry,
            "exact_backend_available": self.exact_backend_available,
            "counts": {
                KIND_CLASH: sum(1 for f in self.findings if f.kind == KIND_CLASH),
                KIND_CLEARANCE: sum(1 for f in self.findings if f.kind == KIND_CLEARANCE),
                KIND_UNJUDGED: sum(1 for f in self.findings if f.kind == KIND_UNJUDGED),
            },
        }


def exact_backend_available() -> bool:
    """True when boolean intersection can measure penetration volume.

    Reported in every result. Without it findings degrade to surface
    intersection, which is still a real clash but carries no depth -- and a
    consumer ranking by severity must know the difference.
    """
    try:
        import manifold3d  # noqa: F401
    except Exception:
        return False
    try:
        import trimesh  # noqa: F401
    except Exception:
        return False
    return True


def aabb_overlaps(box_a: Sequence[float], box_b: Sequence[float], pad: float = 0.0) -> bool:
    """Cheap pre-filter. Boxes are (minx,miny,minz,maxx,maxy,maxz).

    ``pad`` widens both boxes by the largest clearance rule in play, so a pair
    that is far apart in box terms cannot hide a clearance violation. Without
    the pad this filter would discard exactly the near-miss pairs the
    clearance check exists to find -- the pre-filter would silently defeat
    half the engine.
    """
    ax0, ay0, az0, ax1, ay1, az1 = box_a
    bx0, by0, bz0, bx1, by1, bz1 = box_b
    # bool() is not cosmetic: bounds arrive as numpy floats, so the comparison
    # chain yields numpy.bool_, which fails `is True` and does not JSON
    # serialise. Callers must get a real Python bool.
    return bool(
        ax0 - pad <= bx1 and bx0 - pad <= ax1
        and ay0 - pad <= by1 and by0 - pad <= ay1
        and az0 - pad <= bz1 and bz0 - pad <= az1
    )


def _mesh_pair_measure(mesh_a, mesh_b) -> tuple[str, float | None, float | None]:
    """Return (method, penetration_volume, min_distance) for two meshes.

    Tries the strongest available evidence first and says which it used.
    """
    import numpy as np

    # 1. Exact boolean: measures HOW MUCH two solids interpenetrate.
    if exact_backend_available():
        try:
            if mesh_a.is_watertight and mesh_b.is_watertight:
                inter = mesh_a.intersection(mesh_b)
                vol = float(getattr(inter, "volume", 0.0) or 0.0)
                if vol > 0:
                    return METHOD_EXACT, vol, 0.0
                # Watertight, no volume -> genuinely apart. Measure the gap.
                d = _min_distance(mesh_a, mesh_b)
                return METHOD_DISTANCE, 0.0, d
        except Exception:  # noqa: BLE001 -- fall through to a weaker method
            logger.debug("boolean intersection failed; falling back", exc_info=True)

    # 2. Surface intersection: proves contact without needing watertightness.
    try:
        if _surfaces_intersect(mesh_a, mesh_b):
            return METHOD_SURFACE, None, 0.0
    except Exception:  # noqa: BLE001
        logger.debug("surface intersection test failed", exc_info=True)

    # 3. Separation.
    try:
        return METHOD_DISTANCE, 0.0, _min_distance(mesh_a, mesh_b)
    except Exception:  # noqa: BLE001
        logger.debug("distance query failed", exc_info=True)
        return METHOD_SKIPPED, None, None


def _min_distance(mesh_a, mesh_b) -> float:
    """Shortest distance between two meshes, in metres.

    Sampled both ways: nearest-point queries are asymmetric, and taking one
    direction only can overstate the gap when one mesh is much denser.
    """
    import numpy as np
    from trimesh.proximity import ProximityQuery

    d1 = float(np.min(ProximityQuery(mesh_a).vertex(mesh_b.vertices)[0]))
    d2 = float(np.min(ProximityQuery(mesh_b).vertex(mesh_a.vertices)[0]))
    return min(d1, d2)


def _surfaces_intersect(mesh_a, mesh_b) -> bool:
    """Do the two surfaces actually cross? Uses ray casting as a proxy."""
    import numpy as np

    if not aabb_overlaps(mesh_a.bounds.flatten(), mesh_b.bounds.flatten()):
        return False
    contains = mesh_b.contains(mesh_a.vertices)
    if bool(np.any(contains)):
        return True
    return bool(np.any(mesh_a.contains(mesh_b.vertices)))


def judge_pair(
    element_a: str,
    element_b: str,
    mesh_a,
    mesh_b,
    required_clearance_m: float | None = None,
    rule_id: str | None = None,
    category_a: str | None = None,
    category_b: str | None = None,
) -> Finding:
    """Judge one element pair. The single decision point of this block.

    Precedence is deliberate: interpenetration is a clash regardless of any
    clearance rule, so it is decided first and cites no rule. Only elements
    that do NOT touch are measured against a clearance requirement.
    """
    method, penetration, distance = _mesh_pair_measure(mesh_a, mesh_b)

    if method == METHOD_SKIPPED:
        return Finding(
            element_a, element_b, KIND_UNJUDGED, method,
            category_a=category_a, category_b=category_b,
            note="geometry unavailable for at least one element; not judged clean",
        )

    if method in (METHOD_EXACT, METHOD_SURFACE) and (penetration is None or penetration > 0):
        return Finding(
            element_a, element_b, KIND_CLASH, method,
            distance_m=0.0, penetration_volume_m3=penetration,
            category_a=category_a, category_b=category_b,
            note=None if method == METHOD_EXACT
            else "contact proven; penetration depth not measurable without a watertight mesh",
        )

    if required_clearance_m is not None and distance is not None and distance < required_clearance_m:
        return Finding(
            element_a, element_b, KIND_CLEARANCE, METHOD_DISTANCE,
            distance_m=distance, penetration_volume_m3=0.0,
            rule_id=rule_id, required_clearance_m=required_clearance_m,
            category_a=category_a, category_b=category_b,
        )

    return Finding(
        element_a, element_b, KIND_CLEAR, METHOD_DISTANCE,
        distance_m=distance, penetration_volume_m3=0.0,
        rule_id=rule_id, required_clearance_m=required_clearance_m,
        category_a=category_a, category_b=category_b,
    )
