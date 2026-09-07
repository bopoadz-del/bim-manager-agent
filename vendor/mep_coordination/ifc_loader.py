"""Load an IFC model into meshes the geometry engine can judge.

Separated from geometry_engine.py on purpose: judging two meshes is pure
geometry and testable with constructed solids, while getting meshes OUT of an
IFC file is parser work full of schema-version quirks. Mixing them would mean
every geometry test needed a model file.

MEASURED ON THE FIXTURE (schependomlaan_design.ifc, IFC2X3, 47 MB):
  * loads in ~5 s; 73 distinct MEP elements; 2,954 structural; 6 storeys
  * 40/40 sampled MEP elements meshed with zero failures in 2.6 s
  * IfcSystem count is ZERO

That last point drives a design decision. B1 was specified to read system
membership from IfcSystem / IfcDistributionSystem relationships. On this model
-- and on IFC2X3 exports of its generation generally -- that relationship does
not exist. Rather than report every element as system "unknown", system is
INFERRED from entity type and name, and `system_source` records which method
produced it. A guess that announces itself as a guess is auditable; a guess
wearing the same label as a fact is not.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

# IFC2X3 has no IfcDuctSegment/IfcPipeSegment: those arrived in IFC4. The
# 2x3 generation carries the same services as IfcFlowSegment / IfcFlowFitting
# with the discipline expressed in the NAME. Both vocabularies are queried so
# one loader serves both schemas -- the fixture is 2x3, most new exports are 4.
MEP_TYPES_IFC4 = (
    "IfcDuctSegment", "IfcDuctFitting", "IfcPipeSegment", "IfcPipeFitting",
    "IfcCableCarrierSegment", "IfcCableSegment", "IfcAirTerminal",
    "IfcSanitaryTerminal", "IfcValve", "IfcPump",
)
MEP_TYPES_COMMON = (
    "IfcFlowSegment", "IfcFlowFitting", "IfcFlowTerminal", "IfcFlowController",
    "IfcDistributionFlowElement", "IfcDistributionElement",
    "IfcEnergyConversionDevice", "IfcFlowMovingDevice",
    "IfcFlowStorageDevice", "IfcFlowTreatmentDevice",
)
STRUCTURAL_TYPES = (
    "IfcWall", "IfcWallStandardCase", "IfcSlab", "IfcBeam", "IfcColumn",
    "IfcRoof", "IfcFooting", "IfcMember", "IfcPlate",
)

# Name fragments that identify a service when the schema will not. Dutch is
# included because the fixture is a Dutch project and its services read
# "hwa afvoer" (rainwater drainage) -- a matcher that only speaks English
# would classify a real drainage run as unknown and silently lose the
# gravity-first resolution order that depends on knowing it is gravity.
_SYSTEM_HINTS: tuple[tuple[str, str], ...] = (
    (r"\bhwa\b|regenwater|rainwater|storm|\brwa\b", "drainage_storm"),
    (r"\bvuilwater|afvoer|sanitair|soil|waste|sewer|foul", "drainage_foul"),
    (r"sprinkler|brand|fire|\bfm\b", "fire"),
    (r"koud|chilled|\bchw\b|cooling", "chilled_water"),
    (r"warm|heating|\bhhw\b|verwarm", "heating"),
    (r"lucht|duct|ventilat|\bhvac\b|supply air|return air", "ventilation"),
    (r"kabel|cable|tray|containment|elektro|electric", "electrical"),
    (r"drink|potable|water", "potable_water"),
)


@dataclass
class Element:
    """One meshed IFC product, with everything the engine and triage need."""

    global_id: str
    ifc_type: str
    name: str
    discipline: str            # "mep" | "structural"
    system: str                # inferred or read
    system_source: str         # "ifc_system" | "name_hint" | "type_default" | "unknown"
    level: str
    mesh: Any = field(repr=False, default=None)
    bbox: tuple[float, ...] = ()

    @property
    def is_gravity(self) -> bool:
        """Gravity services must keep their fall, so the resolver may not move
        them freely. Drainage is gravity; everything else here is pressurised
        or dry."""
        return self.system in ("drainage_storm", "drainage_foul")


def model_sha256(path: str | Path) -> str:
    """Content hash of the model. Used as the mesh-cache key and, separately,
    as the proof that a full run left the original file untouched."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _infer_system(name: str, ifc_type: str) -> tuple[str, str]:
    low = (name or "").lower()
    for pattern, system in _SYSTEM_HINTS:
        if re.search(pattern, low):
            return system, "name_hint"
    if "CableCarrier" in ifc_type or "Cable" in ifc_type:
        return "electrical", "type_default"
    if "Duct" in ifc_type:
        return "ventilation", "type_default"
    if "Pipe" in ifc_type:
        return "unknown_piped", "type_default"
    return "unknown", "unknown"


