"""Non-destructive catalog over recorded sessions in ``data/raw/``.

Reads only ``session_manifest.json`` from each session directory. It never
opens, rewrites, renames, or repairs raw event files, and it never opens the
Parquet of an actively-recording session. Historical manifests that predate
the provenance fix (e.g. a delayed session mislabeled ``source_mode=live``)
are re-classified correctly at read time from ``data_delay_minutes`` - the
raw file on disk is left untouched.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

PROVENANCE_SYNTHETIC = "SYNTHETIC"
PROVENANCE_REAL_DELAYED = "REAL_DELAYED"
PROVENANCE_REAL_REPLAY = "REAL_REPLAY"
PROVENANCE_REAL_REALTIME = "REAL_REALTIME"
PROVENANCE_UNKNOWN = "UNKNOWN"
_REAL_PROVENANCES = frozenset(
    {PROVENANCE_REAL_DELAYED, PROVENANCE_REAL_REPLAY, PROVENANCE_REAL_REALTIME},
)


@dataclass(frozen=True, slots=True)
class SessionEntry:
    """Classification of one recorded session, derived from its manifest only."""

    session_id: str
    manifest_path: Path
    provenance: str
    is_delayed: bool
    finalized: bool
    active: bool
    continuity_status: str
    depth_updates: int
    trades: int
    dropped_message_count: int
    malformed_event_count: int
    rejected_event_count: int
    missed_trade_event_count: int
    utc_start: str | None
    utc_end: str | None
    eligible_for_analysis: bool
    eligible_for_order_flow_replay: bool
    eligible_for_model_training: bool
    valid_for_live_decisions: bool
    reasons: tuple[str, ...]
    model_training_reasons: tuple[str, ...]


def classify_manifest(manifest: dict[str, object], manifest_path: Path) -> SessionEntry:
    """Classify one session for analysis and stricter model-training use."""
    synthetic = bool(manifest.get("synthetic", False))
    delay = _as_int(manifest.get("data_delay_minutes"))
    is_delayed = bool(manifest.get("is_delayed", False)) or (delay is not None and delay > 0)
    source_mode = str(manifest.get("source_mode", "unknown"))
    provenance = str(manifest.get("provenance") or _derive_provenance(synthetic, is_delayed, source_mode))

    clean = bool(manifest.get("clean_shutdown", False))
    utc_end = manifest.get("utc_end")
    finalized = utc_end is not None
    active = utc_end is None
    continuity = str(manifest.get("continuity_status", "unknown"))
    counts = cast(dict[str, object], manifest.get("event_counts", {})) \
        if isinstance(manifest.get("event_counts"), dict) else {}
    depth_updates = _as_int(counts.get("depth_updates")) or 0
    trades = _as_int(counts.get("trades")) or 0
    lifetime_dropped = _as_int(manifest.get("dropped_message_count")) or 0
    quality = cast(dict[str, object], manifest.get("data_quality", {})) \
        if isinstance(manifest.get("data_quality"), dict) else {}
    session_dropped = _as_int(quality.get("session_dropped_messages"))
    if session_dropped is None:
        session_dropped = _as_int(quality.get("bridge_dropped_messages"))
    if session_dropped is None:
        session_dropped = lifetime_dropped
    dropped = session_dropped
    receiver_intake_lost = _as_int(quality.get("receiver_intake_lost")) or 0
    queue_overflow = (
        (_as_int(quality.get("receiver_queue_overflow")) or 0)
        + (_as_int(quality.get("recorder_queue_overflow")) or 0)
        + receiver_intake_lost
    )
    malformed = _as_int(quality.get("malformed_events")) or 0
    rejected = _as_int(quality.get("rejected_events")) or 0
    out_of_order = _as_int(quality.get("out_of_order_events")) or 0
    duplicate = _as_int(quality.get("duplicate_stream_events")) or 0
    missed_trades = _as_int(quality.get("missed_trade_events")) or 0
    sequence_gaps = _as_int(quality.get("trade_sequence_gaps")) or 0

    reasons: list[str] = []
    if synthetic:
        reasons.append("synthetic: excluded from all real metrics")
    if active:
        reasons.append("active recording: skip until finalized")
    if not clean and finalized:
        reasons.append("unclean shutdown")
    if continuity != "continuous":
        reasons.append(f"continuity: {continuity}")
    if depth_updates == 0:
        reasons.append("no depth updates")
    if trades == 0:
        reasons.append("no trades recorded (depth-only)")
    if dropped > 0:
        reasons.append(f"bridge dropped {dropped} messages during this session")
    if lifetime_dropped != dropped:
        reasons.append(f"(bridge lifetime drop total: {lifetime_dropped})")
    if receiver_intake_lost > 0:
        reasons.append(f"receiver intake lost {receiver_intake_lost} events")
    if queue_overflow > receiver_intake_lost:
        reasons.append(f"capture queues overflowed {queue_overflow - receiver_intake_lost} times")
    if malformed > 0:
        reasons.append(f"receiver rejected {malformed} malformed messages")
    if rejected > 0:
        reasons.append(f"feed guard rejected {rejected} market events")
    if missed_trades > 0:
        reasons.append(f"trade sequence indicates {missed_trades} missing events")
    if sequence_gaps > 0:
        reasons.append(f"{sequence_gaps} trade-sequence gap(s)")
    if not synthetic and provenance not in _REAL_PROVENANCES:
        reasons.append(
            f"provenance {provenance}: source mode was never declared "
            "(missing delayed_mode/realtime_started control event)"
        )

    eligible = (
        finalized
        and clean
        and not synthetic
        and provenance in _REAL_PROVENANCES
        and continuity == "continuous"
        and depth_updates > 0
    )
    order_flow_eligible = (
        eligible
        and trades > 0
        and dropped == 0
        and queue_overflow == 0
        and malformed == 0
        and rejected == 0
        and out_of_order == 0
        and duplicate == 0
        and missed_trades == 0
        and sequence_gaps == 0
    )

    bridge_value = manifest.get("bridge_provenance", {})
    bridge = cast(dict[str, object], bridge_value) if isinstance(bridge_value, dict) else {}
    protocol_version = str(bridge.get("protocol_version") or "")
    provider = str(bridge.get("provider") or "").strip()
    declared_raw = bridge.get("declared_capabilities", [])
    declared = {
        str(capability).strip()
        for capability in declared_raw
        if str(capability).strip()
    } if isinstance(declared_raw, list) else set()
    observed_value = bridge.get("observed", {})
    observed = cast(dict[str, object], observed_value) if isinstance(observed_value, dict) else {}
    observed_aggressor = _as_int(observed.get("trades_with_aggressor_side")) or 0
    required_capabilities = {"aggregated_depth", "trades", "aggressor_side", "source_timestamps"}

    model_reasons = list(reasons)
    if not order_flow_eligible and not model_reasons:
        model_reasons.append("session did not pass the order-flow quality gate")
    version_parts = protocol_version.split(".")
    protocol_ok = (
        len(version_parts) >= 2
        and version_parts[0].isdigit()
        and version_parts[1].isdigit()
        and int(version_parts[0]) == 1
        and int(version_parts[1]) >= 2
    )
    if not protocol_ok:
        model_reasons.append("model training requires accepted bridge protocol 1.2 or newer")
    if not bool(bridge.get("handshake_accepted", False)):
        model_reasons.append("accepted bridge handshake provenance is missing")
    if not provider:
        model_reasons.append("bridge provider is missing")
    missing_capabilities = sorted(required_capabilities - declared)
    if missing_capabilities:
        model_reasons.append(
            "bridge did not declare required capabilities: " + ", ".join(missing_capabilities)
        )
    if trades > 0 and observed_aggressor != trades:
        model_reasons.append(
            f"aggressor-side coverage incomplete: {observed_aggressor}/{trades} trades"
        )
    storage_value = manifest.get("storage", {})
    storage = cast(dict[str, object], storage_value) if isinstance(storage_value, dict) else {}
    if not finalized or not storage:
        model_reasons.append("closed storage provenance is missing")

    model_eligible = order_flow_eligible and not model_reasons
    valid_live = provenance == PROVENANCE_REAL_REALTIME and eligible and not is_delayed

    return SessionEntry(
        session_id=str(manifest.get("session_id") or manifest_path.parent.name),
        manifest_path=manifest_path,
        provenance=provenance,
        is_delayed=is_delayed,
        finalized=finalized,
        active=active,
        continuity_status=continuity,
        depth_updates=depth_updates,
        trades=trades,
        dropped_message_count=dropped,
        malformed_event_count=malformed,
        rejected_event_count=rejected,
        missed_trade_event_count=missed_trades,
        utc_start=_as_str(manifest.get("utc_start")),
        utc_end=_as_str(utc_end),
        eligible_for_analysis=eligible,
        eligible_for_order_flow_replay=order_flow_eligible,
        eligible_for_model_training=model_eligible,
        valid_for_live_decisions=valid_live,
        reasons=tuple(reasons),
        model_training_reasons=tuple(dict.fromkeys(model_reasons)),
    )


def _invalid_manifest_entry(manifest_path: Path, reason: str) -> SessionEntry:
    """Return an auditable fail-closed entry for unreadable or invalid JSON."""
    return SessionEntry(
        session_id=manifest_path.parent.name,
        manifest_path=manifest_path,
        provenance=PROVENANCE_UNKNOWN,
        is_delayed=False,
        finalized=False,
        active=True,
        continuity_status="unreadable",
        depth_updates=0,
        trades=0,
        dropped_message_count=0,
        malformed_event_count=0,
        rejected_event_count=0,
        missed_trade_event_count=0,
        utc_start=None,
        utc_end=None,
        eligible_for_analysis=False,
        eligible_for_order_flow_replay=False,
        eligible_for_model_training=False,
        valid_for_live_decisions=False,
        reasons=(reason,),
        model_training_reasons=(reason,),
    )


def build_catalog(raw_root: Path) -> tuple[SessionEntry, ...]:
    """Scan ``raw_root`` for session manifests and classify each (read-only)."""
    if not raw_root.is_dir():
        return ()
    entries: list[SessionEntry] = []
    for manifest_path in sorted(raw_root.glob("*/*/session_manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # An actively-writing or truncated manifest remains visible and
            # fails closed instead of aborting discovery.
            entries.append(_invalid_manifest_entry(
                manifest_path,
                "manifest unreadable: active or truncated",
            ))
            continue
        if not isinstance(manifest, dict):
            # Valid JSON with the wrong top-level shape is malformed, not an
            # absent session. Keep it in the catalog so exclusion counts and
            # provenance audits account for every discovered manifest.
            entries.append(_invalid_manifest_entry(
                manifest_path,
                "manifest invalid: top-level JSON value must be an object",
            ))
            continue
        entries.append(classify_manifest(manifest, manifest_path))
    return tuple(entries)


def eligibility_report(entries: Iterable[SessionEntry]) -> dict[str, object]:
    """Summarize a catalog for the smoke test / daily coverage view."""
    items = tuple(entries)
    eligible = [entry for entry in items if entry.eligible_for_analysis]
    model_training_exclusion_reasons = Counter(
        reason
        for entry in items
        if not entry.eligible_for_model_training
        for reason in entry.model_training_reasons
    )
    return {
        "total_sessions": len(items),
        "finalized": sum(1 for entry in items if entry.finalized),
        "active_skipped": sum(1 for entry in items if entry.active),
        "synthetic": sum(1 for entry in items if entry.provenance == PROVENANCE_SYNTHETIC),
        "real_delayed": sum(1 for entry in items if entry.provenance == PROVENANCE_REAL_DELAYED),
        "eligible_for_analysis": len(eligible),
        "eligible_for_order_flow_replay": sum(
            1 for entry in items if entry.eligible_for_order_flow_replay
        ),
        "eligible_for_model_training": sum(
            1 for entry in items if entry.eligible_for_model_training
        ),
        "model_training_exclusion_reasons": dict(
            sorted(model_training_exclusion_reasons.items())
        ),
        "valid_for_live_decisions": sum(1 for entry in items if entry.valid_for_live_decisions),
    }


def _derive_provenance(synthetic: bool, is_delayed: bool, source_mode: str) -> str:
    if synthetic:
        return PROVENANCE_SYNTHETIC
    if is_delayed:
        return PROVENANCE_REAL_DELAYED
    if source_mode in {"replay", "historical"}:
        return PROVENANCE_REAL_REPLAY
    if source_mode == "live":
        return PROVENANCE_REAL_REALTIME
    return PROVENANCE_UNKNOWN


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None
