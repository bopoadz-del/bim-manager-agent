"""Carry the verification distinction into the deliverables.

The kit writes `change_set.json` and the BCF package, and it does not know about
conditional verification — that concept belongs to this service, which is what
runs the monitors. So the kit writes its output and this module adds one thing to
it: whether each entry was fully verified, or accepted with checks this model
could not answer, and which checks those were.

It matters most here. A change set is what leaves the building: it is emailed,
imported into Revit, worked from on site. If the distinction lives only in the
service's database, then the moment the deliverable is exported it becomes
indistinguishable from a fully verified one, and every downstream reader is told
something stronger than what was measured.

Nothing under `vendor/` is edited. The kit's files are read back and enriched.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

FULL = "full"
CONDITIONAL = "conditional"

#: What a reader is being told, in the deliverable itself.
VERIFICATION_NOTE = {
    FULL: "Every check of all three monitors passed against this model.",
    CONDITIONAL: (
        "No monitor objected, but this model could not answer the checks listed in "
        "unprovable_checks. Those are not passes. An engineer accepting this entry "
        "is accepting them."
    ),
}


def verification_of(proposal: Any) -> str:
    verdict = getattr(proposal, "verdict", "")
    return CONDITIONAL if verdict == "verified_conditional" else FULL


def enrich_change_set(path: str | Path, by_clash: dict[str, dict[str, Any]]) -> Path:
    """Add verification status to every entry the kit wrote.

    ``by_clash`` maps clash_key to ``{"verification": ..., "unprovable_checks": [...]}``.
    """
    p = Path(path)
    payload = json.loads(p.read_text(encoding="utf-8"))

    conditional = 0
    for entry in payload.get("entries", []):
        info = by_clash.get(entry.get("clash_id"), {})
        verification = info.get("verification", FULL)
        entry["verification"] = verification
        entry["unprovable_checks"] = info.get("unprovable_checks", [])
        entry["verification_note"] = VERIFICATION_NOTE[verification]
        if verification == CONDITIONAL:
            conditional += 1

    total = len(payload.get("entries", []))
    payload["verification_summary"] = {
        "entries": total,
        "fully_verified": total - conditional,
        "conditionally_verified": conditional,
        "note": (
            "A conditionally verified entry is one no monitor objected to and that "
            "this model could not fully check. Read unprovable_checks on each before "
            "applying it."
        ),
    }
    p.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return p


def annotate_bcf(path: str | Path, by_pair: dict[frozenset, dict[str, Any]]) -> Path:
    """Add a verification comment to every BCF topic.

    A ``Comment`` is standard BCF 2.1, so this survives into any viewer that
    reads the format rather than being an out-of-band note only this service
    understands.

    ``by_pair`` maps a frozenset of the two element GlobalIds to the same shape
    ``enrich_change_set`` takes.
    """
    p = Path(path)
    if not p.exists():
        return p

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        with zipfile.ZipFile(p) as zf:
            zf.extractall(work)

        for markup in sorted(work.rglob("markup.bcf")):
            try:
                tree = ET.parse(markup)
            except ET.ParseError:
                continue
            root = tree.getroot()

            # The kit puts both element GlobalIds in the topic title, which is
            # the one place they appear without re-parsing the viewpoint.
            title = root.findtext("./Topic/Title") or ""
            info = next(
                (v for k, v in by_pair.items() if all(gid and gid in title for gid in k)),
                None,
            )
            if info is None:
                continue

            verification = info.get("verification", FULL)
            text = VERIFICATION_NOTE[verification]
            unprovable = info.get("unprovable_checks") or []
            if unprovable:
                text += " Not checkable here: " + ", ".join(unprovable) + "."

            comment = ET.SubElement(root, "Comment", {"Guid": str(uuid.uuid4())})
            ET.SubElement(comment, "Date").text = (
                root.findtext("./Topic/CreationDate") or ""
            )
            ET.SubElement(comment, "Author").text = "mep-judge"
            ET.SubElement(comment, "Comment").text = f"Verification: {verification}. {text}"
            tree.write(markup, encoding="UTF-8", xml_declaration=True)

        rebuilt = work.parent / "rebuilt.bcfzip"
        with zipfile.ZipFile(rebuilt, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(work.rglob("*")):
                if item.is_file():
                    zf.write(item, item.relative_to(work).as_posix())
        shutil.move(str(rebuilt), str(p))
    return p
