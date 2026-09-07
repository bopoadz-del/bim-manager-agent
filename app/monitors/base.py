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


@dataclass
class MonitorResult:
    monitor: str
    passed: bool
    reason: str
    checks: list[Check] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def unprovable_checks(self) -> list[str]:
        return [c.name for c in self.checks if c.status == UNPROVABLE]

    def as_dict(self) -> dict[str, Any]:
        return {
            "monitor": self.monitor,
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
            return cls(monitor, False, reason, checks, evidence or {})
        unprovable = [c.name for c in checks if c.status == UNPROVABLE]
        reason = "all checks passed"
        if unprovable:
            reason = f"passed; not provable from this model: {', '.join(unprovable)}"
        return cls(monitor, True, reason, checks, evidence or {})


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


def run_all(monitors: list[Monitor], ctx: MonitorContext) -> tuple[bool, dict[str, MonitorResult]]:
    """Run every monitor. All must pass.

    Every monitor runs even after one has failed. A resolver that retries needs
    the full objection list, not just the first complaint -- otherwise it fixes
    the boundary problem, resubmits, and only then discovers the gravity problem
    that was there all along, burning an attempt from a cap of three.
    """
    results = {m.name: m.run(ctx) for m in monitors}
    return all(r.passed for r in results.values()), results
