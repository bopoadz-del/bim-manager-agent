"""``app.blocks`` is the vendored ``mep_coordination`` kit, mounted in place.

The kit's own modules import each other as ``app.blocks.geometry_engine`` and
``app.blocks.ifc_loader``. Rather than rewrite those imports -- which would mean
editing vendored files, which VENDOR.lock exists to forbid -- this package
points its ``__path__`` at the pinned directory. Import machinery then resolves
``app.blocks.X`` to ``vendor/mep_coordination/X.py`` verbatim.

The pin is enforced separately by ``scripts/vendor_kit.py --check`` in CI, so a
block cannot be quietly edited here: it is changed upstream in Cerebrum-Blocks
and re-pinned, or it is not changed at all.
"""
from __future__ import annotations

from pathlib import Path

VENDOR_DIR = Path(__file__).resolve().parent.parent.parent / "vendor" / "mep_coordination"

if not VENDOR_DIR.is_dir():  # pragma: no cover - a broken checkout, not a code path
    raise ImportError(
        f"vendored kit missing at {VENDOR_DIR}. "
        "Run: python scripts/vendor_kit.py --sync --source <Cerebrum-Blocks clone>"
    )

__path__ = [str(VENDOR_DIR)]
