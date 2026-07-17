# Feed Capabilities

Two complementary sources of truth about what the market feed can do — a
**declaration** from the Java bridge and an **observation** from the real stream.
When they disagree, observation wins.

## Declared (Java handshake)

`app/market/protocol.py` consumes the bridge's `connected` handshake:

- `protocol_version` — the receiver refuses an incompatible MAJOR version rather
  than misparsing it; a newer MINOR is accepted as additive.
- `stream_id` (stable per JVM load) + `connection_id` (new per connection) —
  `ConnectionTracker` classifies each handshake as `initial`, `reconnect`
  (same stream, new connection), `new_stream` (a different bridge process, which
  mid-session means the previous stream ended without a clean marker), or
  `duplicate`.
- `capabilities` — what the bridge claims: `aggregated_depth`, `trades`,
  `aggressor_side`, `source_timestamps`. It does **not** claim MBO.

A declaration is a promise, not proof.

## Observed (`app/market/capabilities.py`)

`FeedCapabilities` accumulates evidence from the actual event stream. Each
capability has a status ordered worst→best:

| Status | Meaning |
|---|---|
| `UNAVAILABLE` | structurally impossible (MBO: the bridge exposes no per-order IDs) |
| `UNVERIFIED` | nothing observed yet — the honest start state, never auto-upgraded |
| `DEGRADED` | present but wrong (e.g. thousands of depth updates, zero trades) |
| `HEURISTIC` | derived by this project, not native feed truth (liquidity blocks) |
| `AVAILABLE` | events of this kind genuinely arrived with the required field |

Capability ids are a typed enum, so `caps.get("mbo")` with a typo cannot silently
become a no-op.

### Why observation matters

The GUI previously showed a hardcoded `"Trade prints: AVAILABLE"`. In the real
recordings **71 of 200 finalized sessions carried no trades at all**, and every
one would still have claimed trades were available. Now a depth-only feed reports
trades as `DEGRADED` with the reason "order-flow setups cannot qualify", and CVD
inherits the aggressor-side status because it can never be more trustworthy than
its source.

## Setup gating

A capability the feed cannot supply disables only the setups that need it, never
the whole engine. `iceberg_continuation` is disabled without MBO; every other
order-flow setup keeps running on depth + trades + aggressor side.

Tested in `tests/test_feed_capabilities.py` and `tests/test_bridge_protocol.py`.
