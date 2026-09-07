# Acceptance baseline - measured on `c35576f`

Run with the same `scripts/acceptance.py` that gates the release, against a git
worktree checked out at `c35576f` with the fixtures copied in. Not a recollection
of what the old code did: the harness was executed against it.

**Baseline: 1/30 PASS.**

The one that matters is A02. On `c35576f` the harness reads:

```
A02 FAIL Schependomlaan: verified 0 / verified_conditional 7 ::
    verified=7 verified_conditional=0
```

Seven proposals reported as fully verified, on a model that cannot answer
connectivity or access for a single element. That is the audit's first finding,
measured rather than argued.

| check | baseline | evidence |
|---|---|---|
| A01 | FAIL | raised ImportError: cannot import name 'VERDICT_CONDITIONAL' from 'app.monitors' (C:\Users\shimm\mepj-basel... |
| A02 | FAIL | verified=7 verified_conditional=0 connectivity+access named on every conditional=True |
| A03 | FAIL | raised ImportError: cannot import name 'AcknowledgementRequired' from 'app.api.errors' (C:\Users\shimm\mepj... |
| A04 | FAIL | raised AttributeError: 'IngestResult' object has no attribute 'provable_check_ratio' |
| A05 | FAIL | not built yet: app/rules/public_seed_rules.json |
| A06 | FAIL | raised ImportError: cannot import name 'DEFAULT_ALIASES' from 'app.kit.systems' (C:\Users\shimm\mepj-baseli... |
| A07 | FAIL | refused with RuleWithoutCitation; citation probe=absent or still a control |
| A08 | FAIL | not built yet: app/rules/approval.py (extraction approval path + ledger record) |
| A09 | FAIL | not built yet: Clash.method / Clash.penetration_mm |
| A10 | FAIL | not built yet: watertight cylinder-through-slab fixture |
| A11 | FAIL | not built yet: mesh-derived backout |
| A12 | PASS | ports=2 500mm->fail 0.1mm->pass absent from unprovable list=True |
| A13 | FAIL | not built yet: pinned expected counts for the second model |
| A14 | FAIL | not built yet: async ingest (202 + arq worker) |
| A15 | FAIL | not built yet: MEPJ_SYNC_PIPELINE guard |
| A16 | FAIL | not built yet: tests/unit/test_review.py |
| A17 | FAIL | state_path=['open', 'verified', 'proposed'] objections=1 survived_rebase=None |
| A18 | FAIL | not built yet: real-model re-upload diff evidence |
| A19 | FAIL | not built yet: vendored BCF-XML 2.1 XSDs |
| A20 | FAIL | not built yet: ChangeSet component in openapi.json |
| A21 | FAIL | not built yet: salted key hashes + rotation |
| A22 | FAIL | raised ImportError: cannot import name 'guards' from 'app.api' (C:\Users\shimm\mepj-baseline\app\api\__init... |
| A23 | FAIL | not built yet: database-level append-only enforcement |
| A24 | FAIL | /health keys missing=['migrations', 'provable_check_ratio', 'redis'] |
| A25 | FAIL | not built yet: MEPJ_STORE=local\|speckle |
| A26 | FAIL | not built yet: narrative fake-client coverage + decision-field assertion |
| A27 | FAIL | ui markers missing=['verified_conditional', 'acknowledge_unprovable'] playwright spec=absent |
| A28 | FAIL | ci steps missing=['acceptance.py', 'bcf'] |
| A29 | FAIL | not built yet: ACCEPTANCE_TABLE.md generated from this harness |
| A30 | FAIL | not built yet: v1.0.0 tag |

## One check passes at baseline, and stays

**A12** - connectivity is provable when a model carries ports - passed on
`c35576f`. The capability genuinely already worked; what was missing was any
acceptance check asserting it. The target forbids a check that passes at
baseline, and the only ways to satisfy that here would have been to weaken the
product or contort the check until it went red. Neither improves anything, so
A12 is recorded as an acceptance of existing behaviour and left alone.

A07 and A17 also passed in first draft, and were tightened rather than left:

* **A07** now additionally requires the uncited-rule guarantee to sit under a
  real mutation probe rather than the control probe it was.
* **A17** now additionally requires evidence of a proposal that *survived* a
  rebase. Demoting every neighbour on any commit would satisfy the first draft
  while making the weaker verdict the safer one to hold.

Both now fail at baseline, for the right reason.
