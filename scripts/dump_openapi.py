"""Write the OpenAPI schema to a file, so CI can diff it against the committed one.

An API contract that changes without anyone noticing is how a client breaks in
production over a rename nobody thought was visible.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    from app.main import app

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "openapi.json")
    out.write_text(json.dumps(app.openapi(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
