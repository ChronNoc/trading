"""Bidirectional Paper Trading Lab - an isolated, paper-only experiment.

A completely separate subsystem for testing a simultaneous large long+short,
ultra-tight-stop strategy with pre-set activation levels. It imports nothing
from the live execution stack and can never place a real order.
"""

from app.labs.bidirectional.config import (
    AccountConfig,
    ActivationSpec,
    BreakEvenConfig,
    TrailConfig,
)
from app.labs.bidirectional.engine import (
    ARMED,
    CANCELLED,
    COMPLETED,
    DISABLED,
    TRIGGERED,
    LabEngine,
    Leg,
    PairedSetup,
)
from app.labs.bidirectional.market import MarketEvent

__all__ = [
    "AccountConfig", "ActivationSpec", "BreakEvenConfig", "TrailConfig",
    "LabEngine", "Leg", "PairedSetup", "MarketEvent",
    "ARMED", "TRIGGERED", "COMPLETED", "CANCELLED", "DISABLED",
]
