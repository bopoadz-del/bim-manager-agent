"""Extract candidate clearance rules from a project specification.

The output of this module is never a rule. It is a *candidate* rule, written to
a pending file, carrying the clause it came from and the sha256 of the exact
source text. A human approves the file once per project, and only then does the
rule table load it -- through ``clearance_rules.load_rules``, which refuses
anything without a citation regardless of who wrote it.

That approval gate is the point. A clearance figure is a number an engineer will
move a duct to satisfy, and the difference between 300 mm and 400 mm is a
building that passes inspection and one that does not. A model that reads a
specification well is a useful assistant and is not a source of authority.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from app.llm.client import LLMClient, get_client

SYSTEM_PROMPT = """You extract minimum-separation requirements from construction specifications.

Return ONLY a JSON array. Each element must have exactly these keys:
  rule_id      short stable id, uppercase, hyphenated
  system_a     one of: gas_main, electrical_lv, electrical_hv, water, drainage_foul,
               drainage_storm, ventilation, sprinkler, telecom, building, "*"
  system_b     same vocabulary
  min_gap_mm   number, millimetres
  axis         "any", "vertical" or "horizontal"
  precedence   "project_spec"
  source_doc   the document name you were given
  source_clause the clause or note reference, verbatim
  quote        the exact sentence you took the number from, verbatim

Rules:
- Extract ONLY requirements stated in the text. Never infer a value from a
  similar rule, a code you know, or a typical figure.
- If a passage states a separation without a number, skip it.
- If you are unsure a passage is a separation requirement, skip it.
- An empty array is a correct answer when the text contains no such requirement.
"""

REQUIRED_KEYS = {
    "rule_id",
    "system_a",
    "system_b",
    "min_gap_mm",
    "axis",
    "precedence",
    "source_doc",
    "source_clause",
    "quote",
}


def chunk_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()[:16]


def _parse(raw: str) -> list[dict[str, Any]]:
    match = re.search(r"\[.*\]", raw, re.DOTALL)
    if not match:
        raise ValueError("model did not return a JSON array")
    parsed = json.loads(match.group(0))
    if not isinstance(parsed, list):
        raise ValueError("model returned JSON that is not an array")
    return parsed


def _validate(candidates: list[dict[str, Any]], source_text: str) -> list[dict[str, Any]]:
    """Keep only candidates that are complete and whose quote is really present.

    The quote check is the load-bearing one. It is the difference between a
    citation and a claim of a citation: if the sentence the model says it read is
    not in the document, the number attached to it did not come from the document
    either, whatever the model asserted.
    """
    haystack = " ".join(source_text.split()).lower()
    kept = []
    for c in candidates:
        if not isinstance(c, dict) or not REQUIRED_KEYS.issubset(c):
            continue
        quote = " ".join(str(c.get("quote", "")).split()).lower()
        if len(quote) < 12 or quote not in haystack:
            continue
        try:
            c["min_gap_mm"] = float(c["min_gap_mm"])
        except (TypeError, ValueError):
            continue
        if c["min_gap_mm"] <= 0:
            continue
        c["source_text_hash"] = chunk_hash(str(c["quote"]))
        c["approved"] = False
        kept.append(c)
    return kept


def extract_candidates(
    spec_text: str,
    source_doc: str,
    client: LLMClient | None = None,
    max_chars: int = 12000,
) -> list[dict[str, Any]]:
    client = client or get_client()
    candidates: list[dict[str, Any]] = []
    for start in range(0, len(spec_text), max_chars):
        chunk = spec_text[start : start + max_chars]
        raw = client.complete(
            SYSTEM_PROMPT,
            f"Document: {source_doc}\n\n{chunk}",
            max_tokens=3000,
        )
        try:
            candidates.extend(_validate(_parse(raw), chunk))
        except (ValueError, json.JSONDecodeError):
            # A chunk the model answered badly is a chunk with no rules, not a
            # reason to abandon the document.
            continue
    return candidates


def write_pending(candidates: list[dict[str, Any]], out_path: str | Path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "status": "pending_human_approval",
                "note": (
                    "Nothing here is a rule yet. Review every quote against the source "
                    "document, set approved to true on the ones you accept, then run "
                    "approve_pending(). Unapproved candidates are never loaded."
                ),
                "candidates": candidates,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return path


def approve_pending(pending_path: str | Path, out_path: str | Path) -> Path:
    """Write the approved candidates out in the shape ``load_rules`` accepts.

    Approval is a human editing ``approved`` to true. This function only moves
    what a person has already signed off; it never approves anything itself.
    """
    payload = json.loads(Path(pending_path).read_text(encoding="utf-8"))
    approved = [c for c in payload.get("candidates", []) if c.get("approved") is True]
    rules = [
        {
            "rule_id": c["rule_id"],
            "system_a": c["system_a"],
            "system_b": c["system_b"],
            "min_gap_mm": c["min_gap_mm"],
            "axis": c["axis"],
            "precedence": c["precedence"],
            "source": {
                "doc": c["source_doc"],
                "clause": c["source_clause"],
                "text_hash": c["source_text_hash"],
            },
        }
        for c in approved
    ]
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rules, indent=2), encoding="utf-8")
    return path
