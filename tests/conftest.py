"""Shared fixtures.

Every test gets its own SQLite file, its own artifacts directory and its own
model store. Nothing is shared between tests, so a test that leaves state behind
cannot make the next one pass. The mesh cache is the one deliberate exception:
it is keyed on model content hash, so sharing it across a session is safe and
turns a 50-second Schependomlaan mesh into a one-off cost.
"""
from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

warnings.filterwarnings("ignore", category=RuntimeWarning, module="trimesh")

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures"
GENERATED = FIXTURE_DIR / "generated"
ALIASES = FIXTURE_DIR / "system_aliases.json"

# The 47 MB public model. Fetched by scripts/fetch_fixtures.sh, never committed.
SCHEPENDOMLAAN = FIXTURE_DIR / "models" / "schependomlaan_design.ifc"
INFRA_PLUMBING = FIXTURE_DIR / "models" / "Infra-Plumbing.ifc"


@pytest.fixture(scope="session", autouse=True)
def _generated_fixtures():
    """Build the trap models once per session if they are not already there."""
    from tests.fixtures.build_fixtures import build_all

    if not (GENERATED / "boundary_trap.ifc").exists():
        build_all(GENERATED)
    return GENERATED


@pytest.fixture(scope="session")
def mesh_cache(tmp_path_factory) -> Path:
    return tmp_path_factory.mktemp("mesh_cache")


@pytest.fixture
def settings(tmp_path, mesh_cache, monkeypatch):
    from app.config import Settings, get_settings

    db_path = tmp_path / "test.db"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=True)
    # Share the session mesh cache so repeated loads of the same model are free.
    (artifacts / "mesh_cache").mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("MEPJ_DATABASE_URL", f"sqlite+pysqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("MEPJ_ARTIFACTS_DIR", str(artifacts))
    monkeypatch.setenv("MEPJ_UPLOAD_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("MEPJ_BOOTSTRAP_API_KEY", "test-bootstrap-key")
    monkeypatch.setenv("MEPJ_BUILD_SHA", "test-sha")
    monkeypatch.delenv("MEPJ_SPECKLE_TOKEN", raising=False)

    get_settings.cache_clear()
    s = Settings()
    yield s
    get_settings.cache_clear()


@pytest.fixture
def db(settings):
    from app.db import get_session_factory, reset_engine
    from app.models import Base

    reset_engine()
    from app.db import get_engine

    Base.metadata.create_all(get_engine())
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        reset_engine()


@pytest.fixture
def store(settings):
    from app.store.local import LocalModelStore

    return LocalModelStore(Path(settings.artifacts_dir) / "streams")


@pytest.fixture
def project(db):
    from app.models import Project

    p = Project(name="acceptance project")
    db.add(p)
    db.commit()
    return p


@pytest.fixture
def client(settings, db):
    """A TestClient wired to the same session the test holds."""
    from fastapi.testclient import TestClient

    from app.db import get_db
    from app.main import app

    def _override():
        yield db

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        c.headers.update({"X-API-Key": "test-bootstrap-key"})
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def rules():
    from app.agents.coordinator import load_project_rules

    return load_project_rules()


def make_model_version(db, project, ifc_path: Path, aliases: Path | None = ALIASES):
    from app.blocks.ifc_loader import model_sha256

    from app.models import ModelVersion

    mv = ModelVersion(
        project_id=project.id,
        ifc_sha256=model_sha256(ifc_path),
        ifc_path=str(ifc_path),
        aliases_path=str(aliases) if aliases else None,
        status="ingesting",
    )
    db.add(mv)
    db.commit()
    return mv


def requires(path: Path):
    """Skip when a large downloaded fixture is absent, and say how to get it."""
    return pytest.mark.skipif(
        not path.exists(),
        reason=f"{path.name} not present; run scripts/fetch_fixtures.sh",
    )


os.environ.setdefault("PYTHONHASHSEED", "0")
