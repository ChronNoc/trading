"""Automatic, idempotent post-finalization episode builds (STAGE 4).

When a session finalizes cleanly, its episodes should be built automatically in
the background - but never for an active recording, and never twice for the same
inputs. A build is keyed by ``(session_id, source-file hashes, builder version,
strategy version)``; if the persisted build summary already matches that key,
the session is skipped. Build status and any error are persisted so a failure is
visible and does not silently vanish.

This module is deliberately thread-agnostic at its core: :func:`run_pending_builds`
is a plain function, and :func:`schedule_pending_builds` is the thin wrapper that
runs it on a daemon thread so the GUI stays responsive.
"""

from __future__ import annotations

import json
import threading
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.research.episode_builder import (
    BUILDER_VERSION,
    STRATEGY_VERSION,
    _source_hashes,
    build_episodes,
    write_episode_artifacts,
)
from app.research.session_catalog import SessionEntry, build_catalog

OUTCOME_BUILT = "built"
OUTCOME_SKIPPED_UP_TO_DATE = "skipped_up_to_date"
OUTCOME_SKIPPED_ACTIVE = "skipped_active"
OUTCOME_SKIPPED_INELIGIBLE = "skipped_ineligible"
OUTCOME_FAILED = "failed"


@dataclass(frozen=True, slots=True)
class BuildStatus:
    """Outcome of considering one session for an automatic build."""

    session_id: str
    outcome: str
    reason: str
    completed: int = 0
    accepted: int = 0
    error: str = ""


def build_signature(session_dir: Path) -> dict[str, object]:
    """Return the idempotency signature for a session's current inputs."""
    return {
        "source_file_hashes": _source_hashes(session_dir),
        "builder_version": BUILDER_VERSION,
        "strategy_version": STRATEGY_VERSION,
    }


def needs_build(session_id: str, session_dir: Path, processed_root: Path) -> tuple[bool, str]:
    """Return (needs_build, reason) by comparing current inputs to the last build.

    A rebuild is required when there is no prior summary, or when the source
    hashes, builder version, or strategy version have changed. Unchanged inputs
    are skipped so repeated runs do no redundant work.
    """
    summary_path = processed_root / f"{session_id}.build.json"
    if not summary_path.is_file():
        return True, "no prior build summary"
    try:
        prior = json.loads(summary_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return True, "prior build summary unreadable"
    if not isinstance(prior, dict):
        return True, "prior build summary malformed"
    signature = build_signature(session_dir)
    if prior.get("builder_version") != signature["builder_version"]:
        return True, "builder version changed"
    if prior.get("strategy_version") != signature["strategy_version"]:
        return True, "strategy version changed"
    if prior.get("source_file_hashes") != signature["source_file_hashes"]:
        return True, "source data changed"
    return False, "up to date"


def build_finalized_session(
    entry: SessionEntry,
    *,
    processed_root: Path,
    labels_root: Path,
) -> BuildStatus:
    """Build one session's episodes if needed; never touch an active recording."""
    if entry.active or not entry.finalized:
        return BuildStatus(entry.session_id, OUTCOME_SKIPPED_ACTIVE, "session is active/not finalized")
    if not entry.eligible_for_analysis:
        return BuildStatus(entry.session_id, OUTCOME_SKIPPED_INELIGIBLE, "not analysis-eligible")
    session_dir = entry.manifest_path.parent
    required, reason = needs_build(entry.session_id, session_dir, processed_root)
    if not required:
        return BuildStatus(entry.session_id, OUTCOME_SKIPPED_UP_TO_DATE, reason)
    try:
        result = build_episodes(session_dir, session_id=entry.session_id, provenance=entry.provenance)
        write_episode_artifacts(result, processed_root=processed_root, labels_root=labels_root)
        _write_status(processed_root, entry.session_id, OUTCOME_BUILT, reason, error="")
        return BuildStatus(
            entry.session_id, OUTCOME_BUILT, reason,
            completed=result.completed, accepted=result.accepted_candidates,
        )
    except Exception as exc:  # noqa: BLE001 - persist any failure, do not crash the worker
        detail = f"{type(exc).__name__}: {exc}"
        _write_status(processed_root, entry.session_id, OUTCOME_FAILED, reason, error=detail, tb=traceback.format_exc())
        return BuildStatus(entry.session_id, OUTCOME_FAILED, reason, error=detail)


def run_pending_builds(
    raw_root: Path,
    processed_root: Path,
    labels_root: Path,
) -> list[BuildStatus]:
    """Build every finalized, eligible session whose inputs changed. Idempotent."""
    catalog = build_catalog(raw_root)
    statuses: list[BuildStatus] = []
    for entry in catalog:
        statuses.append(build_finalized_session(entry, processed_root=processed_root, labels_root=labels_root))
    return statuses


def schedule_pending_builds(
    raw_root: Path,
    processed_root: Path,
    labels_root: Path,
    *,
    on_complete: "Sequence[object] | None" = None,
    done_callback=None,
) -> threading.Thread:
    """Run :func:`run_pending_builds` on a daemon thread and return it.

    Keeps the GUI responsive: the caller can join or poll ``done_callback``.
    """
    def _worker() -> None:
        statuses = run_pending_builds(raw_root, processed_root, labels_root)
        if done_callback is not None:
            done_callback(statuses)

    thread = threading.Thread(target=_worker, name="episode-build-worker", daemon=True)
    thread.start()
    return thread


def _write_status(processed_root: Path, session_id: str, outcome: str, reason: str, *, error: str, tb: str = "") -> None:
    processed_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "session_id": session_id,
        "outcome": outcome,
        "reason": reason,
        "error": error,
        "traceback": tb,
        "recorded_utc": datetime.now(UTC).isoformat(),
        "builder_version": BUILDER_VERSION,
        "strategy_version": STRATEGY_VERSION,
    }
    path = processed_root / f"{session_id}.buildstatus.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
