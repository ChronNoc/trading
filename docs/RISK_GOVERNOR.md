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

Fees are the prices with the site's 30% coupon code as shown on 2026-11-08 (list price in brackets
where it matters); re-check before buying.

| 25K case | Fee with code (reset) | Floor | Evaluation consistency | Fit for this bot |
|---|---|---|---|---|
| **LucidFlex** | $45.50 ($45); list $65 | end-of-day trailing, $1,000 | 40% (50% is a paid add-on), at least 3 days | **chosen** |
| LucidPro | $75.60 ($75); list $108 | end-of-day trailing, $1,000 | none, can pass in one day | same floor for 66% more per attempt; the funded stage adds a 40% consistency rule and a payout buffer. "No consistency" buys nothing for a one-to-three-contract bot whose days never get near the cap |
| LucidDaily | $60.20 end-of-day floor, $50.40 intraday; list $86 / $72 | evaluation end-of-day or intraday; **funded intraday including open P&L** | 50% (a 30% option is cheaper) | a floor that counts open profit given back punishes a scalper; a hard breach unless flat a minute either side of high-impact news |
| LucidDirect | $230.30 with code; $329 list | end-of-day | funded, 20% per payout cycle | no evaluation, five to seven times the cost, and a per-cycle consistency rule a lumpy bot cannot hold |
| Maxx, Black | | | | invite-only / legacy, not purchasable |

An independent red-team re-derived these from Lucid's pages on 2026-11-08 and also chose Flex 25K.
What would change the choice: a proven edge (a 50K Flex, target/limit 1.5, then becomes worth it), or
Lucid stating that its end-of-day floor counts open P&L (Flex would still win, by less).

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
short in the same contract at once, in one or several accounts; across accounts also a mini against
a micro, and correlated contracts such as long ES against short NQ), so the Bidirectional Paper Lab,
which opens a long and a short together, must never be pointed at a Lucid account; news trading is
allowed on Flex; everything is auto-closed at 4:45 PM Eastern. The optional Daily Loss Limit is $600
on the Flex 25K plan card when switched on at checkout; the governor's $250 day budget is stricter.

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

The code gaps behind that (as found before the governor existed; it closes them when switched on):

| Gap | Where | Effect on a Lucid evaluation |
|---|---|---|
| `lock_out()` was defined but never called anywhere in `app/` (the governor now calls it when the account is no longer active; with the governor off it is still never called) | `app/paper/execution.py` | the account kept trading below the floor; a real one would have failed |
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
| **Geometric survival** | (consequence of the line above) | a string of worst-case days shrinks the buffer by 25% a day and cannot reach the floor **as long as every loss is about one stop**. A single loss bigger than the part of the buffer the day budget leaves untouched (at least 75% of it: at least $225 even at the retire threshold) can still reach it: a gap far beyond a stop. Tested both ways. |
| **Per-trade risk cap** | same | min(budget / 2, 4% of the limit) = **$40**, costs included: commission, slippage, and half the spread a market entry pays (the stop distance is measured from the mid). Price moving between the signal and the fill is not included. The 4% cap binds at normal buffers; the half-budget term only matters as the buffer shrinks, and keeps one contract of a typical stop affordable down to the retire threshold, so an account never sits "active" but frozen. |
| **Hard stop margin** | gaps and slippage | flatten and lock the day within 10% of the limit above the floor ($100). It is a flatten trigger, not a cushion. |
| **Retire** | too little room to survive variance | stop the account when less than 30% of the limit is left |
| **Daily profit cap** | consistency: no day may supply more than **40%** of the **net** account profit (50% only with the add-on); losing days lower the account profit, so they count | stop new entries and flatten an open position once the day's profit, open profit included, reaches (40% - 10-point cushion) of the target = **$375**. The best day of a $1,250 pass then lands near 30%; a winner that jumps straight past the cap in one price event can add a little more (about 33% in a test). Read from the profile, so it follows the checkout choice. |
| **Pass** | target, consistency, minimum days | the target reached, the best day within the consistency share of net profit, and at least **3 trading days** with a trade |
| **Holding hours** | liquidity, and the 16:45 ET auto-close | entries 09:35 to 16:15 New York; flat by 16:40, and any position seen outside 09:35-16:40 (for example after midnight when no event came in between) is flattened, never carried overnight |
| **Entries and streaks** | automation allowed, high-frequency trading prohibited | at most 8 entries a day, 120 s apart; 5 minutes' cooldown after a loss; stop the day after 3 losses in a row. A break-even trade counts as a loss (the cautious reading). |
| **Trade quality** | costs, not results | (target - round-trip costs) / (stop + round-trip costs) must be at least 1.0 |
| **Microscalping** | prohibited above 50% of profit from trades held 5 s or less | **no profit is taken before 6 seconds**: the target, the break-even move and the trailing stop all wait, while the original stop protects from the first event. A winner can then never be a microscalp. The share is still measured (against net profit) and shown; because new trades can only lower it, it no longer blocks entries, which used to freeze an account for good after one fast winner. |
| **Scale on evidence** | none; this is honesty | 1 contract until the bot's own closed trades **indicate** a positive expectancy (at least 100 trades, 95% lower bound above zero, profit factor 1.2); then up to 3, still inside the $40 cap. This is not proof: the test is repeated after every trade, so a strategy with no edge at all is labelled "proven" at some point about 36% of the time by trade 500 and 57% by trade 1,000 (red-team, 300 runs). It never arms anything live. A refuted strategy is held to 1 contract, not stopped, so evidence keeps flowing. |

