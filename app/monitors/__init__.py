"""The three monitors, and the only supported way to run them.

The set is fixed and ordered. Nothing in the service may run one monitor and
skip another: a proposal is verified when geometry, boundary and integrity all
pass, and any other combination is not a verified proposal.
"""
from __future__ import annotations

from app.monitors.base import (
    FAIL,
    PASS,
    UNPROVABLE,
    Check,
    Monitor,
    MonitorContext,
    MonitorResult,
    run_all,
)
from app.monitors.boundary import BoundaryMonitor
from app.monitors.geometry import GeometryMonitor
from app.monitors.integrity import IntegrityMonitor

ALL_MONITORS: list[Monitor] = [GeometryMonitor(), BoundaryMonitor(), IntegrityMonitor()]

__all__ = [
    "ALL_MONITORS",
    "BoundaryMonitor",
    "Check",
    "FAIL",
    "GeometryMonitor",
    "IntegrityMonitor",
    "Monitor",
    "MonitorContext",
    "MonitorResult",
    "PASS",
    "UNPROVABLE",
    "run_all",
]
