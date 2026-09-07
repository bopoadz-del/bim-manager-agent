"""clearance_rules -- sourced minimum-gap rules, and the violations they judge.

WHY THIS EXISTS
geometry_engine (block 1) measures distance. It does not know what distance
is required -- that number has to come from somewhere a person can check,
because a clearance requirement that cannot be traced back to a clause is
just a plausible-sounding number, and a coordination report built on
plausible-sounding numbers is a liability, not a finding.

THE CORE INVARIANT: A RULE WITHOUT A CLAUSE IS NOT A RULE.
Every rule this module accepts must cite ``source.clause`` and
``source.text_hash`` (see ``clearance_rule.schema.json``). ``load_rules()``
refuses -- loudly, by raising ``RuleWithoutCitation`` -- any rule missing
either one. It does not warn and continue, and it does not drop the rule
silently and keep going: a coordination pass that quietly ignored an
uncited rule would look complete while missing exactly the input someone
forgot to source. Refusing to load is the only response that cannot be
mistaken for success. See ``test_mutation_probe_...`` in
``test_clearance_rules.py`` for the proof that this guard is load-bearing.

PRECEDENCE, AND WHY IT IS ASYMMETRIC
Two rules can govern the same system pair: a code minimum and a
project-specific requirement. ``resolve_precedence()`` lets a project rule
win ONLY when it is STRICTER (a larger ``min_gap_mm``) than the code rule it
would replace. A looser project rule never overrides a code minimum. This
is not a coin-flip design choice -- a code minimum is a legal floor. A
project spec is free to demand more than the law requires (tighter routing,
more maintenance access, whatever the client wants), but it cannot demand
less: it has no authority to relax someone else's statutory minimum. If a
looser project rule were allowed to win, an unremarkable typo in a project
spec -- or a well-meaning value-engineering pass -- could silently legalise
a violation. The asymmetry in ``resolve_precedence()`` is what stops that.

READS   a rule source: a JSON file path, or an already-loaded list of rule
        dicts (e.g. handed in by a RAG ingestion step that just extracted
        them from a project specification). See ``seed_rules.README.md`` for
        exactly which rules ship in ``seed_rules.json``, where each one was
        retrieved from, and why nothing beyond those is seeded.
WRITES  nothing. This block is pure judgment over data already in memory.
NEVER   invents a clearance value or a citation. Every rule in
        ``seed_rules.json`` carries a real ``source.clause`` and
        ``source.text_hash`` traceable to an actual retrieved drawing note --
        none were typed in from memory or general clearance practice, and
        none will be added later without the same standard of citation.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

# Kinds of geometry_engine.Finding this block will actually judge. A hard
# clash already has a verdict (block 1 decided it, and cites no rule); this
# block only adjudicates pairs that did NOT touch.
JUDGEABLE_KINDS = ("clear", "clearance")

VALID_AXES = ("any", "vertical", "horizontal")
VALID_PRECEDENCE = ("code", "project_spec")

WILDCARD = "*"


class RuleWithoutCitation(Exception):
    """Raised by load_rules() when a rule has no source.clause or no
    source.text_hash.

    This is deliberately a hard failure, not a warning. A rule with no
    citation is not a degraded rule -- it is not a rule at all, and letting
    it load with a warning would mean every downstream consumer has to
    re-derive that distinction for itself. One refusal, at the one place
    rules enter the system, is what makes the invariant actually hold.
    """

    def __init__(self, rule_id: Any):
        self.rule_id = rule_id
        super().__init__(
            f"rule {rule_id!r} has no source.clause and/or no source.text_hash "
            "-- a rule without a clause is not a rule, refusing to load it"
        )


class InvalidRule(Exception):
    """Raised by load_rules() for a structurally malformed rule -- missing a
    required field other than the citation, or an axis/precedence value
    outside the enum. Kept distinct from RuleWithoutCitation so a caller can
    tell "this rule is broken" apart from "this rule is uncited", since the
    order specifically calls out the citation failure as the one to name and
    surface loudly.
    """


@dataclass(frozen=True)
class RuleSource:
    doc: str
    clause: str
    text_hash: str


@dataclass(frozen=True)
class Rule:
    """One sourced minimum-gap requirement between two system categories.

    ``system_a``/``system_b`` may be the wildcard "*", meaning "any system".
    Matching against a finding is unordered -- a rule for (a, b) also
    matches a finding reported as (b, a), because a Finding's element_a /
    element_b order reflects IFC traversal order, not any semantic ranking
    of the two systems.
    """

    rule_id: str
    system_a: str
    system_b: str
    min_gap_mm: float
    axis: str
    source: RuleSource
    precedence: str

    def specificity(self) -> int:
        """Lower is more specific. Used to prefer an exact-pair rule over a
        wildcard one before precedence is even considered -- a rule written
        for THIS pair should beat a generic fallback regardless of which of
        the two is code or project_spec."""
        return (self.system_a == WILDCARD) + (self.system_b == WILDCARD)

    def matches_pair(self, category_a: str | None, category_b: str | None) -> bool:
        a = category_a if category_a is not None else WILDCARD
        b = category_b if category_b is not None else WILDCARD

        def side_matches(rule_side: str, value: str) -> bool:
            return rule_side == WILDCARD or rule_side == value

        forward = side_matches(self.system_a, a) and side_matches(self.system_b, b)
        backward = side_matches(self.system_a, b) and side_matches(self.system_b, a)
        return forward or backward

    def matches_axis(self, finding_axis: str) -> bool:
        """A vertical-only (or horizontal-only) rule must not fire on
        separation measured along the other axis. When the finding's own
        axis is unknown ("any"), only an axis-agnostic rule may apply --
        applying an axis-specific rule to an unknown axis would be guessing
        which direction the gap was measured in, which this block will not
        do any more than it will guess a clearance value."""
        if self.axis == "any":
            return True
        return finding_axis == self.axis


def _require(d: dict, key: str, rule_id: Any) -> Any:
    if key not in d:
        raise InvalidRule(f"rule {rule_id!r} is missing required field {key!r}")
    return d[key]


def _parse_rule(raw: dict) -> Rule:
    rule_id = raw.get("rule_id", "<unknown>")

    source_raw = raw.get("source") or {}
    clause = source_raw.get("clause")
    text_hash = source_raw.get("text_hash")
    # THE hard requirement. Checked before anything else about the rule is
    # validated, because no amount of correctness elsewhere makes an uncited
    # rule acceptable -- this is the one check that must never be skippable.
    if not clause or not text_hash:
        raise RuleWithoutCitation(rule_id)
    doc = source_raw.get("doc")
    if not doc:
        raise InvalidRule(f"rule {rule_id!r} source is missing 'doc'")

    system_a = _require(raw, "system_a", rule_id)
    system_b = _require(raw, "system_b", rule_id)
    min_gap_mm = _require(raw, "min_gap_mm", rule_id)
    axis = raw.get("axis", "any")
    precedence = _require(raw, "precedence", rule_id)

    if axis not in VALID_AXES:
        raise InvalidRule(f"rule {rule_id!r} has invalid axis {axis!r}; must be one of {VALID_AXES}")
    if precedence not in VALID_PRECEDENCE:
        raise InvalidRule(
            f"rule {rule_id!r} has invalid precedence {precedence!r}; must be one of {VALID_PRECEDENCE}"
        )
    try:
        min_gap_mm = float(min_gap_mm)
    except (TypeError, ValueError) as exc:
        raise InvalidRule(f"rule {rule_id!r} has non-numeric min_gap_mm {min_gap_mm!r}") from exc
    if min_gap_mm <= 0:
        raise InvalidRule(f"rule {rule_id!r} has non-positive min_gap_mm {min_gap_mm!r}")

    return Rule(
        rule_id=str(rule_id),
        system_a=str(system_a),
        system_b=str(system_b),
        min_gap_mm=min_gap_mm,
        axis=axis,
        source=RuleSource(doc=str(doc), clause=str(clause), text_hash=str(text_hash)),
        precedence=precedence,
    )


def load_rules(path_or_list: str | Path | Iterable[dict]) -> list[Rule]:
    """Load and validate clearance rules.

    ``path_or_list`` is either a path to a JSON file holding a list of rule
    dicts (e.g. seed_rules.json, or a file a RAG ingestion step just wrote),
    or an already-loaded iterable of rule dicts (e.g. handed in-process by
    that same ingestion step without touching disk).

    HARD REQUIREMENT: any rule missing ``source.clause`` or
    ``source.text_hash`` raises ``RuleWithoutCitation`` naming the
    ``rule_id`` -- immediately, not after the rest of the file has loaded.
    This function does not have a "skip bad rules and continue" mode: that
    mode is exactly the silent-drop behaviour the order forbids.
    """
    if isinstance(path_or_list, (str, Path)):
        text = Path(path_or_list).read_text(encoding="utf-8")
        raw_rules = json.loads(text)
    else:
        raw_rules = list(path_or_list)

    if not isinstance(raw_rules, list):
        raise InvalidRule("rule source must be a JSON array of rule objects")

    return [_parse_rule(raw) for raw in raw_rules]


def resolve_precedence(candidates: Sequence[Rule]) -> Rule | None:
    """Pick the single rule that governs, from a set of rules that all match
    the same system pair (at the same specificity -- see the caller).

    THE ASYMMETRY, encoded directly rather than left implicit in a sort key:
    a project_spec rule beats a code rule ONLY when it is STRICTER (larger
    min_gap_mm). A looser project_spec rule loses to the code rule it would
    otherwise have replaced. Why not "most recent wins" or "project always
    wins" (the usual override patterns)? Because a code minimum is a floor
    a project has no authority to lower -- only to raise. Encoding that as a
    plain comparison (rather than e.g. a precedence-order list) keeps the
    one safety property this function exists for impossible to lose in a
    future refactor: whichever rule is strictest always wins over a code
    rule; a project rule only ever wins by being at least as strict.
    """
    if not candidates:
        return None

    code_rules = [r for r in candidates if r.precedence == "code"]
    project_rules = [r for r in candidates if r.precedence == "project_spec"]

    strictest_code = max(code_rules, key=lambda r: r.min_gap_mm, default=None)
    strictest_project = max(project_rules, key=lambda r: r.min_gap_mm, default=None)

    if strictest_code is None:
        return strictest_project
    if strictest_project is None:
        return strictest_code

    # The asymmetry: project only wins by being STRICTER. Equal or looser
    # and the code minimum stands -- a project rule cannot even tie its way
    # into overriding a legal minimum, only exceed it.
    if strictest_project.min_gap_mm > strictest_code.min_gap_mm:
        return strictest_project
    return strictest_code


def find_applicable_rule(rules: Sequence[Rule], category_a: str | None, category_b: str | None, axis: str) -> Rule | None:
    """Find the rule that governs one system pair on one axis.

    Exact-pair rules are considered before wildcard rules (specificity beats
    genericity), and only within the most specific tier that has any match
    is precedence resolved -- a wildcard project rule should not out-rank an
    exact-pair code rule just by being stricter; it never gets the chance to
    compete against it.
    """
    axis_ok = [r for r in rules if r.matches_pair(category_a, category_b) and r.matches_axis(axis)]
    if not axis_ok:
        return None

    best_specificity = min(r.specificity() for r in axis_ok)
    tier = [r for r in axis_ok if r.specificity() == best_specificity]
    return resolve_precedence(tier)


@dataclass(frozen=True)
class Violation:
    """One clearance breach: a Finding whose measured distance fell short of
    the rule that governs it. Carries the rule_id and the clause text so a
    reader never has to take the violation's word for the requirement --
    they can go read the clause themselves."""

    element_a: str
    element_b: str
    category_a: str | None
    category_b: str | None
    distance_mm: float
    required_min_gap_mm: float
    rule_id: str
    source_doc: str
    source_clause: str
    axis: str


def evaluate(findings: Iterable[Any], rules: Sequence[Rule]) -> list[Violation]:
    """Judge every clearance-relevant Finding against the loaded rules.

    Only findings with kind "clear" or "clearance" are judged -- a "clash"
    already has its own verdict from geometry_engine and cites no rule (two
    interpenetrating solids are a clash under any rule), and "unjudged"
    findings have no reliable distance to compare. A finding is included
    here on ``distance_m`` alone; whatever kind geometry_engine already gave
    it is re-checked against the CURRENT rule set, because rules loaded from
    a project spec may be stricter than whatever produced the finding in
    the first place -- re-judging is how a tightened project rule turns a
    previously-"clear" finding into a violation.

    ``axis`` is read from the finding via getattr(), defaulting to "any",
    because geometry_engine.Finding (block 1) does not carry an axis field.
    This block treats an unknown axis as ineligible for axis-specific rules
    rather than guessing -- see Rule.matches_axis.
    """
    violations: list[Violation] = []
    for f in findings:
        kind = getattr(f, "kind", None)
        if kind not in JUDGEABLE_KINDS:
            continue
        distance_m = getattr(f, "distance_m", None)
        if distance_m is None:
            continue

        category_a = getattr(f, "category_a", None)
        category_b = getattr(f, "category_b", None)
        axis = getattr(f, "axis", None) or "any"

        rule = find_applicable_rule(rules, category_a, category_b, axis)
        if rule is None:
            continue

        distance_mm = distance_m * 1000.0
        if distance_mm < rule.min_gap_mm:
            violations.append(
                Violation(
                    element_a=getattr(f, "element_a", None),
                    element_b=getattr(f, "element_b", None),
                    category_a=category_a,
                    category_b=category_b,
                    distance_mm=distance_mm,
                    required_min_gap_mm=rule.min_gap_mm,
                    rule_id=rule.rule_id,
                    source_doc=rule.source.doc,
                    source_clause=rule.source.clause,
                    axis=axis,
                )
            )

    return violations
