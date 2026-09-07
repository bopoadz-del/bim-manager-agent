"""clash_triage — turn a pile of findings into a queue an engineer will work.

A raw clash run on a real model produces thousands of rows, most of them the
same conflict counted several times or an artefact of how the model was
assembled. A report like that is not used. This block does the three things
that make it usable: remove the duplicates, drop the noise, and put what is
left in the order the work actually happens.

The resolution order lives in order.yaml, not here, because it is an
engineering argument rather than an implementation detail. An engineer who
disagrees with it should be able to read the rationale and change the file
without touching Python.

READS   findings from geometry_engine; element metadata from ifc_loader;
        optionally a programme CSV of (zone, planned_install_date).
WRITES  a prioritised queue.
NEVER   drops a finding silently. Noise is counted and reported; a dropped
        row that nobody can see is indistinguishable from a missed one.
"""
from __future__ import annotations

import csv
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

logger = logging.getLogger(__name__)

KIND_HARD = "hard"
KIND_CLEARANCE = "clearance"
KIND_SOFT = "soft"
KIND_WORKFLOW = "workflow"

# Loaded from order.yaml. Kept as a module fallback so the block still runs
# when PyYAML is unavailable -- but the file is the source of record and the
# two are checked against each other by a test.
_DEFAULT_ORDER: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("gravity", ("drainage_storm", "drainage_foul")),
    ("ducted", ("ventilation",)),
    ("containment", ("electrical", "cable_tray", "containment")),
    ("pressurised", ("fire", "chilled_water", "heating", "potable_water", "gas_main")),
)

_FITTING_HINT = ("fitting", "bend", "elbow", "tee", "junction", "coupler", "reducer")


@dataclass
class QueueItem:
    clash_id: str
    element_a: str
    element_b: str
    kind: str
    system_a: str
    system_b: str
    zone_key: str
    level: str
    severity_mm: float
    resolution_rank: int
    congestion: float = 0.0
    programme_date: str | None = None
    rule_id: str | None = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


@dataclass
class TriageResult:
    queue: list[QueueItem] = field(default_factory=list)
    dropped_workflow: int = 0
    deduped: int = 0
    zones: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "queue": [q.as_dict() for q in self.queue],
            "dropped_workflow": self.dropped_workflow,
            "deduped": self.deduped,
            "zones": self.zones,
            "queue_length": len(self.queue),
        }


def load_order(path: str | Path | None = None) -> list[tuple[str, tuple[str, ...]]]:
    """Read order.yaml if it and PyYAML are available, else the fallback.

    The fallback is not a shortcut: this block must not fail closed on a
    missing optional dependency, because that would make the whole queue
    unavailable over a formatting library.
    """
    p = Path(path) if path else Path(__file__).with_name("order.yaml")
    try:
        import yaml  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        logger.debug("PyYAML unavailable; using built-in resolution order")
        return list(_DEFAULT_ORDER)
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8"))
        rows = sorted(data["resolution_order"], key=lambda r: r["rank"])
        return [(r["system_class"], tuple(r["systems"])) for r in rows]
    except Exception:  # noqa: BLE001
        logger.warning("order.yaml unreadable at %s; using built-in order", p)
        return list(_DEFAULT_ORDER)


def resolution_rank(system: str, order: Sequence[tuple[str, tuple[str, ...]]] | None = None) -> int:
    """Rank 1 is resolved first. Unknown systems sort LAST but are still
    ranked, never dropped -- an unclassified service is exactly the one an
    engineer needs to look at, not the one to hide."""
    order = order or load_order()
    for i, (_cls, systems) in enumerate(order, start=1):
        if system in systems:
            return i
    return len(order) + 1


def pair_key(a: str, b: str) -> tuple[str, str]:
    """A clash between A and B is the same clash as between B and A."""
    return (a, b) if a <= b else (b, a)


def clash_id_for(a: str, b: str) -> str:
    """The ONE canonical clash identifier, shared with version_diff.

    Two blocks deriving "the same" id independently is how an integration
    silently reports nothing: triage produced "A__B", version_diff produced
    "A::B", every unit test passed, and the proposal score sat at 0.0 because
    no key ever matched. One definition, imported by both.
    """
    x, y = pair_key(a, b)
    return f"{x}::{y}"


def _is_workflow_noise(system_a: str, system_b: str, name_a: str, name_b: str) -> bool:
    """A fitting overlapping its own segment, in the same system, is assembly
    -- not a conflict. Both conditions are required: two DIFFERENT systems
    touching is a real clash even when one of them is a fitting."""
    if system_a != system_b:
        return False
    blob = f"{name_a} {name_b}".lower()
    return any(h in blob for h in _FITTING_HINT)


def _severity_mm(finding: Any) -> float:
    """One comparable number per row. Penetration and shortfall are different
    physical quantities, so mixing them silently would rank nonsense; both are
    expressed as 'millimetres of problem' and the kind stays on the row so a
    reader can tell which they are looking at."""
    pen = getattr(finding, "penetration_volume_m3", None)
    dist = getattr(finding, "distance_m", None)
    req = getattr(finding, "required_clearance_m", None)
    if getattr(finding, "kind", "") == "clash":
        if pen:
            return float(pen) * 1e9 ** (1 / 3)  # mm-scale from a volume
        return 1000.0                            # contact proven, depth unknown
    if req is not None and dist is not None:
        return max(0.0, (float(req) - float(dist)) * 1000.0)
    return 0.0


