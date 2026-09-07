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
DEFAULT_ALIAS_SOURCE = "default_alias"

#: Shipped aliases, Dutch and English, ordered because the first match wins.
#:
#: The kit's hint list is English and abbreviation-free, which is not a criticism
#: of it -- it is a list that has to stop somewhere. But the only real building
#: model available here is Dutch, and thirteen of its seventy-three MEP elements
#: are named "vent. rooster": a ventilation grille. "ventilat" does not match the
#: abbreviation and "rooster" is not an English word, so those thirteen classify
#: as `unknown`, no rule can reach them, and the model reports less than it holds.
#:
#: Order is load-bearing. "hwa afvoer" is rainwater drainage and contains
#: "afvoer", which on its own means foul drainage -- so every storm pattern is
#: listed before every foul one. Getting that backwards would silently reclassify
#: sixty rainwater pipes as foul and change which rules govern them.
DEFAULT_ALIASES: tuple[dict[str, str], ...] = (
    # -- storm drainage first, so "hwa afvoer" does not match "afvoer" --
    {"lang": "nl", "pattern": r"\bhwa\b|hemelwater|regenwater|\brwa\b",
     "system": "drainage_storm", "note": "hemelwaterafvoer: rainwater drainage"},
    {"lang": "en", "pattern": r"rainwater|stormwater|\bstorm\b",
     "system": "drainage_storm", "note": ""},
    # -- foul drainage --
    {"lang": "nl", "pattern": r"vuilwater|sanitair|\bdwa\b|riool",
     "system": "drainage_foul", "note": "vuilwaterafvoer: foul drainage"},
    {"lang": "en", "pattern": r"\bfoul\b|\bsoil\b|\bwaste\b|sewer",
     "system": "drainage_foul", "note": ""},
    # -- ventilation. "vent." is the abbreviation the fixture actually uses. --
    {"lang": "nl", "pattern": r"\bvent\.|ventilatie|luchtbehandeling|\blucht\b|rooster",
     "system": "ventilation", "note": "rooster: grille; vent. rooster: ventilation grille"},
    {"lang": "en", "pattern": r"\bduct\b|air handling|\bahu\b|grille|louvre|louver|diffuser",
     "system": "ventilation", "note": ""},
    # -- electrical --
    {"lang": "nl", "pattern": r"elektra|kabelgoot|laagspanning|\bls\b",
     "system": "electrical_lv", "note": "kabelgoot: cable tray; laagspanning: low voltage"},
    {"lang": "en", "pattern": r"cable tray|cable basket|\blv\b|low voltage|busbar",
     "system": "electrical_lv", "note": ""},
    # -- fire --
    {"lang": "nl", "pattern": r"sprinkler|brandblus|blusleiding",
     "system": "fire_sprinkler", "note": "blusleiding: fire main"},
    {"lang": "en", "pattern": r"sprinkler|fire main|standpipe",
     "system": "fire_sprinkler", "note": ""},
    # -- condensate --
    {"lang": "nl", "pattern": r"condens", "system": "condensate", "note": ""},
    {"lang": "en", "pattern": r"condensate", "system": "condensate", "note": ""},
)


def default_aliases() -> list[SystemAlias]:
    """The shipped table, compiled. Projects prepend their own to override."""
    return [
        SystemAlias(re.compile(a["pattern"], re.IGNORECASE), a["system"], a["note"])
        for a in DEFAULT_ALIASES
    ]


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
        return default_aliases()
    p = Path(path)
    if not p.exists():
        return default_aliases()
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
    # A project table extends the shipped one rather than replacing it: a project
    # that names one system should not silently lose the other eleven.
    return out + default_aliases()


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
