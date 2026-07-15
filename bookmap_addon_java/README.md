# MNQ WebSocket Forwarder Java Add-on

This is the loadable Java Bookmap L1/Simplified add-on that replaces the earlier
experimental Python adapter in `bookmap_addon/`.

The add-on forwards Bookmap market-by-price depth updates, trades, replay/live
mode markers, heartbeats, and data-gap events to the local Python receiver:

```text
ws://127.0.0.1:8765/bookmap
```

It does not submit orders, does not import any broker code, and does not contain
credentials.

## API Version

Production compilation prefers the API jars from the installed Bookmap build:

```text
C:\Program Files\Bookmap\lib\bm-l1api.jar
C:\Program Files\Bookmap\lib\bm-simplified-api-wrapper.jar
```

The current machine was verified against Bookmap 7.7.0 build 22. If those
installed jars are unavailable, Gradle falls back to the official Maven API
artifacts pinned to `7.6.0.20`.

Production source is not compiled against the handwritten stubs. Bookmap API
classes are compile-only and excluded from the final add-on JAR because Bookmap
provides those classes when it loads the plugin.

## Build

From the repository root:

```powershell
$env:JAVA_HOME='C:\path\to\jdk-17'
$env:Path="$env:JAVA_HOME\bin;$env:Path"
.\bookmap_addon_java\gradlew.bat -p .\bookmap_addon_java --no-daemon clean test shadowJar
```

Use a full JDK with `javac`; Bookmap's bundled runtime alone is not sufficient
to compile the add-on.

The loadable JAR is:

```text
bookmap_addon_java\build\libs\mnq-bookmap-forwarder-all.jar
```

## Start the Python Receiver

In a second terminal, start the local recorder before enabling the add-on:

```powershell
.\.venv\Scripts\python.exe -m tools.start_receiver
```

Expected startup text:

```text
listening on ws://127.0.0.1:8765/bookmap, writing to data/raw/{date}/session_<UTC timestamp>/
```

Each Bookmap connection creates a new session folder:

```text
data/raw/YYYY-MM-DD/session_<UTC timestamp>/
  depth_parts/part-*.parquet
  trade_parts/part-*.parquet
  session_manifest.json
  connection_events.jsonl
```

Closed parts are readable while recording. A clean session shutdown also
creates the compatible `depth.parquet` and `trades.parquet` files.

## Manual Bookmap Installation

1. Open Bookmap.
2. Open `Settings` -> `API plugins configuration`.
3. Choose `Add`.
4. Select `bookmap_addon_java\build\libs\mnq-bookmap-forwarder-all.jar`.
5. Select the add-on named `MNQ WebSocket Forwarder`.
6. Enable it on an MNQ instrument.
7. Leave the WebSocket URL as `ws://127.0.0.1:8765/bookmap`.
8. Leave non-loopback URLs disabled unless you are deliberately testing on a
   locked-down local network.

## Smoke Test

Live data:

1. Start the Python receiver.
2. Load the add-on on MNQ.
3. Confirm `connection_events.jsonl` receives `connected` and `heartbeat`.
4. Confirm closed files appear under `depth_parts/` and `trade_parts/`.
5. Stop the Bookmap stream cleanly and confirm `depth.parquet`,
   `trades.parquet`, and the finalized manifest exist.

Replay data:

1. Start the Python receiver.
2. Replay a Bookmap MNQ session with the add-on enabled.
3. Confirm a historical/replay marker is recorded before `realtime_started`
   when Bookmap exposes that transition.
4. Treat sessions with `data_gap` or an unclean shutdown as invalid for
   analysis.

## Diagnostics

- `connection_events.jsonl` contains `connected`, `heartbeat`,
  `replay_started`, `realtime_started`, `data_gap`, `disconnected`, and
  `session_ended` events.
- `session_manifest.json` records alias, source mode, add-on version, receiver
  version, event counts, dropped messages, continuity status, clean shutdown,
  and analysis validity.
- The Java add-on uses a bounded queue and reports a `data_gap` event when it
  drops messages.

## Known Limitations

- This repository can compile and unit test the Java add-on, but real Bookmap
  loading must still be verified manually inside Bookmap.
- Simplified depth callbacks do not carry an event timestamp, so the add-on uses
  Bookmap's latest `TimeListener` timestamp for depth and trade messages.
- Only MNQ is forwarded by default. Other symbols require an explicit advanced
  setting change.
