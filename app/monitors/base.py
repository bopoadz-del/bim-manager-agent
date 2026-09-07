"""Monitor contract.

Three monitors run after every proposal and all three must pass before anything
is committed. They are deliberately separate rather than one big check, because
they fail for different reasons and a reviewer needs to see which one objected:
"this move solves the clash but pushes a duct into the next zone" and "this move
solves the clash but reverses the fall on a drain" are different conversations.

A monitor returns a result, never a bare boolean. A bare boolean cannot say what
it looked at, and a monitor whose reasoning is not recorded is a monitor nobody
can argue with -- which is worse than no monitor at all, because it carries
authority it has not earned.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PASS = "pass"
FAIL = "fail"
UNPROVABLE = "unprovable"


@dataclass
class Check:
    """One sub-question inside a monitor.

    ``status`` is three-valued on purpose. ``unprovable`` means the model does
    not carry the data the check needs -- no ports, no access table. That is not
    a pass and must never be recorded as one; it is a statement that this
    particular assurance is absent from this run.
    """

    name: str
    status: str
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.status == FAIL


#: A monitor's verdict. Three-valued for the same reason a check is.
VERDICT_PASS = "pass"
VERDICT_CONDITIONAL = "conditional"
VERDICT_FAIL = "fail"


@dataclass
class MonitorResult:
    """One monitor's verdict over its checks.

    ``verdict`` is the answer; ``passed`` is a narrow convenience that means
    *fully* passed and nothing else. An earlier version had only ``passed``, and
    returned True whenever no check had failed — so a monitor whose connectivity
    and access checks were both ``unprovable`` reported a pass, the resolver
    committed on it, and the distinction survived only as a sentence in
    ``reason`` that nothing read.

    On the one real building model available, every element lacks ports and no
    access table exists, so that path was not an edge case: it was every
    proposal. "Nothing failed" was being shown to reviewers as "everything
    passed", which is the precise substitution this class exists to prevent.
    """

    monitor: str
    verdict: str
    reason: str
    checks: list[Check] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        """True only when every check actually passed."""
        return self.verdict == VERDICT_PASS

    @property
    def conditional(self) -> bool:
        """No check failed, and at least one could not be checked at all."""
        return self.verdict == VERDICT_CONDITIONAL

    @property
    def failed(self) -> bool:
        return self.verdict == VERDICT_FAIL

    @property
    def acceptable(self) -> bool:
        """Nothing objected. Not the same as everything having been verified."""
        return self.verdict in (VERDICT_PASS, VERDICT_CONDITIONAL)

    @property
    def unprovable_checks(self) -> list[str]:
        return [c.name for c in self.checks if c.status == UNPROVABLE]

    def as_dict(self) -> dict[str, Any]:
        return {
            "monitor": self.monitor,
            "verdict": self.verdict,
            "passed": self.passed,
            "reason": self.reason,
            "checks": [asdict(c) for c in self.checks],
            "unprovable": self.unprovable_checks,
            "evidence": self.evidence,
        }

    @classmethod
    def from_checks(
        cls, monitor: str, checks: list[Check], evidence: dict[str, Any] | None = None
    ) -> MonitorResult:
        failures = [c for c in checks if c.failed]
        if failures:
            reason = "; ".join(f"{c.name}: {c.detail}" for c in failures)
            return cls(monitor, VERDICT_FAIL, reason, checks, evidence or {})

        unprovable = [c.name for c in checks if c.status == UNPROVABLE]
        if unprovable:
            reason = (
                "nothing objected, but this model cannot answer: "
                + ", ".join(unprovable)
            )
            return cls(monitor, VERDICT_CONDITIONAL, reason, checks, evidence or {})

        return cls(monitor, VERDICT_PASS, "all checks passed", checks, evidence or {})


@dataclass
class MonitorContext:
    """Everything the monitors need to judge one proposed move."""

    model: Any
    element_gid: str
    vector_mm: tuple[float, float, float]
    zone_key: str
    zone_gids: list[str]
    buffer_gids: list[str]
    neighbour_zone_gids: dict[str, list[str]]
    rules: list[Any]
    access_rules: list[Any] = field(default_factory=list)
    store: Any = None
    stream: str | None = None
    baseline: dict[str, Any] = field(default_factory=dict)

    def element(self) -> Any:
        return self.model.element(self.element_gid)


class Monitor:
    """Base class. Subclasses implement :meth:`run` and nothing else."""

    name = "monitor"

    def run(self, ctx: MonitorContext) -> MonitorResult:
        raise RuntimeError(f"{type(self).__name__} does not implement run()")


def aggregate(results: dict[str, MonitorResult]) -> str:
    """The verdict over a whole monitor set.

    ``fail`` if any monitor objected. ``pass`` only if every monitor fully
    passed. ``conditional`` in between — nothing objected, but something could
    not be checked, and a caller must not be able to spend that as a pass.
    """
    if any(r.failed for r in results.values()):
        return VERDICT_FAIL
    if all(r.passed for r in results.values()):
        return VERDICT_PASS
    return VERDICT_CONDITIONAL


def unprovable_across(results: dict[str, MonitorResult]) -> list[str]:
    """Every check the model could not answer, as ``monitor.check``."""
    return sorted(
        f"{name}.{check}"
        for name, result in results.items()
        for check in result.unprovable_checks
    )


def run_all(monitors: list[Monitor], ctx: MonitorContext) -> tuple[str, dict[str, MonitorResult]]:
    """Run every monitor and return the aggregate verdict with the results.

    Every monitor runs even after one has failed. A resolver that retries needs
    the full objection list, not just the first complaint -- otherwise it fixes
    the boundary problem, resubmits, and only then discovers the gravity problem
    that was there all along, burning an attempt from a cap of three.

    Returns the verdict as a string rather than a boolean on purpose. The
    boolean version of this function silently merged "verified" with "nothing
    objected", and every caller inherited the merge.
    """
    results = {m.name: m.run(ctx) for m in monitors}
    return aggregate(results), results
