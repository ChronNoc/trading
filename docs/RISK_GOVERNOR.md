# Lucid risk governor

`app/risk/lucid_governor.py`, configured by `config/risk_governor.yaml`, wired into the paper
engine by `app/paper/streaming_engine.py`. **PAPER only**: it never routes an order, and LIVE
stays locked by `config/production_config.yaml` and `app/execution/live_gate.py`.

## What it is, and what it is not

It is a new set of account-level rules built around how a Lucid evaluation is actually won and
lost. It is **not** a way to make the strategy profitable. No risk rule can turn a
negative-expectancy strategy positive: it decides how long the account survives while the
strategy proves or disproves itself, keeps the account inside Lucid's rules, and refuses to
scale size up without the bot's own evidence. Profit has to come from the strategy, and the paper
record says it has not shown that yet (below).

Every number is a **fraction of the account profile's own limits** (its max-loss limit, its
profit target, its consistency share). Nothing was tuned from backtest results, so nothing can
be adjusted to make a result look better.

## Which Lucid case, and why: LucidFlex 25K evaluation

Lucid sells several programs. Compared from Lucid's own help centre and plan cards (read
2026-11-08; the LucidFlex facts below were re-checked on the primary pages):

| 25K case | Fee (reset) | Floor | Evaluation consistency | Fit for this bot |
|---|---|---|---|---|
| **LucidFlex** | $45.50 ($45) | end-of-day trailing, $1,000 | 40% (50% is a paid add-on), at least 3 days | **chosen** |
| LucidPro | $75.60 ($75) | end-of-day trailing, $1,000 | none, can pass in one day | same floor for 66% more per attempt; the funded stage adds a 40% consistency rule and a payout buffer. "No consistency" buys nothing for a one-to-three-contract bot whose days never get near the cap |
| LucidDaily | $60.20 ($60) | evaluation end-of-day or intraday; **funded intraday including open P&L** | 40% | a floor that counts open profit given back punishes a scalper; a hard breach unless flat a minute either side of high-impact news |
| LucidDirect | $329 list | end-of-day | funded, 20% per payout cycle | no evaluation, about five times the cost, and a per-cycle consistency rule a lumpy bot cannot hold |
| Maxx, Black | | | | invite-only / legacy, not purchasable |

Why Flex 25K:

* **The cheapest attempt**, and the floor is the same end-of-day trail as Pro: intraday swings
  never ratchet it; only closing balances do.
* **Smallest target for its limit.** Target / limit is 1.25 on 25K, 1.5 on 50K and 2.0 on 100K
  and 150K. With no edge, the chance of reaching the target before the floor is
  limit / (target + limit): 44%, 40%, 33%, 33%. A bigger account only helps if there is proven
  edge to put extra size behind, and there is none yet.
* **No consistency rule once funded**, and automation is permitted.
* Its 20-micro cap is far above anything the rules will use (one to three contracts).

Lucid rules the governor is built around: automated strategies are allowed; **microscalping is
prohibited** (flagged when more than 50% of profit comes from trades held 5 seconds or less);
**high-frequency trading is prohibited** (no number given); **hedging is prohibited** (long and
short in the same contract at once, in one or several accounts), so the Bidirectional Paper Lab,
which opens a long and a short together, must never be pointed at a Lucid account; news trading is
allowed on Flex; everything is auto-closed at 4:45 PM Eastern.

## What the old rules did to a Lucid account

From the real paper ledger (1,007 trades, 33 sessions, 13 days, none synthetic) and the code:

| Finding | Number |
|---|---|
| Net result, current configuration (596 trades) | **-$4,394**; expectancy **-$7.37 a trade**, profit factor 0.52 |
| Gross result before commission | -$2,020: costs are not the only problem, the strategy has no gross edge yet |
| Worst drawdown | **-$4,645** against a $1,000 limit; two runs lost over $1,500 each; **153 trades opened at or below the floor** |
| Trades a day | 99 on average (up to 229): far past anything resembling a careful evaluation |
| Risk per full stop | about $119 (up to $135) at four contracts; ten trades lost more than the $130 cap |
| Full stops | 103 trades hit their initial stop and lost $11,216; every other trade together won $4,875 |
| Documents' own verdict | "not proven profitable" (profitability meter: claim not supported) |