Every rule is individually testable and reports whether it passed and why; each rejected setup is
logged with every failing rule, not only the first. The governor never loosens anything: the
stricter of it and the generic engine always wins.

Every setting in `config/risk_governor.yaml` has a bound on both sides (in `GovernorPolicy`): it can
be made stricter, or looser within limits on purpose, but never so loose that a Lucid rule or the
survival guarantee goes. For example: entries at most 20 a day, at least 30 s apart, a cooldown of
at least 60 s after a loss, a flat time no later than 16:44, a microscalping window of at least 5 s,
at most 5 contracts, and no NaN anywhere.

## What it can and cannot do for profit: the measured answer

`python -m tools.lucid_fit_report` simulates a Lucid evaluation (the governor's own rule code, a
stop of 40 and target of 80 ticks, 250-day horizon) for a strategy whose TRUE net expectancy is
assumed, beside the previous production behaviour (a fixed four contracts, no daily caps, about 20
trades a day). Re-run on 2026-11-08 at **2,000 runs per row** (seed 7) after the fix pack, with the
95% interval of each pass rate; the first version of this table used 150 runs, and its old-rules
column was too high (it also used the winning-days consistency formula fixed above).

| True edge (R a trade, net) | Old rules: pass (95%) / **busted** | Governor: pass (95%) / retired / **busted** | Median days to pass (governor) |
|---|---|---|---|
| -0.15 | 1% (0-1%) / **99%** | 0% (0-0%) / 100% / **0%** | - |
| -0.05 | 6% (5-7%) / **94%** | 2% (2-3%) / 97% / **0%** | 102 |
| 0 | 17% (16-19%) / **83%** | 19% (17-20%) / 78% / **0%** | 121 |
| +0.05 | 36% (34-38%) / **64%** | 59% (57-62%) / 38% / **0%** | 103 |
| +0.10 | 57% (55-59%) / **43%** | 89% (87-90%) / 11% / **0%** | 76 |
| +0.20 | 86% (84-87%) / **14%** | 99% (99-100%) / 1% / **0%** | 42 |
| +0.35 | 98% (97-98%) / **2%** | 100% (100-100%) / 0% / **0%** | 24 |

At edge 0 about 3% of governed runs are still active at the 250-day horizon (not shown). An
independent re-implementation by the red-team (20,000 runs per row) lands within 1-3 points of
every governor row.

What that says, plainly:

* **No simulated governed account busts, in any row, but only because every simulated loss is
  exactly one stop.** That is the model, not a guarantee: in the red-team's stress runs at +0.10R
  (10,000 runs each), 0.5% of losers losing 25 times the stop gave 5.5% busts, and 0.2% losing 50
  times gave 32%; gaps up to 12 times the stop on 1% of losers gave none. A losing strategy is
  retired instead of failed.
* **With a real edge it passes more often than the old rules** (89% against 57% at +0.10R), but
  slower: weeks to months, not days, because it trades one contract.
* **These pass rates are an upper bound.** They assume independent trades. With the same realised
  +0.10R but clustered losses (a Markov chain, persistence 0.4) the governed pass rate fell to
  56%, and two extra ticks of slippage on every loser took it to 73% (red-team stress, no busts
  in either).
