# Strategy candidate recommendation

**THE SYSTEM STOPS HERE.** This report is the pipeline's final output.
No config flag was changed. No broker connection was opened. Going live
is a separate, manual, human decision - see AGENTS.md and Part 7.

## Candidate: reload=2|volume=450|pull=0.55|target=2.0|stop=1.0

### Validation (walk-forward) metrics
- trades: 177
- win rate: 0.8870
- profit factor: 32.4659
- max drawdown (R): 0.9200
- sortino: 8.1478
- expectancy (R): 1.1715
- composite: 4.5470

### Holdout metrics (evaluated exactly once)
- trades: 52
- win rate: 0.8654
- profit factor: 45.9389
- max drawdown (R): 0.4400
- sortino: 13.7974
- expectancy (R): 1.1321
- composite: 4.4922

### Worst-case cost pass
- expectancy (R): 1.0315

### Per-regime expectancy
- ranging/high_vol: 1.2578R
- ranging/low_vol: 1.0220R
- trending/high_vol: 1.3043R
- trending/low_vol: 1.0941R

### Gates
- [pass] label_leakage: all 3 features decision-time safe
- [pass] sample_size: 177 trades meets minimum 100
- [pass] regime_robustness: positive in regimes ['ranging/high_vol', 'ranging/low_vol', 'trending/high_vol', 'trending/low_vol'] and sessions ['london', 'new_york_afternoon', 'new_york_open']
- [pass] monte_carlo: risk of ruin 0.0000, p95 drawdown 1.73R
- [pass] sensitivity: neighbors held composite >= 3.8756 vs base 4.5470
- [pass] worst_case_costs: expectancy 1.0315R survives worst-case costs

Shadow comparison: no previous best exists; this is the first leader.

Data source: SYNTHETIC episodes - not valid for live decisions until real Stage C data replaces them.