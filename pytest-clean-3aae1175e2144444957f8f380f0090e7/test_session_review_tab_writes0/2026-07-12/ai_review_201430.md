# Session review

> AI COMMENTARY - NOT A TRADING SIGNAL
> All data in this report is SYNTHETIC prototype data, never real market results.

Generated: 2026-07-12 20:14 UTC

## Totals

- Decisions recorded: 1
- Accepted: 0
- Rejected: 1

## Condition scoreboard

| Condition | Evaluations | Failures | Failure rate |
| --- | --- | --- | --- |
| Bid reload | 1 | 1 | 100% |
| At important level | 1 | 0 | 0% |

## Chronic blocker

The condition that failed most often was **Bid reload** (1 of 1 evaluations). If this rate looks wrong to your eye, that threshold is the first one to revisit.

## Decision log

- `20:14:30` Rejected lookalike was REJECTED because nobody reloaded the bid, so the level was not truly defended. What did look right: price was sitting at a meaningful level. That combination is a lookalike - exactly the trap these rules exist to filter out.

## Questions to verify by eye

- Watch the level in the replay: was there truly no resting size refreshing at the bid?
