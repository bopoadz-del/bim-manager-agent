# mep-judge — RUNLOG

Append-only. Findings first, blockers second. A finding recorded here is one the
code now handles; a blocker is one it does not, and each says who can clear it.

---

## F1 — the seed rule table cannot fire on any model, and fails silently
**Found:** running the kit's rule table against the kit's own loader for the
first time.

The loader infers an element's system from its name using a fixed hint list:
`electrical`, `drainage_storm`, `ventilation`, `potable_water`, and so on. The
seed rules are written against a different vocabulary: `gas_main`,
`electrical_lv`, `building`. There is **no hint for gas at all**. So on every
model, `find_applicable_rule` matches nothing, no finding is ever assigned a
required clearance, and the run reports a building with no clearance violations
because it never asked the question.

Nothing raises. The findings list is simply shorter, and a short findings list
reads exactly like good news.

**Handled:**
- `app/kit/systems.py` — a project-supplied alias table mapping name patterns
  onto the rule vocabulary. Naming only: **no alias can introduce, relax or
  invent a clearance distance.** Those still come from a cited rule or they do
  not exist.
- `unreachable_rules()` reports every rule whose systems are absent from the
  model. It is written to `model_version.stats.rules_never_applied`, logged as a
  warning on every ingest, and quoted in ACCEPTANCE A1.
- A malformed alias file raises rather than being skipped, so a project that
  meant to supply aliases and mistyped the filename does not silently get the
  behaviour of a project that supplied none.

**Still true:** on `schependomlaan_design.ifc` all three rules remain
unreachable, because the model genuinely contains no gas main. The run reports
hard clashes only, and says so.

## F2 — the pre-filter pad was sized by rules that could never fire
**Found:** measuring pair counts on the real fixture.

The pad is the widest clearance any loaded rule can demand. With the 5 m
building-to-gas rule loaded — against a model containing neither gas nor building
fabric — every bounding box was inflated by 5 m for a comparison that could not
happen.

| pad | pairs admitted |
|---|---|
| 0 m | 5,204 |
| 0.4 m | 18,270 |
| 5 m | **185,401** |

180,197 exact boolean tests whose only possible answer was "clear".

**Handled:** `applicable_rules()` filters to rules whose systems are actually
present before the pad is computed.

## F3 — structure-versus-structure pairs drowned the report
**Found:** same measurement. Schependomlaan is 1,364 structural elements to 73
MEP, so most pairs are wall-meets-slab — a structural coordination question, not
an MEP one. Reporting them buries the handful that belong to the MEP coordinator.

**Handled:** `candidate_pairs(require_mep=True)`. At least one side must be MEP.
5,204 → 80 admitted pairs, and the 7 real clashes are all still there.

## F4 — a hard clash asked for a 25 mm move however deep the overlap
**Found:** the rebase trap escalating a solvable clash.

For a clearance finding, triage severity is the shortfall — a length, and the
right number to move by. For a hard clash it is derived from penetration
*volume*, which is a fine ranking score and not a distance. Feeding it to the
resolver as one asked a 2 m³ overlap to be solved by a 2 m move and a thin deep
one by almost nothing. In practice `required_gap` was 0 and every candidate was
the 25 mm installation margin.

**Handled:** `Coordinator._backout_mm()` computes the least distance that
separates the two bounding boxes, along the axis of least overlap.

## F5 — the retry cap was spent on moves that could not work
**Found:** a solvable clearance violation escalating after three attempts.

The kit orders candidates by displacement. A unit diagonal is a hair shorter than
a unit axis move, so **every diagonal sorts ahead of the axis move of the same
size** — and a diagonal splits its displacement between two axes, separating less
along the one that is actually tight. With three monitored attempts, all three
went to candidates that geometrically could not achieve the gap.

