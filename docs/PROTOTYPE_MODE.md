# MNQ Assistant Prototype Mode

Prototype mode is a free one-click demo path for the MNQ Assistant. It starts the normal
receiver, recorder, runtime controller, GUI, strategy/risk pipeline, and SHADOW stub, then
feeds them deterministic synthetic Bookmap-compatible messages.

It does not require Bookmap, paid market data, administrator permissions, broker
credentials, or any Tradovate connection.

## Safety Guarantees

- The GUI shows `PROTOTYPE DATA - NOT REAL MARKET DATA`.
- The instrument is `MNQ-PROTOTYPE`.
- Source mode is `PROTOTYPE`.
- Runtime mode remains SHADOW only.
- Synthetic data is written under `data/prototype/raw/`.
- Prototype reports are written under `data/prototype/reports/`.
- Manifests mark `synthetic: true`, `source_mode: prototype`,
  `valid_for_real_training: false`, and `analysis_scope: prototype_only`.
- Prototype code does not import execution or broker modules.

## Start It

From Windows Explorer, double-click:

```bat
start_mnq_prototype.bat
```

From PowerShell:

```powershell
cd C:\Users\roiga\Documents\Codex\2026-07-10\set-up-this-repo-from-scratch\mnq_bot
.\start_mnq_prototype.bat
```

Headless smoke run:

```powershell
.\.venv\Scripts\python.exe -m tools.start_prototype --no-gui
```

The launcher prints:

```text
MNQ Prototype running in SHADOW mode.
PROTOTYPE DATA - NOT REAL MARKET DATA
listening on ws://127.0.0.1:8765/bookmap, writing to data/prototype/raw/{date}/session_<UTC timestamp>/
```

## Playback Controls

The Live Dashboard includes prototype controls:

- Pause
- Resume
- Restart
- Speed: 1x, 2x, 5x, 10x
- Jump to clean
- Jump to rejected

These controls only affect the synthetic feed producer. They do not alter risk,
strategy, recorder, receiver, or execution code.

## Scenario

The deterministic scenario includes:

- Warm-up and normal market context
- One clean long absorption reclaim setup that should be accepted
- One lookalike with sell volume but no bid reload/reclaim that should be rejected
- A volatility transition
- A disconnect/reconnect exercise

The default seed is `20260711`. Logical timestamps are ordered and start at the New York
open in UTC.

## Live Smoke Test

1. Start `start_mnq_prototype.bat`.
2. Confirm the GUI opens.
3. Confirm the Live Dashboard shows `PROTOTYPE DATA - NOT REAL MARKET DATA`.
4. Confirm Source mode shows `PROTOTYPE`.
5. Wait for event counters to increase.
6. Confirm the Decision/Prototype panel eventually shows accepted or rejected setup lines.
7. Close the GUI window to stop the runtime.

## Replay Smoke Test

1. Run prototype mode long enough for files to be written.
2. Open the Replay tab.
3. Pick the newest session under `data/prototype/raw/{date}/session_*`.
4. Confirm depth and trade rows render.
5. Confirm the session manifest under the same folder marks the data as synthetic and
   invalid for real training.

## Known Limitations

- Prototype data is plausible, not a market simulator or real Bookmap recording.
- The accepted/rejected examples are deterministic demonstrations, not model training data.
- Runtime reports are SHADOW-only and contain hypothetical order text, not P&L.
- Bookmap manual loading is not exercised by prototype mode; use the Java bridge smoke
  test for real Bookmap integration.

## Assistant Agents (offline, free)

The GUI ships with rule-based assistant agents in `app/agents/`. They run fully
offline with no API key, no subscription, and no network access, and they never
touch the strategy, risk, or execution pipeline - they only narrate and review
decisions the deterministic engines already made. Every agent output is labeled
`AI COMMENTARY - NOT A TRADING SIGNAL`.

- **Narrator** (Decision explanation tab): retells the current accepted/rejected
  decision in plain trading English, built from the same pass/fail condition
  lines the engine logged.
- **Condition history matrix** (Decision explanation tab): rows are strategy
  conditions, columns are the last decisions, cells are green/red pass/fail -
  the chronic blocker condition becomes obvious at a glance.
- **Decision timeline** (Decision explanation tab): one colored row per decision
  this session.
- **Watchdog** (window header): flags feed gaps, paused playback, and trade
  events stalling while depth continues.