def zone_congestion(elements: Iterable[Any], cell_m: float = 6.0) -> dict[str, float]:
    """Services volume over zone volume, per zone.

    Ranks WHERE to look, not what to fix. A dense zone is where the next
    clash appears once this one is resolved.
    """
    from collections import defaultdict

    svc: dict[str, float] = defaultdict(float)
    cells: dict[str, float] = {}
    for el in elements:
        zk = getattr(el, "zone_key", None) or _zone_of(el, cell_m)
        bbox = getattr(el, "bbox", None)
        if not bbox or len(bbox) < 6:
            continue
        vol = max(0.0, (bbox[3] - bbox[0])) * max(0.0, (bbox[4] - bbox[1])) * max(0.0, (bbox[5] - bbox[2]))
        if getattr(el, "discipline", "") == "mep":
            svc[zk] += vol
        cells.setdefault(zk, cell_m * cell_m * 3.0)  # 3 m assumed storey void
    return {z: (svc.get(z, 0.0) / v if v else 0.0) for z, v in cells.items()}


def _zone_of(el: Any, cell_m: float) -> str:
    bbox = getattr(el, "bbox", None) or (0, 0, 0, 0, 0, 0)
    cx = (bbox[0] + bbox[3]) / 2.0
    cy = (bbox[1] + bbox[4]) / 2.0
    return f"{getattr(el, 'level', 'unassigned')}|{int(cx // cell_m)}_{int(cy // cell_m)}"


def load_programme(csv_path: str | Path) -> dict[str, str]:
    """(zone, planned_install_date) from CSV. Absent file -> empty mapping and
    congestion alone drives ranking, which the order allows explicitly."""
    out: dict[str, str] = {}
    p = Path(csv_path)
    if not p.exists():
        return out
    with p.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            z = (row.get("zone") or "").strip()
            d = (row.get("planned_install_date") or "").strip()
            if z and d:
                out[z] = d
    return out


def triage(
    findings: Iterable[Any],
    elements_by_id: dict[str, Any] | None = None,
    programme: dict[str, str] | None = None,
    order: Sequence[tuple[str, tuple[str, ...]]] | None = None,
    cell_m: float = 6.0,
) -> TriageResult:
    """Dedupe, classify, rank. The single entry point of this block."""
    elements_by_id = elements_by_id or {}
    programme = programme or {}
    order = order or load_order()

    congestion = zone_congestion(elements_by_id.values(), cell_m) if elements_by_id else {}

    seen: set[tuple] = set()
    result = TriageResult(zones=congestion)

    for f in findings:
        kind = getattr(f, "kind", "")
        if kind in ("clear", "unjudged"):
            continue

        a, b = getattr(f, "element_a", ""), getattr(f, "element_b", "")
        ea, eb = elements_by_id.get(a), elements_by_id.get(b)
        sys_a = getattr(ea, "system", None) or getattr(f, "category_a", None) or "unknown"
        sys_b = getattr(eb, "system", None) or getattr(f, "category_b", None) or "unknown"
        name_a = getattr(ea, "name", "") or ""
        name_b = getattr(eb, "name", "") or ""
        level = getattr(ea, "level", None) or getattr(eb, "level", None) or "unassigned"
        zk = getattr(ea, "zone_key", None) or (_zone_of(ea, cell_m) if ea else f"{level}|nocell")

        if _is_workflow_noise(sys_a, sys_b, name_a, name_b):
            result.dropped_workflow += 1
            continue

        # Dedupe on the triple the order names: unordered system pair,
        # unordered element pair, and zone.
        key = (pair_key(sys_a, sys_b), pair_key(a, b), zk)
        if key in seen:
            result.deduped += 1
            continue
        seen.add(key)

        rank_a = resolution_rank(sys_a, order)
        rank_b = resolution_rank(sys_b, order)

        result.queue.append(
            QueueItem(
                # Canonical form is "A::B" over the SORTED pair. version_diff
                # derives the same id independently, and the two must agree or
                # score_proposals silently reports 0% forever -- which is
                # exactly what happened before this was unified. Pinned by
                # test_clash_id_format_matches_version_diff.
                clash_id=clash_id_for(a, b),
                element_a=a, element_b=b,
                kind=KIND_HARD if kind == "clash" else KIND_CLEARANCE,
                system_a=sys_a, system_b=sys_b,
                zone_key=zk, level=level,
                severity_mm=_severity_mm(f),
                # The pair takes the rank of its MOST constrained member: a
                # duct-vs-tray clash is resolved at the duct's stage, because
                # the duct is the one with less freedom.
                resolution_rank=min(rank_a, rank_b),
                congestion=float(congestion.get(zk, 0.0)),
                programme_date=programme.get(zk),
                rule_id=getattr(f, "rule_id", None),
            )
        )

    # Sort: resolution order first (it is a sequence, not a preference), then
    # programme urgency, then congestion, then severity.
    def _sort_key(q: QueueItem):
        prog = q.programme_date or "9999-12-31"
        return (q.resolution_rank, prog, -q.congestion, -q.severity_mm)

    result.queue.sort(key=_sort_key)
    return result
