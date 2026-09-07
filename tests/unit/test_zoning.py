"""Zoning: the cut has to be deterministic, bounded, and honest about buffers."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agents.zoning import (
    DEDICATED_RISER,
    ZonePlan,
    buffer_gids,
    neighbours_of,
    plan_zones,
)


@dataclass
class FakeElement:
    """Only the attributes zoning reads. Geometry is not involved in zoning."""

    global_id: str
    name: str = ""
    system: str = "ventilation"
    discipline: str = "mep"
    level: str = "L0"
    bbox: tuple = ()
    mesh: Any = None

    @property
    def is_gravity(self) -> bool:
        return self.system.startswith("drainage")


def _grid(n_cells: int, per_cell: int, level: str = "L0") -> list[FakeElement]:
    out = []
    for c in range(n_cells):
        x = c * 6.0
        for i in range(per_cell):
            out.append(
                FakeElement(
                    global_id=f"{level}-c{c}-e{i}",
                    name=f"duct {c}-{i}",
                    level=level,
                    bbox=(x, 0.0, 0.0, x + 1.0, 1.0, 1.0),
                )
            )
    return out


def test_zoning_is_deterministic(caplog):
    elements = _grid(6, 10)
    a = [(p.zone_key, p.element_gids) for p in plan_zones(elements, max_elements=800)]
    b = [(p.zone_key, p.element_gids) for p in plan_zones(list(reversed(elements)), max_elements=800)]
    assert a == b, "the same model must always produce the same zones, whatever the input order"


def test_cells_merge_up_to_the_ceiling_and_no_further():
    elements = _grid(8, 10)  # 80 elements over 8 cells
    plans = plan_zones(elements, max_elements=25)
    assert plans, "zoning produced nothing"
    for plan in plans:
        assert plan.element_count <= 25, f"{plan.zone_key} has {plan.element_count} elements"
    assert sum(p.element_count for p in plans) == len(elements), "zoning lost elements"


def test_every_element_lands_in_exactly_one_zone():
    elements = _grid(5, 12) + _grid(5, 12, level="L1")
    plans = plan_zones(elements, max_elements=20)
    seen: dict[str, int] = {}
    for plan in plans:
        for gid in plan.element_gids:
            seen[gid] = seen.get(gid, 0) + 1
    assert set(seen) == {e.global_id for e in elements}
    duplicated = [g for g, n in seen.items() if n > 1]
    assert not duplicated, f"elements in more than one zone: {duplicated[:5]}"


def test_zones_never_span_two_levels():
    plans = plan_zones(_grid(3, 5) + _grid(3, 5, level="L1"), max_elements=800)
    ordinary = [p for p in plans if p.dedicated_reason is None]
    assert ordinary, "expected ordinary zones"
    for plan in ordinary:
        assert "|" in plan.zone_key
        assert plan.level in ("L0", "L1")


def test_a_riser_becomes_its_own_zone_across_levels():
    """A riser is the one group whose coordination question is not per-storey."""
    elements = _grid(2, 4)
    elements.append(
        FakeElement(
            global_id="riser-1",
            name="hwa stack riser R-01",
            system="drainage_storm",
            level="L0",
            bbox=(0.0, 0.0, 0.0, 0.3, 0.3, 12.0),
        )
    )
    plans = plan_zones(elements, max_elements=800)
    dedicated = [p for p in plans if p.dedicated_reason == DEDICATED_RISER]
    assert len(dedicated) == 1
    assert dedicated[0].element_gids == ["riser-1"]
    assert dedicated[0].level == "(all)"


def test_a_tall_thin_element_is_a_riser_even_without_the_word():
    tall = FakeElement(global_id="t1", name="PIPE-9981", bbox=(0.0, 0.0, 0.0, 0.2, 0.2, 9.0))
    plans = plan_zones([tall], max_elements=800)
    assert plans[0].dedicated_reason == DEDICATED_RISER


def test_buffer_holds_what_is_near_and_excludes_what_is_far():
    inside = FakeElement(global_id="in", bbox=(0.0, 0.0, 0.0, 1.0, 1.0, 1.0))
    near = FakeElement(global_id="near", bbox=(2.0, 0.0, 0.0, 2.5, 1.0, 1.0))
    far = FakeElement(global_id="far", bbox=(50.0, 0.0, 0.0, 51.0, 1.0, 1.0))
    by_id = {e.global_id: e for e in (inside, near, far)}
    plan = ZonePlan(zone_key="L0|0_0", level="L0", grid_cell="0_0", element_gids=["in"])

    assert buffer_gids(plan, by_id, buffer_m=2.0) == ["near"]
    assert buffer_gids(plan, by_id, buffer_m=0.5) == []


def test_neighbours_name_the_zone_that_owns_each_buffered_element():
    a = ZonePlan(zone_key="L0|0_0", level="L0", grid_cell="0_0", element_gids=["in"])
    b = ZonePlan(zone_key="L0|1_0", level="L0", grid_cell="1_0", element_gids=["near", "other"])
    assert neighbours_of(a, [a, b], ["near"]) == {"L0|1_0": ["near"]}
    assert neighbours_of(a, [a, b], ["unowned"]) == {}
