"""H2 — public standards, and the scope conditions that decide what can be used.

The headline here is negative and it is the point: most published clearance
figures cannot be applied from IFC geometry alone, because their scope depends on
facts a model does not carry. These tests pin that down so the withholding cannot
quietly stop happening.
"""
from __future__ import annotations

import json

import pytest

from app.rules import (
    SLOPE_ONLY,
    load_public_rules,
    raw_rules,
    to_kit_rules,
    unverified_rule_ids,
    validate_against_kit_schema,
    verification_note,
    withheld_for_scope,
)


def test_every_record_carries_standard_edition_and_clause():
    """A rule without all three is not a rule this repository will ship."""
    for record in raw_rules():
        assert record.get("standard"), f"{record['rule_id']} has no standard"
        assert record.get("edition"), f"{record['rule_id']} has no edition"
        assert record["source"].get("clause"), f"{record['rule_id']} has no clause"
        assert record["source"].get("doc")
        assert record["source"].get("text_hash")


def test_the_kit_schema_accepts_what_we_hand_it():
    assert validate_against_kit_schema() == []


def test_rules_load_through_the_kit_loader_not_around_it():
    """They face the same citation check as every other rule."""
    loaded = load_public_rules()
    assert loaded
    for rule in loaded:
        assert rule.source.clause
        assert rule.source.text_hash


def test_slope_citations_are_not_loaded_as_separations():
    """A fall of 1/4 inch per foot is not a gap, whatever the units look like."""
    slope_ids = {r["rule_id"] for r in raw_rules() if r.get("applies_as") == SLOPE_ONLY}
    assert slope_ids, "the file should carry slope citations for the fall check"
    loaded = {r.rule_id for r in load_public_rules()}
    assert not (slope_ids & loaded)


def test_rules_whose_scope_ifc_cannot_establish_are_withheld_with_a_reason():
    withheld = withheld_for_scope()
    assert withheld, "nothing withheld means the scope gate is not running"
    for entry in withheld:
        assert entry["reason"], f"{entry['rule_id']} withheld with no reason given"
        assert entry["standard"] and entry["clause"]

    loaded = {r.rule_id for r in load_public_rules()}
    assert not (loaded & {e["rule_id"] for e in withheld})


@pytest.mark.parametrize(
    "rule_id",
    [
        "ASHRAE-62.1-2022-INTAKE-EXHAUST",
        "NEC-2023-110.26-A1-DEPTH",
        "SMACNA-DUCT-ACCESS-457",
        "NFPA13-2022-DEFLECTOR-457",
    ],
)
def test_the_over_broad_rules_are_the_ones_withheld(rule_id):
    """Each of these means something much narrower than its system pair.

    ASHRAE's 3 m is intake-to-plumbing-vent, not duct-to-drain. NEC 110.26 is
    working space at equipment likely to require examination while energized, not
    clearance around every LV element. SMACNA's 457 mm is at access doors.
    NFPA 13's 457 mm is to the top of storage. Applied to the nearest available
    system category, each becomes a requirement the standard does not impose.
    """
    assert rule_id in {e["rule_id"] for e in withheld_for_scope()}
    assert rule_id not in {r.rule_id for r in load_public_rules()}


def test_what_survives_is_expressible_without_extra_facts():
    """Sprinkler-to-wall and sprinkler-to-sprinkler need only the two elements."""
    applied = {r.rule_id for r in load_public_rules()}
    assert applied == {
        "NFPA13-2022-SPRINKLER-TO-WALL-102",
        "NFPA13-2022-SPRINKLER-SPACING-1829",
    }


def test_nothing_here_claims_to_have_been_verified():
    """The transcriptions are unchecked and the file says so on every rule."""
    assert unverified_rule_ids() == {r["rule_id"] for r in raw_rules()}
    note = verification_note()
    assert note["status"] == "unverified_transcription"
    assert "did not have the source documents open" in note["meaning"]
    assert note["scope_finding"]


def test_the_hot_surface_rule_is_absent_and_says_why():
    """It was asked for. No clause could be identified with enough confidence."""
    note = verification_note()
    assert "not_included" in note
    assert "hot-surface" in note["not_included"]
    assert not any("hot" in r["rule_id"].lower() for r in raw_rules())


def test_a_withheld_rule_can_still_be_read_by_a_human():
    """Withheld is not deleted. The citation stays so an engineer can judge it."""
    record = next(
        r for r in raw_rules() if r["rule_id"] == "ASHRAE-62.1-2022-INTAKE-EXHAUST"
    )
    assert record["quote"]
    assert record["confidence"]
    assert record["scope_note"]


def test_kit_rules_carry_the_standard_and_edition_into_source_doc():
    """The kit schema forbids extra fields, so the provenance rides in doc."""
    for rule in to_kit_rules():
        assert rule["source"]["doc"]
        assert any(ch.isdigit() for ch in rule["source"]["doc"]), (
            "the edition must survive the strip to kit shape"
        )


def test_the_file_is_valid_json_with_the_expected_shape():
    from app.rules import RULES_FILE

    payload = json.loads(RULES_FILE.read_text(encoding="utf-8"))
    assert set(payload) >= {"note", "verification", "rules"}
    assert len(payload["rules"]) >= 8
