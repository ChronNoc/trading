"""Honest end-to-end pipeline status for the GUI (STAGE 3).

Renders the nine workflow stages from receiver-listening through the locked
LIVE gate, each with an explicit status, the exact blocker, the plain-language
next action, and the real counts behind it. Nothing here fabricates confidence
or profitability; a stage is only ``ready`` when the real evidence says so, and
the demo/live gates stay locked by construction.

The core :func:`compute_pipeline` is pure; :func:`load_build_aggregate` and
:func:`load_pipeline` are the thin disk-reading wrappers.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

# Stage status vocabulary (STAGE 3 spec).
STATUS_NOT_STARTED = "not_started"
STATUS_RUNNING = "running"
STATUS_READY = "ready"
STATUS_BLOCKED = "blocked"
STATUS_FAILED = "failed"
STATUS_LOCKED = "locked"


@dataclass(frozen=True, slots=True)
class PipelineStage:
    """One workflow stage with its status, blocker, next action, and counts."""

    key: str
    label: str
    status: str
    blocker: str
    next_action: str
    detail: str


@dataclass(frozen=True, slots=True)
class PipelineInputs:
    """Plain inputs describing current runtime and research state."""

    receiver_listening: bool = False
    bookmap_connected: bool = False
    recording: bool = False
    data_stale: bool = True
    current_session_drops: int = 0
    finalized_sessions: int = 0
    eligible_sessions: int = 0
    sessions_built: int = 0
    evaluations: int = 0
    accepted_setups: int = 0
    completed_outcomes: int = 0
    excluded_by_reason: Mapping[str, int] = field(default_factory=dict)
    sessions_failing_continuity: int = 0
    validation_passed: bool = False
    validation_stage_label: str = "insufficient evidence"


@dataclass(frozen=True, slots=True)
class BuildAggregate:
    """Aggregated counts across all persisted session build summaries."""

    sessions_built: int
    evaluations: int
    accepted_setups: int
    completed_outcomes: int
    ledger_eligible: int
    sessions_failing_continuity: int
    excluded_by_reason: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class PipelineStatus:
    """The nine ordered workflow stages."""

    stages: tuple[PipelineStage, ...]

    def by_key(self, key: str) -> PipelineStage:
        """Return the stage with the given key (raises KeyError if absent)."""
        for stage in self.stages:
            if stage.key == key:
                return stage
        raise KeyError(key)


def compute_pipeline(inputs: PipelineInputs) -> PipelineStatus:
    """Build the nine-stage pipeline status from plain runtime/research inputs."""
    stages = [
        _stage_receiver(inputs),
        _stage_connected(inputs),
        _stage_recording(inputs),
        _stage_finalized(inputs),
        _stage_episodes(inputs),
        _stage_completed(inputs),
        _stage_validation(inputs),
        _stage_demo_gate(),
        _stage_live_gate(),
    ]
    return PipelineStatus(stages=tuple(stages))


def _stage_receiver(i: PipelineInputs) -> PipelineStage:
    if i.receiver_listening:
        status, blocker, nxt = STATUS_READY, "", "Receiver is listening on 127.0.0.1:8765."
    else:
        status, blocker, nxt = (
            STATUS_NOT_STARTED,
            "The assistant WebSocket server is not listening.",
            "Start the assistant (start_mnq_assistant.bat) and confirm it prints it is listening.",
        )
    return PipelineStage("receiver_listening", "1. Receiver listening", status, blocker, nxt,
                         "ws://127.0.0.1:8765/bookmap")


def _stage_connected(i: PipelineInputs) -> PipelineStage:
    if not i.receiver_listening:
        return PipelineStage("bookmap_connected", "2. Bookmap add-on connected", STATUS_NOT_STARTED,
                             "Receiver is not listening yet.", "Start the assistant first.", "")
    if i.bookmap_connected:
        return PipelineStage("bookmap_connected", "2. Bookmap add-on connected", STATUS_RUNNING,
                             "", "Add-on is streaming to the receiver.", "MNQ WebSocket Forwarder connected")
    return PipelineStage("bookmap_connected", "2. Bookmap add-on connected", STATUS_BLOCKED,
                         "Receiver is listening but no Bookmap add-on has connected.",
                         "In Bookmap, enable the MNQ WebSocket Forwarder add-on on an MNQ chart.", "")


def _stage_recording(i: PipelineInputs) -> PipelineStage:
    if not i.bookmap_connected:
        return PipelineStage("recording_healthy", "3. Recording healthy", STATUS_NOT_STARTED,
                             "No connected Bookmap feed.", "Connect the Bookmap add-on first.", "")
    if i.data_stale:
        return PipelineStage("recording_healthy", "3. Recording healthy", STATUS_FAILED,
                             "Connected but no fresh market data is arriving.",
                             "Check the Bookmap add-on log and the MNQ contract subscription.",
                             f"current-session drops: {i.current_session_drops}")
    if not i.recording:
        return PipelineStage("recording_healthy", "3. Recording healthy", STATUS_BLOCKED,
                             "Feed is live but the recorder is not writing.",
                             "Confirm the recorder session started.", "")
    return PipelineStage("recording_healthy", "3. Recording healthy", STATUS_READY,
                         "", "Recording is healthy; let it capture complete sessions.",
                         f"current-session drops: {i.current_session_drops}")


def _stage_finalized(i: PipelineInputs) -> PipelineStage:
    if i.finalized_sessions > 0:
        return PipelineStage("session_finalized", "4. Session finalized", STATUS_READY, "",
                             "Finalized sessions are available for analysis.",
                             f"{i.finalized_sessions} finalized, {i.eligible_sessions} order-flow-eligible")
    return PipelineStage("session_finalized", "4. Session finalized", STATUS_NOT_STARTED,
                         "No finalized sessions yet.",
                         "Let a recording run to a clean end so it finalizes.", "")


def _stage_episodes(i: PipelineInputs) -> PipelineStage:
    if i.eligible_sessions == 0:
        return PipelineStage("episodes_built", "5. Real episodes built", STATUS_NOT_STARTED,
                             "No order-flow-eligible finalized session to analyze.",
                             "Record a clean, continuous session.", "")
    if i.sessions_built > 0:
        return PipelineStage("episodes_built", "5. Real episodes built", STATUS_READY, "",
                             "Episode build ran over eligible sessions.",
                             f"{i.sessions_built} built, {i.evaluations} evaluations, {i.accepted_setups} accepted")
    return PipelineStage("episodes_built", "5. Real episodes built", STATUS_BLOCKED,
                         "Eligible sessions exist but no episode build has run.",
                         "Run 'Analyze finalized sessions' (or python -m tools.build_real_episodes).", "")


def _stage_completed(i: PipelineInputs) -> PipelineStage:
    if i.completed_outcomes > 0:
        return PipelineStage("completed_outcomes", "6. Completed paper outcomes", STATUS_READY, "",
                             "Quality-gated completed outcomes are available for the ledger.",
                             f"{i.completed_outcomes} completed outcomes")
    top = ", ".join(f"{k}:{v}" for k, v in Counter(i.excluded_by_reason).most_common(3)) or "none recorded"
    blocker = (
        "Zero setups have completed on real data. The strategy accepted no setup - a valid, honest result."
        if i.accepted_setups == 0
        else "Setups were accepted but none produced a quality-gated completed outcome."
    )
    return PipelineStage("completed_outcomes", "6. Completed paper outcomes", STATUS_BLOCKED, blocker,
                         "Keep collecting sessions; do not loosen thresholds. Review rejection reasons.",
                         f"top rejections - {top}")


def _stage_validation(i: PipelineInputs) -> PipelineStage:
    if i.validation_passed:
        return PipelineStage("validation_gate", "7. Validation gate", STATUS_READY, "",
                             "All evidence gates passed on real data.", "Ready for the reviewed demo gate.")
    return PipelineStage("validation_gate", "7. Validation gate", STATUS_BLOCKED,
                         f"Insufficient evidence: {i.validation_stage_label}.",
                         "Accumulate the required completed-setup sample across independent days.",
                         "No profitability is claimed.")


def _stage_demo_gate() -> PipelineStage:
    return PipelineStage("demo_gate", "8. Tradovate DEMO gate", STATUS_LOCKED,
                         "Locked: requires a passed validation gate and a separate reviewed safety gate.",
                         "No action - remains locked in this build; demo execution is not enabled.",
                         "No orders are submitted.")


def _stage_live_gate() -> PipelineStage:
    return PipelineStage("live_gate", "9. LIVE gate", STATUS_LOCKED,
                         "Unavailable: live execution is out of scope and disabled.",
                         "No action - live trading is not available in this application.",
                         "OBSERVE mode only.")


def load_build_aggregate(processed_root: Path) -> BuildAggregate:
    """Aggregate all persisted *.build.json summaries under ``processed_root``."""
    excluded: Counter[str] = Counter()
    sessions_built = evaluations = accepted = completed = ledger = failing = 0
    if processed_root.is_dir():
        for path in sorted(processed_root.glob("*.build.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(data, dict):
                continue
            sessions_built += 1
            evaluations += int(data.get("evaluations", 0))
            accepted += int(data.get("accepted_candidates", 0))
            completed += int(data.get("completed", 0))
            ledger += int(data.get("ledger_eligible", 0))
            quality = data.get("replay_quality")
            if isinstance(quality, dict) and quality.get("continuity_ok") is False:
                failing += 1
            tally = data.get("rejected_condition_tally")
            if isinstance(tally, dict):
                for key, value in tally.items():
                    excluded[str(key)] += int(value)
    return BuildAggregate(
        sessions_built=sessions_built,
        evaluations=evaluations,
        accepted_setups=accepted,
        completed_outcomes=completed,
        ledger_eligible=ledger,
        sessions_failing_continuity=failing,
        excluded_by_reason=dict(excluded),
    )


def load_pipeline(
    raw_root: Path,
    processed_root: Path,
    *,
    receiver_listening: bool = False,
    bookmap_connected: bool = False,
    recording: bool = False,
    data_stale: bool = True,
    current_session_drops: int = 0,
) -> PipelineStatus:
    """Assemble pipeline inputs from disk + runtime flags and compute status."""
    from app.research.profitability_progress import load_progress
    from app.research.session_catalog import build_catalog

    catalog = build_catalog(raw_root)
    finalized = [e for e in catalog if e.finalized and not e.active]
    eligible = [e for e in finalized if e.eligible_for_order_flow_replay]
    aggregate = load_build_aggregate(processed_root)
    progress = load_progress(raw_root, processed_root)
    inputs = PipelineInputs(
        receiver_listening=receiver_listening,
        bookmap_connected=bookmap_connected,
        recording=recording,
        data_stale=data_stale,
        current_session_drops=current_session_drops,
        finalized_sessions=len(finalized),
        eligible_sessions=len(eligible),
        sessions_built=aggregate.sessions_built,
        evaluations=aggregate.evaluations,
        accepted_setups=aggregate.accepted_setups,
        completed_outcomes=aggregate.completed_outcomes,
        excluded_by_reason=aggregate.excluded_by_reason,
        sessions_failing_continuity=aggregate.sessions_failing_continuity,
        validation_passed=progress.profitable_claim_supported,
        validation_stage_label=progress.stage_label,
    )
    return compute_pipeline(inputs)
