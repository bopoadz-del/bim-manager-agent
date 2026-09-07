"""Speckle-backed model store.

Used when a host and token are configured. Zone branches are Speckle branches
named ``zone/<zone_key>``; each accepted move is a commit carrying the element
id and the displacement in millimetres. The BoundaryMonitor reads branch heads
back through :meth:`heads`, so a move committed by a neighbouring zone is
visible to the next check without any extra plumbing.

Displacements are stored as commit metadata rather than as rewritten geometry.
That is deliberate: the service's central promise is that the model of record is
never written to, and a store that re-uploaded transformed meshes would make
that promise harder to audit. A change set of vectors, each citing the clause
that authorises it, is what an engineer applies in the authoring tool.
"""
from __future__ import annotations

import json
import logging

from app.store.base import BranchHead, Vector

log = logging.getLogger(__name__)


class SpeckleUnavailable(RuntimeError):
    """Raised at construction when Speckle cannot be used, with the reason."""


class SpeckleModelStore:
    backend = "speckle"

    def __init__(self, host: str, token: str):
        if not token:
            raise SpeckleUnavailable("no Speckle token configured")
        try:
            from specklepy.api.client import SpeckleClient
        except ImportError as exc:
            raise SpeckleUnavailable(f"specklepy not installed: {exc}") from exc

        self.host = host
        self._client = SpeckleClient(host=host)
        try:
            self._client.authenticate_with_token(token)
        except Exception as exc:
            raise SpeckleUnavailable(f"Speckle authentication failed: {exc}") from exc

    def ensure_stream(self, model_version_id: str, name: str) -> str:
        existing = self._client.stream.search(name)
        for s in existing or []:
            if getattr(s, "name", None) == name:
                return s.id
        return self._client.stream.create(name=name, description=f"mep-judge {model_version_id}")

    def ensure_branch(self, stream: str, zone_key: str) -> str:
        branch_name = f"zone/{zone_key}"
        existing = self._client.branch.get(stream, branch_name)
        if existing is None:
            self._client.branch.create(stream, branch_name, "mep-judge zone branch")
        return branch_name

    def commit(
        self,
        stream: str,
        zone_key: str,
        element_gid: str,
        vector_mm: Vector,
        message: str,
        meta: dict | None = None,
    ) -> str:
        from specklepy.objects.base import Base
        from specklepy.transports.server import ServerTransport

        branch_name = self.ensure_branch(stream, zone_key)
        payload = Base()
        payload.element_gid = element_gid
        payload.vector_mm = list(vector_mm)
        payload.meta = json.dumps(meta or {})

        from specklepy.api import operations

        transport = ServerTransport(client=self._client, stream_id=stream)
        obj_id = operations.send(base=payload, transports=[transport])
        return self._client.commit.create(
            stream_id=stream, object_id=obj_id, branch_name=branch_name, message=message
        )

    def branch_head(self, stream: str, zone_key: str) -> BranchHead:
        from specklepy.api import operations
        from specklepy.transports.server import ServerTransport

        branch = self._client.branch.get(stream, f"zone/{zone_key}", commits_limit=1000)
        if branch is None or not getattr(branch, "commits", None):
            return BranchHead(zone_key=zone_key)

        transport = ServerTransport(client=self._client, stream_id=stream)
        offsets: dict[str, Vector] = {}
        items = list(branch.commits.items)
        # Speckle returns newest first; accumulate in commit order so a second
        # move on one element adds to the first rather than replacing it.
        for commit in reversed(items):
            try:
                obj = operations.receive(commit.referencedObject, transport)
            except Exception as exc:
                log.warning("speckle: unreadable commit %s on %s: %s", commit.id, zone_key, exc)
                continue
            gid = getattr(obj, "element_gid", None)
            vec = getattr(obj, "vector_mm", None)
            if not gid or not vec:
                continue
            prev = offsets.get(gid, (0.0, 0.0, 0.0))
            offsets[gid] = (prev[0] + vec[0], prev[1] + vec[1], prev[2] + vec[2])
        return BranchHead(zone_key=zone_key, offsets=offsets, commit_count=len(items))

    def heads(self, stream: str, zone_keys: list[str]) -> dict[str, BranchHead]:
        return {z: self.branch_head(stream, z) for z in zone_keys}
