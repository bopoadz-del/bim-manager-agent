"""Fail the build when a named package drops below a coverage floor.

pytest-cov's own --cov-fail-under applies to the whole run. The acceptance
criterion is narrower and stricter: the agents and the monitors specifically,
because those are the modules that decide things. A repository-wide average can
stay healthy while the code that matters rots.
"""
from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("packages", nargs="+")
    ap.add_argument("--min", type=float, required=True)
    ap.add_argument("--xml", default="coverage.xml")
    args = ap.parse_args()

    xml = Path(args.xml)
    if not xml.exists():
        print(f"{xml} not found; run pytest with --cov-report=xml first", file=sys.stderr)
        return 2

    root = ET.parse(xml).getroot()
    wanted = [p.replace("\\", "/").rstrip("/") for p in args.packages]

    # Coverage records each file relative to a <source> root, not to the
    # repository, so "app/agents/coordinator.py" appears as
    # "agents/coordinator.py". Rejoining them is the difference between checking
    # the floor and silently checking nothing.
    sources = [Path(s.text or ".") for s in root.iter("source")]
    repo = Path.cwd().resolve()

    def repo_relative(filename: str) -> list[str]:
        raw = filename.replace("\\", "/")
        out = [raw]
        for source in sources:
            try:
                out.append((source / raw).resolve().relative_to(repo).as_posix())
            except (ValueError, OSError):
                continue
        return out

    covered = missed = 0
    matched_files = 0
    for cls in root.iter("class"):
        candidates = repo_relative(cls.get("filename") or "")
        if not any(c.startswith(w) for c in candidates for w in wanted):
            continue
        matched_files += 1
        for line in cls.iter("line"):
            if int(line.get("hits", "0")) > 0:
                covered += 1
            else:
                missed += 1

    total = covered + missed
    if total == 0 or matched_files == 0:
        print(
            f"no coverage data matched {wanted}. The floor was not checked, which "
            f"is not the same as the floor being met.",
            file=sys.stderr,
        )
        return 2

    pct = 100.0 * covered / total
    print(
        f"{', '.join(wanted)}: {pct:.1f}% "
        f"({covered}/{total} statements across {matched_files} files)"
    )
    if pct < args.min:
        print(f"below the {args.min:.0f}% floor", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
