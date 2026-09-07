"""The only place in this service where a language model may be called.

Two uses are allowed, and they share a property: neither one decides anything.

* **Rule extraction** turns a project specification into candidate B2 rules.
  Every candidate carries the clause it came from and the hash of the source
  chunk, and no candidate becomes a rule until a human approves it. The model
  proposes; the engineer decides; the citation makes the decision checkable.
* **Narrative text** turns an already-decided proposal into BCF issue prose or a
  coordination-meeting agenda. The numbers, the clauses and the verdicts are all
  fixed before this runs. It writes sentences around them.

Everything else is forbidden. No language model touches whether two solids
clash, whether a monitor passed, or whether a zone may merge. Those questions
have measurable answers, and a system that asks a model to guess at a measurable
answer has chosen fluency over correctness in the one place where correctness is
the entire product.

``tests/unit/test_llm_boundary.py`` enforces this mechanically: it imports every
decision module and fails if any of them can reach an LLM client. The rule is
written here in prose so a reader understands it, and there so it cannot quietly
stop being true.
"""
from __future__ import annotations

from app.llm.client import LLMClient, LLMUnavailable, get_client

__all__ = ["LLMClient", "LLMUnavailable", "get_client"]
