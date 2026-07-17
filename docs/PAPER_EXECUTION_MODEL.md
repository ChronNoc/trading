# Paper Execution Model

How a real market event becomes a simulated trade, and why each assumption is
what it is. Everything below is enforced by tests in `tests/test_paper_execution.py`,
`tests/test_paper_lifecycle.py`, and `tests/test_paper_ledger.py`.

## The automatic lifecycle

No button exists. The launcher builds the engine at startup and the receiver
feeds it every event:

```
market event
  → MarketState.update                     (app/market/state.py)
  → causal window (deque, past events only)
  → executor.on_tick                       ← resolves EXISTING exposure first
  → evaluate_day_trading_plan              (app/strategy/order_flow.py)
  → candidate intent                       ← only from an ACCEPTED setup
  → gate() structural checks
  → size_intent() via app.risk.sizing      ← the project risk engine, not a copy
  → submit() → PENDING order
  → causal fill on a LATER event → open position
  → manage stop / target / time stop
  → close → P&L, costs, balance
  → PaperLedger.append (JSONL, durable)
  → GUI status + reports/research
```

Position management runs on **every** event, not on the evaluation stride: a stop
may not wait for the next evaluation.

## Causality rules

| Rule | Why |
|---|---|
| An order is fillable only from `event_index + 1` | The event that produced the signal cannot also fill it — that is lookahead. |
| A position never resolves on its own entry event | Same reason, at the exit side. |
| The window holds only events already seen | The strategy cannot consult the future. |

## Ambiguity always resolves against the trade

| Situation | Resolution |
|---|---|
| Stop and target both reachable in one event | `AMBIGUOUS`, booked **at the stop**. The print order is unknowable, so the win is refused. |
| Price gaps through the stop | Fills at the **gapped price**, not the stop. A stop-market cannot fill where nothing traded. |
| Crossed / unusable book | Entry is **blocked**, never guessed. |
| Contract unresolved | Entry is **blocked**: an untraceable trade is not allowed. |

## Fills are prices MNQ could actually trade

A market buy lifts the offer; a market sell hits the bid. Neither transacts at
the mid, which is frequently not a tradeable price at all (e.g. `29500.875`).
Every fill is therefore taken from the opposing side of the book, has adverse
slippage added, and is snapped to the 0.25 grid **away from us** (`align_to_tick`).
When the book is absent, the traded price is the best available evidence.

> This was a real defect found during implementation: entries were booking at
> mid + slippage, producing impossible prices like `29500.875` and a P&L that
> could never have been achieved. Fixing it moved a fixture trade from `+2.02`
> to `+1.52` — worse, and correct.

## Costs and economics

| Quantity | Value | Source |
|---|---|---|
| Tick size | `0.25` | MNQ contract spec |
| Tick value | `$0.50` | MNQ contract spec |
| Point value | `$2.00` / contract | 4 ticks × $0.50 |
| Commission | `$1.24` / contract round turn | `ExecutionConfig` |
| Entry slippage | 1 tick, always adverse | `ExecutionConfig` |
| Stop slippage | 1 tick, always adverse | `ExecutionConfig` |

All money and prices are `Decimal`. A float never touches a price, a P&L, or the
ledger — the ledger stores money as strings and asserts it in tests.

## Structural constraints before risk (`gate()`)

`one_position_max`, `duplicate_setup_occurrence`, `setup_cooldown` (5 min),
`daily_entry_lock` (3/day), `daily_loss_lock` (3/day), `drawdown_lock`,
`stale_or_crossed_book`, `contract_unresolved`, `invalid_stop`.

Each returns a stable machine-readable code plus human text. The GUI shows the
text; nothing is rejected silently. Sizing then runs through
`app.risk.sizing.calculate_position_size` — the same engine the offline ledger
uses, so streaming and replay cannot drift apart — and is capped by the account
profile's `max_contracts`.

## What can never happen

* **No fabricated trade.** A position opens only when the strategy genuinely
  accepts a setup and every gate passes. Zero trades is a valid outcome and the
  GUI prints the exact reasons.
* **No invented levels.** The stop and target come from the strategy's own
  derived context. If it supplies neither, there is no trade
  (`strategy_levels_incomplete`) — nothing is guessed to make a trade possible.
* **No broker contact.** `app/paper/*` imports no `app.execution`, tradovate,
  gateway, or broker module. Proven structurally by AST inspection across the
  whole package in `tests/test_streaming_paper_engine.py`.
* **No silent data loss.** An event the parser rejects increments
  `malformed_events`, which the GUI shows as a warning.

## Synthetic fixtures are quarantined

Tests use deterministic synthetic tapes. Every such trade is stamped
`is_synthetic_fixture: true`, rendered as `[FIXTURE]` in the GUI, and excluded
from `LedgerRecovery.real_records` and `realized_pnl`. **A fixture trade is never
evidence of profitability.**

## The ledger

`data/paper/paper_trades.jsonl` (gitignored — user-owned trading records).
Append-only; reopening continues the file and never overwrites it. A torn final
line from a crash is tolerated: intact records survive and `damaged_tail` is
reported rather than hidden. Each row carries full lineage — session, setup,
strategy version, contract, decision/open/close event indices — so any trade can
be traced back to the exact events that caused it.
