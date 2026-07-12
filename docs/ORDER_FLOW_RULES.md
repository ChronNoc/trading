# MNQ Order-Flow Rule Map

This project treats the discretionary day-trading checklist as observe/shadow rules first.
The rules are designed for Bookmap review, delayed-data recording, replay labeling, and
explainable decision logs. They are not live execution permission.

## Checklist mapping

The strategy note is implemented in `app/strategy/order_flow.py` as named pass/fail
conditions:

- `clear_dol`: price has a clear draw on liquidity, such as a higher-timeframe level,
  manual level, psychological number, or visible liquidity block.
- `controlling_side_known`: recent price movement and CVD agree on buyers or sellers.
- `durable_defending_block`: a large bid/ask block persisted for multiple snapshots.
- `defending_block_holds`: the block did not appear and vanish quickly.
- `reload_confirmed`: the defending block dropped below threshold and reloaded.
- `defending_block_stable`: the block is holding a zone instead of chasing price.
- `absorption_confirmed`: aggressive opposite-side volume hit the block and failed to
  push price through it.
- `aggressive_side_failed`: the attacking buyers/sellers did not break the defended
  price by too many ticks.
- `loading_confirmed`: fresh liquidity loaded behind the intended direction after the
  absorption.
- `continuation_confirmed`: CVD, directional market orders, loading, and price reaction
  all support the continuation.
- `clear_take_profit`: there is enough room to the target or DOL.
- `valid_stop_location`: a stop can sit beyond the defended block.
- `market_not_too_fast`: spread, short-term volatility, and velocity are inside limits.
- `entry_after_reaction`: the entry is not on the first touch without response.
- `opening_observation_complete`: the first minutes are observe-only for bias discovery.
- `news_lockout`: a news lockout blocks the setup when active.

## Current safety stance

Free Bookmap delayed futures data is for recording and review only. The automatic runtime
must keep delayed sessions in recording-only mode and must not use delayed data for live
Tradovate decisions.

## Tuning path

1. Record delayed Bookmap sessions.
2. Replay each session and label clean setups, lookalikes, and rejected setups.
3. Compare each condition against forward outcomes by trading day.
4. Tune thresholds only from past sessions, then validate on later days.
5. Keep every accepted and rejected decision logged with its condition list.
