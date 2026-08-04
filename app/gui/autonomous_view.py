"""Read-only view of autonomous-intelligence state and generated reports.

Pure and Qt-free so it can run off the GUI thread and be tested directly. It
reads REAL repository artifacts:

* the ``AutonomousStore`` (candidate records, completed checkpoints, append-only
  activity log) - honestly reporting an empty/absent store when the autonomous
  service has not run;
* the generated ``data/reports`` tree - categorised, dated, and provenance-
  tagged report files.

Nothing here writes, trains, promotes, or trades. It never claims profitability;
a report's provenance (real / delayed / replay / synthetic / mixed) is surfaced
so decorative color can never imply proven results.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STORE_ROOT = Path("data/autonomous")
DEFAULT_REPORTS_ROOT = Path("data/reports")

# Governed lifecycle order for the task board (kept independent of the backend
# enum import so this stays a light, Qt-free read layer).
LIFECYCLE_ORDER: tuple[str, ...] = (
    "PROPOSED", "DATA_VALIDATED", "OFFLINE_TRAINED", "WALK_FORWARD_VALIDATED",
    "STABILITY_VALIDATED", "COST_VALIDATED", "SHADOW_CANDIDATE", "SHADOW_OBSERVING",
    "SHADOW_ELIGIBLE", "SHADOW_APPROVED", "REJECTED", "FAILED_REQUIRES_REWORK",
)

_DATE_TOKENS = tuple(f"{d:02d}" for d in range(1, 32))


@dataclass(frozen=True, slots=True)
class CandidateView:
    """One autonomous candidate, flattened for display."""

    candidate_id: str
    kind: str
    state: str
    hypothesis: str
    proposer: str
    created: str
    attempts: int
    last_error: str


@dataclass(frozen=True, slots=True)
class ActivityEvent:
    """One append-only autonomous activity record."""

    when: str
    event: str
    detail: str


@dataclass(frozen=True, slots=True)
class ReportView:
    """One generated report file with its metadata and provenance."""

    title: str
    category: str
    covered: str
    provenance: str
    path: str
    modified: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class AutonomousSnapshot:
    """Immutable view of autonomous state + reports for the GUI."""

    service_running: bool
    store_present: bool
    candidate_count: int
    completed_count: int
    counts_by_state: dict[str, int] = field(default_factory=dict)
    candidates: tuple[CandidateView, ...] = ()
    recent_activity: tuple[ActivityEvent, ...] = ()
    reports: tuple[ReportView, ...] = ()
    disk_bytes: int = 0
    note: str = ""


def _iso(ns: int) -> str:
    if ns <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(ns / 1e9, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    except (ValueError, OSError, OverflowError):
        return "-"


def _read_candidates(store_root: Path) -> tuple[list[CandidateView], dict[str, int]]:
    candidates_dir = store_root / "candidates"
    views: list[CandidateView] = []
    counts: dict[str, int] = {}
    if not candidates_dir.is_dir():
        return views, counts
    for path in sorted(candidates_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(payload, dict):
            continue
        state = str(payload.get("state", "PROPOSED"))
        counts[state] = counts.get(state, 0) + 1
        errors = payload.get("errors") or ()
        views.append(CandidateView(
            candidate_id=str(payload.get("candidate_id", path.stem))[:16],
            kind=str(payload.get("kind", "?")),
            state=state,
            hypothesis=str(payload.get("hypothesis", "")),
            proposer=str(payload.get("proposer", "")),
            created=_iso(int(payload.get("created_at_ns", 0) or 0)),
            attempts=int(payload.get("attempt_count", 0) or 0),
            last_error=str(errors[-1]) if errors else "",
        ))
    return views, counts


def _read_activity(store_root: Path, *, limit: int = 40) -> list[ActivityEvent]:
    activity_path = store_root / "activity.jsonl"
    if not activity_path.is_file():
        return []
    try:
        lines = activity_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    events: list[ActivityEvent] = []
    for line in lines[-limit:]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        detail = record.get("candidate_id") or record.get("state") or record.get("detail") or ""
        events.append(ActivityEvent(
            when=_iso(int(record.get("recorded_at_ns", 0) or 0)),
            event=str(record.get("event", "event")),
            detail=str(detail)[:48],
        ))
    events.reverse()  # newest first
    return events


def _disk_bytes(store_root: Path) -> int:
    if not store_root.is_dir():
        return 0
    total = 0
    for path in store_root.rglob("*"):
        try:
            if path.is_file():
                total += path.stat().st_size
        except OSError:
            continue
    return total


def _covered_range(path: Path, reports_root: Path) -> str:
    # Scan only the path RELATIVE to reports_root - never absolute parts, or an
    # ancestor directory that happens to be date-named (e.g. a dated project
    # folder) would be mistaken for the report's covered date.
    try:
        parts = path.relative_to(reports_root).parts
    except ValueError:
        parts = path.parts
    for part in parts:
        if _is_date(part):
            return part
    return "-"


def _provenance(path: Path, text_head: str) -> str:
    lowered = (str(path).lower() + " " + text_head.lower())
    if "synthetic" in lowered or "prototype" in lowered:
        return "SYNTHETIC"
    if "replay" in lowered:
        return "REPLAY"
    if "delayed" in lowered or "/paper/" in str(path).replace("\\", "/") or "daily_learning" in lowered:
        return "DELAYED"
    return "REAL/UNVERIFIED"


def _is_date(token: str) -> bool:
    return len(token) == 10 and token[4] == "-" and token[7] == "-"


def _category(path: Path, reports_root: Path) -> str:
    try:
        rel = path.relative_to(reports_root)
    except ValueError:
        return "report"
    if len(rel.parts) <= 1:
        return path.stem
    # Per-session reports live under data/reports/<date>/session_.../ - a leading
    # date component means a session report, not a category of its own.
    if _is_date(rel.parts[0]):
        return "session"
    return rel.parts[0]


def _read_reports(reports_root: Path, *, limit: int = 400) -> list[ReportView]:
    if not reports_root.is_dir():
        return []
    reports: list[ReportView] = []
    for path in reports_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in (".md", ".json"):
            continue
        try:
            stat = path.stat()
            head = path.read_text(encoding="utf-8", errors="replace")[:400]
        except OSError:
            continue
        reports.append(ReportView(
            title=path.stem.replace("_", " "),
            category=_category(path, reports_root),
            covered=_covered_range(path, reports_root),
            provenance=_provenance(path, head),
            path=str(path),
            modified=_iso(int(stat.st_mtime * 1e9)),
            size_bytes=stat.st_size,
        ))
        if len(reports) >= limit:
            break
    reports.sort(key=lambda report: report.modified, reverse=True)
    return reports


def read_autonomous_snapshot(
    store_root: Path | str = DEFAULT_STORE_ROOT,
    reports_root: Path | str = DEFAULT_REPORTS_ROOT,
) -> AutonomousSnapshot:
    """Read real autonomous state + reports into an immutable snapshot."""
    store_root = Path(store_root)
    reports_root = Path(reports_root)

    store_present = (store_root / "candidates").is_dir()
    candidates, counts = _read_candidates(store_root)
    activity = _read_activity(store_root)
    reports = _read_reports(reports_root)

    completed = 0
    checkpoint = store_root / "checkpoint.json"
    if checkpoint.is_file():
        try:
            completed = len(json.loads(checkpoint.read_text(encoding="utf-8")).get("completed", ()))
        except (json.JSONDecodeError, OSError, AttributeError):
            completed = 0

    running = store_present and (bool(candidates) or bool(activity))
    if not store_present:
        note = ("The autonomous intelligence service has not run in this environment "
                "(no data/autonomous store). Candidates appear here once it proposes them; "
                "reports below are read live from data/reports.")
    elif not candidates:
        note = "Autonomous store present but no candidates proposed yet."
    else:
        note = f"{len(candidates)} candidate(s) tracked; shadow-only, no runtime authority."

    return AutonomousSnapshot(
        service_running=running,
        store_present=store_present,
        candidate_count=len(candidates),
        completed_count=completed,
        counts_by_state={state: counts.get(state, 0) for state in LIFECYCLE_ORDER if counts.get(state)},
        candidates=tuple(candidates),
        recent_activity=tuple(activity),
        reports=tuple(reports),
        disk_bytes=_disk_bytes(store_root),
        note=note,
    )
