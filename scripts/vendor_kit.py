"""Vendor the mep_coordination kit blocks from Cerebrum-Blocks at a pinned SHA.

The blocks are the product's judgement. They are not reimplemented here and they
are not edited here: this script copies them verbatim at a recorded commit and
writes a lock file of per-file sha256. ``--check`` re-verifies, so CI fails if
anyone hand-edits a vendored block instead of changing it upstream and re-pinning.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = "https://github.com/bopoadz-del/Cerebrum-Blocks"
KIT_PATH = "block_store/kits/mep_coordination/bundle/app/blocks"
ROOT = Path(__file__).resolve().parent.parent
DEST = ROOT / "vendor" / "mep_coordination"
LOCK = ROOT / "VENDOR.lock"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _run(cmd: list[str], cwd: Path | None = None) -> str:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {proc.stderr.strip()}")
    return proc.stdout.strip()


def _copy_from(source_repo: Path, sha: str, dest: Path) -> None:
    """Extract KIT_PATH at ``sha`` out of ``source_repo`` into ``dest``."""
    with tempfile.TemporaryDirectory() as tmp:
        tar = Path(tmp) / "kit.tar"
        with tar.open("wb") as fh:
            proc = subprocess.run(
                ["git", "archive", sha, KIT_PATH],
                cwd=source_repo, stdout=fh, stderr=subprocess.PIPE, text=False,
            )
        if proc.returncode != 0:
            raise RuntimeError(f"git archive {sha} failed: {proc.stderr.decode()}")
        _run(["tar", "-xf", str(tar), "-C", tmp])
        extracted = Path(tmp) / KIT_PATH
        if not extracted.is_dir():
            raise RuntimeError(f"{KIT_PATH} absent at {sha}")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(extracted, dest)
    # __pycache__ is not part of the pin.
    for junk in dest.rglob("__pycache__"):
        shutil.rmtree(junk, ignore_errors=True)


def _is_generated(path: Path) -> bool:
    """Bytecode and caches are produced by running the code, not by the pin."""
    return "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo")


def _manifest(dest: Path) -> dict[str, str]:
    return {
        str(p.relative_to(dest)).replace("\\", "/"): _sha256(p)
        for p in sorted(dest.rglob("*"))
        if p.is_file() and not _is_generated(p)
    }


def vendor(source_repo: Path, sha: str) -> dict:
    resolved = _run(["git", "rev-parse", sha], cwd=source_repo)
    _copy_from(source_repo, resolved, DEST)
    lock = {
        "repo": REPO,
        "path": KIT_PATH,
        "ref": "main",
        "sha": resolved,
        "files": _manifest(DEST),
    }
    LOCK.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return lock


def check() -> int:
    """Return 0 when the vendored tree still matches VENDOR.lock exactly."""
    if not LOCK.exists():
        print("VENDOR.lock missing -- run scripts/vendor_kit.py --sync", file=sys.stderr)
        return 1
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    actual = _manifest(DEST)
    expected = lock["files"]
    problems = []
    for name, digest in expected.items():
        if name not in actual:
            problems.append(f"missing: {name}")
        elif actual[name] != digest:
            problems.append(f"edited in place: {name}")
    for name in actual:
        if name not in expected:
            problems.append(f"untracked addition: {name}")
    if problems:
        print(f"vendored kit drifted from pin {lock['sha'][:12]}:", file=sys.stderr)
        for p in problems:
            print(f"  {p}", file=sys.stderr)
        print("Change the kit upstream and re-pin. Do not edit vendor/.", file=sys.stderr)
        return 1
    print(f"vendor ok: {len(expected)} files at {lock['sha'][:12]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify the tree matches the lock")
    ap.add_argument("--sync", action="store_true", help="re-copy from a local clone")
    ap.add_argument("--source", type=Path, help="local Cerebrum-Blocks clone")
    ap.add_argument("--sha", default="main", help="commit to pin (default: main)")
    args = ap.parse_args()
    if args.check:
        return check()
    if args.sync:
        if not args.source:
            print("--sync needs --source <clone>", file=sys.stderr)
            return 2
        lock = vendor(args.source, args.sha)
        print(f"pinned {len(lock['files'])} files at {lock['sha']}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
