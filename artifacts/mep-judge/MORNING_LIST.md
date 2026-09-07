# MORNING LIST — BIM Manager agent

**Three blockers remain, all owner-gated.** The product is named: **BIM Manager agent** (B5 cleared; `mep-judge` remains the package, image and repository name). B1 and B2 are cleared: CI
(https://github.com/bopoadz-del/mep-judge/actions/runs/34109449830) builds the image, runs the container, checks `/health` against the commit
sha, and runs all 107 tests on Python 3.12 including A1 against the real 47 MB
model.

| # | Blocked on | Who clears it | What it unblocks |
|---|---|---|---|
| B3 | No Speckle server | owner: `MEPJ_SPECKLE_HOST` + `MEPJ_SPECKLE_TOKEN` | 3D diff in the review UI; the Speckle store path |
| B4 | No Render service | owner: Render account, `render blueprint launch` | the deploy half of A8 |
| B6 | No real project rule/access table | owner: a spec to extract from, an engineer to approve | clearance and access checks on a real building |

**Nothing else is parked.** Every finding in RUNLOG F1–F13 is fixed in the code,
not deferred. The suite is green, the probes kill 8/8, and there are no
placeholders.

## The one thing to read first

`schependomlaan_design.ifc` produced **zero clearance findings**, because all
three seed rules name systems that model does not contain. The run is correct and
the report is thin. Until B6 is cleared, this service finds hard clashes on real
models and nothing else — and it says so, in the logs, in `/health` data, in
`model_version.stats.rules_never_applied`, and in ACCEPTANCE A1.

That is the difference between a tool that is quiet because the building is clean
and one that is quiet because it never asked.
