"""The HTTP surface: auth, typed errors, and never 200-on-failure."""
from __future__ import annotations

import json

import pytest

from app.api.auth import ROLE_OPERATOR, ROLE_REVIEWER, hash_key, issue_key
from app.models import Project
from tests.conftest import GENERATED


def test_every_endpoint_refuses_an_unknown_key(client):
    client.headers.update({"X-API-Key": "not-a-real-key"})
    response = client.post("/projects", json={"name": "x"})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_a_missing_key_is_401_not_500(client):
    response = client.get("/models/anything", headers={"X-API-Key": ""})
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


def test_a_reviewer_key_cannot_ingest(client, db):
    project = Project(name="p")
    db.add(project)
    db.flush()
    key = issue_key(db, project, "a reviewer", role=ROLE_REVIEWER)
    db.commit()

    client.headers.update({"X-API-Key": key})
    response = client.post(
        f"/projects/{project.id}/models",
        files={"ifc": ("m.ifc", b"nope", "application/octet-stream")},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


def test_a_key_is_scoped_to_its_own_project(client, db):
    mine = Project(name="mine")
    theirs = Project(name="theirs")
    db.add_all([mine, theirs])
    db.flush()
    key = issue_key(db, mine, "operator", role=ROLE_OPERATOR)
    db.commit()

    client.headers.update({"X-API-Key": key})
    response = client.post(
        f"/projects/{theirs.id}/models",
        files={"ifc": ("m.ifc", b"nope", "application/octet-stream")},
    )
    assert response.status_code == 403


def test_only_the_hash_of_a_key_is_stored(db):
    from app.models import ApiKey

    project = Project(name="p")
    db.add(project)
    db.flush()
    plaintext = issue_key(db, project, "k")
    db.commit()

    rows = db.query(ApiKey).all()
    assert len(rows) == 1
    assert rows[0].key_hash == hash_key(plaintext)
    assert plaintext not in rows[0].key_hash
    assert all(plaintext not in (r.label or "") for r in rows)


def test_a_missing_project_is_404_with_a_code(client):
    response = client.get("/models/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert "does-not-exist" in body["message"]


@pytest.mark.parametrize("filename", ["model.rvt", "model.nwd", "model.nwc"])
def test_an_authoring_format_is_refused_with_the_export_instruction(client, db, filename):
    project = Project(name="p")
    db.add(project)
    db.commit()

    response = client.post(
        f"/projects/{project.id}/models",
        files={"ifc": (filename, b"binary", "application/octet-stream")},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "unprocessable_model"
    assert "Export" in body["message"] or "export" in body["message"]
    assert "IFC" in body["message"]


def test_a_non_ifc_upload_is_refused_rather_than_parsed(client, db):
    project = Project(name="p")
    db.add(project)
    db.commit()
    response = client.post(
        f"/projects/{project.id}/models",
        files={"ifc": ("notes.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 422


def test_a_change_set_that_does_not_exist_yet_is_404_not_empty_json(client, db, project):
    from app.models import ModelVersion, Zone

    mv = ModelVersion(project_id=project.id, ifc_sha256="a" * 64, ifc_path="/x.ifc")
    db.add(mv)
    db.flush()
    zone = Zone(model_version_id=mv.id, zone_key="L0|0_0")
    db.add(zone)
    db.commit()

    response = client.get(f"/zones/{zone.id}/change_set.json")
    assert response.status_code == 404, (
        "an absent change set must not be served as an empty one; a client cannot "
        "tell 'nothing to do' from 'not ready' if both are 200"
    )


def test_diffing_a_version_against_itself_is_rejected(client, db, project):
    from app.models import ModelVersion

    mv = ModelVersion(project_id=project.id, ifc_sha256="a" * 64, ifc_path="/x.ifc")
    db.add(mv)
    db.commit()
    response = client.get(f"/models/{mv.id}/diff/{mv.id}")
    assert response.status_code == 400
    assert response.json()["code"] == "bad_request"


def test_openapi_documents_every_route_with_a_tag(client):
    schema = client.get("/openapi.json").json()
    untagged = [
        f"{method.upper()} {path}"
        for path, ops in schema["paths"].items()
        for method, op in ops.items()
        if not op.get("tags")
    ]
    assert not untagged, f"routes with no tag: {untagged}"


def test_the_full_upload_and_review_round_trip(client, db, project):
    """One real model, uploaded over HTTP, through to an approved zone."""
    ifc = GENERATED / "gravity_trap.ifc"
    with ifc.open("rb") as fh:
        response = client.post(
            f"/projects/{project.id}/models",
            files={"ifc": (ifc.name, fh.read(), "application/octet-stream")},
        )
    assert response.status_code == 201, response.text
    body = response.json()
    version_id = body["model_version"]["id"]
    assert body["zones_created"] >= 1
    assert body["model_version"]["ifc_sha256"]

    zones = client.get(f"/models/{version_id}/zones").json()
    assert zones
    zone_id = zones[0]["id"]

    clashes = client.get(f"/zones/{zone_id}/clashes").json()
    assert clashes, "the fixture has clashes; the API returned none"
    assert all(
        c["state"] in ("open", "proposed", "verified", "verified_conditional", "escalated")
        for c in clashes
    )

    proposals = client.get(f"/zones/{zone_id}/proposals").json()
    assert proposals
    accepted = [
        p for p in proposals if p["verdict"] in ("verified", "verified_conditional")
    ]
    assert accepted
    for p in accepted:
        for field in ("monitor_geometry", "monitor_boundary", "monitor_integrity"):
            assert p[field]["verdict"] != "fail"
        # These fixtures carry no ports and no access table, so integrity cannot
        # answer two of its checks and the verdict must say so rather than
        # rounding up to a full verification.
        if p["verdict"] == "verified_conditional":
            assert p["unprovable_checks"], "a conditional must name what it could not check"
        else:
            assert p["unprovable_checks"] == []

    change_set = client.get(f"/zones/{zone_id}/change_set.json")
    assert change_set.status_code == 200
    payload = change_set.json()
    assert "entries" in payload
    summary = payload["verification_summary"]
    assert summary["entries"] == len(payload["entries"])
    assert summary["fully_verified"] + summary["conditionally_verified"] == summary["entries"]
    for entry in payload["entries"]:
        assert entry["verification"] in ("full", "conditional")

    bcf = client.get(f"/zones/{zone_id}/bcf.zip")
    assert bcf.status_code == 200
    assert bcf.content[:2] == b"PK", "BCF must be a real zip"

    # Approving a zone with conditional verifications requires naming each
    # unanswered check; a blank approval is refused.
    blind = client.post(
        f"/zones/{zone_id}/review",
        json={"decision": "approve", "reviewer": "an engineer"},
    )
    outstanding: list[str] = []
    if blind.status_code == 422:
        assert blind.json()["code"] == "acknowledgement_required"
        outstanding = blind.json()["detail"]["unacknowledged"]
        assert outstanding

    approved = client.post(
        f"/zones/{zone_id}/review",
        json={
            "decision": "approve",
            "reviewer": "an engineer",
            "notes": "looks right",
            "acknowledge_unprovable": outstanding,
        },
    )
    assert approved.status_code == 200, approved.text
    review = approved.json()
    assert review["zone_status"] == "merged"
    assert review["acknowledged"] == outstanding

    # And the original file is untouched.
    from app.blocks.ifc_loader import model_sha256

    assert model_sha256(ifc) == body["model_version"]["ifc_sha256"]


def test_an_edit_decision_without_edits_is_rejected(client, db, project):
    from app.models import ModelVersion, Zone

    mv = ModelVersion(project_id=project.id, ifc_sha256="a" * 64, ifc_path="/x.ifc")
    db.add(mv)
    db.flush()
    zone = Zone(model_version_id=mv.id, zone_key="L0|0_0", status="awaiting_review")
    db.add(zone)
    db.commit()

    response = client.post(
        f"/zones/{zone.id}/review",
        json={"decision": "edit", "reviewer": "an engineer", "edits": []},
    )
    assert response.status_code == 400


def test_vendor_lock_matches_the_vendored_tree():
    """CI runs this too. A hand-edited block must fail the build."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent.parent
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "vendor_kit.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=str(root),
    )
    assert result.returncode == 0, result.stderr

    lock = json.loads((root / "VENDOR.lock").read_text(encoding="utf-8"))
    assert len(lock["sha"]) == 40
    assert lock["ref"] == "main", "the hat must be pinned to main, not a branch"
    assert lock["files"], "the lock records no files"
