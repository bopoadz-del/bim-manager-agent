"""The HTTP surface."""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api import events
from app.api.auth import Principal, authenticate, issue_key
from app.api.errors import BadRequest, Conflict, NotFound, UnprocessableModel
from app.api.schemas import (
    ClashOut,
    DiffOut,
    HealthOut,
    IngestOut,
    KeyIssued,
    ModelVersionOut,
    ProjectIn,
    ProjectOut,
    ProposalOut,
    ReviewIn,
    ReviewOut,
    ZoneOut,
)
from app.config import get_settings
from app.db import get_db
from app.models import (
    CLASH_STATES,
    ZONE_STATES,
    ApiKey,
    Clash,
    ModelVersion,
    Project,
    Proposal,
    ReviewDecision,
    Zone,
)
from app.pipeline import run_pipeline

log = logging.getLogger(__name__)
router = APIRouter()


# -- helpers --------------------------------------------------------------
def _get_project(db: Session, project_id: str) -> Project:
    project = db.get(Project, project_id)
    if project is None:
        raise NotFound(f"project {project_id} does not exist")
    return project


def _get_model_version(db: Session, version_id: str, principal: Principal) -> ModelVersion:
    mv = db.get(ModelVersion, version_id)
    if mv is None:
        raise NotFound(f"model version {version_id} does not exist")
    principal.require_project(mv.project_id)
    return mv


def _get_zone(db: Session, zone_id: str, principal: Principal) -> Zone:
    zone = db.get(Zone, zone_id)
    if zone is None:
        raise NotFound(f"zone {zone_id} does not exist")
    mv = db.get(ModelVersion, zone.model_version_id)
    if mv is not None:
        principal.require_project(mv.project_id)
    return zone


# -- projects and keys ----------------------------------------------------
@router.post("/projects", response_model=ProjectOut, status_code=201, tags=["projects"])
def create_project(
    body: ProjectIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
) -> ProjectOut:
    principal.require_operator()
    project = Project(name=body.name)
    db.add(project)
    db.commit()
    return ProjectOut(id=project.id, name=project.name, created_at=project.created_at)


@router.post("/projects/{project_id}/keys", response_model=KeyIssued, status_code=201, tags=["projects"])
def create_key(
    project_id: str,
    label: str = Form(default=""),
    role: str = Form(default="reviewer"),
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
) -> KeyIssued:
    principal.require_operator()
    project = _get_project(db, project_id)
    plaintext = issue_key(db, project, label=label, role=role)
    db.commit()
    return KeyIssued(key=plaintext, role=role, label=label)


# -- models ---------------------------------------------------------------
@router.post(
    "/projects/{project_id}/models", response_model=IngestOut, status_code=201, tags=["models"]
)
def upload_model(
    project_id: str,
    ifc: UploadFile = File(...),
    programme: UploadFile | None = File(default=None),
    parent_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
) -> IngestOut:
    principal.require_operator()
    principal.require_project(project_id)
    project = _get_project(db, project_id)

    name = (ifc.filename or "").lower()
    if name.endswith((".rvt", ".nwd", ".nwc")):
        raise UnprocessableModel(
            "this service reads IFC. Export from the authoring tool first "
            "(Navisworks: File > Export > IFC; Revit: File > Export > IFC).",
            {"filename": ifc.filename},
        )
    if not name.endswith(".ifc"):
        raise UnprocessableModel("expected a .ifc file", {"filename": ifc.filename})

    settings = get_settings()
    upload_dir = Path(settings.upload_dir) / project_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    target = upload_dir / (ifc.filename or "model.ifc")
    with target.open("wb") as fh:
        shutil.copyfileobj(ifc.file, fh)

    programme_path = None
    if programme is not None and programme.filename:
        programme_path = upload_dir / programme.filename
        with programme_path.open("wb") as fh:
            shutil.copyfileobj(programme.file, fh)

    from app.blocks.ifc_loader import model_sha256

    try:
        sha = model_sha256(target)
    except OSError as exc:
        raise UnprocessableModel(f"uploaded file could not be read: {exc}") from exc

    mv = ModelVersion(
        project_id=project.id,
        ifc_sha256=sha,
        ifc_path=str(target),
        parent_id=parent_id,
        status="ingesting",
    )
    db.add(mv)
    db.commit()

    result = run_pipeline(db, mv, programme_csv=programme_path)
    db.commit()
    db.refresh(mv)

    return IngestOut(
        model_version=_mv_out(mv),
        zones_created=result.zones_created,
        clashes_created=result.clashes_created,
        joints_excluded=result.joints_excluded,
        pairs_admitted=result.pairs_admitted,
        boundary_owned=result.boundary_owned,
        proposals_verified=result.proposals_verified,
        proposals_conditional=result.proposals_conditional,
        provable_check_ratio=result.provable_check_ratio,
    )


