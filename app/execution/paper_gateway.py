"""Paper execution gateway - the ONLY gateway research/replay/shadow may touch.

This module deliberately imports nothing from the Tradovate order, bracket, or
live-execution modules, so every paper/research path that receives a
:class:`PaperExecutionGateway` is structurally unable to reach a broker. A test
enforces the import boundary. All simulated activity is appended to an
append-only JSONL log with full lineage; nothing is ever sent anywhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

ENVIRONMENT_PAPER = "PAPER"


@dataclass(frozen=True, slots=True)
class PaperOrderRecord:
    """One simulated order intention with immutable lineage."""

    order_id: str
    session_id: str
    setup_id: str
    direction: str
    contracts: int
    entry: Decimal
    stop: Decimal
    target: Decimal
    status: str  # simulated_submitted | simulated_cancelled | simulated_flattened
    recorded_utc: str


class PaperExecutionGateway:
    """Simulation-only gateway: records intents, never talks to any broker."""

    environment = ENVIRONMENT_PAPER

    def __init__(self, log_path: Path) -> None:
        """Create a gateway appending simulated orders to ``log_path``."""
        self._log_path = log_path
        self._counter = 0

    @property
    def is_connected(self) -> bool:
        """Paper is always 'connected' - there is nothing external to connect."""
        return True

    @property
    def can_submit_real_orders(self) -> bool:
        """Structurally false: this gateway has no broker transport at all."""
        return False

    def place_bracket(
        self,
        *,
        session_id: str,
        setup_id: str,
        direction: str,
        contracts: int,
        entry: Decimal,
        stop: Decimal,
        target: Decimal,
    ) -> PaperOrderRecord:
        """Record a simulated bracket order and return its immutable record."""
        if direction not in {"long", "short"}:
            raise ValueError("direction must be 'long' or 'short'")
        if contracts <= 0:
            raise ValueError("contracts must be positive")
        self._counter += 1
        record = PaperOrderRecord(
            order_id=f"paper-{self._counter:06d}",
            session_id=session_id,
            setup_id=setup_id,
            direction=direction,
            contracts=contracts,
            entry=entry,
            stop=stop,
            target=target,
            status="simulated_submitted",
            recorded_utc=datetime.now(UTC).isoformat(),
        )
        self._append(record)
        return record

    def cancel_all(self) -> PaperOrderRecord | None:
        """Record a simulated cancel-all marker (no broker call exists)."""
        return self._marker("simulated_cancelled")

    def flatten(self) -> PaperOrderRecord | None:
        """Record a simulated flatten marker (no broker call exists)."""
        return self._marker("simulated_flattened")

    def _marker(self, status: str) -> PaperOrderRecord:
        self._counter += 1
        record = PaperOrderRecord(
            order_id=f"paper-{self._counter:06d}", session_id="", setup_id="", direction="long",
            contracts=1, entry=Decimal("0"), stop=Decimal("0"), target=Decimal("0"),
            status=status, recorded_utc=datetime.now(UTC).isoformat(),
        )
        self._append(record)
        return record

    def _append(self, record: PaperOrderRecord) -> None:
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "order_id": record.order_id, "session_id": record.session_id, "setup_id": record.setup_id,
            "direction": record.direction, "contracts": record.contracts, "entry": str(record.entry),
            "stop": str(record.stop), "target": str(record.target), "status": record.status,
            "recorded_utc": record.recorded_utc,
        }
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True) + "\n")
