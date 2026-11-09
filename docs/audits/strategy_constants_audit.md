# Strategy constants audit

Generated from the code by `python -m tools.constants_audit`; do not edit by hand. `tests/test_constants_audit.py` fails if a tunable number is added without a rating or if this file is stale.

## What a rating means

A rating is a judgement about **fragility**: how likely a fixed value is to stop fitting when volatility, liquidity or the time of day changes. It is not evidence that any value was tuned, and not a claim that any value is wrong. The reason for the audit: a public MNQ study catalogued its own constants and found its edge fitted to one volatility regime, with fixed point thresholds failing when the regime changed.

- **HIGH**: a fixed market-scale value (ticks, points, contracts) that gates entries, defines a label or sets an exit, with no scaling to volatility or liquidity, and not at the one-tick floor.
- **MEDIUM**: a fixed time or count window that shapes decisions; a value near the tick floor; a hand-picked ratio; or a cost or fill assumption that flatters results when too optimistic.
- **LOW**: an exchange fact, an account or safety limit, an evidence gate (stricter is the safe direction), a mode switch, a disabled placeholder, or a small fixed-capacity model setting.

## Summary

187 parameters across 20 classes: **23 high**, 55 medium, 109 low.

By kind: market-scale 55, time 25, count 20, ratio 15, cost 22, limit 13, structural 12, switch 20, model 5.

## Validate or scale these first (HIGH)

Every one is a fixed number in market units. The suggested handling for a market-scale value is to express it as a fraction of the walk-forward typical first-hour range (`reference_multiple` in `app/strategy/dynamic_thresholds.py`, fed by `VolatilityRegimeTable.typical_range_for`), then check results per volatility regime. For a label, report results per regime before trusting any pooled number.

| Parameter | Default | Role | Why it is fragile |
|---|---|---|---|
| `OrderFlowThresholds.large_block_minimum` | `90` | entry-gate | Fixed contracts for a 'large' resting block. Displayed size varies with time of day, contract month and regime and is not scaled. Same number is copied into EpisodeConfig; DailyLearningConfig uses 75. |
| `OrderFlowThresholds.absorption_volume_minimum` | `400` | entry-gate | Fixed contracts that must trade into a level. Volume per minute varies several-fold across sessions and regimes; not scaled. Copied into EpisodeConfig. |
| `OrderFlowThresholds.follow_through_volume_minimum` | `50` | entry-gate | Fixed contracts of follow-through volume; not scaled to the session's activity. |
| `OrderFlowThresholds.loading_liquidity_minimum` | `50` | entry-gate | Fixed contracts of new resting liquidity; not scaled to normal book depth. |
| `OrderFlowThresholds.cvd_support_minimum` | `25` | entry-gate | Fixed contracts of cumulative-delta support; not scaled to activity. |
| `OrderFlowThresholds.absorption_max_progress_ticks` | `3` | entry-gate | Fixed ticks of allowed progress while absorbing; the same 3 ticks means different things in calm and fast markets. |
| `OrderFlowThresholds.max_volatility_ticks` | `12` | entry-gate | Fixed ticks of allowed volatility. The code comment records a measurement (74-82% of real 180 s windows exceed it, so it is judged over a 10 s tail), which is evidence, yet the number is still a fixed tick count that moves with the regime. |
| `OrderFlowThresholds.max_price_velocity_ticks_per_second` | `8` | entry-gate | Fixed speed cap; what counts as fast depends on the regime. |
| `OrderFlowThresholds.max_dol_distance_ticks` | `120` | entry-gate | Fixed 120 ticks (30 points) of target reach. Exactly the kind of fixed point threshold that stops fitting when volatility changes. |
| `MomentumThresholds.min_progress_ticks` | `8` | entry-gate | Fixed ticks of required progress; not scaled to volatility. |
| `MomentumThresholds.max_pullback_ticks` | `6` | entry-gate | Fixed ticks of allowed pullback; a calm-market limit that a fast market breaches constantly. |
| `RegimeClassifier.low_volatility` | `0.15` | regime | Absolute tick-volatility cut-off used to route profiles. Regime boundaries are where fixed cut-offs drift; recalibrate from the observed distribution. |
| `RegimeClassifier.high_volatility` | `0.80` | regime | Absolute tick-volatility cut-off; same concern as the low cut-off. |
| `RegimeClassifier.extreme_volatility` | `1.50` | regime | Absolute tick-volatility cut-off; same concern. |
| `RegimeClassifier.thin_liquidity` | `50` | regime | Fixed contract count for 'thin' depth; depth varies with time of day and contract month. |
| `RegimeClassifier.deep_liquidity` | `250` | regime | Fixed contract count for 'deep' depth; same concern. |
| `EpisodeConfig.stop_buffer_points` | `10` | exit | Fixed 10 points (40 ticks) beyond the defended level; not scaled to volatility. |
| `EpisodeConfig.large_block_minimum` | `90` | entry-gate | Copy of OrderFlowThresholds.large_block_minimum; two copies can drift apart. |
| `EpisodeConfig.absorption_volume_minimum` | `400` | entry-gate | Copy of OrderFlowThresholds.absorption_volume_minimum; two copies can drift apart. |
| `SessionTrainingConfig.target_ticks` | `12` | label | Fixed 12-tick target defines every ML label. Base rate and difficulty change with volatility, so labels are not comparable across regimes. |
| `SessionTrainingConfig.stop_ticks` | `8` | label | Fixed 8-tick stop defines every ML label; same concern as the target. |
| `PaperAccountConfig.stop_points` | `10` | exit | Fixed 10-point stop; not scaled to volatility. |
| `ActivationSpec.stop_ticks` | `2` | exit | Fixed 2-tick stop defines the whole Lab outcome; tight stops lose to noise and costs. |

