# MNQ Assistant Automatic Runtime

The one-click runtime starts the assistant in SHADOW mode only. It listens for the
Bookmap bridge on `ws://127.0.0.1:8765/bookmap`, records raw depth/trade events,
updates market state, classifies the session/regime, routes to a strategy profile,
and logs shadow decisions. It does not import the execution package and cannot place
Tradovate orders.

The Windows launcher is prepared for the free Bookmap data feed by default. It marks
incoming Bookmap data as 15 minutes delayed, keeps the app in recording-only mode, and
keeps shadow decisions disabled while still recording raw depth/trade events for review.

## Windows one-click start

From File Explorer, double-click:

```bat
start_mnq_assistant.bat
```

The batch file uses `.venv\Scripts\python.exe` from this repo. If the virtual
environment or dependencies are missing, it prints a clear error and stops.

## Command-line start

```powershell
cd C:\path\to\mnq_bot
.\.venv\Scripts\python.exe -m tools.start_assistant --delayed-data-minutes 15
```

Headless recording/controller mode:

```powershell
.\.venv\Scripts\python.exe -m tools.start_assistant --no-gui --delayed-data-minutes 15
```

If you later move to verified real-time data, explicitly pass `--delayed-data-minutes 0`.

## Java bridge build

Production compilation prefers the installed Bookmap 7.7.0 build 22 API jars:

```text
C:\Program Files\Bookmap\lib\bm-l1api.jar
C:\Program Files\Bookmap\lib\bm-simplified-api-wrapper.jar
```

When those jars are absent, Gradle falls back to the official Maven API
artifacts `api-core:7.6.0.20` and `api-simplified:7.6.0.20`.

Build and test the Java add-on on Windows:

```powershell
$env:JAVA_HOME='C:\path\to\jdk-17'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
.\bookmap_addon_java\gradlew.bat -p .\bookmap_addon_java --no-daemon clean test shadowJar
```

Built JAR:

```text
bookmap_addon_java\build\libs\mnq-bookmap-forwarder-all.jar
```

Startup output includes:

```text
MNQ Assistant running in SHADOW mode.
Bookmap delayed data mode: 15 minutes. Recording only; shadow decisions disabled.
listening on ws://127.0.0.1:8765/bookmap, writing to data/raw/{date}/session_<UTC timestamp>/
```

## Bookmap workflow

1. Start the MNQ Assistant first.
2. Open Bookmap.
3. Open the add-ons/API module area in Bookmap.
4. Load `bookmap_addon_java\build\libs\mnq-bookmap-forwarder-all.jar`.
5. Attach the add-on to one exact MNQ contract alias, such as `MNQU6`.
6. Leave the add-on WebSocket URL as `ws://127.0.0.1:8765/bookmap`.
7. Start the free delayed data feed or replay. The assistant marks the free feed as
   `delayed` when launched by `start_mnq_assistant.bat`.

If Bookmap disconnects, the assistant keeps waiting and resumes on reconnect. Data gaps
are marked in the raw session manifest and health events.

## GUI workflow

The Live Dashboard shows runtime state, Bookmap status, recording state, exact contract,
source mode, detected session, regime, selected profile, fallback reason, warm-up sample
count, dropped message count, and shadow decision count. Manual session selection and
manual calibration are intentionally not exposed.

The Replay tab reads recorded sessions from `data/raw/{date}/session_*` and displays the
raw depth/trade events already written by the recorder.

## Reports

Session reports are written under:

```text
data/reports/YYYY-MM-DD/session_<id>/
```

Each report folder contains `summary.json`, `decisions.jsonl`,
`detected_setups.csv`, `profile_transitions.jsonl`, and `health_events.jsonl`.

After every completed Bookmap connection, the assistant also refreshes the observe-only
daily learning report for that UTC recording date:

```text
data/reports/daily_learning/YYYY-MM-DD/daily_learning.json
data/reports/daily_learning/YYYY-MM-DD/daily_learning.md
```

The daily report separates recording/data quality, descriptive market observations
(CVD, blocks, reloads, and possible absorption), and completed strategy outcomes. The
observation score is not win rate or profitability. It is not a live model retrain and
it never makes delayed Bookmap data decision-ready.

Manual daily report command:

```powershell
.\.venv\Scripts\python.exe -m tools.daily_learning_summary --date 2026-07-12
```

Example report summary fields:

```json
{
  "runtime_mode": "SHADOW",
  "source_mode": "delayed",
  "data_delay_minutes": 15,
  "selected_profile": "ny_open_normal_trend",
  "accepted_decisions": 0,
  "rejected_decisions": 1,
  "execution_data_included": false,
  "broker_execution_data": null
}
```

## Smoke tests

Live smoke test:

1. Run `start_mnq_assistant.bat`.
2. Confirm the console prints the local WebSocket listening message.
3. Load the Java add-on in Bookmap and attach it to a single exact MNQ contract.
4. Confirm the GUI shows Bookmap `connected`, recording `yes`, the exact contract,
   source mode `delayed`, data delay `15 minutes delayed`, and decisions `no`.
5. Let it run for the intended soak period.
6. Confirm readable files appear under the session's `depth_parts/` and
   `trade_parts/` directories while recording.
7. Stop the Bookmap stream and confirm the GUI shows connection lost while the app
   remains open, then confirm finalized closed files exist under `depth_parts/`
   and `trade_parts/`. Replay and research consume these immutable parts directly;
   production shutdown does not rewrite them into duplicate monolithic files.

Replay smoke test:

1. Run `.\.venv\Scripts\python.exe -m tools.start_assistant --no-gui --delayed-data-minutes 15`.
2. Start a Bookmap replay with the Java add-on attached.
3. Confirm a new `data/raw/YYYY-MM-DD/session_*` folder appears.
4. Start the GUI and open the Replay tab.
5. Select that session and confirm recorded depth/trade rows render.

## Runtime states

`STARTING`, `WAITING_FOR_BOOKMAP`, `RECORDING_ONLY`, `AUTO_WARMUP`,
`SHADOW_READY`, `SHADOW_ACTIVE`, `DATA_STALE`, `CONNECTION_LOST`,
`PROFILE_UNAVAILABLE`, `STOPPING`, and `STOPPED`.

## Known limitations

The runtime is unit-tested and can record from the local WebSocket bridge, but actual
loading inside Bookmap still requires manual verification in the Bookmap UI. Dynamic
thresholds currently use historical or conservative local baselines; no automatic model
retraining or live execution is included.
