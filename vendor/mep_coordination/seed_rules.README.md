# Where seed_rules.json comes from, and why it only has three rules

`seed_rules.json` ships with exactly three rules. Every one of them is
retrieval-sourced from a real, named drawing -- nothing in this file was
typed in from memory or general knowledge of clearance practice.

## The source

  * **Document:** `DD-2023-118_DG2 Infra P1_Vol 3 – Drawings (3 of 7).pdf`
  * **Drawing:** `IP-INF-053-0000-JCB-DWG-LP-600-0000002 A`
  * **Location on the drawing:** the `NOTES` block, items 4, 5 and 6
  * **Retrieval chunk:** chunk_index 628
  * **text_hash:** `2d085ef2123b39a9` (hash of that chunk's exact text, shared
    by all three rules below since all three notes were retrieved together
    as one chunk)

Verbatim text retrieved:

    4. PROXIMITY DISTANCE FROM BUILDING TO PE GAS MAINS IS 5.0M
    5. MINIMUM CLEARANCE BETWEEN GAS MAINS AND LOW VOLTAGE ELECTRICAL
       UTILITIES 400MM IN ANY DIRECTION.
    6. MINIMUM CLEARANCE BETWEEN GAS MAINS AND ANY OTHER UTILITIES 300MM
       IN ANY DIRECTION.

## The three rules, and why there are only three

| rule_id             | pair                          | min_gap_mm | clause         |
|----------------------|-------------------------------|-----------:|----------------|
| `MEP-GAS-LV-400`     | gas_main / electrical_lv      | 400        | NOTES item 5   |
| `MEP-GAS-ANY-300`    | gas_main / `*` (any utility)  | 300        | NOTES item 6   |
| `MEP-GAS-BLDG-5000`  | gas_main / building           | 5000       | NOTES item 4   |

These are transcribed exactly as retrieved -- no rounding, no unit
conversion beyond metres-to-millimetres as the note itself states ("5.0M" ->
5000mm), and no extrapolation to systems the notes do not name. `NOTES item
6` ("gas mains and any other utilities") is deliberately seeded as the
wildcard pair `gas_main` / `*`: the note itself is general, so narrowing it
to a specific system pair would be inventing a specificity the source text
does not have. `NOTES item 5` narrows only the one pair the source text
itself narrows (gas mains vs. low-voltage electrical), and because it names
that pair exactly, `find_applicable_rule()` in `clearance_rules.py` prefers
it over the item-6 wildcard for a gas/LV finding -- see
`test_the_specific_gas_lv_seed_rule_beats_the_wildcard_gas_seed_rule` in
`test_clearance_rules.py`, which exists specifically to prove that ordering
holds, because getting it backwards would silently apply 300mm where the
drawing requires 400mm.

All three are `precedence: "project_spec"`, because that is what they are:
requirements stated on the project's own infrastructure drawing, not a
clause from a referenced code. Nothing here claims to be a code minimum.

## What is still NOT seeded, and why

No rules derived from SBC 501, SBC 701, NFPA, or ASHRAE are seeded. This
authoring session had retrieval access to the project drawing above, and
only to that drawing -- no code book text was retrieved, so no `clause` or
`text_hash` exists for any code-derived clearance number. Per the invariant
this block enforces ("a rule without a clause is not a rule"), inventing one
to fill the gap would recreate the exact failure `load_rules()` exists to
refuse. If and when a code clause is actually retrieved with citable text,
it belongs here as a `precedence: "code"` rule -- until then, absence here
means "not yet sourced," not "does not apply."

## The invariant still holds

Nothing about this update loosens `load_rules()`. Any rule added to this
file in the future -- from this drawing, another drawing, or a code book --
still needs a real `source.clause` and `source.text_hash` or it is refused
at load with `RuleWithoutCitation`, naming the offending `rule_id`. This
file has three rules because three rules were actually sourced, not because
three is special; it stays empty of anything else until something else is.