## Same name in several places

A parameter defined in more than one class can drift apart. Rows marked **differ** need a human to confirm the values mean the same thing.

| Name | Values | |
|---|---|---|
| `tick_size` | OrderFlowThresholds `0.25`, EpisodeConfig `0.25`, SessionTrainingConfig `0.25`, AccountConfig `0.25`, DailyLearningConfig `0.25` | agree |
| `large_block_minimum` | OrderFlowThresholds `90`, EpisodeConfig `90` | agree |
| `absorption_volume_minimum` | OrderFlowThresholds `400`, EpisodeConfig `400` | agree |
| `feature_window_size` | ShadowPipelineConfig `50`, SimulatorConfig `50` | agree |
| `tick_value` | EpisodeConfig `0.50`, PaperAccountConfig `0.50`, AccountConfig `0.50` | agree |
| `window_span_seconds` | EpisodeConfig `180.0`, SessionTrainingConfig `180.0` | agree |
| `break_even_trigger_ticks` | EpisodeConfig `0`, ExecutionConfig `0` | agree |
| `break_even_lock_ticks` | EpisodeConfig `0`, ExecutionConfig `0` | agree |
| `trail_activation_ticks` | EpisodeConfig `0`, ExecutionConfig `0` | agree |
| `trail_distance_ticks` | EpisodeConfig `0`, ExecutionConfig `0` | agree |
| `max_entries_per_day` | EpisodeConfig `3`, ExecutionConfig `3`, ProjectSafetyLimits `3` | agree |
| `max_losses_per_day` | EpisodeConfig `3`, ExecutionConfig `3`, PaperAccountConfig `3`, ProjectSafetyLimits `3` | agree |
| `fixed_contracts` | EpisodeConfig `0`, ExecutionConfig `0` | agree |
| `max_risk_per_trade_usd` | EpisodeConfig `0`, ExecutionConfig `0` | agree |
| `min_reward_risk` | EpisodeConfig `0`, ExecutionConfig `0` | agree |
| `entry_order_type` | EpisodeConfig `'market'`, ExecutionConfig `'market'` | agree |
| `entry_limit_offset_ticks` | EpisodeConfig `0`, ExecutionConfig `0` | agree |
| `entry_limit_cancel_ticks` | EpisodeConfig `0`, ExecutionConfig `0` | agree |
| `entry_require_trade_through` | EpisodeConfig `False`, ExecutionConfig `False` | agree |
| `stop_ticks` | SessionTrainingConfig `8`, ActivationSpec `2` | **differ** |
| `commission_per_contract` | ExecutionConfig `1.24`, FillModelConfig `1.24`, AccountConfig `0.62` | **differ** |
| `entry_slippage_ticks` | ExecutionConfig `1`, AccountConfig `0` | **differ** |
| `stop_slippage_ticks` | ExecutionConfig `1`, AccountConfig `1` | agree |
| `starting_balance` | PaperAccountConfig `100000`, AccountConfig `100000` | agree |

