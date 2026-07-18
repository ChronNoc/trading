# Evidence Pack — paper execution, durability, diagnostics

## 2026-07-19 continuation update

This section supersedes older test totals and current-state claims below while
preserving the historical evidence. Starting HEAD:
`dfc3759c8806256adcf006431f24544dc987b11a`.

- Persistent detached supervisor/backend implemented with an OS-held singleton
  lease, versioned process identity, PID-reuse protection, configuration
  fingerprint, startup handshake, health timeout, bounded restart/backoff, and
  verified clean stop. Killing a healthy test backend caused the supervisor to
  replace it and recover to healthy without starting a duplicate.
- The GUI is now an attachable client. Snapshot providers run outside Qt and
  emit immutable, strictly decoded snapshots. Closing/reopening the GUI cannot
  own or stop capture.
- Bridge protocol 1.1 adds a global stream sequence and strict production
  handshake. Stream gaps, missed events, duplicates, out-of-order events,
  malformed/rejected messages, bridge drops, and persistence loss are named and
  included in eligibility. A transport payload removed but not confirmed sent
  is counted as a real loss.
- The Java bridge's process-lifetime drop counter is baselined at each new
  session. Old loss no longer invalidates a fresh session; new loss still does.
- Strategy checks now expose observed and required values. MBO-only behavior is
  `UNAVAILABLE` on the aggregated bridge rather than a generic failed condition.
- Complete Python suite: **804 passed, 4 third-party deprecation warnings**.
- Java: **24 bridge tests**, clean `shadowJar`; JAR 26,226 bytes, 19 classes,
  **0 `velox` classes**.
- Acceptance verifier: **23/23 passed**.
- Production-path load at a 2,500 events/second target plus a 6,000-event burst:
  **31,000 accepted = 31,000 persisted = 31,000 analysed**, zero unexplained
  loss, zero overflow, final lag 1.8601 ms.
- Production-process soak at 1,650 events/second for 72 seconds:
  **122,924 accepted = 122,924 persisted**, zero analysis skips, 30 causal paper
  evaluations, analysis queue high-water 31, PASS.

This update is automated integration evidence, not a real Bookmap-market claim.
No new live/delayed Bookmap session or real Tradovate DEMO session was available.
The 30-minute command exists but was not run in this continuation. Historical
recordings remain untouched, LIVE remains locked, and profitability is not
claimed.

Every number below was produced by a command in this repository and is
reproducible. Where something is unproven or unavailable, it says so.

Base: `06f32fd` → head `38e68d4`, branch `feature/automatic-runtime`.

## Production-readiness phase (final)

**Strategy verified on REAL recordings** (read-only diagnostic replays):
a 26.8M-event overnight session and RTH sessions through the production
engine. The time-based window works on real data (absorption 60.6%, reload
53.3%, CVD 45.4% pass rates overnight - all were 0% before). Overnight
sessions reject honestly (RTH-only plan, message now says so). MEASURED: the
real delayed book runs ~20 levels/side, top sizes 13-63 in thin RTH stretches -
the fixture-calibrated 90/400 block thresholds are unreachable there. The
canonical strategy is UNCHANGED; two experimental candidates (60/250, 40/150)
joined the research grid so finalized-session learning can calibrate the
threshold/quality relationship with evidence. Diagnostic replay at 40/150:
stop location 68x, absorption 33x (vs 0 at canonical); full confluence stayed
rare - the strategy being selective.

**The complete product loop, proven in the real backend process**
(`tests/test_learning_chain.py`): clean session over a real socket -> finalized
`data_quality.ok: true` -> automatic episode build -> research ran all 5
candidates exactly once -> daily learning + paper reports generated. Bookmap
reconnects produce fresh sessions with correct clean/unclean flags, no stall.

