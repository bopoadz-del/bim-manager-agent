"""Do the tests actually catch the failures they claim to?

A green suite proves the tests pass. It does not prove they would fail if the
code were wrong, and a test that cannot fail is a comment with a runtime cost.

Each probe below breaks one specific safety property -- commit on a single zone's
approval, ignore the retry cap, let a monitor wave everything through -- runs the
tests that are supposed to notice, and requires them to go red. A probe whose
tests stay green is a *survivor*, and a survivor is a hole in the suite, reported
by name.

Run: python scripts/mutation_probes.py
"""
from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Written while a mutation is applied, removed when it is restored. If a run is
#: killed mid-probe -- a timeout, a Ctrl-C -- the finally clause never runs and a
#: deliberately sabotaged module is left on disk. That happened once here: a
#: monitor sat with its comparison stubbed out to [], [] and the next run
#: reported the probe as a stale anchor rather than as the emergency it was.
#: The marker turns a silent sabotage into a refusal to start.
IN_FLIGHT = ROOT / ".mutation_in_flight"


@dataclass
class Probe:
    name: str
    target: str
    find: str
    replace: str
    tests: list[str]
    breaks: str

    @property
    def path(self) -> Path:
        return ROOT / self.target


PROBES = [
    Probe(
        name="arbitration_commits_on_a_single_zone_pass",
        target="app/agents/coordinator.py",
        find="        if any(v == VERDICT_FAIL for v in zone_verdicts.values()):",
        replace="        if False:  # MUTANT",
        tests=["tests/unit/test_arbitration.py"],
        breaks="a boundary move one zone rejected would be committed anyway",
    ),
    Probe(
        name="resolver_ignores_the_retry_cap",
        target="app/agents/zone_resolver.py",
        find="            if attempt >= self.max_attempts:",
        replace="            if attempt >= 10_000:  # MUTANT",
        tests=["tests/unit/test_guards.py::test_the_resolver_stops_at_the_retry_cap"],
        breaks="the resolver would grind through every candidate instead of escalating",
    ),
    Probe(
        name="geometry_monitor_ignores_new_clashes",
        target="app/monitors/geometry.py",
        find="        introduced, worsened = compare(before, after)",
        replace="        introduced, worsened = [], []  # MUTANT",
        tests=["tests/unit/test_monitors.py"],
        breaks="a move landing inside another element would be verified",
    ),
    Probe(
        name="boundary_monitor_ignores_the_neighbour",
        target="app/monitors/boundary.py",
        find="        introduced, worsened = compare(before, after)",
        replace="        introduced, worsened = [], []  # MUTANT",
        tests=["tests/acceptance/test_acceptance.py::test_A2_boundary_trap_is_caught_by_the_boundary_monitor"],
        breaks="a move that pushes an element into the next zone would be committed",
    ),
    Probe(
        name="integrity_monitor_ignores_fall",
        target="app/monitors/integrity.py",
        find="    if preserves_fall(element, ctx.vector_mm):",
        replace="    if True:  # MUTANT",
        tests=[
            "tests/acceptance/test_acceptance.py::test_A3_a_move_that_reverses_fall_is_refused",
            "tests/unit/test_monitors.py::test_integrity_refuses_a_vertical_move_on_a_gravity_element",
        ],
        breaks="a drain could be lifted and its fall reversed",
    ),
    Probe(
        name="integrity_monitor_claims_unprovable_checks_as_passed",
        target="app/monitors/base.py",
        find='        unprovable = [c.name for c in checks if c.status == UNPROVABLE]',
        replace='        unprovable = []  # MUTANT',
        tests=["tests/unit/test_monitors.py::test_unprovable_is_not_a_pass"],
        breaks="'we could not check this' would read as 'we checked and it is fine'",
    ),
    Probe(
        name="public_rules_bypass_the_citation_check",
        target="app/rules/__init__.py",
        find='        rule["source"] = {k: record["source"][k] for k in SOURCE_FIELDS}',
        replace='        rule.pop("source", None)  # MUTANT',
        tests=["tests/unit/test_public_rules.py"],
        breaks="public rules would reach the table without the clause they cite",
    ),
    Probe(
        name="scope_gate_stops_withholding",
        target="app/rules/__init__.py",
        find='        if not record.get("scope_inferable", False):',
        replace="        if False:  # MUTANT",
        tests=["tests/unit/test_public_rules.py"],
        breaks=(
            "rules whose scope IFC cannot establish would be applied anyway -- "
            "ASHRAE's intake-to-vent 3 m becoming 3 m between every duct and drain"
        ),
    ),
    Probe(
        name="inert_control",
        target="app/rules/__init__.py",
        find="RULES_FILE = Path(__file__).resolve().parent / \"public_seed_rules.json\"",
        replace="RULES_FILE = Path(__file__).resolve().parent / \"public_seed_rules.json\"  # MUTANT",
        tests=["tests/unit/test_public_rules.py"],
        breaks="(control probe: an inert edit; these tests must still pass)",
    ),
    Probe(
        name="ledger_orders_history_by_timestamp",
        target="app/ledger.py",
        find="        .order_by(LedgerEvent.seq)",
        replace="        .order_by(LedgerEvent.ts)  # MUTANT",
        tests=["tests/unit/test_guards.py::test_history_is_ordered_by_sequence_not_by_clock"],
        breaks="events written in the same microsecond come back in arbitrary order",
    ),
    Probe(
        name="conditional_is_promoted_to_verified",
        target="app/agents/zone_resolver.py",
        find="                proposal_verdict = VERDICT_CONDITIONAL",
        replace="                proposal_verdict = VERDICT_VERIFIED  # MUTANT",
        tests=["tests/unit/test_conditional_verdict.py"],
        breaks="a proposal with checks nobody could run would be reported as fully verified",
    ),
    Probe(
        name="unprovable_counts_as_a_pass_in_the_aggregate",
        target="app/monitors/base.py",
        find="    if all(r.passed for r in results.values()):",
        replace="    if all(r.acceptable for r in results.values()):  # MUTANT",
        tests=["tests/unit/test_conditional_verdict.py"],
        breaks="'we could not check this' would aggregate as 'we checked and it passed'",
    ),
    Probe(
        name="vendored_kit_drift_goes_unnoticed",
        target="scripts/vendor_kit.py",
        find="            problems.append(f\"edited in place: {name}\")",
        replace="            pass  # MUTANT",
        tests=["tests/unit/test_guards.py"],
        breaks="a hand-edited vendored block would pass CI",
    ),
]