## Every parameter

### OrderFlowThresholds

`app.strategy.order_flow.OrderFlowThresholds`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `tick_size` | `0.25` | structural | instrument | LOW | MNQ tick size, an exchange fact. |
| `large_block_minimum` | `90` | market-scale | entry-gate | HIGH | Fixed contracts for a 'large' resting block. Displayed size varies with time of day, contract month and regime and is not scaled. Same number is copied into EpisodeConfig; DailyLearningConfig uses 75. |
| `durable_block_min_snapshots` | `3` | count | entry-gate | MEDIUM | Observations a block must survive; depends on snapshot cadence. |
| `min_reload_count` | `1` | count | entry-gate | LOW | One reload is the smallest meaningful bar, not a fitted value. |
| `absorption_volume_minimum` | `400` | market-scale | entry-gate | HIGH | Fixed contracts that must trade into a level. Volume per minute varies several-fold across sessions and regimes; not scaled. Copied into EpisodeConfig. |
| `follow_through_volume_minimum` | `50` | market-scale | entry-gate | HIGH | Fixed contracts of follow-through volume; not scaled to the session's activity. |
| `loading_liquidity_minimum` | `50` | market-scale | entry-gate | HIGH | Fixed contracts of new resting liquidity; not scaled to normal book depth. |
| `cvd_support_minimum` | `25` | market-scale | entry-gate | HIGH | Fixed contracts of cumulative-delta support; not scaled to activity. |
| `absorption_max_progress_ticks` | `3` | market-scale | entry-gate | HIGH | Fixed ticks of allowed progress while absorbing; the same 3 ticks means different things in calm and fast markets. |
| `min_reaction_ticks` | `1` | market-scale | entry-gate | LOW | One tick is the smallest possible move, so there is nothing to fit. |
| `min_continuation_ticks` | `2` | market-scale | entry-gate | MEDIUM | Two ticks, near the tick floor. |
| `minimum_target_ticks` | `4` | market-scale | exit | MEDIUM | A floor, not a tuned target; costs make anything smaller unattractive. |
| `minimum_stop_ticks` | `2` | market-scale | exit | MEDIUM | A floor near the tick granularity so a stop is not inside the noise. |
| `max_spread_ticks` | `4` | market-scale | entry-gate | MEDIUM | A liquidity safety filter; MNQ normally trades one tick wide, so four is rarely binding. |
| `max_volatility_ticks` | `12` | market-scale | entry-gate | HIGH | Fixed ticks of allowed volatility. The code comment records a measurement (74-82% of real 180 s windows exceed it, so it is judged over a 10 s tail), which is evidence, yet the number is still a fixed tick count that moves with the regime. |
| `max_price_velocity_ticks_per_second` | `8` | market-scale | entry-gate | HIGH | Fixed speed cap; what counts as fast depends on the regime. |
| `speed_tail_seconds` | `10` | time | context | MEDIUM | Fixed horizon for the speed check. |
| `first_observation_minutes` | `10` | time | entry-gate | MEDIUM | Fixed opening-observation length. |
| `enforce_opening_observation` | `True` | switch | entry-gate | LOW | Visible profile choice (canonical keeps it on). |
| `min_reaction_snapshots` | `3` | count | entry-gate | MEDIUM | Fixed number of snapshots; depends on snapshot cadence. |
| `max_dol_distance_ticks` | `120` | market-scale | entry-gate | HIGH | Fixed 120 ticks (30 points) of target reach. Exactly the kind of fixed point threshold that stops fitting when volatility changes. |
| `block_move_tolerance_ticks` | `2` | market-scale | entry-gate | MEDIUM | Two ticks, near the tick floor. |
| `block_stability_tail_seconds` | `0` | time | context | LOW | Zero in canonical (whole window); the relaxed profile sets it. |
| `use_initial_balance_levels` | `False` | switch | entry-gate | LOW | Opt-in: adds the first-hour high/low as targets. Off by default, so behaviour is unchanged. |

