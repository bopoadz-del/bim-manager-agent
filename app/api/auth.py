"""Per-project API keys and the reviewer role.

Keys are stored as sha256 digests. The plaintext is returned exactly once, at
issue, and never again -- a key this service can print back is a key that leaks
from its own database.

Two roles. ``operator`` may ingest models and drive the pipeline; ``reviewer``
may read and may approve, reject or edit a zone. Review is the narrower and more
consequential right, so it is the default: a key issued without a stated role
can sign off work but cannot start new work.
"""
from __future__ import annotations

import hashlib
import secrets

from fastapi import Depends, Header
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import Forbidden, Unauthorized
from app.config import get_settings
from app.db import get_db
from app.models import ApiKey, Project

ROLE_REVIEWER = "reviewer"
ROLE_OPERATOR = "operator"
KEY_PREFIX = "mepj_"


def hash_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


class Principal:
    def __init__(self, project_id: str | None, role: str, key_id: str | None, label: str = ""):
        self.project_id = project_id
        self.role = role
        self.key_id = key_id
        self.label = label

    @property
    def is_operator(self) -> bool:
        return self.role == ROLE_OPERATOR

    def name(self) -> str:
        return self.label or (self.key_id or "anonymous")

    def require_project(self, project_id: str) -> None:
        """A key is scoped to its project and to nothing else."""
        if self.project_id is not None and self.project_id != project_id:
            raise Forbidden(
                "key is not valid for this project",
                {"key_project": self.project_id, "requested": project_id},
            )

    def require_operator(self) -> None:
        if not self.is_operator:
            raise Forbidden("this endpoint requires an operator key", {"role": self.role})


def authenticate(
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> Principal:
    settings = get_settings()
    if not x_api_key:
        raise Unauthorized("missing X-API-Key header")

    if settings.bootstrap_api_key and secrets.compare_digest(
        x_api_key, settings.bootstrap_api_key
    ):
        # The bootstrap key exists to create the first project and issue the
        # first real key. It is project-scoped to nothing, so it is the one key
        # worth rotating on a schedule.
        return Principal(project_id=None, role=ROLE_OPERATOR, key_id="bootstrap", label="bootstrap")

    row = db.execute(
        select(ApiKey).where(ApiKey.key_hash == hash_key(x_api_key))
    ).scalars().first()
    if row is None:
        raise Unauthorized("unknown API key")
    return Principal(row.project_id, row.role, row.id, row.label)


def issue_key(db: Session, project: Project, label: str, role: str = ROLE_REVIEWER) -> str:
    if role not in (ROLE_REVIEWER, ROLE_OPERATOR):
        raise Forbidden(f"unknown role {role!r}", {"allowed": [ROLE_REVIEWER, ROLE_OPERATOR]})
    plaintext = generate_key()
    db.add(ApiKey(project_id=project.id, key_hash=hash_key(plaintext), label=label, role=role))
    db.flush()
    return plaintext