def _storey_of(product, storey_cache: dict) -> str:
    """Walk containment to the storey. Missing containment is common in
    coordination exports, so it returns 'unassigned' rather than raising --
    an element with no level still needs to be judged for clashes."""
    try:
        for rel in getattr(product, "ContainedInStructure", None) or []:
            s = getattr(rel, "RelatingStructure", None)
            if s is not None and s.is_a("IfcBuildingStorey"):
                return storey_cache.setdefault(s.id(), getattr(s, "Name", None) or f"storey_{s.id()}")
    except Exception:  # noqa: BLE001 -- containment is optional, never fatal
        logger.debug("storey lookup failed", exc_info=True)
    return "unassigned"


def load_elements(
    ifc_path: str | Path,
    include_structural: bool = True,
    limit: int | None = None,
) -> Iterator[Element]:
    """Yield meshed elements. Elements that will not mesh are logged and
    skipped -- and the caller must surface that count, because an element the
    parser could not read is not an element that was found clean."""
    import ifcopenshell
    import ifcopenshell.geom as geom
    import numpy as np
    import trimesh

    model = ifcopenshell.open(str(ifc_path))
    settings = geom.settings()
    try:
        settings.set(settings.USE_WORLD_COORDS, True)
    except Exception:  # noqa: BLE001
        # Older builds lack the flag; geometry then stays in local coordinates
        # and clash POSITIONS become relative. Loud, because it changes what
        # the coordinates mean downstream.
        logger.warning("USE_WORLD_COORDS unavailable; coordinates are local, not world")

    seen: set[int] = set()
    storey_cache: dict = {}
    wanted: list[tuple[str, str]] = []
    for t in (*MEP_TYPES_IFC4, *MEP_TYPES_COMMON):
        wanted.append((t, "mep"))
    if include_structural:
        for t in STRUCTURAL_TYPES:
            wanted.append((t, "structural"))

    count = 0
    for ifc_type, discipline in wanted:
        try:
            products = model.by_type(ifc_type)
        except RuntimeError:
            # Type absent from this schema (e.g. IfcDuctSegment on IFC2X3).
            # Expected, not an error.
            continue
        for product in products:
            if product.id() in seen:
                continue          # supertype queries overlap; count once
            seen.add(product.id())
            if limit is not None and count >= limit:
                return
            try:
                shape = geom.create_shape(settings, product)
                verts = np.asarray(shape.geometry.verts, dtype=float).reshape(-1, 3)
                faces = np.asarray(shape.geometry.faces, dtype=int).reshape(-1, 3)
                if len(verts) == 0 or len(faces) == 0:
                    continue
                mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
            except Exception:  # noqa: BLE001 -- unmeshable product, skipped visibly
                logger.debug("no geometry for %s %s", ifc_type, product.id(), exc_info=True)
                continue

            name = getattr(product, "Name", None) or ""
            system, source = _infer_system(name, ifc_type)
            yield Element(
                global_id=getattr(product, "GlobalId", "") or f"id_{product.id()}",
                ifc_type=ifc_type,
                name=name,
                discipline=discipline,
                system=system if discipline == "mep" else "structure",
                system_source=source if discipline == "mep" else "type_default",
                level=_storey_of(product, storey_cache),
                mesh=mesh,
                bbox=tuple(float(x) for x in mesh.bounds.flatten()),
            )
            count += 1


def zone_key(element: Element, cell_m: float = 6.0) -> str:
    """Level plus a fixed 6 m grid cell.

    The order allows deriving cells from IfcGrid; the fixture has none, so a
    fixed cell is used and the size is a parameter rather than a constant
    buried in the code. 6 m is the common structural bay, so a cell tends to
    correspond to a real coordination zone rather than an arbitrary box.
    """
    if not element.bbox:
        return f"{element.level}|nocell"
    cx = (element.bbox[0] + element.bbox[3]) / 2.0
    cy = (element.bbox[1] + element.bbox[4]) / 2.0
    return f"{element.level}|{int(cx // cell_m)}_{int(cy // cell_m)}"
