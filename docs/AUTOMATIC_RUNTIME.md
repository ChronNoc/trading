# MNQ Assistant Automatic Runtime

The one-click runtime starts the assistant in SHADOW mode only. It listens for the
Bookmap bridge on `ws://127.0.0.1:8765/bookmap`, records raw depth/trade events,
updates market state, classifies the session/regime, routes to a strategy profile,
and logs shadow decisions. It does not import the execution package and cannot place
Tradovate orders.

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
.\.venv\Scripts\python.exe .\tools\start_assistant.py
```

Headless recording/controller mode:

```powershell
.\.venv\Scripts\python.exe .\tools\start_assistant.py --no-gui
```

## Java bridge build

Selected Bookmap API artifacts:

```text
com.bookmap.api:api-core:7.6.0.20
com.bookmap.api:api-simplified:7.6.0.20
```

Build and test the Java add-on on Windows:

```powershell
$env:JAVA_HOME=(Resolve-Path '..\work\jdk-21.0.11+10')
$workRoot=(Resolve-Path '..\work').Path
$env:GRADLE_USER_HOME=Join-Path $workRoot 'gradle-home-jdk21'
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
listening on ws://127.0.0.1:8765/bookmap, writing to data/raw/{date}/session_<UTC timestamp>/
```

## Bookmap workflow

1. Start the MNQ Assistant first.
2. Open Bookmap.
3. Open the add-ons/API module area in Bookmap.
4. Load `bookmap_addon_java\build\libs\mnq-bookmap-forwarder-all.jar`.
5. Attach the add-on to one exact MNQ contract alias, such as `MNQU6`.
6. Leave the add-on WebSocket URL as `ws://127.0.0.1:8765/bookmap`.
7. Start live data or replay. The assistant automatically detects live/replay control events.

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

Example report summary fields:

```json
{
  "runtime_mode": "SHADOW",
  "source_mode": "live",
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
   source mode `live`, and runtime state moving from warm-up to SHADOW ready/active.
5. Stop the Bookmap stream and confirm the GUI shows connection lost while the app
   remains open.

Replay smoke test:

1. Run `.\.venv\Scripts\python.exe .\tools\start_assistant.py --no-gui`.
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
