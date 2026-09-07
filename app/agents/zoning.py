"""Cutting a model into zones a reviewer can actually hold in their head.

The kit's ``zone_key`` gives level plus a fixed grid cell, which is the right
primitive and the wrong granularity on its own: a 6 m cell in a plant room holds
four hundred elements and a 6 m cell in a car park holds two. So cells are
merged, by adjacency within a level, until each zone is under the element
ceiling.

Two constraints shape the merge:

* **Never split a room.** A zone boundary through the middle of a plant room
  hands two people the same coordination problem and neither the whole of it.
  Rooms are inferred from level plus contiguity, so the merge only ever grows
  cells, never subdivides one.
* **Risers and shafts are their own zones.** A riser is vertical, so it appears
  in every level's grid at the same cell and is the one element group whose
  coordination question spans storeys. Splitting it by level would ask each
  storey to solve a problem that is not local to it.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

RISER_HINTS = ("riser", "stack", "shaft", "standleiding", "stijgleiding")
CORRIDOR_HINTS = ("corridor", "gang", "hallway", "main run", "hoofdleiding")

DEDICATED_RISER = "riser_or_shaft"
DEDICATED_CORRIDOR = "main_corridor"


@dataclass
class ZonePlan:
    zone_key: str
    level: str
    grid_cell: str
    element_gids: list[str] = field(default_factory=list)
    #: The kit-level cells this zone was merged from. Congestion is measured per
    #: cell by the kit, so a merged zone has to add its parts back up -- looking
    #: it up under the merged name finds nothing and silently scores zero.
    member_cells: list[str] = field(default_factory=list)
    dedicated_reason: str | None = None
    congestion: float = 0.0
    programme_rank: int = 0
    priority: float = 0.0

    @property
    def element_count(self) -> int:
        return len(self.element_gids)


def _name_matches(element: Any, hints: tuple[str, ...]) -> bool:
    text = f"{getattr(element, 'name', '') or ''}".lower()
    return any(h in text for h in hints)


def _is_riser(element: Any) -> bool:
    """A riser by name, or by shape: much taller than it is wide."""
    if _name_matches(element, RISER_HINTS):
        return True
    box = getattr(element, "bbox", None)
    if not box or len(box) < 6:
        return False
    dx, dy, dz = box[3] - box[0], box[4] - box[1], box[5] - box[2]
    footprint = max(dx, dy)
    return dz > 2.5 and footprint > 0 and dz / footprint > 4.0


def _cell_coords(grid_cell: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(-?\d+)_(-?\d+)", grid_cell)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _split_key(zone_key: str) -> tuple[str, str]:
    level, _, cell = zone_key.partition("|")
    return level, cell


def _merge_cells(
    cells: dict[str, list[str]], max_elements: int
) -> list[tuple[str, list[str], list[str]]]:
    """Grow adjacent cells into groups no larger than ``max_elements``.

    Greedy, in a fixed sorted order, so the same model always yields the same
    zones. Determinism matters more than optimality here: a reviewer who reopens
    a run must see the zone they reviewed yesterday, with the same name.
    """
    coords: dict[tuple[int, int], str] = {}
    loose: list[tuple[str, list[str]]] = []
    for cell, gids in cells.items():
        xy = _cell_coords(cell)
        if xy is None:
            loose.append((cell, gids))
        else:
            coords[xy] = cell

    groups: list[tuple[str, list[str], list[str]]] = []
    unassigned = sorted(coords)
    claimed: set[tuple[int, int]] = set()

    for origin in unassigned:
        if origin in claimed:
            continue
        member_cells = [origin]
        claimed.add(origin)
        gids = list(cells[coords[origin]])

        # Grow outward one ring at a time while there is room.
        frontier = [origin]
        while frontier and len(gids) < max_elements:
            x, y = frontier.pop(0)
            for nx, ny in ((x + 1, y), (x, y + 1), (x - 1, y), (x, y - 1)):
                nxt = (nx, ny)
                if nxt in claimed or nxt not in coords:
                    continue
                candidate = cells[coords[nxt]]
                if len(gids) + len(candidate) > max_elements:
                    continue
                claimed.add(nxt)
                member_cells.append(nxt)
                gids.extend(candidate)
                frontier.append(nxt)

        member_cells.sort()
        names = [coords[c] for c in member_cells]
        name = names[0]
        if len(member_cells) > 1:
            name = f"{name}+{len(member_cells) - 1}"
        groups.append((name, gids, names))

    groups.extend((cell, gids, [cell]) for cell, gids in loose)
    return groups


def plan_zones(
    elements: list[Any],
    max_elements: int = 800,
    cell_m: float = 6.0,
) -> list[ZonePlan]:
    """Turn a meshed model into a deterministic list of zones."""
    from app.blocks.ifc_loader import zone_key as kit_zone_key

    risers: list[Any] = []
    corridors: list[Any] = []
    ordinary: list[Any] = []
    for el in elements:
        if _is_riser(el):
            risers.append(el)
        elif _name_matches(el, CORRIDOR_HINTS):
            corridors.append(el)
        else:
            ordinary.append(el)

    plans: list[ZonePlan] = []

    by_level: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for el in ordinary:
        level, cell = _split_key(kit_zone_key(el, cell_m=cell_m))
        by_level[level][cell].append(el.global_id)

    for level in sorted(by_level):
        for cell_name, gids, members in _merge_cells(dict(by_level[level]), max_elements):
            plans.append(
                ZonePlan(
                    zone_key=f"{level}|{cell_name}",
                    level=level,
                    grid_cell=cell_name,
                    element_gids=sorted(gids),
                    member_cells=[f"{level}|{m}" for m in members],
                )
            )

    # Risers and corridors span levels; they get one zone each, keyed by system
    # so two unrelated stacks do not become one review package.
    for group, reason in ((risers, DEDICATED_RISER), (corridors, DEDICATED_CORRIDOR)):
        by_system: dict[str, list[str]] = defaultdict(list)
        for el in group:
            by_system[getattr(el, "system", "unknown")].append(el.global_id)
        for system in sorted(by_system):
            plans.append(
                ZonePlan(
                    zone_key=f"{reason}|{system}",
                    level="(all)",
                    grid_cell=reason,
                    element_gids=sorted(by_system[system]),
                    dedicated_reason=reason,
                )
            )

    return [p for p in plans if p.element_gids]


def buffer_gids(
    plan: ZonePlan, elements_by_id: dict[str, Any], buffer_m: float
) -> list[str]:
    """Elements outside the zone but within ``buffer_m`` of its bounding box.

    The buffer is what the monitors check against. Without it a move could be
    verified against an empty edge of the zone and land on a pipe fifty
    centimetres outside it.
    """
    boxes = [elements_by_id[g].bbox for g in plan.element_gids if elements_by_id.get(g) is not None]
    boxes = [b for b in boxes if b and len(b) >= 6]
    if not boxes:
        return []
    lo = [min(b[i] for b in boxes) - buffer_m for i in range(3)]
    hi = [max(b[i + 3] for b in boxes) + buffer_m for i in range(3)]

    inside = set(plan.element_gids)
    out = []
    for gid, el in elements_by_id.items():
        if gid in inside:
            continue
        b = getattr(el, "bbox", None)
        if not b or len(b) < 6:
            continue
        if all(b[i] <= hi[i] and b[i + 3] >= lo[i] for i in range(3)):
            out.append(gid)
    return sorted(out)


def neighbours_of(
    plan: ZonePlan, plans: list[ZonePlan], buffer_ids: list[str]
) -> dict[str, list[str]]:
    """Which other zones own the elements in this zone's buffer."""
    owner: dict[str, str] = {}
    for other in plans:
        if other.zone_key == plan.zone_key:
            continue
        for gid in other.element_gids:
            owner[gid] = other.zone_key

    out: dict[str, list[str]] = defaultdict(list)
    for gid in buffer_ids:
        zone = owner.get(gid)
        if zone is not None:
            out[zone].append(gid)
    return {k: sorted(v) for k, v in sorted(out.items())}
