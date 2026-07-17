# Automatic delayed-paper pipeline

## The regression this documents (fixed in `f81486e`)

**Symptom.** Bookmap connected, receiver listening, recording yes, market data
updating, zero drops — yet: paper engine `idle`, setup `none`, decision
`awaiting evaluation`, "No setup has been evaluated yet on real delayed data",
evaluations 0 forever.

**Root cause.** Two different questions were conflated into one flag:

| Question | Correct answer for delayed data |
|---|---|
| May this data reach a **broker**? | **Never.** |
| May this data be evaluated on **paper**? | **Yes, immediately.** |

`AutomaticRuntimeController.handle_control_event`'s `delayed_mode` branch set
`decisions_allowed = False` and drove the runtime to `RECORDING_ONLY`
("recording only; shadow decisions disabled"). Setup evaluation therefore only
ran **offline**, over *finalized* sessions — so an actively recording session
produced zero evaluations indefinitely. `SnapshotSource._paper()` compounded it
by returning a hardcoded placeholder rather than reading any engine.

**Fix.** `app/paper/streaming_engine.py` evaluates the live stream causally and
is fed every validated event by the launcher. `decisions_allowed` still gates
broker/shadow decisions (delayed data must never route an order); it no longer
gates paper evaluation.

## The pipeline

```
Bookmap event
  -> Java forwarder            (verified aggressor + pip semantics)
  -> WebSocket receiver        (BoundedIntakeBuffer, loss is loud)
  -> validated normalized event
  -> [controller]              broker/shadow decisions: DISABLED for delayed
  -> [DelayedPaperEngine]      paper evaluation: ALWAYS ON
        MarketState.update(event)          # causal, event by event
        rolling window (bounded)
        warm-up gate (bounded + visible)
        derive_strategy_context(...)       # same fn as the episode builder
        evaluate_day_trading_plan(...)     # same fn as the episode builder
        -> EvaluationRecord (full lineage)
  -> GUI snapshot              (immutable, no I/O on the Qt thread)
```

## Two independent eligibilities — never conflate again

| | Streaming paper | Finalized research |
|---|---|---|
| Needs a finalized session? | **No** | Yes |
| Runs while recording? | **Yes** | No |
| Needs clean shutdown/checksums? | No | Yes |
| Zero session drops required? | No (loss is reported) | **Yes** |
| Purpose | live causal simulation | canonical evidence |

An active session is simultaneously *eligible for streaming paper* and *not yet
eligible for finalized research*. That is correct, not a bug.

## Guarantees (each pinned by a test)

- Auto-start: feeding events is the only trigger — no button, no finalized file.
- Warm-up is bounded and visible (`WAITING -> WARMING_UP -> EVALUATING`).
- Evaluation count climbs while the stream runs; it never sticks at zero.
- Every rejection names its failing condition; reasons are tallied for the GUI.
- Full lineage: session id, setup id, event index, timestamp, direction,
  strategy version, every condition + message.
- Causality: `event_index <= events_seen`; no lookahead, no replay peeking.
- One strategy: streaming and replay share `evaluate_day_trading_plan`.
- Structural safety: the module imports no execution/broker/tradovate module.
- Capability gating disables only affected setups (no MBO -> no iceberg setup).
- Session id is never "unknown" once recording starts.

## Honest zero

Zero trades is acceptable **only** with visible evaluations and exact reasons.
On real recorded MNQ the strategy currently rejects every setup (most common:
`opening_observation_complete`, `clear_take_profit`, `valid_stop_location`).
Thresholds are never loosened to manufacture trades.
