"""Filesystem-backed model store.

Branch heads are JSON files under ``<root>/<stream>/<zone>.json``, appended to
as commits land. This is the default backend and a real one: it holds actual
committed state, it is read back by the BoundaryMonitor mid-run, and a commit
made by zone A is visible to zone B's next check without any coordination
between them.

Concurrency: each commit rewrites one zone file, and only that zone's resolver
writes it, so two workers never contend for the same file. A reader may see a
head from a moment ago -- which is the same guarantee a Speckle fetch gives, and
is why the coordinator re-runs both zones' monitors before committing anything
that crosses a boundary.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from app.store.base import BranchHead, Vector


class LocalModelStore:
    backend = "local"

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _stream_dir(self, stream: str) -> Path:
        d = self.root / stream
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _safe(zone_key: str) -> str:
        return "".join(c if c.isalnum() or c in "-_." else "_" for c in zone_key)

    def _branch_file(self, stream: str, zone_key: str) -> Path:
        return self._stream_dir(stream) / f"{self._safe(zone_key)}.json"

    def ensure_stream(self, model_version_id: str, name: str) -> str:
        stream = f"mv-{model_version_id}"
        meta = self._stream_dir(stream) / "_stream.json"
        if not meta.exists():
            meta.write_text(
                json.dumps({"stream": stream, "name": name, "backend": self.backend}, indent=2),
                encoding="utf-8",
            )
        return stream

    def ensure_branch(self, stream: str, zone_key: str) -> str:
        path = self._branch_file(stream, zone_key)
        if not path.exists():
            self._write(path, {"zone_key": zone_key, "commits": []})
        return f"zone/{zone_key}"

    @staticmethod
    def _write(path: Path, payload: dict) -> None:
        """Atomic replace, so a crash mid-write cannot leave a half-read head."""
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def commit(
        self,
        stream: str,
        zone_key: str,
        element_gid: str,
        vector_mm: Vector,
        message: str,
        meta: dict | None = None,
    ) -> str:
        path = self._branch_file(stream, zone_key)
        if not path.exists():
            self.ensure_branch(stream, zone_key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        commit_id = f"{zone_key}:{len(payload['commits']) + 1}"
        payload["commits"].append(
            {
                "id": commit_id,
                "element_gid": element_gid,
                "vector_mm": list(vector_mm),
                "message": message,
                "meta": meta or {},
            }
        )
        self._write(path, payload)
        return commit_id

    def branch_head(self, stream: str, zone_key: str) -> BranchHead:
        path = self._branch_file(stream, zone_key)
        if not path.exists():
            return BranchHead(zone_key=zone_key)
        payload = json.loads(path.read_text(encoding="utf-8"))
        offsets: dict[str, Vector] = {}
        for c in payload["commits"]:
            gid = c["element_gid"]
            prev = offsets.get(gid, (0.0, 0.0, 0.0))
            v = c["vector_mm"]
            # Moves accumulate: a second commit on the same element is a further
            # displacement from where the first one left it, not a replacement.
            offsets[gid] = (prev[0] + v[0], prev[1] + v[1], prev[2] + v[2])
        return BranchHead(zone_key=zone_key, offsets=offsets, commit_count=len(payload["commits"]))

    def heads(self, stream: str, zone_keys: list[str]) -> dict[str, BranchHead]:
        return {z: self.branch_head(stream, z) for z in zone_keys}