**20-minute production soak** (`tools/soak.py`): 1.2M+ events, lag p50 0.27 ms
/ p95 0.73 ms, zero skips, zero causality breaks, evaluations continuously
advancing, no drift across thirds of the run (no leak signature), under
concurrent full-test-suite CPU load.

**Defects found and fixed in this phase**: evaluate/derive block-selector
incoherence (impossible tallies); silent provenance ineligibility (no reason
emitted); harness sandbox hole (temp backends pointed processed/labels/
research-state at the REAL data tree); PipelineStateHolder dropping the
rotating wrapper (lost recorder metrics AND the capture-priority pressure
signal - found by live soak metrics, not tests).

## Production-failure fix (this continuation)

The running application failed on the REAL Bookmap stream: session drops
17,030 → 43,296 at ~1,331 ev/s, persisted ~1,285/s, processing age ~5,000 ms,
session permanently invalidated, 18,614 evaluations with zero candidates.

**Root cause 1 (drops):** ALL analysis ran inline on the receiver's asyncio
loop. Strategy evaluation blocked `recv()`; TCP backpressure filled the Java
ForwardingQueue; the JAVA side dropped (that is what "Session drops" counts).
Three full MarketState.update calls per event tripled the per-event book-copy
cost. **Fix:** `AnalysisFeed` — the capture loop only parses, guards, updates
state once, records, and O(1)-offers; analysis runs on its own thread with
capture-priority backpressure; overflow skips are paper-only, counted, and
reported as causality gaps (position closed as DATA_GAP, re-warm before entry).

**Root cause 2 (zero candidates):** the strategy window was 200 EVENTS ≈ 0.12
SECONDS at the real rate — every time-scale order-flow condition was
structurally unsatisfiable, in streaming and in replay alike. **Fix:**
`CausalWindow` — 180 s market-time window, 250 ms sampling, 1/s evaluation
cadence, shared by streaming and replay. Sampling provably preserves the
canonical semantics (all volume/CVD measures are first-vs-last cumulative
deltas; trades are never coalesced away). No threshold was changed.

**Also found and fixed:** `session_ended` finalized the recorder while the
pipeline queue still held the session's tail (silent tail loss every session);
writer-thread re-validation (JSON round-trip per event) removed via
`record_normalized`; manifest fsync throttled from ~2.7/s to 1 per 5 s;
invalidated sessions now rotate to a fresh clean session after a 30 s healthy
window (damage accounting untouched); FAIL messages now carry observed vs
required values and the GUI shows per-condition pass/fail evidence.

**Measured on the production path** (`tools/pipeline_loadtest.py`, real
WebSocket, ~150-level books, real depth:trade mix, temp dirs):

| Scenario | Result |
|---|---|
| 2,500 ev/s × 75 s, evaluations live | 187,500 sent = persisted = analysed; queues ~50; lag 0.18 ms |
| 4,000 ev/s + 10,000 burst | 70,000 = 70,000 = 70,000; zero unintended loss; lag 3.5 ms |
| Real-world before (screenshots) | −46 ev/s deficit at 1,331; 5,000 ms age; 43k drops |

`tools/soak.py --minutes N --rate R` is the long-soak command (JSONL metrics,
nonzero exit on conservation/lag/queue/stall breaches).

## Commits this continuation

| Hash | Phase |
|---|---|
| `7795d8b` | causal paper-trading lifecycle (open/manage/close simulated positions) |
| `2e8456b` | drain capture on shutdown instead of killing it mid-write |
| `57c4b96` | compute MNQ rollover instead of a table that expires |
| `6506c06` | profitability meter in the GUI, computed off the Qt thread |
| `7cd57af` | fix the temp-file race that destroyed 31 recorded sessions |
| `6b0fcef` | crash/hang diagnostics + Windows rename retry |
| `1346e3f` | stop the test suite from corrupting tracked user data |
| `d771a19` | evidence pack + implementation state |
| `4c6e0e2` | observe feed capabilities instead of declaring them |
| `b350887` | automatic paper-trading daily report from the real ledger |
| `38e68d4` | versioned bridge handshake: stream/connection IDs + capabilities |

