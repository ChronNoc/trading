# Order-flow traceability matrix (STAGE 5 / correction #9)

The Bookmap bridge delivers **depth updates and trades only** — no visual bubbles
or heatmap pixels. Every capability below is an *honestly derived* equivalent
computed from depth/trade events, evaluated by the deterministic strategy
(`evaluate_day_trading_plan`) and the causal context tracker. No future event is
used to derive any context, level, threshold, entry, or direction.

Every strategy condition is logged per evaluation in the decision audit
(`DecisionAudit`, written to `data/processed/<session>.decisions.jsonl`) with:
`checks` (pass/fail), `condition_messages` (observed value vs threshold),
`source_event_range` (evidence timestamps, always ending at or before the decision
index — proven by `tests/test_order_flow_traceability.py`), `provenance`, and
`ordering_mode`.

| Requested capability | Derived from depth/trades as | Named condition / context field | Source |
|---|---|---|---|
| Persistent liquidity blocks | Resting size holding at a level across events | `durable_defending_block` | depth |
| Block duration & cancellation | How long a block holds / when it is pulled | `defending_block_holds`, `defending_block_stable` | depth |
| Stacking / pulling | Size added vs removed near the level | `defending_block_stable` | depth |
| Aggressive buy/sell bubbles | Trade aggressor bursts (proxy for bubbles) | `market_bubbles_support_direction` | trades |
| CVD & CVD divergence | Cumulative signed trade volume vs price | `cvd_supports_direction` | trades |
| Absorption | Aggression met by resting size without price giving way | `absorption_confirmed` | depth+trades |
| Failed auction / failed breakout | Rejection after probing a level | `continuation_confirmed`, `entry_after_reaction` | depth+trades |
| Bid/ask reload & iceberg proxy | Repeated refills at a level after being hit | `reload_confirmed`, `loading_confirmed` | depth |
| Sweeps | Rapid multi-level aggression | `aggressive_side_failed` (context) | trades |
| Trade velocity | Trades per unit time | `market_not_too_fast` | trades |
| Price velocity | Price change per unit time | `market_not_too_fast` | depth |
| Spread | Best bid/ask distance | context (`_context_from_window`) | depth |
| Volatility | Recent range / dispersion | regime context | depth |
| Distance to prior-day & overnight levels | Session-so-far highs/lows only (no lookahead) | causal context (`CausalLevelTracker`) | depth |
| DOL (draw on liquidity) | Nearest opposing resting pool | context | depth |
| Psychological levels | Round-number proximity (100-point grid) | context (`psychological_interval`) | derived |
| Controlling side | Which side is defending/initiating | `controlling_side_known` | depth+trades |
| Opening observation | Warm-up window completed before deciding | `opening_observation_complete` | both |
| Defended level / stop / target | Level held, stop beyond it, R-multiple target | `valid_stop_location`, `clear_take_profit` | derived |

**Verification status:** the derivation, per-condition evidence logging, and the
no-lookahead property are covered by `tests/test_order_flow_traceability.py` and
`tests/test_episode_builder.py`. On the current real data every eligible
evaluation *rejects* on these conditions (see `rejected_condition_tally`), which
is the honest result — the strategy manufactures nothing.
