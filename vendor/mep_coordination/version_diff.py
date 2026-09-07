"""version_diff -- compare two clash-detection runs against the same model.

WHY THIS EXISTS
A single clash report is a snapshot. Coordination is iterative: a trade
reroutes a duct, the model is re-run, and the question that actually matters
is not "what clashes exist now" but "did that reroute *work*, and did it
break something else". This block answers that by matching findings between
two runs on the pair of elements involved, then bucketing every pair into
exactly one of four outcomes.

THE UNORDERED-PAIR KEY
geometry_engine judges an element pair, and a pair has no inherent order --
"duct D1 clashes with pipe P2" and "pipe P2 clashes with duct D1" describe
the same physical interpenetration. But nothing guarantees two independent
runs (different element iteration order, a re-exported IFC, a different
pass over the model) will record element_a/element_b the same way round
both times. If this block keyed findings on the ordered pair, a clash that
is reported as (D1, P2) in v1 and (P2, D1) in v2 -- same clash, same two
elements, never actually touched -- would be read as two different pairs:
one that vanished from v1 (a false "resolved") and one that appeared from
nowhere in v2 (a false "new"). Sorting the pair before using it as a key
collapses both spellings to one identity, so the match survives the swap.
See test_the_pair_key_is_unordered_not_a_coincidence for the mutation proof.

REGRESSED VS. NEW -- A DELIBERATE DISTINCTION
Both buckets describe "this pair is a problem in v2 that it wasn't in v1",
but they carry very different weight for a coordinator. "new" means this
pair was never checked and cleared before -- it simply was not part of the
v1 record (absent, or judged "unjudged" for lack of geometry). "regressed"
means v1 *proved* the pair clear -- measured, under a rule, no violation --
and v2 now disagrees. That is not a fresh finding; it is something that
used to work and now does not, which is a much stronger signal (a
re-route nudged something else, a rule tightened, geometry changed) and
deserves separate visibility rather than being buried in "new".
"""
from __future__ import annotations

from typing import Any

# Kinds that represent an actual, live problem. "unjudged" is excluded on
# purpose: it means the geometry could not be judged at all, which is
# neither a clash/clearance nor a proof of "clear" -- treating it as either
# would either hide a real problem or manufacture a false regression.
_ACTIVE_KINDS = ("clash", "clearance")
_CLEAR_KIND = "clear"


def _pair_key(element_a: str, element_b: str) -> tuple[str, str]:
    """Sort the pair so (A, B) and (B, A) key identically.

    See the module docstring's UNORDERED-PAIR KEY section for why this is
    not cosmetic: it is the difference between correctly recognising a
    persisting clash and double-counting it as one resolved plus one new.
    """
    return tuple(sorted((element_a, element_b)))


def _clash_id(key: tuple[str, str]) -> str:
    """Stable identifier for a pair, derived from its sorted key.

    Exposed so a remediation proposal (score_proposals) can name the same
    pair a diff bucketed, without either side needing to agree on element
    ordering independently.
    """
    return "::".join(key)


def _entry(key: tuple[str, str], f1: Any | None, f2: Any | None) -> dict[str, Any]:
    return {
        "clash_id": _clash_id(key),
        "element_a": key[0],
        "element_b": key[1],
        "v1": f1.as_dict() if f1 is not None else None,
        "v2": f2.as_dict() if f2 is not None else None,
    }


def diff_versions(findings_v1: list[Any], findings_v2: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """Bucket every element pair seen in either run into one outcome.

    * resolved   -- active (clash/clearance) in v1, not active in v2
                    (v2 has it as "clear", "unjudged", or doesn't have it
                    at all -- all three mean "no longer a live problem").
    * new        -- active in v2, and v1 was NOT a proof of "clear"
                    (v1 had no entry, or v1 was "unjudged" -- either way,
                    v1 never vouched for this pair).
    * regressed  -- v1 was "clear" (a measured, judged pass) and v2 is now
                    active. Kept separate from "new" -- see module docstring.
    * persisting -- active in both v1 and v2.

    Later duplicate findings for the same pair within one run's list
    overwrite earlier ones, on the assumption that a single run judges a
    given pair once; callers passing pre-deduplicated findings get exactly
    that behaviour for free.
    """
    v1_by_key = {_pair_key(f.element_a, f.element_b): f for f in findings_v1}
    v2_by_key = {_pair_key(f.element_a, f.element_b): f for f in findings_v2}

    result: dict[str, list[dict[str, Any]]] = {
        "new": [], "resolved": [], "regressed": [], "persisting": [],
    }

    for key in sorted(set(v1_by_key) | set(v2_by_key)):
        f1 = v1_by_key.get(key)
        f2 = v2_by_key.get(key)
        active1 = f1 is not None and f1.kind in _ACTIVE_KINDS
        active2 = f2 is not None and f2.kind in _ACTIVE_KINDS
        was_clear = f1 is not None and f1.kind == _CLEAR_KIND

        entry = _entry(key, f1, f2)

        if active1 and active2:
            result["persisting"].append(entry)
        elif active1 and not active2:
            result["resolved"].append(entry)
        elif active2 and not active1:
            if was_clear:
                result["regressed"].append(entry)
            else:
                result["new"].append(entry)
        # else: inactive in both (e.g. clear->clear, or absent->unjudged) --
        # not a live problem in either run, so it is not reported at all.

    return result


def _proposal_clash_id(proposal: Any) -> Any:
    if isinstance(proposal, dict):
        return proposal.get("clash_id")
    return getattr(proposal, "clash_id", None)


def score_proposals(proposals: list[Any], diff: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """How many proposed fixes actually landed in the "resolved" bucket.

    A proposal names the clash_id it targets (see _clash_id); "resolved"
    means diff_versions independently confirmed that exact pair moved out
    of active status between the two runs. ``rate`` is a plain fraction --
    guarded to 0.0 when nothing was proposed rather than raising, because
    "no proposals yet" is a normal, expected state for a fresh report, not
    an error condition a caller should have to catch.
    """
    resolved_ids = {entry["clash_id"] for entry in diff["resolved"]}
    proposed = len(proposals)
    resolved = sum(1 for p in proposals if _proposal_clash_id(p) in resolved_ids)
    rate = (resolved / proposed) if proposed else 0.0
    return {"proposed": proposed, "resolved": resolved, "rate": rate}
