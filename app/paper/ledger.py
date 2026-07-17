"""Append-only paper trade ledger: durable, recoverable, never overwritten.

Design rules, all of which exist because a ledger that lies is worse than none:

* **Append-only JSONL.** A closed trade is a historical fact. Nothing rewrites
  or reorders an existing line, so an old row cannot be improved after the fact.
* **Never silently overwrite.** Opening a ledger reads what is already there and
  continues it. An existing file is user-owned data, not scratch space.
* **Crash-tolerant reads.** A process killed mid-write can leave one torn final
  line. Recovery keeps every intact record and reports the damaged tail rather
  than throwing the whole file away.
* **Money is text.** Every monetary field round-trips as a string into Decimal;
  a float never touches the ledger.
* **Synthetic is quarantined.** Fixture-generated trades carry
  ``is_synthetic_fixture: true`` and are excluded from real statistics.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.paper.models import PaperTrade

LEDGER_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class LedgerRecovery:
    """What was recovered from an existing ledger file."""

    records: tuple[dict[str, object], ...]
    damaged_tail: bool
    path: Path

    @property
    def real_records(self) -> tuple[dict[str, object], ...]:
        """Return only non-synthetic records (what real statistics may use)."""
        return tuple(r for r in self.records if not r.get("is_synthetic_fixture", False))

    @property
    def realized_pnl(self) -> Decimal:
        """Sum net P&L of real trades, as Decimal (never float)."""
        return sum((Decimal(str(r["net_pnl"])) for r in self.real_records), Decimal("0"))

    def last_balance(self, default: Decimal) -> Decimal:
        """Return the balance after the last real trade, or ``default`` if none."""
        real = self.real_records
        return Decimal(str(real[-1]["balance_after"])) if real else default


class PaperLedger:
    """Durable append-only sink for closed simulated trades."""

    def __init__(self, path: Path | str, *, fsync: bool = True) -> None:
        """Open (or create) a ledger at ``path`` without disturbing its contents."""
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.Lock()
        self._recovery = self.recover(self._path)
        self._count = len(self._recovery.records)

    @property
    def path(self) -> Path:
        """Filesystem location of this ledger."""
        return self._path

    @property
    def recovered(self) -> LedgerRecovery:
        """Records present when this ledger was opened."""
        return self._recovery

    @property
    def count(self) -> int:
        """Total records written plus recovered."""
        return self._count

    def append(self, trade: PaperTrade) -> None:
        """Append one closed trade durably. Never rewrites earlier lines."""
        record = trade.to_record()
        record["schema_version"] = LEDGER_SCHEMA_VERSION
        line = json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            if self._fsync:
                os.fsync(handle.fileno())
            self._count += 1

    @staticmethod
    def recover(path: Path | str) -> LedgerRecovery:
        """Read an existing ledger, tolerating a torn final line from a crash."""
        target = Path(path)
        if not target.is_file():
            return LedgerRecovery(records=(), damaged_tail=False, path=target)
        records: list[dict[str, object]] = []
        damaged = False
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Only a torn tail is tolerable; damage mid-file is still reported
                # rather than silently dropped.
                damaged = True
        return LedgerRecovery(records=tuple(records), damaged_tail=damaged, path=target)