- **Ask tab**: type questions like "why was the last setup rejected?" or
  "how many events so far?" - answered strictly from the recorded snapshot and
  decision history, never guessed.
- **Session review tab**: one click writes a markdown report (totals, condition
  scoreboard, chronic blocker, narrated decision log, questions to verify by
  eye) under `data/prototype/reports/{date}/ai_review_*.md`.

Optional: if a free local [Ollama](https://ollama.com) server is running on
`127.0.0.1:11434`, `app/agents/llm_backend.py` can rephrase agent text through a
local model. Nothing requires it; every agent has a deterministic fallback.

## Consistency Toolkit

Everything below runs offline and free, inside the prototype itself.

### Price path chart (Live dashboard)

The Live dashboard plots the synthetic last-trade price with a green dot
where a setup was ACCEPTED and a red dot where one was REJECTED, so you can
see decisions in market context instead of only reading them.

### Replay scrubber (Replay tab)

Drag the slider to step through a recorded session event by event - the
free substitute for paid Bookmap Replay, using your own recorded parquet
files.

### Scenario library

Scenario YAML files in `config/prototype_scenarios/` define new market
situations without code changes. Launch one with:

    .venv\Scripts\python.exe -m tools.start_prototype --scenario config\prototype_scenarios\volatile_open_drill.yaml

Each file needs exactly one clean absorption window (`reload: true`,
`reclaim: true`) and one rejected lookalike. Shipped scenarios:

- `default_demo.yaml` - the standard demonstration.
- `volatile_open_drill.yaml` - volatility before any setup, then the real
  setup, then a near-miss that must still be rejected right after a win.

Drilling the same situations repeatedly - especially rejecting lookalikes
after a winner - is what builds trading consistency.

### Video rule extraction (draft only)

    .venv\Scripts\python.exe -m video_analysis.extract_rules path\to\transcript.json path\to\output

Scans a transcript for candidate strategy values (stop ticks, volume
minimums, reclaim ticks) and writes `draft_spec_fragment.yaml` plus
`citations.md` with the exact timestamps. Every value is a DRAFT for human
review; the tool never writes into `config/`.

### Nightly self-check

    run_self_check.bat

Runs the full test suite and writes a PASS/FAIL report under
`logs/self_check/`. To run it automatically every night at 03:00, create a
Windows scheduled task yourself (one line, run once in a normal terminal):

    schtasks /Create /SC DAILY /ST 03:00 /TN "MNQ self-check" /TR "\"C:\Users\roiga\Documents\Codex\2026-07-10\set-up-this-repo-from-scratch\mnq_bot\run_self_check.bat\""

If the report says FAIL, do not trust the prototype until the suite is
green again - consistency starts with tooling you can trust.

## Dark Theme and GUI Automations

The GUI uses a dark trading-terminal theme (slate panels, blue accents,
green/red reserved for accepted/rejected decisions).

The **Automations tab** hosts 12 workflow automations. Every one is
toggleable, none can ever place, modify, or cancel an order, and each shows
how many times it fired this session:

1. **Pause on feed gap** - pauses playback during a disconnect/data gap,
   resumes when the feed recovers.
2. **Slow down on decisions** - drops playback to 2x when a decision lands
   so you can watch it, then restores your speed.
3. **Flash accepted setups** - header banner for a few seconds on every
   ACCEPTED setup (shadow only).
4. **Export decisions to CSV** - every decision appends to
   `data/prototype/automation/decisions_log.csv`.
5. **Snapshot state on decision** - a JSON state snapshot per decision under
   `automation/decision_snapshots/`.
6. **Auto-review at session end** - the markdown session review writes
   itself when the scenario completes.
7. **Daily entry-limit guard** - persistent warning once 3 setups are
   accepted; the live risk lock would refuse more.
8. **Chronic-blocker coach** - names the condition that failed across your
   recent rejections, right on the Decision explanation tab.
9. **Watchdog journal** - every watchdog warning is appended to
   `automation/watchdog_events.jsonl`.
10. **Latest-report pointer** - `automation/LATEST_REPORT.txt` always points
    at the newest session report.
11. **Auto-refresh replay list** - new recordings appear in the Replay tab
    without restarting.
12. **Startup checklist** - venv, scenario library, writable data directory,
    and last self-check status verified at every launch.

Automations 1-2 act only on the synthetic playback controls; 3-12 only
read state and write local files. The strategy and risk engines are never
touched by any of them.