### MomentumThresholds

`app.strategy.momentum.MomentumThresholds`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `min_progress_ticks` | `8` | market-scale | entry-gate | HIGH | Fixed ticks of required progress; not scaled to volatility. |
| `min_pullback_ticks` | `1` | market-scale | entry-gate | LOW | One tick is the smallest possible move. |
| `max_pullback_ticks` | `6` | market-scale | entry-gate | HIGH | Fixed ticks of allowed pullback; a calm-market limit that a fast market breaches constantly. |
| `pullback_window_seconds` | `30` | time | context | MEDIUM | Fixed window for the pullback. |

### RegimeClassifier

`app.market.regime_classifier.RegimeClassifier`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `min_samples` | `5` | count | regime | MEDIUM | History needed before a regime is called. |
| `stale_after_ns` | `5000000000` | time | regime | LOW | Five-second freshness guard. |
| `low_volatility` | `0.15` | market-scale | regime | HIGH | Absolute tick-volatility cut-off used to route profiles. Regime boundaries are where fixed cut-offs drift; recalibrate from the observed distribution. |
| `high_volatility` | `0.80` | market-scale | regime | HIGH | Absolute tick-volatility cut-off; same concern as the low cut-off. |
| `extreme_volatility` | `1.50` | market-scale | regime | HIGH | Absolute tick-volatility cut-off; same concern. |
| `thin_liquidity` | `50` | market-scale | regime | HIGH | Fixed contract count for 'thin' depth; depth varies with time of day and contract month. |
| `deep_liquidity` | `250` | market-scale | regime | HIGH | Fixed contract count for 'deep' depth; same concern. |

### ShadowPipelineConfig

