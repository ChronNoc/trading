"""Tradable instrument definitions (contract multipliers).

MNQ and NQ track the same Nasdaq-100 price with the same 0.25-point tick, so
only the DOLLAR value per tick differs:

* MNQ (Micro E-mini):  tick_value $0.50  ->  $2 per index point per contract
* NQ  (E-mini):        tick_value $5.00  ->  $20 per index point per contract

NQ is 10x the dollars-per-point of MNQ: every profit AND every loss is 10x
larger for the same price move. The dollar-based risk sizer sizes fewer NQ
contracts automatically, but a single NQ stop can still consume a large share
of a micro-sized prop account - choosing NQ is a deliberate, visible decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

DEFAULT_INSTRUMENT = "MNQ"


@dataclass(frozen=True, slots=True)
class Instrument:
    """One futures contract's tick geometry and dollar multiplier."""

    symbol: str
    tick_size: Decimal
    tick_value: Decimal  # dollars per tick per contract

    @property
    def point_value(self) -> Decimal:
        """Dollars per full index point per contract."""
        return self.tick_value / self.tick_size


MNQ = Instrument("MNQ", Decimal("0.25"), Decimal("0.50"))
NQ = Instrument("NQ", Decimal("0.25"), Decimal("5.00"))

INSTRUMENTS: dict[str, Instrument] = {"MNQ": MNQ, "NQ": NQ}


def resolve_instrument(name: str | None) -> Instrument:
    """Return the instrument for ``name`` (case-insensitive), MNQ if unknown."""
    return INSTRUMENTS.get((name or DEFAULT_INSTRUMENT).strip().upper(), MNQ)
