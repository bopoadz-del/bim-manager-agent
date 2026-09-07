"""The whole run, in the order the coordinator defines it.

This is one function on purpose. The pipeline is the part of the system where
order is load-bearing -- zones must exist before clashes can be assigned to
them, boundary clashes must be arbitrated after the zones that own them have
proposed something, and review packages must be built after arbitration or they
will quote verdicts that arbitration went on to overturn. Spreading that order
across HTTP handlers and worker callbacks would make it a property of the
deployment rather than of the code.

The arq worker calls this same function. There is no second implementation of
the sequence for the background path, because two implementations of an ordering
constraint is one implementation and one bug waiting.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.coordinator import Coordinator, load_project_rules
from app.agents.results import IngestResult
from app.api.events import BUS
from app.config import Settings, get_settings
from app.kit.engine import load_model
from app.models import Clash, ModelVersion, Zone

log = logging.getLogger(__name__)


def _emit(topic: str, event: str, **data: Any) -> None:
    BUS.publish(topic, event, data)


def run_pipeline(
    db: Session,
    model_version: ModelVersion,
    programme_csv: str | Path | None = None,
    rules_path: str | Path | None = None,
    aliases_path: str | Path | None = None,
    settings: Settings | None = None,
    store: Any = None,
    monitors: list[Any] | None = None,
    top_n: int | None = None,
) -> IngestResult:
    settings = settings or get_settings()
    coordinator = Coordinator(db, settings=settings, store=store, monitors=monitors)
    topic = model_version.id

    _emit(topic, "ingest.started", model_version_id=model_version.id, sha256=model_version.ifc_sha256)
    ingest = coordinator.ingest(
        model_version,
        programme_csv=programme_csv,
        rules_path=rules_path,
        aliases_path=aliases_path,
    )
    db.flush()
    _emit(
        topic,
        "ingest.complete",
        zones=ingest.zones_created,
        clashes=ingest.clashes_created,
        joints_excluded=ingest.joints_excluded,
        boundary_owned=ingest.boundary_owned,
    )

    rules = load_project_rules(rules_path)
    model = load_model(
        model_version.ifc_path,
        cache_dir=coordinator._cache_dir(),
        aliases_path=model_version.aliases_path,
    )

    limit = top_n if top_n is not None else settings.worker_concurrency
    zones = coordinator.zones_by_priority(model_version.id, limit=limit)
    _emit(topic, "dispatch", zones=[z.zone_key for z in zones], concurrency=limit)

    for zone in zones:
        _emit(topic, "zone.started", zone_id=zone.id, zone_key=zone.zone_key, priority=zone.priority)
        result = coordinator.resolve_zone(zone, model, rules)
        db.flush()
        _emit(
            topic,
            "zone.resolved",
            zone_id=zone.id,
            zone_key=zone.zone_key,
            verified=result.verified,
            escalated=result.escalated,
            handed_to_coordinator=result.handed_to_coordinator,
            resolve_rate=result.resolve_rate,
        )

    # Boundary clashes that a zone proposed but was not allowed to commit.
    pending = list(
        db.execute(
            select(Clash).where(
                Clash.model_version_id == model_version.id,
                Clash.owner == "coordinator",
                Clash.state == "proposed",
            )
        ).scalars()
    )
    for clash in pending:
        verdict = coordinator.arbitrate(clash, model, rules)
        db.flush()
        _emit(
            topic,
            "arbitrated",
            clash_key=clash.clash_key,
            committed=verdict.committed,
            reason=verdict.reason,
            zones=verdict.zones_checked,
            rebased=verdict.rebase_enqueued,
        )

    for zone in db.execute(
        select(Zone).where(
            Zone.model_version_id == model_version.id, Zone.status == "awaiting_review"
        )
    ).scalars():
        package = coordinator.assemble_review_package(zone, model)
        db.flush()
        _emit(topic, "review.ready", zone_id=zone.id, zone_key=zone.zone_key, **package)

    model_version.status = "reviewed"
    _emit(topic, "run.complete", model_version_id=model_version.id)
    return ingest
