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
- clean shutdown (a graceful end — see below)
- completed manifest

An unclean session stays available for diagnostics but is **ineligible** for
canonical research.

## Clean shutdown

A session is "clean" when it ended **gracefully**, by either signal:

1. an explicit `session_ended` control marker (the Java bridge sends one on a
   deliberate stop), which triggers `recorder.finalize(clean_shutdown=True)`; or
2. a **normal transport close** — the producer closed the WebSocket without a
   marker. The receiver inspects its socket-pump task: a normal close (no
   transport error) means the peer finished and every buffered frame was drained,
   so the session is clean and complete; only a transport **error** (abnormal
   close) leaves a possibly-truncated tail and finalizes unclean
   (`transport_closed_uncleanly`).

Crucially, "clean" is only about *how the session ended*, never a claim of zero
loss. Data loss is enforced independently and always wins: `RecorderPipeline`
forces `clean_shutdown=False` for any segment that lost a write, and the quality
counters (drops/overflow/malformed) separately block order-flow eligibility. So a
clean flag can never hide loss.

Before this fix, **every** close without an explicit `session_ended` — including
perfectly graceful ones — was recorded as `websocket_closed` **unclean**, which
matched the dominant real-world ineligibility reason: **202 of 224 finalized
sessions were "unclean shutdown"**. The receiver also drains its queue on
shutdown (`app/runtime/shutdown.py`) instead of being killed mid-write.

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

Of 224 finalized sessions, only **1** is order-flow-eligible: 202× unclean
shutdown, 145× continuity `receiver_error` (~35 the temp-file race, now fixed),
79× depth-only, 19× no depth, plus the two richest sessions blocked purely by
malformed events. The dominant `unclean shutdown` cause is now addressed by
treating a graceful transport close as clean (above); the resulting improvement
is a **prediction** pending new recordings, not a proven result. Whether a given
session's close was graceful or a genuine transport error is decided per session
at capture time — this fix reclassifies only the graceful ones.
