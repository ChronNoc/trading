"""Recover a recorded session from disk, or prove it cannot be recovered.

The salvage audit (``tools/salvage_audit.py``) flags REPROCESS candidates:
sessions whose only training blocker is the now-fixed zero-size-trade artifact.
That flag is an UPPER BOUND - it trusts the capture-time quality counters. This
module is the honest verifier that settles each case against the actual bytes on
disk.

For one session it:

1. Re-reads ONLY the stored Parquet (read-only), stream by stream.
2. Drops zero-size trade rows - the artifact the Bookmap add-on now filters at
   source (a size<=0 "trade" is a marker, not a fill).
3. Re-derives every data-quality counter the catalog actually checks, from the
   surviving on-disk stream: trade-sequence gaps, missed trades, non-monotonic
   / duplicate sequences, out-of-order depth, and aggressor-side coverage.
4. Builds a candidate manifest carrying those re-derived counters and the SAME
   real bridge provenance, then runs the AUTHORITATIVE
   :func:`app.research.session_catalog.classify_manifest` on it. A session is
   certified recovered ONLY when that classifier independently returns
   ``eligible_for_model_training``. The reprocessor invents no gate of its own,
   so it can never bless what the catalog would reject.

When (and only when) certified, and only under ``apply=True``, it writes a NEW
session directory holding the filtered Parquet and the corrected manifest. The
original session is never modified. A ``reprocessed_from`` block records the
source id, the reprocessor version, the dropped-row count, and the source's
ORIGINAL quality counters, so the recovery is fully auditable and nothing is
hidden.

A candidate whose real trades were rejected at capture (permanently absent from
disk - their sequence numbers gone) fails certification with the exact
classifier reasons and is left untouched. No reprocessing can conjure back data
that was never written. Even every recovery combined still leaves the challenger
needing >= 4 trading days with both win and loss labels before it can validate a
model.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from app.database.recorder import (
    SESSION_DEPTH_SCHEMA,
    SESSION_TRADE_SCHEMA,
    atomic_write_text,
    replace_with_retry,
    unique_temp_path,
)
from app.research.session_catalog import SessionEntry, classify_manifest
from tools.salvage_audit import REPROCESS, categorize_entry

REPROCESSOR_VERSION = "1.0.0"
_REPROCESSED_SUFFIX = "__reprocessed"
_BATCH_ROWS = 8192

# Outcomes of considering one session for reprocessing.
OUTCOME_RECOVERED = "recovered"
OUTCOME_UNRECOVERABLE = "unrecoverable"
OUTCOME_ALREADY_ELIGIBLE = "already_eligible"
OUTCOME_NOT_CANDIDATE = "not_a_reprocess_candidate"
OUTCOME_ALREADY_REPROCESSED = "already_reprocessed"


@dataclass(frozen=True, slots=True)
class DiskQuality:
    """Data-quality counters re-derived from the filtered on-disk stream."""

    filtered_trades: int
    filtered_depth: int
    zero_size_dropped: int
    trades_with_aggressor: int
    trade_sequence_gaps: int
    missed_trade_events: int
    duplicate_trade_sequences: int
    out_of_order_depth: int


@dataclass(frozen=True, slots=True)
class ReprocessAssessment:
    """Verdict for one session, decided against the bytes on disk (no writes)."""

    source_session_id: str
    recoverable: bool
    disk_quality: DiskQuality
    candidate_manifest: dict[str, object]
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ReprocessResult:
    """Outcome of reprocessing one session, including any written output."""

    source_session_id: str
    outcome: str
    disk_quality: DiskQuality | None
    reasons: tuple[str, ...]
    output_dir: Path | None = None

    @property
    def recovered_trades(self) -> int:
        """Real trades certified and (when applied) written for training."""
        if self.outcome != OUTCOME_RECOVERED or self.disk_quality is None:
            return 0
        return self.disk_quality.filtered_trades


def _is_zero_size(value: object) -> bool:
    """Return whether a stored trade size represents a zero-size artifact."""
    if value is None:
        return True
    try:
        return float(str(value)) <= 0.0
    except ValueError:
        # An unparseable size is not a real fill; treat it as an artifact.
        return True


def _source_stream_files(session_dir: Path, stem: str) -> tuple[Path, ...]:
    """Return the Parquet inputs for one stream (finalized file or closed parts)."""
    finalized = session_dir / f"{stem}.parquet"
    if finalized.is_file():
        return (finalized,)
    parts_dir = session_dir / ("trade_parts" if stem == "trades" else "depth_parts")
    if not parts_dir.is_dir():
        return ()
    return tuple(sorted(parts_dir.glob("part-*.parquet")))


def _iter_rows(paths: tuple[Path, ...]) -> "Iterator[dict[str, object]]":
    for path in paths:
        parquet = pq.ParquetFile(path)
        for batch in parquet.iter_batches(batch_size=_BATCH_ROWS):
            yield from batch.to_pylist()


def derive_disk_quality(session_dir: Path) -> DiskQuality:
    """Re-derive quality counters from the filtered on-disk stream (read-only).

    Each stream is scanned independently, which is both faster than a causal
    merge and the correct granularity for the catalog's per-stream gates:
    trade continuity is judged over the trade ``sequence_id`` line, depth
    ordering over the depth timestamp line. Zero-size trade rows are dropped
    BEFORE continuity is measured, so a duplicate/artifact marker no longer
    counts against the real fills.
    """
    filtered_trades = 0
    zero_size = 0
    aggressor = 0
    gaps = 0
    missed = 0
    duplicate = 0
    last_seq: int | None = None
    for row in _iter_rows(_source_stream_files(session_dir, "trades")):
        if _is_zero_size(row.get("size")):
            zero_size += 1
            continue
        filtered_trades += 1
        if str(row.get("aggressor_side", "")).lower() in {"buy", "sell"}:
            aggressor += 1
        seq_value = row.get("sequence_id")
        if seq_value is not None:
            seq = int(seq_value)
            if last_seq is not None:
                if seq > last_seq + 1:
                    gaps += 1
                    missed += seq - last_seq - 1
                elif seq <= last_seq:
                    duplicate += 1
            if last_seq is None or seq > last_seq:
                last_seq = seq

    # Depth streams are large (millions of rows), so avoid materializing them as
    # Python dicts. The row count comes from Parquet metadata, and out-of-order
    # detection scans only the timestamp column, vectorized. Mirrors the feed
    # guard exactly: an update earlier than the running high-water mark is
    # counted and does not advance the mark (so the high-water mark is simply the
    # running maximum, which a regression never lowers).
    filtered_depth = 0
    out_of_order_depth = 0
    high_water: int | None = None
    for path in _source_stream_files(session_dir, "depth"):
        parquet = pq.ParquetFile(path)
        filtered_depth += parquet.metadata.num_rows
        for batch in parquet.iter_batches(batch_size=65_536, columns=["timestamp"]):
            arr = batch.column(0).to_numpy(zero_copy_only=False).astype(np.int64, copy=False)
            if arr.size == 0:
                continue
            if high_water is None:
                running_max = np.maximum.accumulate(arr)
                prior = np.concatenate(([arr[0]], running_max[:-1]))
            else:
                extended = np.concatenate(([np.int64(high_water)], arr))
                running_max = np.maximum.accumulate(extended)
                prior = running_max[:-1]
            out_of_order_depth += int(np.count_nonzero(arr < prior))
            high_water = int(running_max[-1])

    return DiskQuality(
        filtered_trades=filtered_trades,
        filtered_depth=filtered_depth,
        zero_size_dropped=zero_size,
        trades_with_aggressor=aggressor,
        trade_sequence_gaps=gaps,
        missed_trade_events=missed,
        duplicate_trade_sequences=duplicate,
        out_of_order_depth=out_of_order_depth,
    )


def build_candidate_manifest(
    original: dict[str, object],
    disk_quality: DiskQuality,
) -> dict[str, object]:
    """Return the manifest a recovered session WOULD carry (no writes).

    It clones the source manifest, keeping its real provenance untouched, and
    replaces exactly the fields the reprocess changes: the event counts, the
    data-quality counters (now re-derived from disk), and the observed
    aggressor/trade coverage. A ``reprocessed_from`` block preserves the source
    lineage and its original counters so nothing is silently overwritten.
    """
    candidate = json.loads(json.dumps(original))  # deep copy via JSON round-trip
    dq = disk_quality

    counts = dict(candidate.get("event_counts") or {})
    counts["trades"] = dq.filtered_trades
    counts["depth_updates"] = dq.filtered_depth
    candidate["event_counts"] = counts

    bridge = dict(candidate.get("bridge_provenance") or {})
    observed = dict(bridge.get("observed") or {})
    observed["trades"] = dq.filtered_trades
    observed["depth_updates"] = dq.filtered_depth
    observed["trades_with_aggressor_side"] = dq.trades_with_aggressor
    bridge["observed"] = observed
    candidate["bridge_provenance"] = bridge

    original_quality = dict(candidate.get("data_quality") or {})
    reprocessed_quality: dict[str, object] = {
        # Rejected/malformed input never reaches disk, so the reprocessed stream
        # carries none. Overflow and drops are excluded upstream (a REPROCESS
        # candidate has neither), so they are zero here too.
        "malformed_events": 0,
        "rejected_events": 0,
        "out_of_order_events": dq.out_of_order_depth,
        "trade_sequence_gaps": dq.trade_sequence_gaps,
        "missed_trade_events": dq.missed_trade_events,
        "stream_sequence_gaps": dq.trade_sequence_gaps,
        "missed_stream_events": dq.missed_trade_events,
        "duplicate_stream_events": dq.duplicate_trade_sequences,
        "clock_drift_alerts": 0,
        "session_dropped_messages": 0,
        "bridge_dropped_messages": original_quality.get("bridge_dropped_messages", 0),
        "receiver_intake_lost": 0,
        "malformed_event_reasons": {},
        "rejected_event_reasons": {},
    }
    invalidating = {k: v for k, v in reprocessed_quality.items()
                    if k not in {"bridge_dropped_messages", "malformed_event_reasons",
                                 "rejected_event_reasons"}}
    reprocessed_quality["ok"] = all(v == 0 for v in invalidating.values())
    candidate["data_quality"] = reprocessed_quality
    candidate["dropped_message_count"] = 0

    candidate["storage"] = {
        "format": "reprocessed_parquet_v1",
        "finalized_single_files": True,
        "reprocessed": True,
    }

    source_id = str(original.get("session_id") or "")
    candidate["reprocessed_from"] = {
        "source_session_id": source_id,
        "reprocessor_version": REPROCESSOR_VERSION,
        "reprocessed_utc": datetime.now(UTC).isoformat(),
        "filter": "drop_size_le_zero",
        "zero_size_rows_dropped": dq.zero_size_dropped,
        "source_data_quality": original_quality,
    }
    return candidate


def assess_session(session_dir: Path) -> ReprocessAssessment:
    """Decide, from disk only, whether a session can be recovered (no writes)."""
    manifest_path = session_dir / "session_manifest.json"
    original = json.loads(manifest_path.read_text(encoding="utf-8"))
    disk_quality = derive_disk_quality(session_dir)
    candidate = build_candidate_manifest(original, disk_quality)
    entry = classify_manifest(candidate, manifest_path)
    reasons = () if entry.eligible_for_model_training else entry.model_training_reasons
    return ReprocessAssessment(
        source_session_id=str(original.get("session_id") or session_dir.name),
        recoverable=entry.eligible_for_model_training,
        disk_quality=disk_quality,
        candidate_manifest=candidate,
        reasons=reasons,
    )


def _reprocessed_dir(source_dir: Path, output_root: Path) -> Path:
    partition = source_dir.parent.name
    return output_root / partition / f"{source_dir.name}{_REPROCESSED_SUFFIX}"


def _write_filtered_stream(
    source_paths: tuple[Path, ...],
    destination: Path,
    schema: pa.Schema,
    *,
    drop_zero_size_trades: bool,
) -> None:
    """Write a single filtered Parquet file for one stream, batched and atomic."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = unique_temp_path(destination, ".tmp")
    writer: pq.ParquetWriter | None = None
    try:
        writer = pq.ParquetWriter(temporary, schema)
        for path in source_paths:
            parquet = pq.ParquetFile(path)
            for batch in parquet.iter_batches(batch_size=_BATCH_ROWS):
                if not drop_zero_size_trades:
                    # Nothing to filter (depth, or trades with no artifacts): copy the
                    # Arrow batch straight through without a Python round-trip.
                    writer.write_batch(batch)
                    continue
                rows = [row for row in batch.to_pylist() if not _is_zero_size(row.get("size"))]
                if rows:
                    writer.write_table(pa.Table.from_pylist(rows, schema=schema))
        writer.close()
        writer = None
        replace_with_retry(temporary, destination)
    finally:
        if writer is not None:
            writer.close()
        temporary.unlink(missing_ok=True)