def _mv_out(mv: ModelVersion) -> ModelVersionOut:
    return ModelVersionOut(
        id=mv.id,
        project_id=mv.project_id,
        ifc_sha256=mv.ifc_sha256,
        element_count=mv.element_count,
        status=mv.status,
        speckle_stream=mv.speckle_stream,
        ingested_at=mv.ingested_at,
        stats=mv.stats or {},
    )


@router.get("/models/{version_id}", response_model=ModelVersionOut, tags=["models"])
def get_model(
    version_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
) -> ModelVersionOut:
    return _mv_out(_get_model_version(db, version_id, principal))


@router.get("/models/{version_id}/zones", response_model=list[ZoneOut], tags=["zones"])
def list_zones(
    version_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
) -> list[ZoneOut]:
    _get_model_version(db, version_id, principal)
    zones = db.execute(
        select(Zone).where(Zone.model_version_id == version_id).order_by(Zone.priority.desc())
    ).scalars()
    return [
        ZoneOut(
            id=z.id,
            zone_key=z.zone_key,
            level=z.level,
            grid_cell=z.grid_cell,
            buffer_m=z.buffer_m,
            congestion=z.congestion,
            programme_rank=z.programme_rank,
            priority=z.priority,
            status=z.status,
            element_count=z.element_count,
            dedicated_reason=z.dedicated_reason,
            branch=z.branch,
        )
        for z in zones
    ]


@router.get("/zones/{zone_id}/clashes", response_model=list[ClashOut], tags=["zones"])
def list_clashes(
    zone_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
) -> list[ClashOut]:
    zone = _get_zone(db, zone_id, principal)
    clashes = db.execute(
        select(Clash).where(Clash.zone_id == zone.id).order_by(Clash.resolution_rank, Clash.clash_key)
    ).scalars()
    return [
        ClashOut(
            id=c.id,
            clash_key=c.clash_key,
            a_gid=c.a_gid,
            b_gid=c.b_gid,
            kind=c.kind,
            depth_or_gap_mm=c.depth_or_gap_mm,
            systems=list(c.systems or []),
            state=c.state,
            attempts=c.attempts,
            owner=c.owner,
            rule_id=c.rule_id,
            required_gap_mm=c.required_gap_mm,
            note=c.note,
        )
        for c in clashes
    ]


@router.get("/zones/{zone_id}/proposals", response_model=list[ProposalOut], tags=["zones"])
def list_proposals(
    zone_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
) -> list[ProposalOut]:
    zone = _get_zone(db, zone_id, principal)
    rows = db.execute(
        select(Proposal)
        .join(Clash, Clash.id == Proposal.clash_id)
        .where(Clash.zone_id == zone.id)
        .order_by(Proposal.created_at)
    ).scalars()
    return [
        ProposalOut(
            id=p.id,
            clash_id=p.clash_id,
            element_gid=p.element_gid,
            move_type=p.move_type,
            move_vector=[float(v) for v in (p.move_vector or [])],
            rule_ids=list(p.rule_ids or []),
            clause_text=p.clause_text,
            verdict=p.verdict,
            attempt=p.attempt,
            monitor_geometry=p.monitor_geometry,
            monitor_boundary=p.monitor_boundary,
            monitor_integrity=p.monitor_integrity,
            unprovable_checks=list(p.unprovable_checks or []),
            created_at=p.created_at,
        )
        for p in rows
    ]