The code gaps behind that:

| Gap | Where | Effect on a Lucid evaluation |
|---|---|---|
| `lock_out()` is defined but never called anywhere in `app/` | `app/paper/execution.py` | the account kept trading below the floor; a real one would have failed |
| Fixed-size mode ignores the drawdown room | `size_intent()` with `paper_fixed_contracts: 4` | size never shrank as the buffer did |
| `paper_max_entries_per_day: 0`, `paper_max_losses_per_day: 0` (unlimited) | `config/production_config.yaml` | no daily stop at all (its own comment says to restore caps for a real evaluation) |
| "Peak balance" was the best balance after any trade | `DelayedPaperEngine._peak_balance` | Lucid's floor trails the best **end-of-day close** |
| No awareness of the consistency rule, the flat time or microscalping | nowhere | one big day can make a pass impossible; Lucid auto-closes at 16:45 |
| A 6-tick target on MNQ loses about 75% of itself to costs | the canonical strategy's own targets | needs a win rate above 90% before it can pay |

## The rules (numbers for the LucidFlex 25K evaluation)

| Rule | Lucid fact it comes from | Number |
|---|---|---|
| **Floor** | the max-loss limit trails the highest **closing** balance, then locks | $24,000 at the start; `AccountProfile.trailing_threshold` |
| **Day loss budget** | touching the floor ends the account | 25% of the buffer above the floor **at the start of the day** = $250 |
| **Geometric survival** | (consequence of the line above) | a string of worst-case days shrinks the buffer by 25% a day and cannot reach the floor; only a gap beyond a stop could. Tested. |
| **Per-trade risk cap** | same | min(budget / 2, 4% of the limit) = **$40**, costs included. The 4% cap binds at normal buffers; the half-budget term only matters as the buffer shrinks, and keeps one contract of a typical stop affordable down to the retire threshold, so an account never sits "active" but frozen. |
| **Hard stop margin** | gaps and slippage | flatten and lock the day within 10% of the limit above the floor ($100) |
| **Retire** | too little room to survive variance | stop the account when less than 30% of the limit is left |
| **Daily profit cap** | consistency: no day may supply more than **40%** of total profit (50% only with the add-on) | stop for the day at (40% - 10-point cushion) of the target = **$375**, so the best day of a $1,250 pass is at most 30%. Read from the profile, so it follows the checkout choice. |
| **Entry window** | liquidity, and the 16:45 ET auto-close | 09:35 to 16:15 New York; flat by 16:40 |
| **Entries and streaks** | automation allowed, high-frequency trading prohibited | at most 8 entries a day, 120 s apart; 5 minutes' cooldown after a loss; stop the day after 3 losses in a row |
| **Trade quality** | costs, not results | (target - round-trip costs) / (stop + round-trip costs) must be at least 1.0 |
| **Microscalping guard** | prohibited above 50% of profit from trades held 5 s or less | block entries once 30% of profit does |
| **Scale on evidence** | none; this is honesty | 1 contract until the bot's own closed trades **prove** a positive expectancy (at least 100 trades, 95% lower bound above zero, profit factor 1.2); then up to 3. A refuted strategy is held to 1 contract, not stopped, so evidence keeps flowing. |

Every rule is individually testable and reports whether it passed and why; each rejected setup is
logged with every failing rule, not only the first. The governor never loosens anything: the
stricter of it and the generic engine always wins.

## What it can and cannot do for profit: the measured answer

`python -m tools.lucid_fit_report` simulates a Lucid evaluation (the governor's own rule code, a
stop of 40 and target of 80 ticks, 150 runs per row, 250-day horizon) for a strategy whose TRUE
net expectancy is assumed, beside the previous production behaviour (a fixed four contracts, no
daily caps):

