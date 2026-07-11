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

This project uses the official Bookmap Maven repository:

```text
https://maven.bookmap.com/maven2/releases/
```

The Bookmap API artifacts are pinned to `7.6.0.20`, matching the current
official `BookmapAPI/DemoStrategies` build file inspected for this bridge. This
intentionally avoids the older `7.1.0.35` API referenced in stale examples.

The source includes minimal compile-time stubs for the exact Bookmap interfaces
and annotations used by this bridge. They mirror the inspected `7.6.0.20`
signatures and are excluded from the built add-on JAR, because Bookmap provides
the real API classes when it loads the plugin.

## Build

From the repository root:

```powershell
.\bookmap_addon_java\gradlew.bat -p .\bookmap_addon_java clean test shadowJar
```

The loadable JAR is:

```text
bookmap_addon_java\build\libs\mnq-bookmap-forwarder-all.jar
```

## Start the Python Receiver

In a second terminal, start the local recorder before enabling the add-on:

```powershell
python tools\start_receiver.py
```

Expected startup text:

```text
listening on ws://127.0.0.1:8765/bookmap, writing to data/raw/{date}/session_<UTC timestamp>/
```

Each Bookmap connection creates a new session folder:

```text
data/raw/YYYY-MM-DD/session_<UTC timestamp>/
  depth.parquet
  trades.parquet
  session_manifest.json
  connection_events.jsonl
```

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
4. Confirm `depth.parquet` and `trades.parquet` receive rows.

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
