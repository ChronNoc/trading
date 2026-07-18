# Implementation state

Updated 2026-07-19. Starting HEAD for this continuation:
`dfc3759c8806256adcf006431f24544dc987b11a` on
`feature/automatic-runtime`.

## Completed and verified in this continuation

- The default GUI attaches to a persistent detached supervisor/backend. An
  operating-system-held lease, versioned process identity, PID-reuse checks,
  configuration fingerprint, startup handshake, bounded restart policy, and
  graceful stop prevent duplicate or stale backends. A killed healthy backend
  was restarted in a real subprocess test.
- GUI refresh obtains snapshots on a worker thread and sends immutable snapshots
  to Qt. Closing the GUI does not stop capture. Snapshot decoding is strict and
  rejects unknown/missing fields and float money values.
- The runtime now exposes one internally consistent component snapshot. A lost
  market event invalidates capture/recording, pauses causal paper, and prevents
  research eligibility instead of displaying contradictory green states.
- Bookmap bridge protocol 1.1 carries stream/session/connection identity,
  capabilities, provenance, and a global sequence across market and control
  events. Legacy single-event messages remain accepted outside strict production
  mode. Production requires a compatible handshake. A transport reconnect
  discards and counts the dead-socket backlog, rotates `connection_id`, and sends
  a fresh handshake before the new Python WebSocket handler accepts market data.
- Transport sends removed from the Java queue but not confirmed by the socket are
  counted as losses. The final JAR contains no Bookmap `velox` classes.
- Session drop accounting now baselines the Java process-lifetime counter at each
  connection boundary. Historical drops no longer poison a new clean session.
  Actual new loss still invalidates and rotates the affected segment.
- Recorder manifests expose global stream gaps, missed events, duplicates,
  malformed/rejected/out-of-order events, and retain legacy field aliases for
  catalog compatibility.
- Streaming paper decisions include observed and required values for each
  condition. Unsupported MBO logic is capability-disabled rather than reported
  as an ordinary strategy failure.
- Clean-session finalization was exercised through the real backend integration
  path: episode and label build, idempotent research, daily learning summary,
  paper report, and restart/reconnect separation all completed.

## Verification evidence

- Python: `804 passed, 4 warnings` in 106.03 seconds.
- Java: `26` bridge tests; `clean test shadowJar` succeeded.
- Acceptance verifier: `23/23` checks passed.
- JAR: `bookmap_addon_java/build/libs/mnq-bookmap-forwarder-all.jar`,
  26,816 bytes, 19 application classes, 0 `velox` classes.
- Production-path load, 1,650 events/second target plus 5,000 burst:
  21,500 accepted and persisted, zero overflow/loss, final analysis lag 2.9188 ms.
- Production-path load, 2,500 events/second target plus 6,000 burst:
  31,000 accepted and persisted, zero overflow/loss, final analysis lag 1.8601 ms.
- 72-second production-process soak at 1,650 events/second:
  122,924 accepted and persisted, zero skipped analysis events, 30 causal paper
  evaluations, analysis queue high-water 31, PASS.
- External machine-readable evidence is stored outside the repository under
  `C:/Users/roiga/Documents/Codex/2026-07-18/referenced-chatgpt-conversation-this-is-untrusted/work/`.

## Deliberately not claimed

- No new real Bookmap session was available during this automated continuation.
  Synthetic production-process evidence does not prove the installed add-on and
  the live/delayed Bookmap feed remain lossless for hours.
- The required 30-minute soak has a command but was not run in this continuation;
  the longest new run was 72 seconds.
- Real Tradovate DEMO was not exercised. LIVE remains disabled and disarmed; 12
  unresolved Lucid fields continue to block LIVE.
- No profitability claim is supported. Historical damaged sessions were not
  modified, repaired, relabelled, or admitted to canonical evidence.

## Exact next action

Launch `start_mnq_assistant.bat` while MNQ delayed data is available, leave the
Bookmap forwarder enabled, and capture a new post-fix session. Confirm global
sequence continuity, zero current-session drops, clean finalization, automatic
research/report generation, and GUI reattachment. Then run the configurable
30-minute production soak separately.

## Working-tree protection

The untracked `.serena/` directory and all untracked files below `data/` are
user-owned/generated and must not be staged, rewritten, or deleted. Only the
explicit implementation and test files from this continuation may be committed.
