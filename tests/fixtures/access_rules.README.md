# access_rules.json -- a TEST FIXTURE, not a code

This table exists so the IntegrityMonitor's access-zone check has something to
run against. Without it that branch is never executed by any test, because no
real project has supplied an access table to this service yet.

**The 600 mm figure is not sourced from any standard and carries no authority.**
It is a number chosen to make the check observable. Its citation points at this
file on purpose: anyone who traces the clause arrives here and reads this
paragraph rather than mistaking it for a requirement.

A real project supplies its own access table, extracted from its specification
with `app/llm/rule_extraction.py` and approved by an engineer, and every rule in
it cites the document and clause it came from.