## Headline

**The bot is not proven profitable, and nothing here claims otherwise.** The
progress meter reads **6.7%**, `profitable_claim_supported = False`, with **0
completed setups on real data**. That is an honest measurement, not a failure.

What changed is that the machinery to earn that evidence now exists and works,
and three defects that were actively destroying the evidence are fixed.

## Verification commands

| Check | Command | Result |
|---|---|---|
| Python suite | `.venv\Scripts\python.exe -m pytest -q` | **753 passed** |
| Acceptance | `.venv\Scripts\python.exe -m tools.verify_final_acceptance` | **21/21 passed** |
| Java addon | `gradlew clean test shadowJar` | **BUILD SUCCESSFUL, 23 tests** |
| JAR | `build/libs/mnq-bookmap-forwarder-all.jar` | **25,895 bytes, 24 files, 0 velox** |
| Meter | `.venv\Scripts\python.exe -m tools.profitability_meter` | **6.7%, claim NOT supported** |
| Raw data | `find data/raw -type f` | **6,316 files / 468,320,293 bytes, unmodified** |

## End-to-end pipeline (real WebSocket, verified)

A real receiver bound an ephemeral socket; a client streamed 1,200 depth + 400
trade events through the whole pipeline:

- 1,600 events reached the paper engine; 120 evaluations ran; 0 malformed dropped
- observed capabilities: depth / trades / aggressor **available**, MBO **unavailable**
- clean drain: the receiver thread exited, 5 parquet files persisted, 0 stray
  `.tmp` files, manifest `clean_shutdown: true, continuity: continuous`
- the resulting session finalized and produced a session + daily report

## The paper-trading lifecycle now runs end to end

Proven in `tests/test_paper_lifecycle.py` on a deterministic synthetic tape:

```
3 accepted setups -> 3 candidates -> 1 order -> causal fill -> long 2 open
  -> target -> exit 29502.00 -> gross 4.00, commission 2.48, net +1.52
```

The other 2 candidates were correctly refused with the real reason
`one_position_max`. **These are fixture results and are not evidence of
profitability** — each is stamped `is_synthetic_fixture`, rendered `[FIXTURE]`,
and excluded from `real_records` and `realized_pnl`.

Design and guarantees: [`docs/PAPER_EXECUTION_MODEL.md`](../docs/PAPER_EXECUTION_MODEL.md).

## Three real defects found and fixed

### 1. Fills at prices MNQ cannot trade (found while proving the lifecycle)

Entries booked at mid + slippage, producing prices like `29500.875`. MNQ trades
in 0.25 only, so **every P&L number was subtly fictional**. Fills now take the
opposing side of the book (a buy lifts the offer, a sell hits the bid), add
adverse slippage, and snap to the grid away from us. The fixture trade moved
`+2.02 → +1.52`: worse, and correct.

### 2. Capture was killed mid-write on every exit

The receiver was a `daemon=True` thread awaiting `asyncio.Future()`, which never
completes — so process exit killed it and its drain `finally` **never ran**. The
recording tail was lost, the session was never finalized, and the automatic
episode build never fired. This is consistent with the dominant ineligibility
reason in the real data: **192 of 200 finalized sessions are "unclean shutdown"**.

Verified against the real receiver (`tests/test_shutdown.py`): it binds a real
socket, drains cleanly, and the thread actually exits.

### 3. A temp-file race destroyed 31 recorded sessions

`_write_manifest` used a **fixed** `session_manifest.json.tmp`, so overlapping
writers collided. Reproduced directly: against the old code **11 of 12
concurrent writers raise the exact `PermissionError` seen in the real
recordings**; against the fix, **0**.

A second, independent cause was then root-caused: Windows refuses a rename while
*any* handle exists on the destination — including the brief ones Defender and
the search indexer take. Renames now retry with bounded backoff. Measured: 16
threads rewriting one manifest produced 1–2 failures per run before, **0 across
5 runs after**.

