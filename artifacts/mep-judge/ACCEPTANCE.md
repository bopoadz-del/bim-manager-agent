# mep-judge — acceptance

Every number below was produced by `pytest tests/acceptance` and written to
`artifacts/mep-judge/evidence/*.json` by the test that measured it. Nothing here
was typed by hand. Re-run the suite and the files regenerate; if a number in this
document and the matching JSON disagree, the JSON is right.

No mocks anywhere in this suite. The geometry engine, the three monitors and the
model store are the production ones, and every model is a real IFC file parsed by
the same loader production uses.

| | |
|---|---|
| tests | **107 passed**, 0 failed (locally on 3.11; CI on **3.12.14**) |
| mutation probes | **8 killed / 8**, 0 survivors (+1 control) |
| coverage, `app/agents` + `app/monitors` | **93.8%** statement, **91%** branch |
| ruff | clean |
| mypy | clean, 38 files |
| placeholders | **0** |
| vendored kit | pinned at `5c0711a066b6eef5e0861cbfa0d793972b719c83`, 15 files, verified |

---

## A1 — Schependomlaan through the full pipeline ✅

`schependomlaan_design.ifc`, 47 MB, IFC2X3, public. 129 s end to end.

| | |
|---|---|
| elements meshed | 1,437 |
| pairs possible | 1,031,766 |
| **pairs admitted by the pre-filter** | **80** |
| hard clashes | 7 |
| joints excluded by connectivity | 2 |
| zones created | 11 |
| zones reaching `awaiting_review` | 8 |
| **proposals verified by all three monitors** | **7** |
| boundary-owned clashes | 7 |

The queue was worked in priority order, congestion first:

| zone | priority | elements | status |
|---|---|---|---|
| `00 begane grond\|-1_2+16` | 0.671 | 359 | awaiting_review |
| `01 eerste verdieping\|-1_2+16` | 0.522 | 304 | awaiting_review |
| `02 tweede verdieping\|0_0+14` | 0.396 | 255 | awaiting_review |
| `03 derde verdieping\|0_0+11` | 0.178 | 178 | awaiting_review |

**Read the 80 honestly.** It is 80 and not 5,204 because a pair is only admitted
when at least one side is MEP: this model is 1,364 structural elements to 73 MEP,
and wall-meets-slab is a structural coordination question, not this product's.

**And read the zero clearance findings more honestly still.** Every rule in the
seed table is a gas rule, this model contains no gas main, and so **all three
rules were never applied to a single pair**. The run therefore reports hard
clashes only. That is recorded in `model_version.stats.rules_never_applied` and
logged as a warning on every ingest, because a clean clearance report from a
check that never ran is the most dangerous output this system could produce. See
RUNLOG **F1**.

## A2 — boundary trap ✅

A duct interpenetrating a wall stub by 300 mm, in a zone whose every other
direction is filled by a lattice of duct. The only move that separates the pair
runs +X, into zone B.

| | |
|---|---|
| monitored attempts | 3 (the cap) |
| **BoundaryMonitor objections** | **1** |
| outcome | `escalated`, with all three attempts and their objections recorded |

> `nothing_pushed_into_neighbour: move creates 2 finding(s) in neighbouring zones Level 00|0_-1, Level 00|1_0`

The ledger holds the sequence: `open → escalated`, with each rejected vector and
the monitor that refused it stored as its own `proposal` row.

## A3 — gravity trap ✅

Two independent guards, tested separately, because a backstop nobody tests is a
comment.

| | |
|---|---|
| candidates B4 offered for a gravity element | 32 |
| **of those, with any vertical component** | **0** |
| IntegrityMonitor on a forced +250 mm vertical move | **FAIL** |
| the same 250 mm applied laterally | PASS |

> `move changes the invert of a gravity element by 250 mm; a drain that runs uphill does not drain`

## A4 — unsourced trap ✅

