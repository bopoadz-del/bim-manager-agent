"""Prose for BCF issues and coordination agendas.

Everything this module writes about has already been decided. The vector, the
clause, the monitor verdicts and the zone status are inputs; the output is
sentences. If the model is unavailable the caller gets a plain deterministic
rendering of the same facts, because an issue with a stiff description is
useful and an issue that failed to generate is not.
"""
from __future__ import annotations

from typing import Any

from app.llm.client import LLMClient, LLMUnavailable, get_client

SYSTEM_PROMPT = """You write coordination issue descriptions for BIM engineers.

You are given decided facts: a clash, a proposed move, the clause authorising it,
and the checks it passed. Write 2-4 plain sentences describing the issue and the
proposed resolution.

Never introduce a number that is not in the input. Never state that something is
safe, approved, compliant, or verified beyond exactly what the input says. Do not
speculate about causes. British spelling.
"""


def _plain(issue: dict[str, Any]) -> str:
    """Deterministic rendering. Also the fallback when no model is available."""
    systems = " / ".join(issue.get("systems") or []) or "unknown systems"
    parts = [f"{issue.get('kind', 'clash').title()} between {systems} in zone {issue.get('zone_key')}."]
    gap = issue.get("depth_or_gap_mm")
    if gap is not None:
        parts.append(f"Measured {float(gap):.0f} mm.")
    if issue.get("clause_text"):
        parts.append(f"Governed by {issue['clause_text']}.")
    vector = issue.get("vector_mm")
    if vector:
        parts.append(
            "Proposed move: "
            + ", ".join(
                f"{axis} {float(v):+.0f} mm"
                for axis, v in zip("XYZ", vector, strict=False)
                if v
            )
            + "."
        )
    verdict = issue.get("verdict")
    if verdict:
        parts.append(f"Monitor verdict: {verdict}.")
    return " ".join(parts)


def issue_text(issue: dict[str, Any], client: LLMClient | None = None) -> str:
    try:
        client = client or get_client()
    except LLMUnavailable:
        return _plain(issue)
    try:
        text = client.complete(SYSTEM_PROMPT, _plain(issue) + "\n\nStructured facts:\n" + repr(issue))
    except Exception:
        return _plain(issue)
    return text.strip() or _plain(issue)


def agenda(zone_key: str, issues: list[dict[str, Any]], client: LLMClient | None = None) -> str:
    """A coordination-meeting agenda for one zone."""
    lines = [f"# Coordination agenda -- zone {zone_key}", ""]
    escalated = [i for i in issues if i.get("verdict") not in ("verified", "approved")]
    resolved = [i for i in issues if i.get("verdict") in ("verified", "approved")]

    lines.append(f"{len(resolved)} proposal(s) ready to review, {len(escalated)} needing a decision.")
    lines.append("")
    if escalated:
        lines.append("## Needs a decision")
        for i in escalated:
            lines.append(f"- {i.get('clash_key')}: {issue_text(i, client)}")
        lines.append("")
    if resolved:
        lines.append("## Ready to review")
        for i in resolved:
            lines.append(f"- {i.get('clash_key')}: {issue_text(i, client)}")
    return "\n".join(lines)
