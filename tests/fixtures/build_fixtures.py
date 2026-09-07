"""Build the small IFC fixtures the acceptance traps need.

These are real IFC4 files written by ifcopenshell and read back through the same
loader the service uses in production. They are not stubs and not stand-ins: the
whole point of a trap fixture is that the pipeline cannot tell it from a real
model, so a monitor that would wave the trap through in production waves it
through here too.

Each fixture is built to make exactly one thing go wrong, with everything else
correct, so a failure names its own cause.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import ifcopenshell


@dataclass
class BoxSpec:
    name: str
    ifc_type: str
    x: float
    y: float
    z: float
    dx: float
    dy: float
    dz: float


#: Namespace for deterministic fixture GlobalIds.
_NS = uuid.UUID("2f9a1d3e-5b47-4c26-9f10-8d7c6b5a4e33")


def _guid(name: str | None = None) -> str:
    """A GlobalId derived from the element name, not from randomness.

    Real authoring tools keep an element's GlobalId across model versions -- that
    persistence is what makes a version diff possible at all. A fixture builder
    that minted fresh ids per run would give v1 and v2 disjoint id sets, and the
    diff would report every pair as new and every old pair as resolved while
    appearing to work.
    """
    if name is None:
        return ifcopenshell.guid.compress(uuid.uuid4().hex)
    return ifcopenshell.guid.compress(uuid.uuid5(_NS, name).hex)


def build(specs: list[BoxSpec], out_path: str | Path, project_name: str = "trap") -> Path:
    """Write an IFC4 file containing one extruded box per spec."""
    f = ifcopenshell.file(schema="IFC4")

    person = f.create_entity("IfcPerson", FamilyName="mep-judge")
    org = f.create_entity("IfcOrganization", Name="mep-judge fixtures")
    p_and_o = f.create_entity("IfcPersonAndOrganization", ThePerson=person, TheOrganization=org)
    app = f.create_entity(
        "IfcApplication",
        ApplicationDeveloper=org,
        Version="0.1.0",
        ApplicationFullName="mep-judge fixture builder",
        ApplicationIdentifier="mep-judge",
    )
    owner = f.create_entity(
        "IfcOwnerHistory", OwningUser=p_and_o, OwningApplication=app, ChangeAction="ADDED"
    )

    length = f.create_entity("IfcSIUnit", UnitType="LENGTHUNIT", Name="METRE")
    area = f.create_entity("IfcSIUnit", UnitType="AREAUNIT", Name="SQUARE_METRE")
    volume = f.create_entity("IfcSIUnit", UnitType="VOLUMEUNIT", Name="CUBIC_METRE")
    units = f.create_entity("IfcUnitAssignment", Units=[length, area, volume])

    origin = f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0))
    axis_z = f.create_entity("IfcDirection", DirectionRatios=(0.0, 0.0, 1.0))
    axis_x = f.create_entity("IfcDirection", DirectionRatios=(1.0, 0.0, 0.0))
    world = f.create_entity(
        "IfcAxis2Placement3D", Location=origin, Axis=axis_z, RefDirection=axis_x
    )
    context = f.create_entity(
        "IfcGeometricRepresentationContext",
        ContextType="Model",
        CoordinateSpaceDimension=3,
        Precision=1e-5,
        WorldCoordinateSystem=world,
    )
    body = f.create_entity(
        "IfcGeometricRepresentationSubContext",
        ContextIdentifier="Body",
        ContextType="Model",
        ParentContext=context,
        TargetView="MODEL_VIEW",
    )

    project = f.create_entity(
        "IfcProject",
        GlobalId=_guid(f"{project_name}/IfcProject"),
        OwnerHistory=owner,
        Name=project_name,
        UnitsInContext=units,
        RepresentationContexts=[context],
    )
    site_placement = f.create_entity("IfcLocalPlacement", RelativePlacement=world)
    site = f.create_entity(
        "IfcSite", GlobalId=_guid(), OwnerHistory=owner, Name="Site", ObjectPlacement=site_placement
    )
    building = f.create_entity(
        "IfcBuilding",
        GlobalId=_guid(f"{project_name}/IfcBuilding"),
        OwnerHistory=owner,
        Name="Building",
        ObjectPlacement=f.create_entity(
            "IfcLocalPlacement", PlacementRelTo=site_placement, RelativePlacement=world
        ),
    )
    storey_placement = f.create_entity(
        "IfcLocalPlacement", PlacementRelTo=building.ObjectPlacement, RelativePlacement=world
    )
    storey = f.create_entity(
        "IfcBuildingStorey",
        GlobalId=_guid(f"{project_name}/IfcBuildingStorey"),
        OwnerHistory=owner,
        Name="Level 00",
        ObjectPlacement=storey_placement,
        Elevation=0.0,
    )

    f.create_entity(
        "IfcRelAggregates", GlobalId=_guid(), RelatingObject=project, RelatedObjects=[site]
    )
    f.create_entity(
        "IfcRelAggregates", GlobalId=_guid(), RelatingObject=site, RelatedObjects=[building]
    )
    f.create_entity(
        "IfcRelAggregates", GlobalId=_guid(), RelatingObject=building, RelatedObjects=[storey]
    )

    products = []
    for spec in specs:
        # Profile is centred on its own origin, so the placement point is the
        # box centre in X/Y and its base in Z.
        profile = f.create_entity(
            "IfcRectangleProfileDef",
            ProfileType="AREA",
            ProfileName=spec.name,
            XDim=spec.dx,
            YDim=spec.dy,
        )
        loc = f.create_entity(
            "IfcCartesianPoint", Coordinates=(float(spec.x), float(spec.y), float(spec.z))
        )
        placement3d = f.create_entity(
            "IfcAxis2Placement3D", Location=loc, Axis=axis_z, RefDirection=axis_x
        )
        solid = f.create_entity(
            "IfcExtrudedAreaSolid",
            SweptArea=profile,
            Position=f.create_entity(
                "IfcAxis2Placement3D",
                Location=f.create_entity("IfcCartesianPoint", Coordinates=(0.0, 0.0, 0.0)),
                Axis=axis_z,
                RefDirection=axis_x,
            ),
            ExtrudedDirection=axis_z,
            Depth=spec.dz,
        )
        shape = f.create_entity(
            "IfcShapeRepresentation",
            ContextOfItems=body,
            RepresentationIdentifier="Body",
            RepresentationType="SweptSolid",
            Items=[solid],
        )
        product_shape = f.create_entity("IfcProductDefinitionShape", Representations=[shape])
        product = f.create_entity(
            spec.ifc_type,
            GlobalId=_guid(spec.name),
            OwnerHistory=owner,
            Name=spec.name,
            ObjectPlacement=f.create_entity(
                "IfcLocalPlacement", PlacementRelTo=storey_placement, RelativePlacement=placement3d
            ),
            Representation=product_shape,
        )
        products.append(product)

    f.create_entity(
        "IfcRelContainedInSpatialStructure",
        GlobalId=_guid(),
        OwnerHistory=owner,
        RelatingStructure=storey,
        RelatedElements=products,
    )

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    f.write(str(out))
    return out


# -- the traps ------------------------------------------------------------
def boundary_trap(out_path: str | Path) -> Path:
    """A2. The only move that solves the clash lands in the next zone.

    Deliberately contains no gas main and therefore no applicable clearance
    rule. An earlier version used a gas-to-tray violation, and the 300 mm
    gas-to-anything wildcard then made every blocker placed near the tray a
    violation in its own right -- the trap tested the confusion it had created
    rather than the boundary.

    A duct interpenetrates a wall stub by 300 mm along its tightest axis. Zone A is
    otherwise a lattice of duct filling the cell in Y and Z, with one clear
    corridor running along +X at the pair's own height. A six-sided shell was
    not enough here: the resolver's search includes in-plane diagonals, and a
    diagonal slips through the corner between two plates. A lattice has no
    corners to slip through.

    So the only move that separates the pair runs +X, and +X crosses into zone B
    -- a wall of duct at every depth the back-out could reach. Nothing but the
    BoundaryMonitor stands between that move and a commit.
    """
    specs = [
        # The fixed side is structure, so the resolver has no choice about which
        # element to move: you sleeve through a wall, you do not shift it. That
        # makes the moving element -- and therefore the direction that separates
        # the pair -- deterministic, which a trap about one specific direction
        # needs to be.
        BoxSpec("building envelope wall W-01", "IfcWall", 5.0, 1.0, 0.85, 1.2, 0.3, 0.3),
        BoxSpec("ventilation duct ZA-2", "IfcFlowSegment", 5.5, 1.0, 0.85, 1.0, 0.3, 0.3),
    ]

    # The lattice. Every (y, z) on a 400 mm pitch except the corridor the pair
    # occupies, out far enough to cover the kit's widest candidate (3x need).
    ticks = [round(-1.0 + 0.4 * i, 2) for i in range(11)]
    n = 0
    for y in ticks:
        for z in ticks:
            if abs(y - 1.0) < 0.2 and abs(z - 1.0) < 0.2:
                continue  # the corridor
            specs.append(
                # Spans the whole cell in X. A shorter lattice let the longer of
                # the two clashing ducts stick out past its end, and the search
                # simply walked it off the end diagonally into clear air.
                BoxSpec(f"ventilation duct LAT-{n:03d}", "IfcFlowSegment",
                        4.5, y, z - 0.15, 3.0, 0.3, 0.3)
            )
            n += 1

    # Cap the far end of the corridor. Without it the corridor is a tunnel open
    # at both ends, and the search simply backs out along -X, which is a
    # perfectly good fix and not the one this trap is about.
    specs.append(
        BoxSpec("ventilation duct CAP-01", "IfcFlowSegment", 3.5, 1.0, 0.85, 0.6, 0.3, 0.3)
    )

    # Zone B: duct at every depth a +X back-out could reach.
    for i, x in enumerate((6.3, 6.9, 7.5, 8.1, 8.7)):
        specs.append(
            BoxSpec(f"ventilation duct VD-{i:02d}", "IfcFlowSegment", x, 1.0, 0.0, 0.6, 3.0, 3.0)
        )
    return build(specs, out_path, project_name="boundary trap")


def gravity_trap(out_path: str | Path) -> Path:
    """A3. A foul drain clashes with a duct, and the only clear space is above.

    The drain is walled in laterally, so every lateral candidate fails geometry
    and the search reaches the vertical ones. B4 withholds Z from a gravity
    element, and IntegrityMonitor's fall check is the backstop that must fail a
    vertical move if one ever reaches it.
    """
    specs = [
        BoxSpec("vuilwater afvoer FD-01", "IfcFlowSegment", 5.0, 5.0, 2.0, 0.3, 0.3, 0.3),
        BoxSpec("ventilation duct VD-01", "IfcFlowSegment", 5.15, 5.0, 2.0, 0.3, 0.3, 0.3),
        BoxSpec("electric cable tray CT-01", "IfcFlowSegment", 5.0, 4.0, 2.0, 3.0, 0.4, 0.4),
        BoxSpec("electric cable tray CT-02", "IfcFlowSegment", 5.0, 6.0, 2.0, 3.0, 0.4, 0.4),
        BoxSpec("ventilation duct VD-02", "IfcFlowSegment", 3.5, 5.0, 2.0, 0.4, 3.0, 0.4),
        BoxSpec("ventilation duct VD-03", "IfcFlowSegment", 6.5, 5.0, 2.0, 0.4, 3.0, 0.4),
    ]
    return build(specs, out_path, project_name="gravity trap")


def unsourced_trap(out_path: str | Path) -> Path:
    """A4. Two services whose separation only the stripped rule governed.

    Run twice: once with the full table and once with the gas-to-LV rule
    removed. With the rule, the pair is a clearance violation with a clause and
    a distance to move to. Without it, there is no authorised distance and the
    finding must be flagged rather than proposed.
    """
    specs = [
        BoxSpec("gas main GM-01", "IfcFlowSegment", 2.0, 2.0, 2.0, 0.2, 0.2, 0.2),
        BoxSpec("electric cable tray CT-01", "IfcFlowSegment", 2.0, 2.25, 2.0, 0.2, 0.2, 0.2),
    ]
    return build(specs, out_path, project_name="unsourced trap")


def diff_pair(out_dir: str | Path) -> tuple[Path, Path]:
    """A7. Version 1, then version 2 with one planted fix and one planted regression.

    The regression is the harder half. For a pair to *regress* it must have been
    measured clear in v1, not merely absent -- "we checked and it was fine, and
    now it is not" is a different and much worse fact than "we are seeing this
    pair for the first time". A pair is only measured when the padded box filter
    admits it, and the pad is the widest rule that can fire on the model. So the
    model carries a wall and a gas main, far from everything, purely to make the
    5 m building-to-gas rule applicable and open the pad wide enough that the
    gas-to-tray pair is measured at all.

    v1: VD-01/VD-02 interpenetrate      -> fixed in v2 (resolved)
        GM-01/CT-01 are 600 mm apart,
        against a 400 mm rule: CLEAR    -> 100 mm in v2 (regressed)
    """
    out = Path(out_dir)
    common = [
        # Far from everything. Present so the 5 m building-to-gas rule counts as
        # applicable and widens the pre-filter pad; never close enough to anything
        # to produce a finding of its own.
        BoxSpec("building envelope wall W-01", "IfcWall", 60.0, 60.0, 0.0, 0.4, 6.0, 3.0),
    ]
    v1 = common + [
        BoxSpec("ventilation duct VD-01", "IfcFlowSegment", 1.0, 1.0, 1.0, 0.4, 0.4, 0.4),
        BoxSpec("ventilation duct VD-02", "IfcFlowSegment", 1.2, 1.0, 1.0, 0.4, 0.4, 0.4),
        BoxSpec("gas main GM-01", "IfcFlowSegment", 6.0, 1.0, 1.0, 0.4, 0.4, 0.4),
        BoxSpec("electric cable tray CT-01", "IfcFlowSegment", 7.0, 1.0, 1.0, 0.4, 0.4, 0.4),
    ]
    v2 = common + [
        # planted FIX: VD-02 moved clear of VD-01
        BoxSpec("ventilation duct VD-01", "IfcFlowSegment", 1.0, 1.0, 1.0, 0.4, 0.4, 0.4),
        BoxSpec("ventilation duct VD-02", "IfcFlowSegment", 3.0, 1.0, 1.0, 0.4, 0.4, 0.4),
        # planted REGRESSION: CT-01 closes to 100 mm, breaching the 400 mm rule
        BoxSpec("gas main GM-01", "IfcFlowSegment", 6.0, 1.0, 1.0, 0.4, 0.4, 0.4),
        BoxSpec("electric cable tray CT-01", "IfcFlowSegment", 6.5, 1.0, 1.0, 0.4, 0.4, 0.4),
    ]
    return (
        build(v1, out / "diff_v1.ifc", project_name="diff v1"),
        build(v2, out / "diff_v2.ifc", project_name="diff v2"),
    )


def rebase_trap(out_path: str | Path) -> Path:
    """A5. Zone B verifies a move; then zone A commits one that invalidates it.

    Three elements in two cells, sized so the cells cannot merge into one zone:

      zone B (cell 0_0)  a gas main and an LV tray 50 mm apart, against a cited
                         400 mm rule -- so the fix is a move of at least 425 mm,
                         not a 25 mm nudge
      zone A (cell 1_0)  a duct 1 m away, inside B's 2 m buffer

    The move has to be large for the trap to mean anything. With a 25 mm
    displacement the element's old and new footprints overlap almost entirely,
    so a neighbour arriving at the new position was already touching the old one
    and the monitor is right to say nothing was made worse. At 425 mm the two
    positions are disjoint, and the test can put zone A's element on the new one
    and nowhere near the old one.
    """
    specs = [
        BoxSpec("gas main GM-01", "IfcFlowSegment", 5.0, 1.0, 1.0, 0.2, 0.2, 0.2),
        BoxSpec("electric cable tray CT-01", "IfcFlowSegment", 5.25, 1.0, 1.0, 0.2, 0.2, 0.2),
        BoxSpec("ventilation duct AA-01", "IfcFlowSegment", 6.5, 1.0, 1.0, 0.2, 0.2, 0.2),
    ]
    return build(specs, out_path, project_name="rebase trap")


def arbitration_trap(out_path: str | Path) -> Path:
    """A clash that genuinely straddles a zone boundary.

    Two ducts overlapping across x = 6.0, so their centres fall in different
    6 m grid cells and therefore different zones. Neither zone owns the pair, so
    neither may commit a fix for it alone -- which is the whole reason
    arbitration exists.
    """
    specs = [
        BoxSpec("ventilation duct XA-1", "IfcFlowSegment", 5.7, 1.0, 1.0, 0.8, 0.3, 0.3),
        BoxSpec("ventilation duct XB-1", "IfcFlowSegment", 6.3, 1.0, 1.0, 0.8, 0.3, 0.3),
    ]
    return build(specs, out_path, project_name="arbitration trap")


def connected_trap(out_path: str | Path) -> Path:
    """A model that actually carries connectivity.

    Every other fixture, and both public models, omit IfcDistributionPort
    entirely -- which is why IntegrityMonitor reports connectivity as
    ``unprovable`` almost everywhere. That is the honest answer for those models
    and it means the code path that runs when ports *are* present goes untested.
    This fixture exists to exercise it: two duct segments joined end to end by a
    real port connection, so moving one away from the other breaks a joint the
    model itself asserts.
    """
    specs = [
        BoxSpec("ventilation duct JN-1", "IfcFlowSegment", 2.0, 1.0, 1.0, 1.0, 0.3, 0.3),
        BoxSpec("ventilation duct JN-2", "IfcFlowSegment", 3.0, 1.0, 1.0, 1.0, 0.3, 0.3),
    ]
    path = build(specs, out_path, project_name="connected trap")

    f = ifcopenshell.open(str(path))
    segments = f.by_type("IfcFlowSegment")
    owner = f.by_type("IfcOwnerHistory")[0]

    ports = []
    for i, segment in enumerate(segments):
        port = f.create_entity(
            "IfcDistributionPort",
            GlobalId=_guid(f"port/{segment.Name}"),
            OwnerHistory=owner,
            Name=f"P{i}",
            PredefinedType="DUCT",
        )
        f.create_entity(
            "IfcRelConnectsPortToElement",
            GlobalId=_guid(f"rel/{segment.Name}"),
            OwnerHistory=owner,
            RelatingPort=port,
            RelatedElement=segment,
        )
        ports.append(port)

    f.create_entity(
        "IfcRelConnectsPorts",
        GlobalId=_guid("rel/joint"),
        OwnerHistory=owner,
        RelatingPort=ports[0],
        RelatedPort=ports[1],
    )
    f.write(str(path))
    return Path(path)


def build_all(out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    v1, v2 = diff_pair(out)
    return {
        "boundary": boundary_trap(out / "boundary_trap.ifc"),
        "gravity": gravity_trap(out / "gravity_trap.ifc"),
        "unsourced": unsourced_trap(out / "unsourced_trap.ifc"),
        "rebase": rebase_trap(out / "rebase_trap.ifc"),
        "arbitration": arbitration_trap(out / "arbitration_trap.ifc"),
        "connected": connected_trap(out / "connected_trap.ifc"),
        "diff_v1": v1,
        "diff_v2": v2,
    }


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else "tests/fixtures/generated"
    for name, path in build_all(target).items():
        print(f"{name}: {path} ({path.stat().st_size} bytes)")