# -- review ---------------------------------------------------------------
@router.post("/zones/{zone_id}/review", response_model=ReviewOut, tags=["review"])
def review_zone(
    zone_id: str,
    body: ReviewIn,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
) -> ReviewOut:
    from app.review import apply_review

    zone = _get_zone(db, zone_id, principal)
    if zone.status == "merged" and body.decision != "reject":
        raise Conflict(
            "zone is already merged; reopen it with a new model version rather than re-approving",
            {"zone_status": zone.status},
        )
    result = apply_review(db, zone, body, actor=principal.name())
    db.commit()
    return result


@router.get("/zones/{zone_id}/change_set.json", tags=["review"])
def get_change_set(
    zone_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
):
    zone = _get_zone(db, zone_id, principal)
    path = Path(get_settings().artifacts_dir) / "review" / zone.id / "change_set.json"
    if not path.exists():
        raise NotFound(
            "no change set for this zone yet; it is produced when the zone reaches review",
            {"zone_status": zone.status},
        )
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/zones/{zone_id}/bcf.zip", tags=["review"])
def get_bcf(
    zone_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
) -> FileResponse:
    zone = _get_zone(db, zone_id, principal)
    path = Path(get_settings().artifacts_dir) / "review" / zone.id / "issues.bcfzip"
    if not path.exists():
        raise NotFound("no BCF for this zone yet", {"zone_status": zone.status})
    return FileResponse(
        path, media_type="application/octet-stream", filename=f"{zone.zone_key}.bcfzip"
    )


@router.get("/models/{version_id}/diff/{previous_id}", response_model=DiffOut, tags=["models"])
def diff_models(
    version_id: str,
    previous_id: str,
    db: Session = Depends(get_db),
    principal: Principal = Depends(authenticate),
) -> DiffOut:
    from app.agents.coordinator import Coordinator

    current = _get_model_version(db, version_id, principal)
    previous = _get_model_version(db, previous_id, principal)
    if current.id == previous.id:
        raise BadRequest("a model version cannot be diffed against itself")

    result = Coordinator(db).diff_against(current, previous)
    db.commit()
    return DiffOut(**result.as_dict())


# -- events ---------------------------------------------------------------
@router.get("/events/{topic}", tags=["events"])
async def event_stream(topic: str, principal: Principal = Depends(authenticate)):
    return StreamingResponse(
        events.stream(topic),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# -- health ---------------------------------------------------------------
@router.get("/health", response_model=HealthOut, tags=["ops"])
def health(db: Session = Depends(get_db)) -> HealthOut:
    from sqlalchemy import text

    from app.blocks.geometry_engine import exact_backend_available
    from app.store import store_status

    settings = get_settings()
    try:
        db.execute(text("select 1"))
        database = "ok"
    except Exception as exc:  # a service that says ok with no database is lying
        database = f"unavailable: {exc}"

    lock_path = Path(__file__).resolve().parent.parent.parent / "VENDOR.lock"
    vendored: dict[str, Any] = {"pinned": False}
    if lock_path.exists():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        vendored = {"pinned": True, "sha": lock["sha"], "files": len(lock["files"])}

    # The most recent run's ratio, so an operator can see at a glance how much of
    # what this service last reported as accepted was actually checkable.
    ratio = None
    try:
        latest = db.execute(
            select(ModelVersion).order_by(ModelVersion.ingested_at.desc()).limit(1)
        ).scalars().first()
        if latest is not None:
            ratio = (latest.stats or {}).get("verdicts", {}).get("provable_check_ratio")
    except Exception:  # a health endpoint must not fail on a reporting extra
        ratio = None

    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        build_sha=settings.build_sha,
        provable_check_ratio=ratio,
        database=database,
        store=store_status(settings),
        exact_geometry_backend=exact_backend_available(),
        vendored_kit=vendored,
        verification={
            "verdicts": ["verified", "verified_conditional"],
            "note": (
                "verified means every check of all three monitors passed. "
                "verified_conditional means no monitor objected and at least one "
                "check could not be answered by the model; approving one requires "
                "acknowledging each unanswered check by name."
            ),
        },
    )


__all__ = ["router", "ApiKey", "CLASH_STATES", "ReviewDecision", "ZONE_STATES"]
