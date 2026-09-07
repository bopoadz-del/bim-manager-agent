"""Reconciling the model's vocabulary with the rule table's.

A real gap, found by running the two halves together. The kit's loader infers a
system from an element's name using a fixed hint list -- ``electrical``,
``drainage_storm``, ``ventilation`` and so on. The kit's seed rules are written
against a different vocabulary: ``gas_main``, ``electrical_lv``, ``building``.
There is no hint for gas at all. So on any model, the seed rules match nothing,
every clearance check silently finds no applicable rule, and the pipeline reports
a clean building because it never asked a question.

That failure is invisible, which is what makes it dangerous. No error is raised.
The findings list simply contains no clearance violations, and an empty list
reads exactly like good news.

The fix is a project-supplied alias table. It maps name patterns onto the rule
vocabulary and it does **only** that: it is naming, never numbers. No alias can
introduce, weaken or invent a clearance value -- those still come from a cited
rule or they do not exist. A project that ships no alias table gets a report of
which rule systems its model never produced, so the silence becomes visible.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ALIAS_SOURCE = "project_alias"


@dataclass(frozen=True)
class SystemAlias:
    """One name pattern mapped to one system in the rule vocabulary."""

    pattern: re.Pattern
    system: str
    note: str = ""


class InvalidAliasTable(Exception):
    """The alias file is not shaped the way this loader requires."""


def load_system_aliases(path: str | Path | None) -> list[SystemAlias]:
    """Load a project alias table.

    Shape::

        {"aliases": [
            {"pattern": "\\\\bgas\\\\b", "system": "gas_main", "note": "why"},
            ...
        ]}

    A malformed table raises rather than being skipped. A project that meant to
    supply aliases and mistyped the file must not silently get the behaviour of
    a project that supplied none -- that is the same invisible silence this
    module exists to remove.
    """
    if path is None:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidAliasTable(f"{p}: not valid JSON: {exc}") from exc

    raw = payload.get("aliases")
    if not isinstance(raw, list):
        raise InvalidAliasTable(f"{p}: expected an 'aliases' array")

    out = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "pattern" not in entry or "system" not in entry:
            raise InvalidAliasTable(f"{p}: alias {i} needs both 'pattern' and 'system'")
        try:
            compiled = re.compile(entry["pattern"], re.IGNORECASE)
        except re.error as exc:
            raise InvalidAliasTable(f"{p}: alias {i} pattern is not a regex: {exc}") from exc
        out.append(SystemAlias(compiled, str(entry["system"]), str(entry.get("note", ""))))
    return out


def apply_system_aliases(elements: list[Any], aliases: list[SystemAlias]) -> dict[str, Any]:
    """Rewrite element systems in place, and report exactly what changed.

    First match wins, so the table is ordered by the project.

    Structural elements are aliased too. The rule vocabulary contains
    ``building`` -- the seed table's building-to-gas separation is a rule about
    building fabric -- so excluding structure from aliasing would make that
    entire class of rule permanently unreachable, which is the exact failure
    this module exists to remove.
    """
    if not aliases:
        return {"applied": 0, "by_system": {}, "aliases": 0}

    by_system: dict[str, int] = {}
    applied = 0
    for el in elements:
        name = getattr(el, "name", "") or ""
        for alias in aliases:
            if alias.pattern.search(name):
                if el.system != alias.system:
                    el.system = alias.system
                    el.system_source = ALIAS_SOURCE
                    applied += 1
                    by_system[alias.system] = by_system.get(alias.system, 0) + 1
                break
    return {"applied": applied, "by_system": by_system, "aliases": len(aliases)}


def unreachable_rules(rules: list[Any], elements: list[Any]) -> list[dict[str, str]]:
    """Rules whose systems do not appear in this model, and so can never fire.

    This is the visible form of the silence described in the module docstring. A
    rule listed here is not a rule that was satisfied -- it is a rule that was
    never consulted, and the difference matters to anyone reading a clean report.
    """
    present = {getattr(e, "system", None) for e in elements}
    present.discard(None)

    out = []
    for rule in rules:
        sides = [rule.system_a, rule.system_b]
        missing = [s for s in sides if s != "*" and s not in present]
        if missing:
            out.append(
                {
                    "rule_id": rule.rule_id,
                    "systems": f"{rule.system_a} <-> {rule.system_b}",
                    "absent_from_model": ", ".join(missing),
                    "consequence": "this rule was never applied to any pair",
                }
            )
    return out


def applicable_rules(rules: list[Any], elements: list[Any]) -> list[Any]:
    """The rules that can actually fire against this model.

    Used to size the pre-filter pad. Padding for a rule whose systems are absent
    widens every bounding box for a comparison that cannot happen: on the
    Schependomlaan fixture the 5 m building-to-gas rule -- with no gas and no
    building fabric in the model -- took the admitted pair count from 5,204 to
    185,401, every one of the extra 180,197 an exact boolean test that could
    only ever return "clear".
    """
    unreachable = {r["rule_id"] for r in unreachable_rules(rules, elements)}
    return [r for r in rules if r.rule_id not in unreachable]