**Handled:** `ZoneResolver._rank_candidates()` scores each candidate with a cheap
bounding-box prediction — does it achieve the required separation, and does it
land inside anything else in this zone — before an attempt is spent on it. The
prediction is not a verdict: whatever survives the ordering still goes through
all three monitors.

**Deliberately excluded from that prediction: the buffer.** Whether a move lands
in a neighbouring zone is the BoundaryMonitor's question. Scoring it here would
answer it with a cheap approximation and quietly rank the move last, so the real
check would never see the case it exists for.

## F6 — the SSE watchdog disconnected healthy clients
**Found:** writing the watchdog test.

It timed out on *silence*. A resolver run on a large model is mostly silence, so
a perfectly healthy browser would be cut off every two minutes. Worse, the case
it was meant to catch — a client that stopped reading — does not present as
silence at all: it blocks the generator at `yield`, so the idle branch is never
reached.

**Handled:** the watchdog now watches dropped frames, which is what a stalled
consumer actually produces. Silence heartbeats forever.

## F7 — ledger history came back in arbitrary order
**Found:** a test asserting a four-step state path.

`history()` ordered by timestamp then by a random UUID. Several transitions
routinely land in the same microsecond — a clash going `proposed` then `verified`
inside one resolver call — so the order was whatever the database felt like.
Which defeats the entire purpose of an append-only ledger.

**Handled:** a monotonic `seq` primary key; `history()` orders by it. A clock
that steps backwards no longer reorders history, and there is a test that steps
it backwards on purpose.

## F8 — `transition()` would have raised on every zone
**Found:** the first ledger test.

The schema calls the state column `status` on a zone and `state` on a clash, per
the specification. `transition()` defaulted to `state`, and every zone call
omitted the field — so the first zone transition in a real run would have raised
`AttributeError` mid-pipeline.

**Handled:** each model declares its own `LEDGER_FIELD`.

## F9 — ingest blanked a stored alias path
**Found:** the alias report reading `applied: 0` on a run that had aliases.

`ingest()` assigned `aliases_path` from its argument unconditionally, so calling
it without one erased a path already on the row. The resolver then re-loaded the
model with a different system vocabulary than the ingest had judged it with.

**Handled:** an argument overrides, otherwise the stored value stands; and
aliasing moved inside `load_model()` so every load of a version is identical.

## F10 — merged zones always scored zero congestion
**Found:** every zone priority reading 0.006 on the smoke run.

Congestion is measured per 6 m cell by the kit. Zoning merges cells and names the
result `0_0+2`, which is not a key the congestion map has ever seen — so the
lookup missed and every merged zone scored zero. The priority queue was flat, and
A1 requires it ordered by congestion.

**Handled:** `ZonePlan.member_cells` records the cells a zone was merged from,
and congestion sums over them.

## F11 — fixture GlobalIds were random, so the diff matched nothing
**Found:** A7 reporting `regressed: 0`.

The fixture builder minted a fresh GUID per element per run, so v1 and v2 shared
no element ids. `diff_versions` matched nothing, filed everything as new or
resolved, and appeared to work. Real authoring tools keep an element's GlobalId
across versions — that persistence is what makes a version diff possible at all.

**Handled:** GUIDs derive from the element name via uuid5.

## F12 — the vendor check called bytecode "drift"
**Found:** `--check` failing after the tests imported the kit.

`__pycache__` is produced by running the code, not by the pin.

**Handled:** the manifest excludes bytecode, and there is a test asserting it
still catches an edited, deleted or added source file.

## F13 — three mutation probes survived the first run
**Found:** `scripts/mutation_probes.py`.

The retry cap, ledger ordering and vendored-kit drift could all be broken without
a single test going red. The suite was green and three safety properties were
untested.

**Handled:** `tests/unit/test_guards.py`. Every test in it names the probe it
answers. 8/8 killed now.

---

## Blockers

Two of the six are now cleared. Four remain, all owner-gated.

