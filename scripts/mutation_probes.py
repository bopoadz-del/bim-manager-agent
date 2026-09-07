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

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


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
        find="            all_passed = all_passed and passed",
        replace="            all_passed = True  # MUTANT",
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
        name="rules_load_without_a_citation",
        target="app/agents/coordinator.py",
        find="    rules = list(clearance_rules.load_rules(str(KIT_SEED_RULES)))",
        replace="    rules = list(clearance_rules.load_rules(str(KIT_SEED_RULES)))  # MUTANT",
        tests=["tests/acceptance/test_acceptance.py::test_A4_a_move_that_needs_a_stripped_rule_is_flagged_not_proposed"],
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
CONTROLS = {"rules_load_without_a_citation"}


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
    survivors, controls_failed, killed = [], [], []

    for probe in PROBES:
        source = probe.path.read_text(encoding="utf-8")
        if probe.find not in source:
            print(f"SKIP  {probe.name}: anchor not found in {probe.target}")
            survivors.append(f"{probe.name} (anchor missing -- probe is stale)")
            continue

        probe.path.write_text(source.replace(probe.find, probe.replace, 1), encoding="utf-8")
        try:
            passed, tail = run_tests(probe.tests)
        finally:
            probe.path.write_text(source, encoding="utf-8")

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
