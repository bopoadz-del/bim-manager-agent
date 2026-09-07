"""ASGI application."""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api.errors import ApiError, api_error_handler
from app.api.routes import router

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

DESCRIPTION = """
Judges MEP coordination on an IFC model, proposes moves that cite a clause, and
verifies every one of them through three independent monitors before a reviewer
ever sees it.

The model of record is never written to. Proposals are vectors and clause
references, applied by an engineer in the authoring tool; the original file's
sha256 is reported before and after every run so the promise is evidence rather
than a claim.
"""

app = FastAPI(
    title="mep-judge",
    version="0.1.0",
    description=DESCRIPTION,
    openapi_tags=[
        {"name": "projects", "description": "Projects and per-project API keys."},
        {"name": "models", "description": "Model versions, ingest and version diff."},
        {"name": "zones", "description": "Zones, their clashes and proposals."},
        {"name": "review", "description": "Reviewer decisions, change sets and BCF."},
        {"name": "events", "description": "Server-sent progress events."},
        {"name": "ops", "description": "Health and build identity."},
    ],
)

app.add_exception_handler(ApiError, api_error_handler)
app.include_router(router)

_UI_DIR = Path(__file__).resolve().parent.parent / "ui" / "static"
if _UI_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=str(_UI_DIR), html=True), name="ui")