### B1 — Docker image and container health ✅ CLEARED
Docker is still not installed on this machine, so this was cleared where it
could be: CI builds the image, runs the container, polls `/health` and asserts
`build_sha == GITHUB_SHA`. Both steps pass.
Run: https://github.com/bopoadz-del/mep-judge/actions/runs/34109449830

### B2 — Python 3.12 ✅ CLEARED
Local Python is 3.11.9, so the suite runs on 3.11 here. CI runs the same 107
tests on **Python 3.12.14**, including A1 against the real 47 MB model, in 67 s.
Run: https://github.com/bopoadz-del/mep-judge/actions/runs/34109449830

### B3 — no Speckle server ⛔ (owner-gated)
Every number in ACCEPTANCE was produced with the local model store, which is a
real backend, not a stand-in. `SpeckleModelStore` has never been run against a
live server. `/health` reports which backend is in use and why.
**Unblocker:** a Speckle host and token (`MEPJ_SPECKLE_HOST`, `MEPJ_SPECKLE_TOKEN`).

### B4 — no Render service ⛔ (owner-gated)
`render.yaml` is written and has never been applied.
**Unblocker:** a Render account and `render blueprint launch`.

### B5 — the product has no name ⛔ (owner-gated)
`mep-judge` is a working name, used as the package name, the image name and the
Render service names.
**Unblocker:** the owner names it.

### B6 — no real project rule table, access table, or programme CSV ⛔
The seed rules are three gas clauses from one drawing. No project has supplied an
access-zone table, so that check reports `unprovable` outside its own fixture —
which is labelled in-file as carrying no authority.
**Unblocker:** a project specification to run `app/llm/rule_extraction.py`
against, and an engineer to approve the candidates.


---

## F14 — three CI faults, each of which made a check look like it had run
**Found:** the first three CI runs, after the code was already green locally.

1. **Every vendored block reported as edited in place.** Nothing was edited.
   `git archive` honours the local `core.autocrlf`, so `--sync` on Windows
   extracted and hashed CRLF while git stored and Linux checked out LF. The same
   commit produced two different locks depending on who ran the sync — and the
   failure looked exactly like tampering, which is the worst possible false
   positive for a check whose whole job is detecting tampering.
   **Fixed:** the pin normalises text to LF before hashing; `.gitattributes`
   marks `vendor/** -text` so git never transforms it in either direction.

2. **A1 silently skipped and the run still went green.** `fetch_fixtures.sh`
   failed with *Permission denied* — the executable bit does not survive a
   commit made on Windows — so the model was absent and pytest reported
   "106 passed, 1 skipped" in ten seconds. A skipped acceptance test is not a
   passed one.
   **Fixed:** CI invokes the script through `bash`, and a following step fails
   the build if the model is not on disk afterwards. The Infra-Plumbing URL was
   also 404ing against a stale path; corrected to the repo's `main` branch and
   its `<schema>/Simple-Scene/` layout.

3. **The coverage gate measured the whole application.** `check_coverage.py`
   joined every filename against every `<source>` root without checking the file
   existed there, so with three `--cov` roots `config.py` resolved as
   `app/agents/config.py`. It reported 83.1% across 37 files while claiming to
   measure the 10 in agents and monitors — and failed the build for the wrong
   reason.
   **Fixed:** a candidate only counts if the resolved path exists. Correctly
   scoped: **93.7%** in CI.

All three share a shape worth naming: the check appeared to run, produced a
number, and the number was about something else.

---

## CI — green

`https://github.com/bopoadz-del/mep-judge/actions/runs/34109449830` on `53f976882408` — Python 3.12.14, Postgres 16, Redis 7.

