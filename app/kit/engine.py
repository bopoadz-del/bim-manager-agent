"""Adapter between the service and the vendored kit.

Nothing in here decides whether a pair clashes. That judgement belongs to
``app.blocks.geometry_engine`` and is not reimplemented, wrapped in a heuristic,
or second-guessed. What this module owns is everything around the judgement:
loading a model once, caching the meshes by model hash, choosing which pairs are
worth handing to the engine, and translating an element so a proposed move can
be re-judged by the same engine that condemned it.

The pre-filter deserves a note. Bounding boxes are padded by the largest
clearance any loaded rule can demand before they are tested for overlap. An
unpadded box test would discard exactly the near-miss pairs a clearance check
exists to find, and it would do so silently -- the pairs would simply never
appear, and their absence would read as a clean model.
"""
from __future__ import annotations

import itertools
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.blocks import (  # type: ignore[attr-defined]
    connectivity,
    geometry_engine,
    ifc_loader,
)  # app.blocks.__path__ is set at runtime; see app/blocks/__init__.py
from app.blocks.clearance_rules import Rule

MESH_CACHE_VERSION = 2


@dataclass
class LoadedModel:
    """One IFC, meshed and indexed. Reused across every zone of a run."""

    path: Path
    sha256: str
    elements: list[Any] = field(default_factory=list)
    graph: Any = None
    skipped_gids: list[str] = field(default_factory=list)
    alias_report: dict = field(default_factory=dict)

    @property
    def by_id(self) -> dict[str, Any]:
        return {e.global_id: e for e in self.elements}

    def element(self, gid: str) -> Any | None:
        return self.by_id.get(gid)


def _cache_path(cache_dir: Path, sha: str) -> Path:
    return cache_dir / f"{sha}.v{MESH_CACHE_VERSION}.meshes"


def load_model(
    ifc_path: str | Path,
    cache_dir: str | Path | None = None,
    include_structural: bool = True,
    limit: int | None = None,
    aliases_path: str | Path | None = None,
) -> LoadedModel:
    """Mesh a model, or restore the meshes from cache keyed on its content hash.

    The cache key is the file's sha256, not its name or mtime: two uploads of
    the same bytes are the same model whatever they were called, and a file
    edited in place is a different model even if the name did not change.

    System aliases are applied here, on every path including a cache hit, and
    the cache stores the pre-alias systems. That ordering is deliberate: it
    guarantees two loads of the same model with the same alias table produce
    identical systems, so the resolver can never judge against a vocabulary the
    ingest did not use.
    """
    path = Path(ifc_path)
    sha = ifc_loader.model_sha256(path)

    cached = None
    if cache_dir is not None:
        cached = _cache_path(Path(cache_dir), sha)
        if cached.exists():
            try:
                with cached.open("rb") as fh:
                    payload = pickle.load(fh)
                restored = LoadedModel(
                    path=path,
                    sha256=sha,
                    elements=payload["elements"],
                    graph=payload["graph"],
                    skipped_gids=payload["skipped"],
                )
                _alias(restored, aliases_path)
                return restored
            except Exception:
                # A cache that cannot be read is a cache miss, never a failure:
                # the model is right there and can simply be meshed again.
                cached.unlink(missing_ok=True)

    elements = list(
        ifc_loader.load_elements(path, include_structural=include_structural, limit=limit)
    )

    import ifcopenshell

    raw = ifcopenshell.open(str(path))
    graph = connectivity.build_graph(raw)

    loaded = LoadedModel(path=path, sha256=sha, elements=elements, graph=graph)

    if cached is not None:
        cached.parent.mkdir(parents=True, exist_ok=True)
        with cached.open("wb") as fh:
            pickle.dump(
                {"elements": elements, "graph": graph, "skipped": loaded.skipped_gids},
                fh,
                protocol=pickle.HIGHEST_PROTOCOL,
            )

    _alias(loaded, aliases_path)
    return loaded


def _alias(model: LoadedModel, aliases_path: str | Path | None) -> None:
    from app.kit.systems import apply_system_aliases, load_system_aliases

    model.alias_report = apply_system_aliases(
        model.elements, load_system_aliases(aliases_path)
    )


