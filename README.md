# mep-judge

A deployable service that takes an IFC model, finds the MEP clashes and clearance
violations that are real, proposes moves that cite a clause, verifies every one of
them through three independent checks, and hands a reviewer a change set.

**The model of record is never written to.** Proposals are vectors and clause
references, applied by an engineer in the authoring tool. The original file's
sha256 is reported before and after every run and shown in the review UI, so the
promise is evidence rather than a claim.

The clash judgement itself is the `mep_coordination` kit from
[Cerebrum-Blocks](https://github.com/bopoadz-del/Cerebrum-Blocks), vendored at a
pinned commit and never reimplemented here. See **Vendoring** below.

---

## What it actually does

```
IFC upload
   ↓  mesh, hash, cache
Coordinator          full clash + distance pass, zoning, triage, priority queue
   ↓  dispatch top-N zones
ZoneResolver         per zone, on its own branch: candidate moves from B4
   ↓  every candidate
Monitors             geometry · boundary · integrity — all three must pass
   ↓  clean sweep
commit to zone branch → ledger
   ↓  boundary-owned proposals
Coordinator          re-runs BOTH zones; commits only on a double pass
   ↓
review package       change_set.json + BCF 2.1 + monitor evidence
```

### The three monitors

Every proposal runs all three. Not one, not the cheap two.

| monitor | asks |
|---|---|
| **geometry** | does the moved element still fit where it now is? Re-judged by the same engine that condemned it. |
| **boundary** | does this push a problem into somebody else's zone? Judged against neighbours' **current branch heads**, not their as-loaded positions. |
| **integrity** | is it still a working part of a system — joints intact, fall preserved, clearances met, access kept? |

A monitor returns a result, never a bare boolean, and each sub-check is
three-valued: `pass`, `fail`, or **`unprovable`**. `unprovable` means the model
does not carry the data the check needs — no ports, no access table. It is never
recorded as a pass, because "we checked and it is fine" and "we could not check"
are different sentences and only one of them is usually true.

### What it refuses to do

- Propose a move with no cited clause behind the distance. Unsourced findings are
  **flagged**, never dressed up as proposals.
- Report an element with no geometry as clear. It is `unjudged`.
- Parse `.rvt`/`.nwd`/`.nwc`. It returns 422 with the export instruction.
- Let a language model near a clash verdict, a monitor result, or a merge
  decision. An LLM may extract candidate rules from a specification (human
  approved, one per project) and write BCF prose. That boundary is enforced by a
  test that walks the imports of every decision module.

---

## Quick start

```bash
git clone <this repo> && cd mep-judge
cp .env.example .env
docker compose up --build          # api on :8000, worker, postgres, redis
curl -s localhost:8000/health | jq
```

Then, with the bootstrap key from `.env`:

```bash
KEY=change-me
PROJECT=$(curl -sX POST localhost:8000/projects -H "X-API-Key: $KEY" \
  -H 'content-type: application/json' -d '{"name":"my project"}' | jq -r .id)

curl -sX POST localhost:8000/projects/$PROJECT/models \
  -H "X-API-Key: $KEY" -F ifc=@model.ifc | jq
```

Open `http://localhost:8000/ui/` for the review UI: paste the key and the model
version id, pick a zone, read the monitor verdicts, approve or reject.

### Without Docker

```bash
pip install -e ".[dev]"
export MEPJ_DATABASE_URL="sqlite+pysqlite:///./var/mepj.db"
alembic upgrade head
uvicorn app.main:app --reload
```

---

## API

| | |
|---|---|
| `POST /projects` | operator only |
| `POST /projects/{p}/keys` | issue a key; the plaintext is shown once |
| `POST /projects/{p}/models` | IFC upload + optional programme CSV → runs the pipeline |
| `GET /models/{v}/zones` | zones, ordered by priority |
| `GET /zones/{z}/clashes` · `GET /zones/{z}/proposals` | with full monitor evidence |
| `POST /zones/{z}/review` | `approve` \| `reject` \| `edit` — an edit is re-monitored |
| `GET /zones/{z}/change_set.json` · `GET /zones/{z}/bcf.zip` | the deliverables |
| `GET /models/{v}/diff/{prev}` | resolved / regressed / new / persisting |
| `GET /events/{topic}` | SSE with heartbeat |
| `GET /health` | build sha, database, store backend, kit pin — no key needed |

Every failure returns a typed `{code, message, detail}` with a real status code.
Nothing returns 200 on failure. The committed `openapi.json` is diffed in CI, so
the contract cannot change unnoticed.

Two roles. `operator` ingests; `reviewer` reads and signs off. Reviewer is the
default, because review is the narrower right and a key issued carelessly should
not be able to start new work.

---

## Vendoring

The kit is the product's judgement, and it is not reimplemented, wrapped in a
heuristic, or edited here.

```bash
python scripts/vendor_kit.py --check      # CI runs this first
python scripts/vendor_kit.py --sync --source ../Cerebrum-Blocks --sha <commit>
```

`VENDOR.lock` records the commit and a sha256 per file. `--check` fails if
anything under `vendor/` was edited in place, so a block is changed upstream and
re-pinned, or it is not changed at all. `/health` reports the live pin.

`app.blocks` mounts `vendor/mep_coordination/` by setting `__path__`, so the
kit's own internal imports resolve untouched.

**Currently pinned:** `5c0711a066b6eef5e0861cbfa0d793972b719c83` (15 files).

---

## Testing

```bash
pytest tests -q                      # 107 tests
pytest tests -m "not slow"           # skip the 47 MB fixture
python scripts/mutation_probes.py    # 8/8 mutants killed
python scripts/check_coverage.py --min 85 app/agents app/monitors
```

`scripts/mutation_probes.py` breaks one safety property at a time and requires
the suite to go red. Its first run had **three survivors**; `tests/unit/test_guards.py`
exists to close them, and each test there names the probe it answers.

The trap fixtures are real IFC files written by ifcopenshell and read back
through the production loader — the pipeline cannot tell them from a real model.
`scripts/fetch_fixtures.sh` downloads the two large public models.

---

## Read these before trusting a report

- [`artifacts/mep-judge/ACCEPTANCE.md`](artifacts/mep-judge/ACCEPTANCE.md) — A1–A9
  with measured evidence, and a closing section on what is *not* proven.
- [`artifacts/mep-judge/RUNLOG.md`](artifacts/mep-judge/RUNLOG.md) — thirteen
  findings, including the one that matters most: **the seed rule table cannot fire
  on either public model, and failed silently until it was made to report itself.**
- [`artifacts/mep-judge/MORNING_LIST.md`](artifacts/mep-judge/MORNING_LIST.md) —
  the six remaining blockers and who clears each.
