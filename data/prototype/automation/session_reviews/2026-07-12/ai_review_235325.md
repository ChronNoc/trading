# Session review

> AI COMMENTARY - NOT A TRADING SIGNAL
> All data in this report is SYNTHETIC prototype data, never real market results.

Generated: 2026-07-12 23:53 UTC

## Totals

- Decisions recorded: 2
- Accepted: 1
- Rejected: 1

## Condition scoreboard

| Condition | Evaluations | Failures | Failure rate |
| --- | --- | --- | --- |
| Ask pull ratio | 2 | 1 | 50% |
| Bid reload | 2 | 1 | 50% |
| Level reclaim | 2 | 1 | 50% |
| Aggressive sell volume | 2 | 0 | 0% |
| At important level | 2 | 0 | 0% |
| Limited downward progress | 2 | 0 | 0% |
| News lockout | 2 | 0 | 0% |

## Chronic blocker

The condition that failed most often was **Level reclaim** (1 of 2 evaluations). If this rate looks wrong to your eye, that threshold is the first one to revisit.

## Decision log

- `23:52:48` watching prototype setup stream was ACCEPTED. All 7 conditions lined up: price was sitting at a meaningful level; heavy aggressive selling hit the book; all that selling barely pushed price lower; buyers kept reloading the bid at the level; sellers pulled their offers away from the market; price reclaimed the defended level; no news lockout was active. The only action was a hypothetical shadow bracket - no real order exists.
- `23:52:53` watching prototype setup stream was REJECTED because nobody reloaded the bid, so the level was not truly defended; the offers stayed stacked overhead; price never reclaimed the defended level. What did look right: price was sitting at a meaningful level; heavy aggressive selling hit the book; all that selling barely pushed price lower; no news lockout was active. That combination is a lookalike - exactly the trap these rules exist to filter out.

## Questions to verify by eye

- Watch the level in the replay: was there truly no resting size refreshing at the bid?
- Check whether price ever traded back above the defended level within the window.
- Look at the ask side: did offers pull, or did they stay stacked?
