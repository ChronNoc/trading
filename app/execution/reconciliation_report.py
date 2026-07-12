"""Daily simulated-vs-actual fill reconciliation report - item 39.

Compares the day's simulated fills against actual fills (empty until a
demo/live path exists) and writes a markdown report of every mismatch.
This module reads and reports only; the protected reconciliation engine
is untouched.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True, slots=True)
class FillRecord:
    """One fill: simulated or actual."""

    order_id: str
    side: str
    contracts: int
    price: Decimal


def build_daily_reconciliation_report(
    *,
    report_date: str,
    simulated: Sequence[FillRecord],
    actual: Sequence[FillRecord],
) -> str:
    """Render the daily comparison as markdown."""
    simulated_by_id = {record.order_id: record for record in simulated}
    actual_by_id = {record.order_id: record for record in actual}
    missing_actual = sorted(set(simulated_by_id) - set(actual_by_id))
    unexpected_actual = sorted(set(actual_by_id) - set(simulated_by_id))
    mismatched: list[str] = []
    for order_id in sorted(set(simulated_by_id) & set(actual_by_id)):
        sim = simulated_by_id[order_id]
        act = actual_by_id[order_id]
        if (sim.side, sim.contracts, sim.price) != (act.side, act.contracts, act.price):
            mismatched.append(
                f"- `{order_id}`: simulated {sim.side} {sim.contracts} @ {sim.price} "
                f"vs actual {act.side} {act.contracts} @ {act.price}",
            )

    clean = not missing_actual and not unexpected_actual and not mismatched
    lines = [
        f"# Daily fill reconciliation - {report_date}",
        "",
        f"- Simulated fills: {len(simulated)}",
        f"- Actual fills: {len(actual)}",
        f"- Verdict: {'CLEAN' if clean else 'DISCREPANCIES FOUND'}",
        "",
    ]
    if missing_actual:
        lines += ["## Simulated fills with no actual counterpart", ""]
        lines += [f"- `{order_id}`" for order_id in missing_actual]
        lines.append("")
    if unexpected_actual:
        lines += ["## Actual fills the simulation never produced", ""]
        lines += [f"- `{order_id}`" for order_id in unexpected_actual]
        lines.append("")
    if mismatched:
        lines += ["## Fills that disagree", "", *mismatched, ""]
    if clean:
        lines.append("No discrepancies.")
        lines.append("")
    return "\n".join(lines)


def write_daily_reconciliation_report(
    output_root: Path,
    *,
    report_date: str,
    simulated: Sequence[FillRecord],
    actual: Sequence[FillRecord],
) -> Path:
    """Write the daily report under the output root and return its path."""
    output_root.mkdir(parents=True, exist_ok=True)
    path = output_root / f"reconciliation_{report_date}.md"
    path.write_text(
        build_daily_reconciliation_report(report_date=report_date, simulated=simulated, actual=actual),
        encoding="utf-8",
    )
    return path
