# Session Lifecycle

The single most important distinction in this project, because conflating the
two questions below is the regression that once left the paper engine idle:

| Question | Answer for delayed data |
|---|---|
| May this session be **evaluated on paper**? | **Yes, immediately**, while it is actively recording. |
| May this session be used as **finalized research evidence**? | **Only after clean finalization.** |
| May this data reach a **broker**? | **Never.** |

## Streaming-paper eligibility

An actively recording session powers the `DelayedPaperEngine` from its first
event. It is causal (no lookahead) and independent of finalized-research
eligibility. Recording does not block evaluation; delayed data does not block
paper simulation.

## Finalized-research eligibility

A session becomes canonical research evidence only when ALL hold
(`app/research/session_catalog.py`):

- session closed and finalized
- both depth and trades present
- zero current-session drops and zero overflows
- zero unrecoverable malformed events
- clean shutdown (a `session_ended` terminal marker was delivered)
- completed manifest

An unclean session stays available for diagnostics but is **ineligible** for
canonical research.

## Clean shutdown

The Java bridge sends a `session_ended` control marker on a clean close, which
triggers `recorder.finalize(clean_shutdown=True)`. The Python receiver drains its
queue on shutdown (`app/runtime/shutdown.py`) instead of being killed mid-write —
previously the receiver was a daemon thread killed at process exit, which is
consistent with the dominant real-world ineligibility reason: **192 of 200
finalized sessions were "unclean shutdown"**, now addressed.

## Rotation and catch-up

Each session gets a stable id and resolved contract at creation, propagated to
the recorder, paper engine, ledger, GUI, and logs. On finalize, the launcher
schedules the idempotent episode build and writes the daily learning report and
the paper daily report. On restart, `schedule_pending_builds` discovers finalized
sessions that have not yet been built and queues them once (never twice).

## Contract rollover

Front-month resolution is computed, not tabulated: MNQ expires the third Friday
of Mar/Jun/Sep/Dec and the front contract's last day is eight days before that
(`app/market/contract_resolver.py`). This is defined for every date and reproduces
the hand-verified schedule exactly, so it can never run out like the old table
(which raised `ValueError` past 2027-03-11).

## Measured bottleneck

Of 200 finalized sessions, only **1** is order-flow-eligible: 192× unclean
shutdown, 145× continuity `receiver_error` (31 the temp-file race, now fixed),
71× depth-only, 11× no depth. The two dominant causes are fixed; the resulting
improvement is a **prediction** pending new recordings, not a proven result.
