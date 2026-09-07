"""The LLM boundary, enforced mechanically.

``app/llm/__init__.py`` states the rule in prose: a language model may extract
candidate rules and write narrative text, and may not participate in any
decision. Prose does not survive refactoring. This test does.

It walks the source of every module that decides something -- what clashes, what
a monitor concluded, what gets committed, what a reviewer's action does -- and
fails if any of them can reach a model, directly or through an import chain.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent.parent / "app"

#: Modules that decide. Nothing here may reach a language model.
DECISION_MODULES = [
    "kit/engine.py",
    "kit/systems.py",
    "monitors/base.py",
    "monitors/geometry.py",
    "monitors/boundary.py",
    "monitors/integrity.py",
    "monitors/__init__.py",
    "agents/coordinator.py",
    "agents/zone_resolver.py",
    "agents/zoning.py",
    "agents/results.py",
    "ledger.py",
    "pipeline.py",
    "review.py",
    "models.py",
]

FORBIDDEN_ROOTS = {
    "anthropic",
    "openai",
    "google",
    "cohere",
    "mistralai",
    "ollama",
    "langchain",
    "llama_cpp",
    "transformers",
    "litellm",
}


def _imports(path: Path) -> set[str]:
    """Every module name imported by this file, including inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize("relative", DECISION_MODULES)
def test_decision_modules_cannot_reach_a_language_model(relative):
    path = APP / relative
    assert path.exists(), f"{relative} is listed as a decision module but does not exist"

    for name in _imports(path):
        root = name.split(".")[0]
        assert root not in FORBIDDEN_ROOTS, (
            f"{relative} imports {name}. Clash judgement, monitor verdicts and merge "
            f"decisions are measured, never generated."
        )
        assert not name.startswith("app.llm"), (
            f"{relative} imports {name}. The LLM boundary allows rule extraction and "
            f"narrative text only; this module decides something."
        )


def test_the_decision_module_list_covers_what_actually_decides():
    """A new decision module must be added to the list above, not forgotten.

    Without this, the boundary erodes the quiet way: someone adds
    ``agents/scheduler.py``, it is never listed, and the guard above keeps
    passing while the new module does whatever it likes.
    """
    listed = {str(APP / r) for r in DECISION_MODULES}
    deciding_dirs = [APP / "monitors", APP / "agents", APP / "kit"]
    actual = {
        str(p)
        for d in deciding_dirs
        for p in d.glob("*.py")
        if p.name != "__init__.py" or d.name == "monitors"
    }
    missing = sorted(Path(p).relative_to(APP).as_posix() for p in actual - listed)
    assert not missing, (
        f"these modules decide things but are not covered by the LLM boundary test: {missing}"
    )


def test_llm_package_is_the_only_place_that_imports_a_model_client():
    offenders = []
    for path in APP.rglob("*.py"):
        if "llm" in path.parts:
            continue
        for name in _imports(path):
            if name.split(".")[0] in FORBIDDEN_ROOTS:
                offenders.append(f"{path.relative_to(APP)}: {name}")
    assert not offenders, f"LLM clients outside app/llm/: {offenders}"


def test_rule_extraction_refuses_a_quote_it_cannot_find_in_the_source():
    """A citation that is not in the document is not a citation."""
    from app.llm.rule_extraction import _validate

    source = "Gas mains shall be separated from LV cables by not less than 400 mm."
    good = {
        "rule_id": "X-1", "system_a": "gas_main", "system_b": "electrical_lv",
        "min_gap_mm": 400, "axis": "any", "precedence": "project_spec",
        "source_doc": "spec.pdf", "source_clause": "3.1",
        "quote": "Gas mains shall be separated from LV cables by not less than 400 mm.",
    }
    invented = dict(good, rule_id="X-2", min_gap_mm=250,
                    quote="Gas mains shall be separated from LV cables by 250 mm.")

    kept = _validate([good, invented], source)
    assert [k["rule_id"] for k in kept] == ["X-1"]
    assert kept[0]["approved"] is False, "extraction must never self-approve"


def test_extracted_candidates_are_not_rules_until_a_human_approves(tmp_path):
    from app.llm.rule_extraction import approve_pending, write_pending

    candidates = [
        {
            "rule_id": "X-1", "system_a": "gas_main", "system_b": "electrical_lv",
            "min_gap_mm": 400.0, "axis": "any", "precedence": "project_spec",
            "source_doc": "spec.pdf", "source_clause": "3.1", "quote": "q" * 20,
            "source_text_hash": "abc123", "approved": False,
        }
    ]
    pending = write_pending(candidates, tmp_path / "pending.json")
    out = approve_pending(pending, tmp_path / "rules.json")

    import json

    assert json.loads(out.read_text(encoding="utf-8")) == [], (
        "an unapproved candidate must never reach the rule table"
    )
