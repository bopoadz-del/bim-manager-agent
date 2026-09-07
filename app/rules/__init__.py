"""Clearance rules from public standards, and the honesty they ship with.

The kit's seed table is three gas rules from one project drawing, so until now
this service found hard clashes on a real model and nothing else. The rules here
change that: they come from published standards anybody can obtain, which is the
whole reason they can live in a public repository at all.

**They are unverified transcriptions.** The figures were written down by the
author of this repository without the source documents open, and several clause
numbers are the author's best identification rather than a certainty. That is
recorded in the file, on every rule, and on every finding a rule produces.

This is not a disclaimer bolted on. It is the same principle the whole product
runs on: a number an engineer will move a duct to satisfy has to carry the place
it came from, so the engineer can check it. A citation exists to be followed. The
``verified`` flag says whether anyone has followed this one yet.

The kit's ``clearance_rule.schema.json`` is ``additionalProperties: false``, so
``standard``, ``edition``, ``quote`` and ``verified`` cannot ride along inside a
rule. The rich record lives here; :func:`to_kit_rules` strips it down to exactly
what the kit accepts, and the standard and edition survive inside ``source.doc``.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

RULES_FILE = Path(__file__).resolve().parent / "public_seed_rules.json"

#: Fields the kit's schema allows. Anything else is ours and is stripped.
KIT_FIELDS = ("rule_id", "system_a", "system_b", "min_gap_mm", "axis", "source", "precedence")
SOURCE_FIELDS = ("doc", "clause", "text_hash")

#: Records that cite a slope requirement rather than a separation. They are kept
#: so the fall check in IntegrityMonitor can name the clause it enforces, and
#: they are NOT loaded as separation rules -- a slope expressed as a gap would be
#: a number that looks like a clearance and is not one.
SLOPE_ONLY = "slope_citation_only"


class UnverifiedRule(Exception):
    """Raised when unverified rules are used somewhere that forbids them."""


def _payload() -> dict[str, Any]:
    return json.loads(RULES_FILE.read_text(encoding="utf-8"))


def raw_rules() -> list[dict[str, Any]]:
    """Every record in the file, rich fields included."""
    return list(_payload()["rules"])


def verification_note() -> dict[str, Any]:
    return dict(_payload()["verification"])


def withheld_for_scope() -> list[dict[str, str]]:
    """Rules that are real, cited, and cannot honestly be applied from IFC.

    The single most useful thing this file learned. A published clearance figure
    almost always carries a scope condition -- equipment *likely to require
    examination while energized*, a hole bored through *wood*, the space at a
    duct *access door*, the top of *storage* beneath a sprinkler. IFC carries
    none of those. Applied to the nearest available system category, the rule
    stops being the rule: ASHRAE's 3 m intake-to-vent separation becomes 3 m
    between every duct and every drain in the building.

    So they are withheld, by name, with the reason. Loading them anyway would
    fill a coordination report with requirements no standard imposes -- the same
    noise this product exists to remove, only now wearing a citation, which
    makes it harder to argue with rather than easier.
    """
    return [
        {
            "rule_id": r["rule_id"],
            "standard": r["standard"],
            "clause": r["source"]["clause"],
            "reason": r.get("scope_note", "scope not expressible from IFC"),
        }
        for r in raw_rules()
        if r.get("applies_as") != SLOPE_ONLY and not r.get("scope_inferable", False)
    ]


def to_kit_rules(records: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Strip rich records down to what the kit's schema accepts.

    Slope citations are dropped: they are references for the fall check, not
    separations, and loading one as a gap would put a number in front of an
    engineer that means something other than what it appears to mean.

    Rules whose scope IFC cannot establish are dropped for the same reason at a
    larger scale -- see :func:`withheld_for_scope`.
    """
    out = []
    for record in records if records is not None else raw_rules():
        if record.get("applies_as") == SLOPE_ONLY:
            continue
        if not record.get("scope_inferable", False):
            continue
        rule = {k: record[k] for k in KIT_FIELDS if k in record}
        rule["source"] = {k: record["source"][k] for k in SOURCE_FIELDS}
        out.append(rule)
    return out


def validate_against_kit_schema(records: list[dict[str, Any]] | None = None) -> list[str]:
    """Problems the kit's own schema would report. Empty means it validates."""
    schema = json.loads(
        (
            Path(__file__).resolve().parent.parent.parent
            / "vendor"
            / "mep_coordination"
            / "clearance_rule.schema.json"
        ).read_text(encoding="utf-8")
    )
    required = set(schema["required"])
    allowed = set(schema["properties"])
    axes = set(schema["properties"]["axis"]["enum"])
    precedences = set(schema["properties"]["precedence"]["enum"])
    source_required = set(schema["properties"]["source"]["required"])

    problems: list[str] = []
    for rule in to_kit_rules(records):
        rid = rule.get("rule_id", "<no id>")
        missing = required - set(rule)
        if missing:
            problems.append(f"{rid}: missing {sorted(missing)}")
        extra = set(rule) - allowed
        if extra:
            problems.append(f"{rid}: schema forbids {sorted(extra)}")
        if rule.get("axis") not in axes:
            problems.append(f"{rid}: axis {rule.get('axis')!r} not in {sorted(axes)}")
        if rule.get("precedence") not in precedences:
            problems.append(f"{rid}: precedence {rule.get('precedence')!r} invalid")
        if not isinstance(rule.get("min_gap_mm"), (int, float)) or rule["min_gap_mm"] <= 0:
            problems.append(f"{rid}: min_gap_mm must be a positive number")
        source_missing = source_required - set(rule.get("source", {}))
        if source_missing:
            problems.append(f"{rid}: source missing {sorted(source_missing)}")
    return problems


def load_public_rules(include_unverified: bool = True) -> list[Any]:
    """Load the public rules through the kit's own loader.

    Going through ``clearance_rules.load_rules`` rather than constructing Rule
    objects here is deliberate: the loader refuses anything without a citation,
    and these rules earn no exemption from the check every other rule faces.
    """
    import tempfile

    from app.blocks.clearance_rules import load_rules  # type: ignore[attr-defined]

    records = raw_rules()
    if not include_unverified:
        records = [r for r in records if r.get("verified") is True]
    kit_rules = to_kit_rules(records)
    if not kit_rules:
        return []

    path = Path(tempfile.mkdtemp()) / "public_rules.json"
    path.write_text(json.dumps(kit_rules), encoding="utf-8")
    return list(load_rules(str(path)))


def verification_index() -> dict[str, dict[str, Any]]:
    """rule_id -> how far anybody has actually checked it."""
    return {
        r["rule_id"]: {
            "standard": r["standard"],
            "edition": r["edition"],
            "clause": r["source"]["clause"],
            "verified": bool(r.get("verified")),
            "confidence": r.get("confidence", ""),
        }
        for r in raw_rules()
    }


def unverified_rule_ids() -> set[str]:
    return {r["rule_id"] for r in raw_rules() if not r.get("verified")}


__all__ = [
    "RULES_FILE",
    "SLOPE_ONLY",
    "UnverifiedRule",
    "load_public_rules",
    "raw_rules",
    "to_kit_rules",
    "unverified_rule_ids",
    "validate_against_kit_schema",
    "withheld_for_scope",
    "verification_index",
    "verification_note",
]
