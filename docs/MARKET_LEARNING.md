# Causal Paper/Shadow Market Learning

The assistant records Bookmap sessions and builds causal setup episodes, fixed-rule outcome labels, deterministic training datasets, daily observation reports, and—only when all objective gates pass—immutable offline challenger artifacts. Exact-approved model evidence can be consumed by a conservative paper-only policy. It never authorizes broker execution.

No current real-data challenger has passed. The implemented synthetic integration proves software mechanics only, not market edge, cross-session real-market learning, or profitability.

## Historical Evidence

Each completed session may contribute:

- depth/trade event totals and source mode;
- protocol/provider/capability and continuity provenance;
- analysis/model-training eligibility and explicit exclusion reasons;
- aggressive buy/sell volume, CVD, price direction, depth imbalance, reload, and absorption observations;
- accepted and rejected setup evaluations;
- fixed target/stop/timeout outcomes with `label_resolved_timestamp_ns`;
- source hashes, replay-order quality, and build summaries.

Descriptive observation coverage is not a performance metric. Strategy-performance sections are populated only from completed quality-gated outcomes.

## Build Real Episodes

After a Bookmap session is finalized, run:

```powershell
.\.venv\Scripts\python.exe -m tools.build_real_episodes
```

The builder streams closed Parquet parts in bounded batches. New recordings replay by receiver-local `receive_sequence`; old recordings fall back to timestamp ordering and expose same-timestamp ambiguity. It writes:

- `data/processed/{session_id}.decisions.jsonl` for accepted/rejected checklists;
- `data/processed/{session_id}.episodes.jsonl` for accepted setups and outcomes;
- `data/processed/{session_id}.build.json` for counts, quality, hashes, and rejection tallies;
- `data/labels/{session_id}.labels.jsonl` for fixed triple-barrier labels.

Empty episode/label files are honest when no setup passes. Eligibility or strategy thresholds are never loosened merely to populate a report or train a model.

New receiver sessions flush readable closed files under `depth_parts/` and `trade_parts/` while recording. On clean finalization they remain compatible with legacy compacted `depth.parquet` and `trades.parquet`. `receive_sequence` is local provenance added by Python after protocol validation.

## Deterministic Challenger Lifecycle

The offline pipeline:

1. selects only finalized model-training-eligible sessions from the catalog;
2. constructs the shared causal feature contract used online;
3. labels each row under the shared fixed triple-barrier rule;
4. records when each label became knowable;
5. validates source hashes before and after construction;
6. canonically orders/encodes rows into a content-addressed dataset;
7. trains each walk-forward fold on prior days only;
8. purges labels unresolved at the fold boundary;
9. evaluates on unseen sessions/dates with no train/test overlap;
10. publishes no challenger unless every objective gate passes;
11. binds model, dataset, validation, feature contract, and artifact bytes in immutable evidence.

Training never writes approval. Runtime loading never guesses “latest”; it requires one exact artifact ID+SHA in the approval file.

## Runtime Paper Policy

Both in-process and detached startup build one shared `ObserveOnlyFeatureSink`, `ObserveOnlyModelLoader`, prediction journal, and outcome tracker. The same loader is passed into the authoritative `DelayedPaperEngine`.

The policy is:

- default-off;
- paper/shadow-only;
- conservative veto-only;
- incapable of promoting a heuristic-rejected setup;
- fail-closed to the unchanged heuristic result when evidence is missing, stale, malformed, mismatched, corrupt, or unavailable.

Usable evidence must atomically match prediction ID, artifact ID/SHA, current session, requested direction, decision timestamp, and maximum correlation age. The authoritative evaluation record persists confidence/raw output, model and prediction identity, decision source, fallback reason, and policy version.

The legacy `decision_impact: "none"` prediction field means the loader does not directly mutate broker state. It does not mean the authoritative paper-policy consumer cannot use correctly correlated evidence.

## Prediction Outcomes and Retraining Boundary

Eligible runtime predictions are appended to `shadow_predictions.jsonl` and registered with `PendingOutcomeTracker`. Later events resolve target-first, stop-first, deterministic tie, or timeout outcomes under the same triple-barrier rule, and append them to `shadow_outcomes.jsonl`.

The current challenger builder does **not** directly read `shadow_outcomes.jsonl`. Instead, it reconstructs equivalent causal historical outcome evidence from persisted completed session events through `build_session_training_rows`. This distinction prevents an unsupported direct feedback-edge claim while preserving a valid causal cross-session retraining path.

## Automatic Reports

When `start_mnq_assistant.bat` is running, each completed Bookmap connection writes:

- `data/reports/{date}/{session_id}/summary.json`;
- `data/reports/{date}/{session_id}/decisions.jsonl`;
- `data/reports/daily_learning/{date}/daily_learning.json`;
- `data/reports/daily_learning/{date}/daily_learning.md`.

The daily report refreshes after each completed session for that UTC recording date. Model-evaluation reporting separately consumes available dataset, attempt, registry, approval, prediction, outcome, heuristic, fallback, and baseline evidence, and marks unavailable comparisons explicitly.

## Manual Report Command

```powershell
.\.venv\Scripts\python.exe -m tools.daily_learning_summary --date 2026-07-12
```

Custom roots:

```powershell
.\.venv\Scripts\python.exe -m tools.daily_learning_summary `
  --raw-root data/raw `
  --report-root data/reports `
  --processed-root data/processed `
  --date 2026-07-12
```

## Safety and Current Evidence Limits

- Delayed/free Bookmap data remains paper/research-only.
- Ambiguous ordering, malformed events, sequence gaps, unfinished outcomes, and causally unresolved labels are excluded.
- Missing capabilities are not represented as numeric zero.
- A reported setup is not proven merely because it appears in a daily report.
- Runtime artifact freshness derives from immutable walk-forward test-date evidence, not filesystem modification time.
- The repository approval file remains empty and runtime loading/scoring disabled.
- `live_enabled` and `live_mode` remain false.
- Broker execution modules are structurally unreachable from ML policy code.

Current real evidence: 250 physical manifests, 5 analysis-eligible sessions, 1 replay-eligible session, 0 model-training-eligible sessions, and 0 current-contract training rows. All 4,109 legacy rows lack `label_resolved_timestamp_ns` and fail closed. Both real attempts failed all five walk-forward gates. No real artifact is published, registered, approved, or available as evidence of profitability.
