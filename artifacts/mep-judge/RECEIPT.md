# RECEIPT — mep-judge v0.1.0

| | |
|---|---|
| tests | **107 passed**, 0 failed, 0 skipped |
| mutation probes | **8 killed / 8**, 0 survivors, 1 control behaved |
| coverage `app/agents` + `app/monitors` | **93.8%** statement, **91%** branch (floor 85%) |
| ruff | clean |
| mypy | clean, 38 source files |
| placeholders (`TODO\|FIXME\|NotImplementedError\|pass  # stub\|raise NotImplemented`) | **0** |
| migration up → down → up | verified on SQLite locally; CI runs Postgres 16 |
| vendored kit | `d7cff230453efd273388491e1cbf1d18f3ebefd5`, 15 files, `--check` clean |
| OpenAPI | `openapi.json` committed, diffed in CI |
| acceptance | A1–A7, A9 ✅ · A8 partially (see below) |
| original IFC sha256 before vs after a full run | **identical** |

## What ran on what

- 107 tests on **Python 3.11.9** (this machine has no 3.12). The Dockerfile and
  CI pin 3.12; the source is kept 3.11-compatible so the suite runs locally.
- Every acceptance number came from the **local model store and the real
  monitors**. No mocks in the acceptance suite.
- A1 ran against the real 47 MB `schependomlaan_design.ifc` in 129 s.

## CI — green

https://github.com/bopoadz-del/mep-judge/actions/runs/34109449830 · Python 3.12.14 · Postgres 16 · Redis 7

107 passed in 67 s with A1 against the real 47 MB model; coverage 93.7% on agents
and monitors; 8/8 mutants killed; migration up/down/up on Postgres; docker image
built; container serves `/health` with `build_sha == GITHUB_SHA`.

It took four runs to get there, and the three failures are worth reading: each
one was a check that appeared to run and produced a number about something else.
See RUNLOG **F14**.

## Still not verified

| | why | who clears it |
|---|---|---|
| Render blueprint deploy | no Render account | owner |
| Speckle store against a live server | no Speckle host or token | owner |
| a real project rule or access table | none supplied | owner |

## The finding that matters

`schependomlaan_design.ifc` produced **zero clearance findings**. Not because the
building is clean: because all three seed rules name systems (`gas_main`,
`electrical_lv`, `building`) that the kit's loader cannot infer and that this
model does not contain. Every clearance check silently found no applicable rule.

Nothing raised. The findings list was simply shorter, and a short findings list
reads exactly like good news.

That is now reported four ways — a warning on every ingest, the
`rules_never_applied` block in `model_version.stats`, ACCEPTANCE A1, and RUNLOG
F1 — and a project-supplied alias table exists to fix it properly. Until a real
project supplies one, **this service finds hard clashes on real models and
nothing else, and says so.**

## Rollback

Initial commit; there is no prior state to roll back to. The vendored kit can be
re-pinned at any commit with
`python scripts/vendor_kit.py --sync --source <clone> --sha <commit>`.
