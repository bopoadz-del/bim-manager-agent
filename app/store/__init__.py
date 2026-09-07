"""Model store selection.

Speckle is used when it is configured and reachable. When it is not, the local
store takes over and the choice is logged with the reason. The fallback is never
silent: a run whose branch heads live on disk rather than on a Speckle server is
a run whose review UI cannot show a 3D diff, and an operator needs to know that
from the logs rather than from a blank panel.
"""
from __future__ import annotations

import logging

from app.config import Settings, get_settings
from app.store.base import BranchHead, ModelStore, Vector
from app.store.local import LocalModelStore

log = logging.getLogger(__name__)

__all__ = ["BranchHead", "LocalModelStore", "ModelStore", "Vector", "get_store", "store_status"]


def _speckle(settings: Settings):
    from app.store.speckle import SpeckleModelStore, SpeckleUnavailable

    try:
        return SpeckleModelStore(settings.speckle_host, settings.speckle_token or ""), None
    except SpeckleUnavailable as exc:
        return None, str(exc)
    except Exception as exc:  # a reachable-but-broken server is still a fallback
        return None, f"speckle unusable: {exc}"


def get_store(settings: Settings | None = None) -> ModelStore:
    settings = settings or get_settings()
    if settings.speckle_token:
        store, reason = _speckle(settings)
        if store is not None:
            return store
        log.warning("falling back to local model store: %s", reason)
    return LocalModelStore(f"{settings.artifacts_dir}/streams")


def store_status(settings: Settings | None = None) -> dict:
    """What the store is and, when it is not Speckle, why not."""
    settings = settings or get_settings()
    if not settings.speckle_token:
        return {"backend": "local", "reason": "no Speckle token configured"}
    store, reason = _speckle(settings)
    if store is None:
        return {"backend": "local", "reason": reason}
    return {"backend": "speckle", "host": settings.speckle_host, "reason": None}
