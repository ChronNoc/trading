# Setup lesson — reconstructed rules (איך לזהות סטאפ אמיתי לעסקה)

Source: mentor's Bookmap course video, machine-translated Hebrew → English.
Translation glossary: "block" = liquidity wall (large resting limit orders),
"Adam/red" = warm heatmap color = large size, "customers" = buyers,
"grandiosy number" = large printed size. **Every rule below must be verified
against the video by a human before entering any config.**

## The setup, as taught (with video timestamps)

1. **A large resting liquidity wall at a meaningful level** (04:16–05:16).
   Identified in Bookmap by heatmap color intensity (the redder, the bigger)
   plus a large printed size number at the level. Wall + big number = "bingo,
   that's your area."
2. **Aggressive market orders attack the wall and FAIL to break it**
   (05:53–06:21) — absorption. Proof of failure: price does not trade
   through the level.
3. **A series of aggressive orders appears on the other side** (07:20–07:33)
   — the reversal begins.
4. **CVD confirmation** (08:25–09:02): if CVD was deeply negative (mentor's
   examples: −800, −1000) and snaps back toward zero during the absorption,
   aggressive buyers are flipping the tape → "ten out of ten, bingo."
   Explicit warning (01:52–02:04): CVD alone is NOT the setup — a rising CVD
   without the wall/absorption context means nothing.
5. **A new wall is created live at the entry level** (09:34–10:09): the
   defenders sell at market AND place fresh limits, building a new wall.
   Entry: limit order at that newly formed wall (10:33–10:39).
6. **Wall quality filter** (12:05–12:35): walls that flash in and out are
   algorithmic spoofing — irrelevant. The wall must be SOLID (persist).
   The entry wall being solid made the example "A+."

## Risk rules stated

- **Stop: 10 points** (10:09–10:27). Rationale: "if it doesn't work at 10
  points it won't work at 15 or 20; more than 10 and the setup is done —
  take the stop, wait for the next setup."
  - MNQ: 10 points = **40 ticks** (current prototype spec uses 8 ticks —
    must be reviewed/changed when resolving the real spec).
- Expectancy thinking in R-multiples; a great session ≈ 1:10 R, normal
  sessions 1:5/1:6; "not every day is a 1:10 day" (02:46–03:00).

## Mapping to the engine

| Mentor's rule | Engine condition | Status |
| --- | --- | --- |
| Wall at meaningful level | at_important_level + liquidity_minimum | exists |
| Absorption (attack fails) | aggressive_sell_volume + downward_progress_ticks | exists |
| Wall holds / reloads | bid_reload_count | exists |
| Opposite-side aggression | (partially reclaimed_level) | partial |
| CVD flip confirmation | MarketState.cumulative_volume_delta | **added — condition not yet in spec** |
| Wall solidity (anti-spoof) | — | **missing — future feature** |
| Stop 10 points (40 ticks) | exit.stop_method | **spec says 8 ticks — review** |

## Open questions for the human

- Exact wall size threshold ("grandiosy number") — not stated numerically in
  this lesson; check the live-session videos.
- CVD flip magnitude that counts as confirmation (−800→0 was an example, not
  a rule).
- How long a wall must persist to count as "solid."

## Live session #1 confirmations (video 2)

Transcript: `data/video_out/live_session_1/`. Rougher translation, but the
repeated hard numbers agree with the lesson:

- **Stop = 10 points** — stated FOUR separate times (38:15 he mentions 30 for
  a wider-context trade, but 10 is the setup rule: 40:12, 40:22, 66:42,
  81:38). This strongly confirms `exit.stop_method`.
- **Targets are R-multiple based** (26:58–41:57): typical target **1:6**,
  ideal **1:10**, and he scales out / takes partials at **1:2** and **1:3**.
  With a 10-point stop, 1:6 ⇒ ~60-point target, 1:10 ⇒ ~100-point. Needs
  human confirmation — translation of these passages is noisy.
- **Break-even management**: he moves the stop to break-even after the trade
  works ("there is no limit / break even ... I can always hide it", 27:20),
  matching the spec's `exit.break_even_rule` field.
- **CVD referenced constantly** as live confirmation (throughout), consistent
  with the lesson's rule #4.
- **Walls ("big blocks") + aggression + absorption** described repeatedly,
  consistent with rules #1–#3.

Still unconfirmed after two videos: exact wall-size number, CVD-flip
magnitude, wall-solidity duration. Worth targeting these in the remaining
Bookmap videos (אסטרטגיות_מסחר_בוקמאפ, ניהול_סיכונים).

## Bookmap strategies video (video 3) — findings

Transcript: `data/video_out/bookmap_strategies/`.

- **Stop = 10 points confirmed a THIRD time** — ~10 more mentions (10:45,
  14:13, 29:02, 40:54, 43:27, 59:00, 63:55...). Across three videos this is
  now the single most-repeated, bulletproof rule. There is also a **tighter
  5-point stop variant** in one cluster (44:00–45:20) — a different/smaller
  setup; needs human review of which context uses 5 vs 10.
- **Targets remain R-multiple based**: 1:3 ("we see once a week, it's
  enough"), up to 1:10 ("the dream"), also 1:7 mentioned. Consistent with
  session #1's 1:6/1:10.
- **IMPORTANT methodology note** (00:15–01:13): the mentor states he trades
  **both** ICT (he names IFVG, SMT) on TradingView **and** Bookmap order flow
  together — "half and half." So the ICT videos in the library (FVG, SMT,
  IFVG, daily bias) are the SAME trader's complementary confluence layer, not
  a separate system. For THIS bot, the Bookmap order-flow concepts (walls,
  absorption, CVD) are what map to the engine; the ICT layer would be a
  separate future feature, not part of the current spec.

## Why no strategy_v*.yaml has been proposed yet (honest status)

Three videos in, the ENTRY thresholds are still not numerically defined:

- **Wall size** — the mentor judges it visually by heatmap color intensity
  ("the redder the bigger"), never a fixed contract count. This may be
  inherently discretionary, which is a real automation problem: an
  order-flow engine needs a number. Candidate approach: measure resting size
  at the wall in your own recorded sessions and pick a percentile threshold.
- **CVD-flip magnitude** — examples given (−800/−1000 → 0) but no rule.
- **Aggressive-volume minimum / reload count / reclaim ticks** — described
  qualitatively, never numerically.

Resolving the spec now would mean GUESSING these values, which AGENTS.md
forbids ("every field resolved by a human; no black-box output"). The
disciplined path: leave them `unresolved` in the spec, then MEASURE them
from your recorded Stage-C sessions (compare real setups vs lookalikes),
and let the discovery pipeline search the ranges. The videos give the
STRUCTURE and the risk rules (stop 10, targets 1:3–1:10); the recorded
data must supply the entry NUMBERS.