| step | result |
|---|---|
| vendored kit matches its pin | ✅ 15 files at `5c0711a066b6` |
| no placeholders | ✅ |
| secret scan (fail closed) | ✅ |
| ruff · mypy | ✅ |
| migration up → down → up | ✅ on Postgres 16 |
| acceptance fixture present | ✅ 47 MB model downloaded |
| tests | ✅ **107 passed** in 67 s, A1 included |
| coverage floor, agents + monitors | ✅ **93.7%** (floor 85%) |
| mutation probes | ✅ **8/8 killed**, 0 survivors |
| OpenAPI schema diff | ✅ unchanged |
| docker build | ✅ |
| container `/health` == `GITHUB_SHA` | ✅ |

---

## F15 — most published clearance rules cannot be applied from IFC alone
**Found:** building the public rules table (H2), when the round-trip test stopped
producing a single acceptable proposal.

Nine separation rules were transcribed from public standards and wired in. The
suite immediately went red: every proposal on the small fixtures escalated. The
cause was not the resolver.

ASHRAE 62.1's 3 m figure governs an **outdoor air intake** against a **plumbing
vent terminal**. Encoded against the nearest available system categories it
becomes `ventilation ↔ drainage_foul` — three metres between every duct and every
drain in the building. On a fixture where a duct sits 150 mm from a drain,
nothing can satisfy it, so everything escalates.

It is not an isolated case. Checked one by one:

| rule | what it actually governs | why IFC cannot say |
|---|---|---|
| NEC 110.26(A) | working space at equipment *likely to require examination while energized* | the model does not mark which electrical elements those are |
| NEC 300.4(A)(1) | cables through bored holes in **wood** framing | neither the material condition nor the bored hole is in the model |
| NFPA 13 deflector | clearance to the top of **storage** | a model has no storage |
| SMACNA 457 mm | clear space at duct **access doors** | access doors are not identified |
| ASHRAE 62.1 | **outdoor intake** to **plumbing vent** | neither sub-type is inferable |

Seven of nine are withheld. Two survive — sprinkler-to-wall and
sprinkler-to-sprinkler — because both sides are element categories and the rule
carries no further condition.

**Handled:** `scope_inferable` per rule, `withheld_for_scope()` reporting each
withholding by name with its reason, and `rules_withheld_for_scope` in
`model_version.stats`. A mutation probe fails if the gate stops withholding.

**Why withhold rather than over-apply.** An over-broad rule produces findings
that look authoritative and are not — the same noise the kit was built to remove,
now wearing a citation, which makes it *harder* to argue with rather than easier.
A coordination engineer who is handed three hundred false clearance violations
carrying NFPA numbers stops reading the report, and is right to.

**Consequence for the target.** A06 asked for `clearance_findings ≥ 1` on
Schependomlaan from the public rules. That model has no sprinklers, so the two
applicable rules cannot fire, and every rule that *would* fire on it only fires
because its scope condition was discarded. The check was rewritten to assert what
is true and useful instead: the bilingual alias table reclassifies 13 elements
the English hint list cannot reach, and the withheld rules are each named with a
reason. **This service still finds hard clashes on that model and nothing else,
and it now explains precisely why in terms of the standards themselves.**

The unlock is not more rules. It is either a project specification with
project-specific separations, or IFC property sets rich enough to establish
scope — an `IfcDistributionControlElement` marked as a panel, an access door
identified, storage zones modelled.

## F16 — a killed mutation run left a sabotaged monitor on disk
**Found:** a probe run reporting `boundary_monitor_ignores_the_neighbour (anchor
missing -- probe is stale)`.

The anchor was not stale. An earlier probe run had been killed by a timeout
between applying its mutation and the `finally` that restores the file, so
`app/monitors/boundary.py` was sitting on disk with its comparison stubbed out to
`[], []` — the BoundaryMonitor was live and blind. The next run then reported it
as a *stale probe*, which is the mildest possible description of the situation.

Two commits nearly went out with a deliberately sabotaged safety monitor.

**Handled:** a `.mutation_in_flight` marker holding the original source is
written before each mutation and removed after restoring. On startup the harness
restores from any marker it finds, says what it repaired, and refuses to report a
result until re-run. A killed run is now loud instead of invisible.
