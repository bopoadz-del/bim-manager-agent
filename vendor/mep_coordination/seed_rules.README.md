# Where seed_rules.json comes from, and why it only has three rules

`seed_rules.json` ships with exactly three rules. Every one of them is
retrieval-sourced from a real project drawing — nothing in this file was typed in
from memory or from general knowledge of clearance practice.

## The source

The three rules come from the `NOTES` block of a project infrastructure
utilities drawing, items 4, 5 and 6, retrieved together as a single chunk.

| | |
|---|---|
| **Document id** | `PROJECT-UTILITIES-DRAWING-001` |
| **Clauses** | `NOTES` items 4, 5 and 6 |
| **Retrieval chunk** | chunk_index 628 |
| **text_hash** | `2d085ef2123b39a9` — the hash of that chunk's exact text, shared by all three rules because all three notes were retrieved together |

**The document is referenced by id, not by name.** The drawing it came from is a
client project deliverable, and this repository does not carry client document
names, drawing numbers or revisions. The mapping from
`PROJECT-UTILITIES-DRAWING-001` to the actual drawing lives with the project, not
in git.

`text_hash` is what makes that safe rather than lossy: it pins the exact source
text these three numbers were read from, so an engineer with access to the
project can verify the transcription against the drawing without this file ever
naming it.

## The three rules, and why there are only three

| rule_id | pair | min_gap_mm | clause |
|---|---|---:|---|
| `MEP-GAS-LV-400` | gas_main / electrical_lv | 400 | NOTES item 5 |
| `MEP-GAS-ANY-300` | gas_main / `*` (any utility) | 300 | NOTES item 6 |
| `MEP-GAS-BLDG-5000` | gas_main / building | 5000 | NOTES item 4 |

These are transcribed exactly as retrieved — no rounding, no unit conversion
beyond the metres-to-millimetres the note itself states, and no extrapolation to
systems the notes do not name.

`NOTES item 6` covers gas mains against any other utility, so it is seeded as the
wildcard pair `gas_main` / `*`: the note is general, and narrowing it to a
specific system pair would invent a specificity the source text does not have.
`NOTES item 5` narrows only the one pair the source text itself narrows, and
because it names that pair exactly, `find_applicable_rule()` prefers it over the
item-6 wildcard for a gas/LV finding — see
`test_the_specific_gas_lv_seed_rule_beats_the_wildcard_gas_seed_rule`, which
exists to prove that ordering holds, because getting it backwards would silently
apply 300 mm where the drawing requires 400 mm.

All three are `precedence: "project_spec"`, because that is what they are:
requirements stated on a project's own drawing, not a clause from a referenced
code. Nothing here claims to be a code minimum.

## What is still NOT seeded, and why

No rules derived from SBC 501, SBC 701, NFPA or ASHRAE are seeded. The authoring
session had retrieval access to the project drawing above and to nothing else —
no code book text was retrieved, so no `clause` or `text_hash` exists for any
code-derived clearance number. Per the invariant this block enforces — a rule
without a clause is not a rule — inventing one to fill the gap would recreate the
exact failure `load_rules()` exists to refuse.

If and when a code clause is actually retrieved with citable text, it belongs
here as a `precedence: "code"` rule. Until then, absence here means "not yet
sourced", not "does not apply".

## The invariant still holds

Nothing about this file loosens `load_rules()`. Any rule added in future — from
this drawing, another drawing, or a code book — still needs a real
`source.clause` and `source.text_hash`, or it is refused at load with
`RuleWithoutCitation` naming the offending `rule_id`. This file has three rules
because three were actually sourced, not because three is special.
