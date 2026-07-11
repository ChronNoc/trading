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
.\.venv\Scripts\python.exe .\tools\start_prototype.py --no-gui
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