| | |
|---|---|
| with the full table | `MEP-GAS-LV-400`, clearance, 50 mm measured against 400 mm required |
| rules removed | `MEP-GAS-LV-400`, `MEP-GAS-ANY-300` |
| clearance findings after removal | **0** |
| resolver verdict on an unsourced clearance | **`flagged_unsourced`**, move vector `(0, 0, 0)` |

Removing only the 400 mm rule was not enough, and finding that out is part of the
result: the 300 mm gas-to-anything wildcard still governs the pair. A table with
one rule removed is not a table with no rule.

## A5 — rebase ✅

Zone `Level 00|1_0` verified a 375 mm move. Zone `Level 00|0_0` then committed a
move that walks its own element onto the space the first zone had claimed.

| | |
|---|---|
| clash state path | `open → verified → proposed` |
| verified move | `(-375, 0, 0)` mm |
| neighbour's commit | `(-1875, 0, 0)` mm |
| zones rebased | `Level 00|0_0` |

> `boundary: no_neighbour_finding_worsened: move tightens 1 finding(s) across the boundary`

The monitors were genuinely re-run against the new branch heads. A proposal that
still held would have kept its verdict; this one no longer holds, so it went back.

## A6 — the original model is never written to ✅

| | |
|---|---|
| sha256 before | `30cc8484c103c25f867b…` |
| sha256 after a full run | **identical** |
| file size | unchanged |
| shown in the review package | yes, and in the UI header |

## A7 — version diff ✅

Two versions of one model with the same GlobalIds, one planted fix and one
planted regression.

| | |
|---|---|
| resolved | **1** |
| regressed | **1** |
| new | 0 |
| persisting | 0 |
| proposal score | 1 proposed, 1 resolved, rate **1.0** |

`regressed` is the one that is hard to earn: the pair had to be *measured clear*
in v1, not merely absent. A pair nobody looked at is `new`, and the difference
between "we checked it and it was fine" and "we never checked" is the difference
this bucket exists to preserve.

## A8 — deployment ✅ (the Render deploy itself is still owner-gated)

| | |
|---|---|
| `/health` returns 200 with the build sha | ✅ verified locally |
| database, store backend and reason reported | ✅ `local`, "no Speckle token configured" |
| vendored kit pin reported at `/health` | ✅ `5c0711a0…`, 15 files |
| exact geometry backend reported | ✅ true |
| image builds from a clean checkout | ✅ **verified in CI** |
| container serves `/health` with `build_sha == GITHUB_SHA` | ✅ **verified in CI** |
| Render blueprint deploys | ⛔ **owner-gated — needs a Render account** |

Docker is not installed on the build machine, so the container half was verified
where it could be: https://github.com/bopoadz-del/mep-judge/actions/runs/34109449830. Everything except the Render deploy is proven.

## A9 — coverage and mutation ✅

| | |
|---|---|
| coverage, agents + monitors | **93.8%** statement, 91% branch (floor: 85%) |
| mutation probes | **8 / 8 killed**, 0 survivors |
| control probe | behaved (suite stayed green on an inert edit) |

The probes are not decoration. The first run had **three survivors** — the retry
cap, ledger ordering, and vendored-kit drift were all breakable without a single
test noticing. `tests/unit/test_guards.py` exists entirely to close them, and
each test there says which probe it answers.

---

## What is not proven

- **No clearance rule has ever fired on a real building model here.** Both public
  fixtures lack the systems the seed rules name. The clearance path is exercised
  by constructed fixtures only.
- **Connectivity is unprovable on every real model tested.** Neither public model
  carries `IfcDistributionPort`. `connected_trap.ifc` exists to exercise the code
  that runs when ports are present; no real export has yet exercised it.
- **No access-zone table has been supplied by a real project**, so that check
  reports `unprovable` outside its own fixture. The fixture table is labelled as
  having no authority, in the file itself.
- **Speckle has never been exercised against a live server.** The local store is
  a real backend and is what every number above was produced with.
