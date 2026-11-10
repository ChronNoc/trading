"""How far an offline training run has got, for a progress bar.

The run writes one small JSON file (``runtime/training_progress.json`` by default), atomically and
at most about once a second; the GUI reads it. Progress is measured in market events replayed,
the bulk of the work: every training size rebuilds its dataset from the eligible sessions, so the
whole run is ``sizes x events``. Fitting and validating the model afterwards is quick and is shown
as its own stage. Nothing here influences the training itself.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PROGRESS_PATH = Path("runtime/training_progress.json")
# A run's state -> the theme's semantic badge state: ONE table for every place that shows a run.
BADGE_STATES = {"running": "neutral", "finished": "ok", "stopped": "warn", "failed": "fail"}
_WRITE_EVERY_SECONDS = 1.0
# A running run rewrites the file at least this often; older means it stopped without saying so
# (closed, crashed, the PC restarted).
STALE_AFTER_SECONDS = 180.0
_REPLACE_ATTEMPTS = 5            # a file being read is usually free again within milliseconds
_REPLACE_RETRY_SECONDS = 0.05


class TrainingProgress:
    """Written by a training run: sizes (candidates) -> sessions -> events."""

    def __init__(self, path: Path = DEFAULT_PROGRESS_PATH, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self._clock = clock
        self._started = clock()
        self._last_write = 0.0
        self._state: dict[str, object] = {
            "status": "running", "started_at": self._started, "updated_at": self._started,
            "candidates_total": 0, "candidate_index": 0, "candidate_label": "",
            "stage": "starting", "sessions_total": 0, "session_index": 0, "session_id": "",
            "events_total": 0, "events_before_session": 0, "events_done": 0,
            "candidates_done_fraction": 0.0, "message": "", "results": [],
        }

    # -- the run reports ------------------------------------------------------------------------

    def plan(self, candidates: int) -> None:
        """How many training sizes this run will train."""
        self._state["candidates_total"] = max(0, int(candidates))
        self._write(force=True)

    def begin_candidate(self, label: str) -> None:
        """A new size starts (its dataset is built, or reused when identical)."""
        self._state["candidate_index"] = int(self._state["candidate_index"]) + 1
        # More than planned (a size that failed does not count against the run's limit): the
        # total grows rather than showing "size 4 of 3".
        self._state["candidates_total"] = max(int(self._state["candidates_total"]),
                                              int(self._state["candidate_index"]))
        self._state.update(candidate_label=label, stage="building data", sessions_total=0, session_index=0,
                           session_id="", events_total=0, events_before_session=0, events_done=0)
        self._write(force=True)

    def plan_sessions(self, events_by_session: list[tuple[str, int]]) -> None:
        """The sessions this size reads, with their event counts (from the catalog)."""
        self._state["sessions_total"] = len(events_by_session)
        self._state["events_total"] = sum(max(0, count) for _, count in events_by_session)
        self._write(force=True)

    def begin_session(self, session_id: str, events_before: int) -> None:
        self._state["session_index"] = int(self._state["session_index"]) + 1
        self._state.update(session_id=session_id, events_before_session=events_before, events_done=events_before)
        self._write(force=True)

    def events(self, done_in_session: int) -> None:
        """Events replayed so far in the current session (cheap to call often)."""
        self._state["events_done"] = int(self._state["events_before_session"]) + max(0, done_in_session)
        self._write()

    def stage(self, name: str) -> None:
        """A later stage of the current size: "training", "validating"."""
        self._state["stage"] = name
        if name != "building data":
            self._state["events_done"] = self._state["events_total"]
        self._write(force=True)

    def end_candidate(self, result: str) -> None:
        results = list(self._state["results"])  # type: ignore[arg-type]
        results.append({"size": self._state["candidate_label"], "result": result})
        total = max(1, int(self._state["candidates_total"]))
        self._state.update(results=results, candidates_done_fraction=min(1.0, len(results) / total))
        self._write(force=True)

    def finish(self, message: str) -> None:
        self._state.update(status="finished", stage="done", message=message, candidates_done_fraction=1.0)
        self._write(force=True)

    def fail(self, message: str) -> None:
        self._state.update(status="failed", message=message)
        self._write(force=True)

    # -- writing --------------------------------------------------------------------------------

    def _write(self, *, force: bool = False) -> None:
        """Write the file; NEVER raise. Reporting must not be able to stop a training run.

        On Windows, replacing a file another process is reading at that instant (the GUI reads it
        every second) is refused with "Access is denied"; on 2026-11-10 that error escaped and
        failed two training sizes. A refused write is retried briefly, then skipped: the next
        report writes the newer state anyway.
        """
        now = self._clock()
        if not force and now - self._last_write < _WRITE_EVERY_SECONDS:
            return
        self._last_write = now
        self._state["updated_at"] = now
        temporary = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps(self._state), encoding="utf-8")
        except OSError:
            return
        for attempt in range(_REPLACE_ATTEMPTS):
            try:
                os.replace(temporary, self.path)
                return
            except OSError:
                if attempt + 1 < _REPLACE_ATTEMPTS:
                    time.sleep(_REPLACE_RETRY_SECONDS)


@dataclass(frozen=True, slots=True)
class TrainingProgressView:
    """What a progress bar shows."""

    state: str            # running | finished | failed | stopped
    fraction: float       # 0..1 over the whole run
    headline: str         # e.g. "Model 2 of 9 (random forest, 8-tick target / 8-tick stop) - building data: session 3 of 6"
    detail: str           # e.g. "41% - about 25 min left"
    results: tuple[str, ...]


def read_training_progress(
    path: Path = DEFAULT_PROGRESS_PATH, *, now: float | None = None,
) -> TrainingProgressView | None:
    """The latest run's progress, or None when no run has written any (or it cannot be read)."""
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    now = time.time() if now is None else now
    try:
        index = max(0, int(state.get("candidate_index") or 0))
        total = max(1, int(state.get("candidates_total") or 0), index)
        events_total = max(0, int(state.get("events_total") or 0))
        events_done = min(events_total, max(0, int(state.get("events_done") or 0)))
        updated = float(state.get("updated_at") or 0.0)
        started = float(state.get("started_at") or updated)
    except (TypeError, ValueError):
        return None
    status = str(state.get("status") or "running")
    stage = str(state.get("stage") or "")
    within = events_done / events_total if events_total else 0.0
    if stage in ("training", "validating"):
        within = 1.0
    fraction = 1.0 if status == "finished" else min(1.0, max(0.0, ((max(index, 1) - 1) + within * 0.97) / total))
    results = tuple(f"{item.get('size')}: {item.get('result')}" for item in state.get("results") or []
                    if isinstance(item, dict))
    if status == "running" and now - updated > STALE_AFTER_SECONDS:
        status = "stopped"
    if status == "finished":
        return TrainingProgressView("finished", 1.0, "Training finished", str(state.get("message") or ""), results)
    if status in ("failed", "stopped"):
        why = str(state.get("message") or "") if status == "failed" else (
            f"no update for {int((now - updated) // 60)} min: the run was closed or the PC restarted")
        return TrainingProgressView(status, fraction, f"Training {status} at {fraction:.0%}", why, results)
    label = str(state.get("candidate_label") or "")
    sessions = int(state.get("sessions_total") or 0)
    session = int(state.get("session_index") or 0)
    where = (f"building data: session {session} of {sessions}" if stage == "building data" and sessions
             else stage or "starting")
    headline = f"Model {max(index, 1)} of {total}" + (f" ({label})" if label else "") + f" - {where}"
    elapsed = max(0.0, now - started)
    if fraction >= 0.02 and elapsed > 0:
        left = elapsed / fraction * (1 - fraction)
        eta = f"about {int(left // 3600)} h {int(left % 3600 // 60)} min left" if left >= 3600 else \
            f"about {max(1, int(left // 60))} min left"
    else:
        eta = "working out the time left"
    return TrainingProgressView("running", fraction, headline, f"{fraction:.0%} - {eta}", results)