def max_rule_gap_m(rules: list[Rule]) -> float:
    """The widest separation any rule can demand, in metres.

    This is the pre-filter pad. Using the widest rule rather than a per-pair
    lookup keeps the filter conservative: it can admit pairs that turn out not
    to need that much room, and admitting a pair costs one exact test, whereas
    excluding one loses a finding permanently.
    """
    if not rules:
        return 0.0
    return max((r.min_gap_mm for r in rules), default=0.0) / 1000.0


def candidate_pairs(
    elements: list[Any], pad_m: float, require_mep: bool = True
) -> list[tuple[Any, Any]]:
    """Pairs whose padded boxes overlap. Everything else cannot reach.

    At least one side must be MEP. Structure against structure is a structural
    coordination question, not an MEP one, and on a real model it is most of the
    pairs -- Schependomlaan is 1,364 structural elements to 73 MEP. Reporting a
    wall touching a slab to an MEP coordinator buries the nine clashes that are
    actually theirs.
    """
    pairs = []
    for a, b in itertools.combinations(elements, 2):
        if require_mep and a.discipline != "mep" and b.discipline != "mep":
            continue
        if not a.bbox or not b.bbox:
            # No box means no basis for exclusion. Hand it to the engine, which
            # will return UNJUDGED rather than a false clean.
            pairs.append((a, b))
            continue
        if geometry_engine.aabb_overlaps(a.bbox, b.bbox, pad=pad_m):
            pairs.append((a, b))
    return pairs


def _rule_for(rules: list[Rule], a: Any, b: Any):
    from app.blocks.clearance_rules import find_applicable_rule

    return find_applicable_rule(rules, getattr(a, "system", None), getattr(b, "system", None), "any")


def judge_pairs(pairs: list[tuple[Any, Any]], rules: list[Rule]) -> list[Any]:
    """Run the engine over every candidate pair, carrying the governing rule."""
    findings = []
    for a, b in pairs:
        rule = _rule_for(rules, a, b)
        findings.append(
            geometry_engine.judge_pair(
                a.global_id,
                b.global_id,
                a.mesh,
                b.mesh,
                required_clearance_m=(rule.min_gap_mm / 1000.0) if rule else None,
                rule_id=rule.rule_id if rule else None,
                category_a=getattr(a, "system", None),
                category_b=getattr(b, "system", None),
            )
        )
    return findings


def full_pass(model: LoadedModel, rules: list[Rule]) -> dict[str, Any]:
    """The whole-model clash and distance pass.

    Returns the real findings, the joints excluded by connectivity, and the
    counts that let a caller state what was tested rather than assert it.
    """
    # Size the pad from the rules that can actually fire on this model.
    from app.kit.systems import applicable_rules

    pad = max_rule_gap_m(applicable_rules(rules, model.elements))
    pairs = candidate_pairs(model.elements, pad)
    findings = judge_pairs(pairs, rules)

    real, joints = connectivity.classify_findings(
        findings,
        model.by_id,
        graph=model.graph,
        exact_backend=geometry_engine.exact_backend_available(),
    )
    return {
        "findings": real,
        "joints": joints,
        "pairs_admitted": len(pairs),
        "pairs_possible": len(model.elements) * (len(model.elements) - 1) // 2,
        "pad_m": pad,
        "exact_backend": geometry_engine.exact_backend_available(),
        "graph": model.graph.as_dict() if model.graph is not None else None,
    }


def translated(element: Any, vector_mm: tuple[float, float, float]) -> Any:
    """A copy of ``element`` moved by ``vector_mm``, for re-judging a proposal.

    A copy, not a mutation: the loaded model is shared by every zone in the run,
    and a monitor that moved the original would leave every later judgement
    measuring a building that does not exist.
    """
    import copy

    moved = copy.copy(element)
    dx, dy, dz = (v / 1000.0 for v in vector_mm)
    if element.mesh is not None:
        mesh = element.mesh.copy()
        mesh.apply_translation([dx, dy, dz])
        moved.mesh = mesh
        moved.bbox = tuple(mesh.bounds.flatten())
    elif element.bbox:
        b = list(element.bbox)
        moved.bbox = tuple(
            [b[0] + dx, b[1] + dy, b[2] + dz, b[3] + dx, b[4] + dy, b[5] + dz]
        )
    return moved
