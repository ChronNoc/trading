# Evidence Pack — paper execution, durability, diagnostics

Every number below was produced by a command in this repository and is
reproducible. Where something is unproven or unavailable, it says so.

Base: `06f32fd` → head `1346e3f`, branch `feature/automatic-runtime`.

## Headline

**The bot is not proven profitable, and nothing here claims otherwise.** The
progress meter reads **6.7%**, `profitable_claim_supported = False`, with **0
completed setups on real data**. That is an honest measurement, not a failure.

What changed is that the machinery to earn that evidence now exists and works,
and three defects that were actively destroying the evidence are fixed.

## Verification commands

| Check | Command | Result |
|---|---|---|
| Python suite | `.venv\Scripts\python.exe -m pytest -q` | **714 passed** |
| Acceptance | `.venv\Scripts\python.exe -m tools.verify_final_acceptance` | **21/21 passed** |
| Java addon | `gradlew clean test shadowJar` | **BUILD SUCCESSFUL, 22 tests** |
| JAR | `build/libs/mnq-bookmap-forwarder-all.jar` | **25,501 bytes, 24 files, 0 velox** |
| Meter | `.venv\Scripts\python.exe -m tools.profitability_meter` | **6.7%, claim NOT supported** |
| Raw data | `find data/raw -type f` | **6,316 files / 468,320,293 bytes, unmodified** |

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