| True edge (R a trade, net) | Old rules: pass / **busted** | Governor: pass / retired / **busted** | Median days to pass (governor) |
|---|---|---|---|
| -0.15 | 1% / **99%** | 0% / 100% / **0%** | - |
| -0.05 | 13% / **87%** | 1% / 99% / **0%** | 100 |
| 0 | 33% / **67%** | 18% / 79% / **0%** | 100 |
| +0.05 | 49% / **51%** | 57% / 41% / **0%** | 102 |
| +0.10 | 60% / **40%** | 89% / 11% / **0%** | 81 |
| +0.20 | 83% / **17%** | 99% / 1% / **0%** | 44 |
| +0.35 | 99% / **1%** | 100% / 0% / **0%** | 25 |

What that says, plainly:

* **The governor makes bust impossible in every row**, and turns a losing strategy into a retired
  account instead of a failed one.
* **With a real edge it passes more often than the old rules** (89% against 60% at +0.10R), but
  slower: weeks to months, not days, because it trades one contract.
* **The strategy is at about -0.05R a trade today** (-$7.37 on roughly $140 of initial risk).
  That is the row where the governor retires 99% of attempts and the old rules bust 87%. It needs
  roughly **+0.05R or better** to have an even chance. Costs alone are about 0.03 to 0.06R a
  trade at its stop sizes, and its gross result is about zero, so the gap is the strategy's
  gross edge, not risk management. These rules protect the account while that is fixed; they
  cannot fix it.

The simulation assumes independent trades and a fixed stop and target; it shows what edge an
account needs, it does not estimate this bot's edge.

## How it plugs in

* `app/risk/lucid_governor.py` is pure: Decimal money, explicit inputs and outputs, no broker,
  network or paper-engine imports (a test enforces it).
* `DelayedPaperEngine(governor_policy=...)` is optional and **off by default**, so every
  existing test and tool behaves exactly as before. The two production start-up paths
  (`tools/start_backend.py`, `tools/start_assistant.py`) pass the policy from
  `config/risk_governor.yaml`.
* It only ever **adds** restrictions. The generic gate, `size_intent()`, the daily caps and the
  protected risk modules (`limits.py`, `sizing.py`, `kill_switch.py`) stay in place underneath.
  **None of the protected files was modified.**
* It fails safe in both directions: a broken governor **blocks** entries rather than waving them
  through, and a broken or weakening `risk_governor.yaml` falls back to the strict defaults (and
  says so in the log). A typo in the file is an error, not a silent default.
* With the governor on, the paper engine's drawdown room is measured from the highest **closing**
  balance, as Lucid does. Without it the engine keeps its previous peak-balance measure.

## What changes when you run it

* The paper account trades **one contract** at the strategy's typical stops (about 57 ticks
  risks $30.74 with costs; the cap is $40). More only with tighter stops, and more than one
  contract only after its own trades prove an edge.
* Entries are limited to 09:35-16:15 New York, at most 8 a day. The previous "relaxed" research
  profile traded overnight, without caps, to collect samples quickly; with the governor on, that
  stops. To collect research samples again, set `enabled: false` in `config/risk_governor.yaml`
  and restart: the generic engine then runs exactly as before.
* Evidence accumulates more slowly. That is the cost of testing the strategy under the rules a
  real evaluation would impose.

## Not covered

* The offline ledger over recorded sessions (`app/research/paper_ledger.py`) still sizes with the
  generic engine; only the live paper engine uses the governor.
* Early-close holidays are not in a calendar: flat before an early close is not automatic.
* The funded stage (payout rules, a smaller starting size) is not modelled.
* Whether Lucid judges the floor on live balance including open profit is not stated on its
  drawdown page; the governor treats open losses as real, which is the cautious reading.
* The governor cannot see slippage beyond a stop; the hard stop margin is the allowance for it.
* The Daily Loss Limit is an optional checkout setting whose amount is not on the evaluation or
  drawdown pages; the governor's $250 day budget is stricter than any plausible figure.