`app.strategy.shadow.ShadowPipelineConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `feature_window_size` | `50` | count | context | MEDIUM | Snapshots per feature window. |
| `bid_reload_level` | `1` | count | context | LOW | A book level index, not a market-scale value. |
| `bid_reload_threshold` | `1` | market-scale | context | LOW | One contract, the minimum possible size. |
| `log_mode` | `'observe'` | switch | infra | LOW | Observe-only logging mode. |

### EpisodeConfig

`app.research.episode_builder.EpisodeConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `tick_size` | `0.25` | structural | instrument | LOW | MNQ tick size, an exchange fact. |
| `tick_value` | `0.50` | structural | instrument | LOW | MNQ dollars per tick per contract, an exchange fact. |
| `stop_buffer_points` | `10` | market-scale | exit | HIGH | Fixed 10 points (40 ticks) beyond the defended level; not scaled to volatility. |
| `fixed_slippage_ticks` | `1` | cost | simulation | MEDIUM | Fixed one tick; optimistic when the market is fast. |
| `volatility_slippage_multiplier` | `0` | cost | simulation | MEDIUM | Zero, so simulated slippage ignores volatility and flatters fast markets. |
| `commission_per_round_turn` | `1.24` | cost | simulation | LOW | A broker fee; verify it against the account's real fee. |
| `queue_position_fraction` | `0` | cost | simulation | MEDIUM | Zero assumes the front of the queue for resting orders, which is optimistic. |
| `window_size` | `200` | count | context | MEDIUM | Events per analysis window. |
| `warmup_events` | `100` | count | context | LOW | Events skipped while the window fills. |
| `decision_stride` | `25` | count | context | MEDIUM | How often decisions are evaluated. |
| `window_span_seconds` | `180.0` | time | context | MEDIUM | Seconds of market time per window. |
| `depth_sample_interval_ms` | `250.0` | time | context | MEDIUM | Depth sampling cadence. |
| `evaluation_interval_ms` | `1000.0` | time | context | MEDIUM | Evaluation cadence. |
| `warmup_span_seconds` | `60.0` | time | context | MEDIUM | Seconds of warm-up before evaluating. |
| `large_block_minimum` | `90` | market-scale | entry-gate | HIGH | Copy of OrderFlowThresholds.large_block_minimum; two copies can drift apart. |
| `absorption_volume_minimum` | `400` | market-scale | entry-gate | HIGH | Copy of OrderFlowThresholds.absorption_volume_minimum; two copies can drift apart. |
| `timeout_seconds` | `900` | time | exit | MEDIUM | Fixed episode timeout. |
| `dedupe_price_ticks` | `4` | market-scale | label | MEDIUM | Fixed ticks used to treat nearby setups as one. |
| `dedupe_seconds` | `300` | time | label | MEDIUM | Fixed seconds used to treat close setups as one. |
| `momentum_enabled` | `False` | switch | entry-gate | LOW | Visible profile choice. |
| `ml_decision_policy_enabled` | `False` | switch | entry-gate | LOW | Visible profile choice (off). |
| `strategy_profile` | `'canonical'` | switch | entry-gate | LOW | Names the profile. |
| `instrument` | `'MNQ'` | structural | instrument | LOW | The traded instrument. |
| `break_even_trigger_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `break_even_lock_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `trail_activation_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `trail_distance_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `max_entries_per_day` | `3` | limit | risk | LOW | Daily limit from the account and safety rules; conservative by design. |
| `max_losses_per_day` | `3` | limit | risk | LOW | Daily limit from the account and safety rules; conservative by design. |
| `fixed_contracts` | `0` | count | risk | LOW | Zero or off by default; no effect until a profile turns it on. |
| `max_risk_per_trade_usd` | `0` | limit | risk | LOW | Zero or off by default; no effect until a profile turns it on. |
| `min_reward_risk` | `0` | ratio | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `target_reward_risk` | `0` | ratio | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `entry_order_type` | `'market'` | switch | entry-gate | LOW | Market orders by default. |
| `entry_limit_offset_ticks` | `0` | market-scale | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `entry_limit_timeout_seconds` | `0` | time | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `entry_limit_cancel_ticks` | `0` | market-scale | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `entry_require_trade_through` | `False` | switch | simulation | LOW | Off, so a touch fills a resting order, which is optimistic. |
| `entry_cooldown_seconds` | `300` | time | entry-gate | MEDIUM | Fixed five-minute cooldown. |
| `time_stop_seconds` | `900` | time | exit | MEDIUM | Fixed 15-minute time stop. |

### SessionTrainingConfig

`app.machine_learning.session_training.SessionTrainingConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `target_ticks` | `12` | market-scale | label | HIGH | Fixed 12-tick target defines every ML label. Base rate and difficulty change with volatility, so labels are not comparable across regimes. |
| `stop_ticks` | `8` | market-scale | label | HIGH | Fixed 8-tick stop defines every ML label; same concern as the target. |
| `horizon_seconds` | `300.0` | time | label | MEDIUM | Fixed 300 s label horizon. |
| `sample_interval_seconds` | `30.0` | time | context | MEDIUM | Fixed sampling cadence for training rows. |
| `window_span_seconds` | `180.0` | time | context | MEDIUM | Seconds of market time per window. |
| `sample_interval_ms` | `250.0` | time | context | MEDIUM | Depth sampling cadence. |
| `warmup_seconds` | `60.0` | time | context | MEDIUM | Seconds of warm-up per session. |
| `tick_size` | `0.25` | structural | instrument | LOW | MNQ tick size, an exchange fact. |

### ModelTrainingConfig

`app.machine_learning.train.ModelTrainingConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `random_state` | `7` | model | model | LOW | A seed. |
| `logistic_max_iter` | `1000` | model | model | LOW | Solver iteration cap. |
| `xgboost_estimators` | `25` | model | model | LOW | Small fixed capacity (25 trees). |
| `xgboost_max_depth` | `2` | model | model | LOW | Shallow trees (depth 2). |
| `xgboost_learning_rate` | `0.20` | model | model | LOW | A conventional learning rate. |

