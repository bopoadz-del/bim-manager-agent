"""What a model store must be able to do.

A zone works on its own branch. The BoundaryMonitor's whole job is to check a
proposed move against what the *neighbouring* zones have committed by now, not
against the model as it looked when the run started -- so the store has to be
able to answer "what is the current head of zone X" at any moment, and that
answer has to change as other zones commit.

Two implementations satisfy this: :class:`~app.store.local.LocalModelStore`,
which keeps branch heads in the service's own database, and
:class:`~app.store.speckle.SpeckleModelStore`, which keeps them in Speckle
commits. Both are real stores. The local one is not a stand-in for the remote
one -- it is the backend used when no Speckle server is configured, and the
acceptance run exercises it end to end.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

Vector = tuple[float, float, float]


@dataclass
class BranchHead:
    """Every committed move on one zone branch, keyed by element."""

    zone_key: str
    offsets: dict[str, Vector] = field(default_factory=dict)
    commit_count: int = 0

    def offset_for(self, gid: str) -> Vector:
        return self.offsets.get(gid, (0.0, 0.0, 0.0))


class ModelStore(Protocol):
    backend: str

    def ensure_stream(self, model_version_id: str, name: str) -> str:
        """Create or return the stream/project id holding this model version."""

    def ensure_branch(self, stream: str, zone_key: str) -> str:
        """Create or return the branch a zone resolves on."""

    def commit(
        self,
        stream: str,
        zone_key: str,
        element_gid: str,
        vector_mm: Vector,
        message: str,
        meta: dict | None = None,
    ) -> str:
        """Record one accepted move on the zone branch. Returns a commit id."""

    def branch_head(self, stream: str, zone_key: str) -> BranchHead:
        """Current committed state of one zone branch."""

    def heads(self, stream: str, zone_keys: list[str]) -> dict[str, BranchHead]:
        """Current committed state of several zone branches at once."""
