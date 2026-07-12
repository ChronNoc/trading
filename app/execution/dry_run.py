"""Dry-run executor - item 37. Logs intended orders; can never send one.

Built now so it exists before any live wiring is ever considered. There
is deliberately no transport, no URL, no credential field anywhere in
this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class IntendedOrder:
    """One order the system WOULD have submitted."""

    side: str
    contracts: int
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal
    reason: str

    def __post_init__(self) -> None:
        """Validate the intent."""
        if self.side not in ("buy", "sell"):
            raise ValueError("side must be 'buy' or 'sell'")
        if self.contracts <= 0:
            raise ValueError("contracts must be positive")


class DryRunExecutor:
    """Append-only journal of intended orders. No network capability exists."""

    def __init__(self, journal_path: Path) -> None:
        """Create an executor journaling to the given path."""
        self.journal_path = journal_path
        self.logged_orders = 0

    def submit(self, order: IntendedOrder, *, now: datetime | None = None) -> Path:
        """Journal the intended order and return the journal path."""
        moment = now or datetime.now(timezone.utc)
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "logged_at": moment.isoformat(),
            "mode": "DRY_RUN",
            "side": order.side,
            "contracts": order.contracts,
            "entry_price": str(order.entry_price),
            "stop_price": str(order.stop_price),
            "target_price": str(order.target_price),
            "reason": order.reason,
            "sent_to_broker": False,
        }
        with self.journal_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
        self.logged_orders += 1
        return self.journal_path