### ExecutionConfig

`app.paper.execution.ExecutionConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `commission_per_contract` | `1.24` | cost | simulation | LOW | A broker fee; the Lab uses 0.62 per side, so confirm both mean the same unit. |
| `entry_slippage_ticks` | `1` | cost | simulation | MEDIUM | Fixed one tick; optimistic when the market is fast. |
| `stop_slippage_ticks` | `1` | cost | simulation | MEDIUM | Fixed one tick; stops can slip more in fast markets. |
| `cooldown_ns` | `300000000000` | time | entry-gate | MEDIUM | Fixed five-minute cooldown. |
| `time_stop_ns` | `900000000000` | time | exit | MEDIUM | Fixed 15-minute time stop. |
| `max_entries_per_day` | `3` | limit | risk | LOW | Daily limit from the account and safety rules; conservative by design. |
| `max_losses_per_day` | `3` | limit | risk | LOW | Daily limit from the account and safety rules; conservative by design. |
| `break_even_trigger_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `break_even_lock_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `trail_activation_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `trail_distance_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `fixed_contracts` | `0` | count | risk | LOW | Zero or off by default; no effect until a profile turns it on. |
| `max_risk_per_trade_usd` | `0` | limit | risk | LOW | Zero or off by default; no effect until a profile turns it on. |
| `min_reward_risk` | `0` | ratio | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `entry_order_type` | `'market'` | switch | entry-gate | LOW | Market orders by default. |
| `entry_limit_offset_ticks` | `0` | market-scale | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `entry_limit_timeout_ns` | `0` | time | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `entry_limit_cancel_ticks` | `0` | market-scale | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `entry_require_trade_through` | `False` | switch | simulation | LOW | Off, so a touch fills a resting order, which is optimistic. |
| `min_profit_hold_ns` | `0` | time | exit | LOW | Zero or off by default; no effect until a profile turns it on. |

### FillModelConfig

`app.simulator.fill_engine.FillModelConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `queue_ahead_contracts` | `0` | cost | simulation | MEDIUM | Zero assumes nothing is ahead in the queue, which is optimistic. |
| `liquidity_fraction` | `1` | ratio | simulation | MEDIUM | Assumes 100% of displayed liquidity can be taken, which is optimistic. |
| `ack_delay_ns` | `0` | cost | simulation | MEDIUM | Zero latency is optimistic. |
| `cancel_delay_ns` | `0` | cost | simulation | MEDIUM | Zero latency is optimistic. |
| `slippage_per_contract` | `0` | cost | simulation | MEDIUM | Zero slippage is optimistic. |
| `commission_per_contract` | `1.24` | cost | simulation | LOW | A broker fee. |

### SlippageModel

`app.simulator.slippage.SlippageModel`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `fixed_ticks` | `0` | cost | simulation | MEDIUM | Zero fixed slippage is optimistic. |
| `volatility_multiplier` | `0` | cost | simulation | MEDIUM | Zero means slippage ignores volatility. |

### SimulatorConfig