def write_recovered_session(
    source_dir: Path,
    assessment: ReprocessAssessment,
    output_root: Path,
) -> Path:
    """Write the certified recovery to a NEW session dir and return its path.

    Only ever called for a recoverable assessment. The source session is left
    completely untouched.
    """
    if not assessment.recoverable:
        raise ValueError("refusing to write a session that did not certify recoverable")
    target = _reprocessed_dir(source_dir, output_root)
    target.mkdir(parents=True, exist_ok=False)

    _write_filtered_stream(
        _source_stream_files(source_dir, "depth"),
        target / "depth.parquet",
        SESSION_DEPTH_SCHEMA,
        drop_zero_size_trades=False,
    )
    _write_filtered_stream(
        _source_stream_files(source_dir, "trades"),
        target / "trades.parquet",
        SESSION_TRADE_SCHEMA,
        drop_zero_size_trades=True,
    )

    manifest = dict(assessment.candidate_manifest)
    manifest["session_id"] = target.name
    atomic_write_text(
        target / "session_manifest.json",
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
    )
    return target


def reprocess_session(
    session_dir: Path,
    *,
    output_root: Path,
    apply: bool,
) -> ReprocessResult:
    """Assess one session and, under ``apply``, write a certified recovery."""
    source_id = session_dir.name
    if _reprocessed_dir(session_dir, output_root).exists():
        return ReprocessResult(source_id, OUTCOME_ALREADY_REPROCESSED, None, ())
    assessment = assess_session(session_dir)
    source_id = assessment.source_session_id
    if not assessment.recoverable:
        return ReprocessResult(
            source_id, OUTCOME_UNRECOVERABLE, assessment.disk_quality, assessment.reasons,
        )
    output_dir: Path | None = None
    if apply:
        output_dir = write_recovered_session(session_dir, assessment, output_root)
    return ReprocessResult(
        source_id, OUTCOME_RECOVERED, assessment.disk_quality, (), output_dir,
    )


def reprocess_all(
    entries: tuple[SessionEntry, ...],
    *,
    output_root: Path,
    apply: bool,
) -> list[ReprocessResult]:
    """Reprocess every REPROCESS candidate in a catalog. Non-candidates are skipped."""
    results: list[ReprocessResult] = []
    for entry in entries:
        session_dir = entry.manifest_path.parent
        if entry.eligible_for_model_training:
            results.append(
                ReprocessResult(entry.session_id, OUTCOME_ALREADY_ELIGIBLE, None, ()),
            )
            continue
        if categorize_entry(entry) != REPROCESS:
            results.append(
                ReprocessResult(entry.session_id, OUTCOME_NOT_CANDIDATE, None, ()),
            )
            continue
        results.append(reprocess_session(session_dir, output_root=output_root, apply=apply))
    return results
