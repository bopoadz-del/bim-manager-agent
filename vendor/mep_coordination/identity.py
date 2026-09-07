"""identity -- the one place an element, or a pair of elements, becomes a string key.

WHY THIS EXISTS
This kit shipped a real production bug: clash_triage derived a clash id as
"A__B" while version_diff derived "A::B" for the exact same pair of elements.
Both modules' unit tests passed -- each one only ever checked its own output
against itself, so each was internally consistent and mutually wrong.

The failure surfaced three blocks downstream, in score_proposals: a proposal
names the clash_id it targets, diff_versions reports which clash_ids moved
into "resolved", and score_proposals matches the two sets. Because "A__B"
never equals "A::B", that match never fired -- not once, for any proposal,
ever. There was no exception, no warning, nothing in a log to grep for. The
score simply sat at 0.0 permanently, and 0.0 is a perfectly plausible number
for "nothing got resolved yet" -- so nobody looked twice. A silent, durable,
plausible-looking zero is the worst shape a bug can take, because every
signal that would normally catch it (a crash, a red test, an obviously wrong
number) is absent by construction.

One shared identity module makes the whole CLASS of bug unrepresentable, not
just this one instance of it: there is exactly one function that turns a
pair of elements into a clash id, both blocks import it instead of
re-deriving it, and a future third block gets the same guarantee for free.
clash_triage.clash_id_for and version_diff._clash_id already produce this
exact form today ("::".join of the sorted pair) -- test_identity.py imports
both of them alongside this module and asserts all three agree on the same
pair. That assertion is the regression pin: it fails the instant either
module's derivation drifts from this one again.

READS   nothing -- pure functions over ids already in memory.
WRITES  nothing.
NEVER   returns a blank or None key. An id that cannot be determined is a
        reason to raise, not a reason to substitute "" and let two
        differently-broken elements collapse into the same bucket.
"""
from __future__ import annotations

from typing import Any


def element_key(element_or_id: Any) -> str:
    """Canonical string id for one element.

    Accepts either a raw id string, or an object carrying a ``global_id``
    attribute (an ifc_loader.Element, a QueueItem's referenced element, a
    test double using the same field name) -- so a caller never has to know
    or check which representation the value in hand happens to be.

    Whitespace is stripped. An id that has passed through a CSV column, a
    copy-paste into a report, or a round trip through a shell argument can
    pick up a trailing space that a human reading it never notices but an
    equality check absolutely does -- an id with an invisible passenger no
    longer matches its clean counterpart, and two clashes that should have
    been recognised as the same one quietly aren't.

    Raises ValueError on an empty, whitespace-only, or missing id --
    deliberately, rather than returning "". A blank key does not merely fail
    to identify one element; it identifies EVERY element with no usable id
    as the same element, merging their clashes into one bucket. That is a
    worse outcome than a crash: the crash is loud and points at the row
    that caused it, while the merged bucket is quiet and surfaces, if it
    surfaces at all, as a wrong answer several blocks downstream -- exactly
    the shape of failure this module exists to close off.
    """
    if element_or_id is None:
        raise ValueError(
            "element_key() got None -- an element with no id cannot be keyed"
        )

    if isinstance(element_or_id, str):
        raw: str | None = element_or_id
    else:
        raw = getattr(element_or_id, "global_id", None)
        if raw is None:
            raise ValueError(
                "element_key() got an object with no usable 'global_id' "
                "attribute -- cannot derive a key from it"
            )

    key = raw.strip()
    if not key:
        raise ValueError(
            "element_key() got an empty or whitespace-only id -- refusing "
            "to return a blank key, which would silently merge distinct "
            "elements into one identity"
        )
    return key


def pair_key(a: Any, b: Any) -> tuple[str, str]:
    """The unordered identity of a clash between two elements.

    A clash between A and B IS the same clash as one between B and A -- the
    two elements do not have an inherent order, and nothing guarantees two
    independent passes over a model (a re-export, a different iteration
    order, two different blocks) will record which one is "element_a" the
    same way twice. Sorting the pair collapses both spellings to one key.

    Both inputs go through element_key() first, so a caller may pass raw id
    strings, element objects, or a mixture of the two, and get the same
    result either way.
    """
    ka, kb = element_key(a), element_key(b)
    return (ka, kb) if ka <= kb else (kb, ka)


def clash_id(a: Any, b: Any) -> str:
    """THE canonical clash identifier: ``"::".join(pair_key(a, b))``.

    This exact separator, and no other, is the form clash_triage.clash_id_for
    and version_diff._clash_id must both produce -- they independently
    derived "A__B" and "A::B" respectively for the incident described in the
    module docstring, and every unit test in both modules passed because
    neither test suite ever ran the other module's output through its own
    matcher. test_identity.py imports both functions directly and asserts
    all three agree on the same pair; that comparison is the regression pin
    for this exact incident, not a restatement of the mechanics above.
    """
    return "::".join(pair_key(a, b))
