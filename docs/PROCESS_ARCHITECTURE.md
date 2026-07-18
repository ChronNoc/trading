# Process Architecture

Process isolation is structural: the GUI holds no socket, thread, or object of
the backend's, so closing or restarting the GUI **cannot** interrupt capture —
there is nothing it could interrupt with.

## Processes

```
start_mnq_assistant.bat
  └─ tools.start_assistant (GUI process)
       ├─ ensure_backend(): starts the backend DETACHED if none is alive,
       │                    attaches if one already is (no duplicates, ever)
       └─ AppWindow reading FileSnapshotProvider (runtime/status.json only)

tools.start_backend (detached backend process — the authority)
  ├─ WebSocket receiver + feed guard + recorder pipeline + session rotation
  ├─ AnalysisFeed thread (controller context + paper engine + ledger)
  ├─ research service (+ its ProcessPoolExecutor worker processes)
  └─ status writer: atomic runtime/status.json heartbeat ~2 Hz

tools.backend_supervisor (optional monitor)
  └─ restarts a dead/hung backend with bounded backoff (2s→60s);
     never restarts an intentional stop
```

## The `runtime/` contract (gitignored)

| File | Meaning |
|---|---|
| `backend.lock` | backend PID. Second backend refuses to start while it is alive; a stale lock (dead PID) is the forced-shutdown marker — reported and cleaned on the next start. |
| `status.json` | atomic heartbeat: encoded `AppSnapshot` + PID + wall time. The GUI decodes the same typed tree (`Decimal` stays `Decimal`) via `app/gui/snapshot_codec.py`. |
| `binding.json` | the actually-bound host/port (port 0 resolves at bind). |
| `stop.request` | clean-shutdown request; the backend runs its normal tested drain (recorder finalize, feed drain, paper flatten), writes `STOPPED_CLEAN/UNCLEAN`, releases the lock. |
| `supervisor.log`, `backend.out` | restart reasons and backend console output. |

## Honesty rules

- A dead or hung backend can never look healthy: the GUI overrides the
  lifecycle to `BACKEND_DOWN` with the exact reason (no status file / heartbeat
  stale Ns / PID gone) while keeping the last real numbers visible.
- Repeated `start_mnq_assistant.bat` launches attach to the running backend —
  they cannot double-record.
- Closing the GUI prints how to check/stop the still-running backend
  (`python -m tools.backend_supervisor --status | --stop`).

## Verified end to end (`tests/test_process_isolation.py`)

A REAL backend subprocess: bound a socket → captured 300 events with a GUI
attached → captured 300 more with **no GUI in existence** → a brand-new GUI
attached and saw all 600 → a duplicate backend launch was refused (exit 3) →
`stop.request` drained it to `STOPPED_CLEAN` with the lock released and a
finalized session manifest on disk. Codec round-trip equality, stale-heartbeat
honesty, and stale-lock cleanup are separately pinned.

## Legacy mode

`--in-process` keeps the old single-process behaviour (GUI owns the capture
thread) for development and the existing in-process test suites. The default is
isolated.