`app.simulator.engine.SimulatorConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `acknowledgement_delay_ns` | `0` | cost | simulation | MEDIUM | Zero latency is optimistic. |
| `cancel_replace_delay_ns` | `0` | cost | simulation | MEDIUM | Zero latency is optimistic. |
| `feature_window_size` | `50` | count | context | MEDIUM | Snapshots per feature window. |
| `short_term_volatility` | `0` | market-scale | context | LOW | An input default of zero, not a tuned threshold. |

### CostModel

`app.discovery.backtest.CostModel`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `commission_r` | `0.02` | ratio | simulation | MEDIUM | Commission as a fraction of risk (0.02 R); hand-picked. |
| `slippage_r` | `0.05` | ratio | simulation | MEDIUM | Slippage as a fraction of risk (0.05 R); hand-picked. |
| `worst_case_multiplier` | `3` | ratio | simulation | MEDIUM | Hand-picked stress multiple. |

### PaperAccountConfig

`app.discovery.paper_account.PaperAccountConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `starting_balance` | `100000` | structural | risk | LOW | Account size. |
| `stop_points` | `10` | market-scale | exit | HIGH | Fixed 10-point stop; not scaled to volatility. |
| `tick_value` | `0.50` | structural | instrument | LOW | MNQ dollars per tick per contract, an exchange fact. |
| `ticks_per_point` | `4` | structural | instrument | LOW | Four ticks per point on MNQ, an exchange fact. |
| `max_trades_per_day` | `3` | limit | risk | LOW | Daily limit from the account and safety rules; conservative by design. |
| `max_losses_per_day` | `3` | limit | risk | LOW | Daily limit from the account and safety rules; conservative by design. |
| `daily_loss_fraction` | `0.01` | limit | risk | LOW | Daily loss cap from the account rules. |
| `commission` | `1.24` | cost | simulation | LOW | A broker fee. |
| `slippage_ticks` | `1` | cost | simulation | MEDIUM | Fixed one tick; optimistic when the market is fast. |
| `max_account_attempts` | `50` | count | risk | LOW | A search cap. |

### AccountConfig

`app.labs.bidirectional.config.AccountConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `starting_balance` | `100000` | structural | risk | LOW | Account size. |
| `tick_size` | `0.25` | structural | instrument | LOW | MNQ tick size, an exchange fact. |
| `tick_value` | `0.50` | structural | instrument | LOW | MNQ dollars per tick per contract, an exchange fact. |
| `commission_per_contract` | `0.62` | cost | simulation | LOW | Per side, per contract (0.62); ExecutionConfig uses 1.24, confirm both mean the same unit. |
| `entry_slippage_ticks` | `0` | cost | simulation | MEDIUM | Zero entry slippage is optimistic. |
| `exit_slippage_ticks` | `0` | cost | simulation | MEDIUM | Zero exit slippage is optimistic. |
| `stop_slippage_ticks` | `1` | cost | simulation | MEDIUM | One tick on stops; can be more in fast markets. |
| `max_gross_contracts` | `100000` | limit | risk | LOW | A sanity cap on simulated exposure. |

### ActivationSpec

`app.labs.bidirectional.config.ActivationSpec`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `long_qty` | `1` | count | entry-gate | LOW | Contracts per side, chosen by the user. |
| `short_qty` | `1` | count | entry-gate | LOW | Contracts per side, chosen by the user. |
| `stop_ticks` | `2` | market-scale | exit | HIGH | Fixed 2-tick stop defines the whole Lab outcome; tight stops lose to noise and costs. |
| `source` | `'last'` | switch | entry-gate | LOW | Which price triggers a level. |
| `comparison` | `'auto'` | switch | entry-gate | LOW | How a level triggers. |
| `mode` | `'paired'` | switch | entry-gate | LOW | Paired or one-cancels-other. |
| `enabled` | `True` | switch | entry-gate | LOW | A level on/off flag. |
| `one_shot` | `True` | switch | entry-gate | LOW | Fire once. |
| `max_activations` | `1` | count | entry-gate | LOW | One activation. |
| `rearm_cooldown_ns` | `0` | time | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `require_leave_reenter` | `False` | switch | entry-gate | LOW | Off by default. |
| `leave_distance_ticks` | `4` | market-scale | entry-gate | MEDIUM | Fixed 4 ticks to re-arm; only matters when re-arming is on. |
| `reenter_distance_ticks` | `0` | market-scale | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `expire_after_ns` | `0` | time | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |
| `cancel_if_moves_away_ticks` | `0` | market-scale | entry-gate | LOW | Zero or off by default; no effect until a profile turns it on. |

### BreakEvenConfig

`app.labs.bidirectional.config.BreakEvenConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `enabled` | `False` | switch | exit | LOW | Off by default. |
| `trigger_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `offset_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |

### TrailConfig

`app.labs.bidirectional.config.TrailConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `enabled` | `False` | switch | exit | LOW | Off by default. |
| `immediate` | `False` | switch | exit | LOW | Off by default. |
| `activation_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `distance_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |
| `step_ticks` | `0` | market-scale | exit | LOW | Zero or off by default; no effect until a profile turns it on. |

### ProjectSafetyLimits

`app.risk.account_profile.ProjectSafetyLimits`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `max_entries_per_day` | `3` | limit | risk | LOW | Daily limit from the account and safety rules; conservative by design. |
| `max_losses_per_day` | `3` | limit | risk | LOW | Daily limit from the account and safety rules; conservative by design. |
| `daily_risk_fraction` | `0.01` | limit | risk | LOW | Daily risk cap from the account rules. |
| `allow_averaging_down` | `False` | switch | risk | LOW | Forbidden by default, a safety rule. |

### ProgressConfig

`app.research.profitability_progress.ProgressConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `max_drop_rate` | `0.01` | ratio | evidence-gate | LOW | Promotion gate; stricter is the safe direction. |
| `min_eligible_sessions` | `20` | count | evidence-gate | LOW | Promotion gate; stricter is the safe direction. |
| `min_completed_setups` | `100` | count | evidence-gate | LOW | Promotion gate; revisit if the setups-per-day rate makes it easy to satisfy. |
| `min_independent_days` | `20` | count | evidence-gate | LOW | Promotion gate; stricter is the safe direction. |
| `min_net_expectancy_r` | `0.05` | ratio | evidence-gate | LOW | Promotion gate; stricter is the safe direction. |
| `min_profit_factor` | `1.30` | ratio | evidence-gate | LOW | Promotion gate; stricter is the safe direction. |
| `max_drawdown_fraction` | `0.10` | ratio | evidence-gate | LOW | Promotion gate; stricter is the safe direction. |
| `max_consecutive_losses` | `6` | count | evidence-gate | LOW | Promotion gate; stricter is the safe direction. |
| `max_day_concentration` | `0.40` | ratio | evidence-gate | LOW | Promotion gate against one lucky day. |
| `max_hour_concentration` | `0.50` | ratio | evidence-gate | LOW | Promotion gate against one lucky hour. |
| `min_side_balance` | `0.20` | ratio | evidence-gate | LOW | Promotion gate against one-sided results. |

### DailyLearningConfig

`app.machine_learning.daily_learning.DailyLearningConfig`

| Field | Default | Kind | Role | Risk | Note |
|---|---|---|---|---|---|
| `large_block_size` | `75` | market-scale | report | MEDIUM | Report-only, but it defines 'large block' as 75 contracts while OrderFlowThresholds uses 90: two definitions. |
| `reload_threshold` | `75` | market-scale | report | MEDIUM | Report-only fixed contract count. |
| `cvd_dominance_fraction` | `0.20` | ratio | report | LOW | Report-only fraction. |
| `tick_size` | `0.25` | structural | instrument | LOW | MNQ tick size, an exchange fact. |

## Not covered

- Numbers written in strategy specification YAML files are chosen per strategy file by the operator and are not defaults in code.
- Status and counter dataclasses hold observations, not tunable choices.
- Ratings judge fragility from the value's shape; they do not know any tuning history.