## Why the meter is stuck at 6.7% (measured, not guessed)

Of **200 finalized sessions, only 1 is order-flow-eligible**:

| Count | Reason |
|---|---|
| 192 | unclean shutdown — addressed by the shutdown fix (`2e8456b`) |
| 145 | continuity: receiver_error — **31 of these were the temp-file race** (`7cd57af`) |
| 71 | no trades recorded (depth-only) |
| 20+ | bridge dropped messages during the session |
| 11 | no depth updates |

**This is the real bottleneck to profitability evidence.** The strategy has not
failed — it has barely been given a usable session to judge. Both dominant
causes are now fixed, so newly recorded sessions should convert to eligible at a
far higher rate. That claim is a *prediction and is not yet proven*: it requires
new recordings from a live Bookmap session, which cannot be manufactured here.

## Diagnostics: failures now leave evidence

Previously there was **no faulthandler, no excepthook, and no log file**, so the
reported "crashes a lot / non-responsive" had nothing to diagnose. Now, in
`logs/`: native crash stacks, uncaught main-thread and **worker-thread**
exceptions, asyncio errors, and — for a hang, which crashes nothing — a
`StallWatchdog` that dumps every thread's stack.

Verified against a simulated freeze: the log names the stuck thread **and the
exact function it is blocked in**. The GIL-starvation freeze would have been
diagnosable in seconds.

## Throughput

Recorder measured at **26,160 events/s** with 0 drops on 60,000 events —
**15.9× the 1,650 ev/s requirement**, after adding fsync and per-path locking.
`tests/test_recorder_load.py` asserts a floor of 5,000 ev/s, itself well above
the requirement.

## Additional subsystems this continuation added

- **Honest feed capabilities** (`4c6e0e2`) — observed from the stream, not a
  constant. A depth-only feed now reports trades `DEGRADED`, matching the 71/200
  depth-only sessions the old hardcoded "AVAILABLE" lied about. See
  `docs/FEED_CAPABILITIES.md`.
- **Automatic paper daily report** (`b350887`) — a fixture-safe digest computed
  from the real ledger, written on every session finalize and via
  `python -m tools.paper_daily_report`. Synthetic trades excluded; zero real
  trades reported honestly. See `docs/PAPER_LEDGER.md`.
- **Versioned bridge handshake** (`38e68d4`) — protocol version, stream id,
  connection id, and a capability declaration on the tolerant control path; the
  strict market-event schema is untouched. Reconnects are distinguishable from a
  new bridge process. See `docs/FEED_CAPABILITIES.md`.
- **Diagnostics** (`6b0fcef`) — a simulated freeze proves the `StallWatchdog`
  names the stuck thread and the exact function it is blocked in.

## Honest limitations

* **The bot is not proven profitable.** 0 completed setups on real data.
* **The eligibility improvement is predicted, not proven.** It needs new
  recordings to confirm.
* **Screenshots in this sandbox render text as tofu boxes.** Qt ships no fonts
  and none are installed here, so `reports/screenshots/*.png` prove layout, not
  text. The offscreen tests assert `QLabel.text()` directly, which is stronger
  evidence of content than a screenshot.
* **Tradovate DEMO is not wired**; LIVE remains locked and `live_enabled` is
  false. 12 Lucid prop-rule fields remain unresolved and block automated
  execution.
* **Fixture trades are not results.** Every synthetic trade is stamped and
  excluded from real statistics.

## Repository hygiene

No credentials tracked; nothing from `data/raw` staged or committed; `data/paper`
and `logs/` gitignored. The test suite was itself corrupting tracked user data
(appending ~291 rows per run to `data/prototype/automation/decisions_log.csv`);
that is fixed by an autouse guard and the files are restored — after a full run,
**no tracked file under `data/` is modified**.