* **The strategy is at about -0.05R a trade today** (-$7.37 on roughly $140 of initial risk).
  That is the row where the governor retires 97% of attempts and the old rules bust 94%. It needs
  roughly **+0.05R or better** to have a better-than-even chance. Costs alone are about 0.03 to
  0.06R a trade at its stop sizes, and its gross result is about zero, so the gap is the
  strategy's gross edge, not risk management. These rules protect the account while that is
  fixed; they cannot fix it.

The simulation shows what edge an account needs; it does not estimate this bot's edge.

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
* It fails safe without ever stopping the recorder:
  * the governor raising while checking an entry **blocks** that entry (`governor_error`);
  * the governor raising while a position is open **flattens** it, locks the day and stops new
    entries;
  * a closed trade the book cannot take stops new entries.
* `config/risk_governor.yaml`:
  * a missing file means the strict defaults (logged);
  * only a real `enabled: false` switches the governor off, and that is logged as a warning;
  * anything else that is not exactly right is an **error**: an unknown or repeated key, `enabled`
    left blank or written as `0`, `null` or `"false"`, an unquoted time like `9:35` (YAML reads it
    as the number 575), NaN, or a value outside its bounds;
  * on an error the strict defaults still guard any open position, but **every entry is refused**
    (`governor_config_invalid`, with the reason in the GUI and the log) until the file is fixed.
    No silent default, no partial loading, and the backend keeps recording.
* **It survives restarts.** The whole book is written atomically to
  `data/paper/governor_state.json` (next to the paper ledger, gitignored) after every entry, close,
  day change and lock, and read back at start-up:
  * the balance, the highest close, today's loss and locks, the losing streak, the cooldowns, the
    consistency history and the trade evidence all carry on;
  * the paper account's balance continues too, instead of going back to $25,000;
  * a state file that exists but cannot be read is never silently replaced: entries are refused
    (`governor_state_unreadable`) until it is fixed;
  * **to start a new evaluation attempt**, stop the backend and delete that file.
* Data gaps: when the feed skips events, an open position is closed at the first price after the
  gap (`DATA_GAP`). That loss is booked by the governor like any other; it is the one loss that can
  land far beyond a stop.
* With the governor on, the paper engine's drawdown room is measured from the highest **closing**
  balance, as Lucid does. Without it the engine keeps its previous peak-balance measure.
* The trading day rolls at 18:00 New York, the CME session start. Lucid takes the closing balance
  "at the end of each trading session" without giving a clock time. One stale event from before the
  roll cannot reopen a finished day.

## What changes when you run it

* The paper account trades **one contract** at the strategy's typical stops (about 57 ticks
  risks about $31 with costs and half the spread; the cap is $40). More only with tighter stops,
  and more than one contract only after its own trades indicate an edge.
* No profit is taken in the first 6 seconds of a trade (Lucid's microscalping rule): a target
  touched sooner is held until then, and the stop is live throughout.
* The paper account now carries on across restarts, from `data/paper/governor_state.json`. Delete
  that file with the backend stopped to start a fresh evaluation attempt.
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
* The governor cannot see slippage beyond a stop when it sizes a trade; a gap is absorbed only by
  the buffer the day budget leaves (see "Geometric survival"). Price moving between the signal and
  the fill is not in the per-trade cap either.
* The Daily Loss Limit ($600 on the Flex 25K plan card, optional at checkout) is not modelled
  separately; the governor's $250 day budget is stricter.
* The governor reads the hard-coded LucidFlex 25K profile (`app/risk/account_profile.py`), not
  `config/prop_profiles/lucid_flex_25k.yaml`. Choosing the 50% consistency add-on at checkout needs a
  code change there, not a config change.
* The "never point the Bidirectional Paper Lab at a Lucid account" rule is documentation only:
  the Lab runs its own separate $100,000 paper account and cannot reach any account today.
* Not enforced: Lucid's cross-account hedging (mini against micro, correlated contracts), the limit
  on funded accounts per household, and the 30-day inactivity rule. They only matter once real
  accounts exist.
* "Scale on evidence" re-tests after every trade (see the rules table); a sequential test would be
  stricter. Its only effect is up to 3 contracts inside the $40 cap.
