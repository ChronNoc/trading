# Failure and Learning Ledger

| ID | Failure / evidence | Root cause | Correction / regression evidence | Remaining exposure |
|---|---|---|---|---|
| F-001 | Real session drops rose while analysis ran inline | Analysis blocked `recv()`, backpressuring Java | Dedicated `AnalysisFeed`; conservation/load tests | Needs new real Bookmap session |
| F-002 | Recorder throughput collapsed under whole-file rewrites | O(n²) persistence | Buffered closed Parquet parts; recorder load tests | Preserve batching invariant |
| F-003 | Sessions finalized unclean at process exit | Daemon lifecycle had no real drain | Explicit shutdown future/drain; `tests/test_shutdown.py` | Kill -9 remains unclean by design |
| F-004 | Manifest temp-file races/Windows rename failures | Fixed temp path and handle contention | Unique temp names + bounded retry | Defender/indexer behavior remains environment-specific |
| F-005 | Bridge lifetime drops poisoned clean new sessions | Process-lifetime counter treated as session loss | Baseline and persist lifetime/session totals separately; recorder/catalog tests | Older manifests fail closed |
| F-006 | Receiver intake loss was not authoritative | Gap marker did not contribute to manifest quality | Persist `receiver_intake_lost`; invalidate eligibility | Rotation policy needs continued audit |
| F-007 | Malformed bursts retained unbounded strings | One string stored per failure | Bounded samples + exact reason counters | Reason cardinality may still need normalization |
| F-008 | Training accepted arbitrary session directories | CLI searched `trades.parquet`, bypassing catalog and parts storage | Strict `eligible_for_model_training`; parts-aware catalog builder | Old hand-built datasets are noncanonical |
| F-009 | Pooled learning produced only a report | Fold models discarded; no immutable identity or registry | Deterministic dataset/challenger bundle and registry | Runtime scoring intentionally absent |
| F-010 | GUI “model health” could be mistaken for real state | Legacy mock/placeholder fields disconnected | Default `AppSnapshot.model` reads registry/approval truth | Legacy window should remain non-authoritative |
| F-011 | Pooled real rows showed no OOS edge | Feature set/model did not beat take-all baseline after costs | Honest rejection and attempt record, no artifact promotion | More clean sessions and causal hypotheses needed |

Failures are evidence, not embarrassment. A rejected challenger remains rejected; material changes create a new dataset/attempt/artifact identity rather than rewriting history.
