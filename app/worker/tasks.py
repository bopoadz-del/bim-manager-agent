"""arq worker.

Geometry is minutes of CPU, so it does not belong in a request handler. The
worker exists to move that work off the web process; it does not own any of the
logic. Every task here is a thin wrapper around :func:`app.pipeline.run_pipeline`
or a coordinator method, so the background path and the inline path cannot drift.

Each task opens its own session and commits or rolls back as a unit. A worker
that shared a session with the enqueuer would be a worker holding a transaction
open across a network hop.
"""
from __future__ import annotations

import logging
from typing import Any

from arq.connections import RedisSettings

from app.config import get_settings
from app.db import session_scope
from app.models import Clash, ModelVersion, Zone

log = logging.getLogger(__name__)


async def ingest_and_resolve(
    ctx: dict, model_version_id: str, programme_csv: str | None = None
) -> dict[str, Any]:
    from app.pipeline import run_pipeline

    with session_scope() as db:
        mv = db.get(ModelVersion, model_version_id)
        if mv is None:
            return {"error": "model_version_not_found", "id": model_version_id}
        result = run_pipeline(db, mv, programme_csv=programme_csv)
        return result.as_dict()


async def resolve_zone(ctx: dict, zone_id: str) -> dict[str, Any]:
    from app.agents.coordinator import Coordinator, load_project_rules
    from app.kit.engine import load_model

    with session_scope() as db:
        zone = db.get(Zone, zone_id)
        if zone is None:
            return {"error": "zone_not_found", "id": zone_id}
        mv = db.get(ModelVersion, zone.model_version_id)
        coordinator = Coordinator(db)
        model = load_model(mv.ifc_path, cache_dir=coordinator._cache_dir(), aliases_path=mv.aliases_path)
        return coordinator.resolve_zone(zone, model, load_project_rules()).as_dict()


async def rebase_zone(ctx: dict, model_version_id: str, changed_zones: list[str], element_gid: str):
    from app.agents.coordinator import Coordinator, load_project_rules
    from app.kit.engine import load_model

    with session_scope() as db:
        mv = db.get(ModelVersion, model_version_id)
        if mv is None:
            return {"error": "model_version_not_found", "id": model_version_id}
        coordinator = Coordinator(db)
        model = load_model(mv.ifc_path, cache_dir=coordinator._cache_dir(), aliases_path=mv.aliases_path)
        rebased = coordinator.enqueue_rebase(
            model_version_id, changed_zones, element_gid, model=model, rules=load_project_rules()
        )
        return {"rebased": rebased}


async def arbitrate(ctx: dict, clash_id: str) -> dict[str, Any]:
    from app.agents.coordinator import Coordinator, load_project_rules
    from app.kit.engine import load_model

    with session_scope() as db:
        clash = db.get(Clash, clash_id)
        if clash is None:
            return {"error": "clash_not_found", "id": clash_id}
        mv = db.get(ModelVersion, clash.model_version_id)
        coordinator = Coordinator(db)
        model = load_model(mv.ifc_path, cache_dir=coordinator._cache_dir(), aliases_path=mv.aliases_path)
        return coordinator.arbitrate(clash, model, load_project_rules()).as_dict()


class WorkerSettings:
    functions = [ingest_and_resolve, resolve_zone, rebase_zone, arbitrate]
    max_jobs = 4
    job_timeout = 3600
    keep_result = 3600

    @staticmethod
    def redis_settings() -> RedisSettings:
        return RedisSettings.from_dsn(get_settings().redis_url)
