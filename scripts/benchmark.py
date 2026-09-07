"""Run the full pipeline on one model and file a benchmark row.

The row is per zone, in the format The Level can grade: hard, clearance, joints
excluded, resolve rate, escalated. It also carries what the run could *not*
check, because a benchmark that reports only what went well is a sales sheet.

Usage:
    python scripts/benchmark.py <model.ifc> [--aliases <table.json>] [--label NAME]
"""
from __future__ import annotations

import argparse
import json
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=RuntimeWarning)

ROOT = Path(__file__).resolve().parent.parent


def run(ifc_path: Path, aliases: Path | None, label: str, out_dir: Path) -> dict:
    from app.blocks.ifc_loader import model_sha256

    from app.config import get_settings
    from app.db import get_engine, get_session_factory, reset_engine
    from app.models import Base, Clash, ModelVersion, Project, Proposal, Zone
    from app.pipeline import run_pipeline
    from app.store.local import LocalModelStore

    reset_engine()
    Base.metadata.create_all(get_engine())
    db = get_session_factory()()

    settings = get_settings()
    store = LocalModelStore(Path(settings.artifacts_dir) / "streams")

    project = Project(name=f"benchmark {label}")
    db.add(project)
    db.commit()

    sha_before = model_sha256(ifc_path)
    mv = ModelVersion(
        project_id=project.id,
        ifc_sha256=sha_before,
        ifc_path=str(ifc_path),
        aliases_path=str(aliases) if aliases else None,
        status="ingesting",
    )
    db.add(mv)
    db.commit()

    started = time.time()
    ingest = run_pipeline(db, mv, store=store, top_n=1000, settings=settings)
    db.commit()
    elapsed = time.time() - started

    zones = db.query(Zone).filter(Zone.model_version_id == mv.id).all()
    rows = []
    for zone in sorted(zones, key=lambda z: -z.priority):
        clashes = db.query(Clash).filter(Clash.zone_id == zone.id).all()
        proposals = (
            db.query(Proposal)
            .join(Clash, Clash.id == Proposal.clash_id)
            .filter(Clash.zone_id == zone.id)
            .all()
        )
        verified = [c for c in clashes if c.state in ("verified", "approved", "merged")]
        escalated = [c for c in clashes if c.state == "escalated"]
        rows.append(
            {
                "zone": zone.zone_key,
                "elements": zone.element_count,
                "congestion": round(zone.congestion, 4),
                "hard": sum(1 for c in clashes if c.kind == "hard"),
                "clearance": sum(1 for c in clashes if c.kind == "clearance"),
                "proposals": len(proposals),
                "resolved": len(verified),
                "resolve_rate": round(len(verified) / len(clashes), 4) if clashes else None,
                "escalated": len(escalated),
                "status": zone.status,
            }
        )

    unprovable: dict[str, int] = {}
    for proposal in db.query(Proposal).all():
        for field in ("monitor_geometry", "monitor_boundary", "monitor_integrity"):
            payload = getattr(proposal, field)
            for name in (payload or {}).get("unprovable", []):
                unprovable[name] = unprovable.get(name, 0) + 1

    total_clashes = db.query(Clash).filter(Clash.model_version_id == mv.id).count()
    total_resolved = sum(r["resolved"] for r in rows)

    report = {
        "label": label,
        "model": ifc_path.name,
        "ifc_sha256": sha_before,
        "sha256_unchanged_after_run": model_sha256(ifc_path) == sha_before,
        "seconds": round(elapsed, 1),
        "elements": ingest.element_count,
        "pairs_possible": ingest.stats["pairs_possible"],
        "pairs_admitted": ingest.pairs_admitted,
        "pad_m": ingest.stats["pad_m"],
        "joints_excluded": ingest.joints_excluded,
        "zones": len(zones),
        "clashes": total_clashes,
        "resolved": total_resolved,
        "resolve_rate": round(total_resolved / total_clashes, 4) if total_clashes else None,
        "boundary_owned": ingest.boundary_owned,
        "connectivity": ingest.stats["graph"],
        "system_aliases": ingest.stats["system_aliases"],
        "rules_loaded": ingest.stats["rules_loaded"],
        "rules_never_applied": [r["rule_id"] for r in ingest.stats["rules_never_applied"]],
        "checks_not_provable_from_this_model": unprovable,
        "zone_rows": rows,
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"benchmark_{label}.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    db.close()
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("model", type=Path)
    ap.add_argument("--aliases", type=Path, default=None)
    ap.add_argument("--label", default=None)
    ap.add_argument("--out", type=Path, default=ROOT / "artifacts" / "mep-judge" / "benchmarks")
    args = ap.parse_args()

    if not args.model.exists():
        print(f"{args.model} not found; run scripts/fetch_fixtures.sh")
        return 2

    label = args.label or args.model.stem
    report = run(args.model, args.aliases, label, args.out)

    print(f"\n=== {report['label']} — {report['model']} ===")
    print(
        f"{report['elements']} elements · {report['pairs_admitted']} of "
        f"{report['pairs_possible']} pairs admitted (pad {report['pad_m']} m) · "
        f"{report['seconds']} s"
    )
    print(
        f"{report['clashes']} clashes · {report['joints_excluded']} joints excluded · "
        f"{report['zones']} zones · resolve rate {report['resolve_rate']}"
    )
    print(f"original sha256 unchanged: {report['sha256_unchanged_after_run']}")
    if report["rules_never_applied"]:
        print(f"RULES NEVER APPLIED: {', '.join(report['rules_never_applied'])}")
    if report["checks_not_provable_from_this_model"]:
        print(f"not provable here: {report['checks_not_provable_from_this_model']}")

    print("\n| zone | elements | hard | clearance | proposals | resolved | rate | escalated |")
    print("|---|---|---|---|---|---|---|---|")
    for r in report["zone_rows"]:
        print(
            f"| {r['zone']} | {r['elements']} | {r['hard']} | {r['clearance']} | "
            f"{r['proposals']} | {r['resolved']} | {r['resolve_rate']} | {r['escalated']} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
