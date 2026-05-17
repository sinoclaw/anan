"""
L4 ProactiveObserver — shared protocols
======================================

ProbeContext and ProbeResult are defined here so that both
observer.py and observability_advisor.py can import them without
creating a circular dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ProbeResult:
    """What a probe found out about an intent."""
    verdict: str    # "verified" | "falsified" | "inconclusive"
    evidence: str   # human-readable explanation
    detail: dict = field(default_factory=dict)


# A probe takes (intent, ctx) and returns a ProbeResult.
# ctx exposes: bus, working_memory, self_model, intent_stack
Probe = Callable[..., ProbeResult]


@dataclass
class ProbeContext:
    bus: Any = None
    working_memory: Any = None
    self_model: Any = None
    intent_stack: Any = None