#: Probes whose tests are expected to stay green. A control that goes red means
#: the harness itself is broken, not that the suite is good.
CONTROLS = {"inert_control"}


def restore_from_marker() -> str | None:
    """Undo a mutation left behind by a killed run. Returns what it repaired."""
    if not IN_FLIGHT.exists():
        return None
    payload = json.loads(IN_FLIGHT.read_text(encoding="utf-8"))
    target = ROOT / payload["target"]
    target.write_text(payload["source"], encoding="utf-8", newline=chr(10))
    IN_FLIGHT.unlink()
    return f"{payload['probe']} in {payload['target']}"


def run_tests(tests: list[str]) -> tuple[bool, str]:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *tests, "-q", "-x", "-p", "no:cacheprovider"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT)},
    )
    return result.returncode == 0, result.stdout[-400:]


def main() -> int:
    repaired = restore_from_marker()
    if repaired:
        print(f"RECOVERED a mutation left by a killed run: {repaired}")
        print("         Re-run to get a clean result.")
        return 1

    survivors, controls_failed, killed = [], [], []

    for probe in PROBES:
        source = probe.path.read_text(encoding="utf-8")
        if probe.find not in source:
            print(f"SKIP  {probe.name}: anchor not found in {probe.target}")
            survivors.append(f"{probe.name} (anchor missing -- probe is stale)")
            continue

        IN_FLIGHT.write_text(
            json.dumps({"probe": probe.name, "target": probe.target, "source": source}),
            encoding="utf-8",
        )
        probe.path.write_text(source.replace(probe.find, probe.replace, 1), encoding="utf-8")
        try:
            passed, tail = run_tests(probe.tests)
        finally:
            probe.path.write_text(source, encoding="utf-8")
            IN_FLIGHT.unlink(missing_ok=True)

        is_control = probe.name in CONTROLS
        if is_control:
            if passed:
                print(f"OK    {probe.name} (control: suite still green, as expected)")
            else:
                controls_failed.append(probe.name)
                print(f"BAD   {probe.name}: control probe turned the suite red\n{tail}")
            continue

        if passed:
            survivors.append(probe.name)
            print(f"ALIVE {probe.name}: tests stayed green.\n      Undetected: {probe.breaks}")
        else:
            killed.append(probe.name)
            print(f"KILL  {probe.name}: caught. ({probe.breaks})")

    real = [p for p in PROBES if p.name not in CONTROLS]
    print(f"\n{len(killed)}/{len(real)} mutants killed; {len(survivors)} survived.")
    if survivors:
        print("Survivors are holes in the suite:")
        for s in survivors:
            print(f"  - {s}")
    if controls_failed:
        print(f"Control probes misbehaved: {controls_failed}")

    return 1 if (survivors or controls_failed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
