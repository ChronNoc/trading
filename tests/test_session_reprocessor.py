"""The reprocessor must recover clean on-disk sessions and refuse broken ones.

Every verdict is checked against the AUTHORITATIVE catalog classifier, so these
tests also guard the contract that a certified recovery is independently
model-training eligible and an unrecoverable one is honestly reported, never
written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.database.recorder import SESSION_DEPTH_SCHEMA, SESSION_TRADE_SCHEMA
from app.research.session_catalog import build_catalog, classify_manifest
from app.research.session_reprocessor import (
    OUTCOME_ALREADY_ELIGIBLE,
    OUTCOME_ALREADY_REPROCESSED,
    OUTCOME_NOT_CANDIDATE,
    OUTCOME_RECOVERED,
    OUTCOME_UNRECOVERABLE,
    assess_session,
    derive_disk_quality,
    reprocess_all,
    reprocess_session,
)


def _trade(seq: int, *, size: str = "5", side: str = "buy", ts: int = 0) -> dict[str, object]:
    return {
        "timestamp_ns": ts or 1_000 + seq,
        "price": "21000.25",
        "size": size,
        "aggressor_side": side,
        "instrument": "MNQ",
        "sequence_id": seq,
        "receive_sequence": seq,
    }


def _depth(i: int) -> dict[str, object]:
    return {
        "timestamp": 1_000 + i,
        "symbol": "MNQ",
        "side": "bid",
        "price": "21000.00",
        "previous_size": "10",
        "new_size": "11",
        "receive_sequence": 10_000 + i,
    }


def _candidate_manifest(session_id: str, *, quality: dict[str, object]) -> dict[str, object]:
    """A REAL, analysis-eligible manifest blocked only by the given quality counters."""
    dq = {
        "ok": False,
        "malformed_events": 0,
        "rejected_events": 0,
        "out_of_order_events": 0,
        "trade_sequence_gaps": 0,
        "missed_trade_events": 0,
        "stream_sequence_gaps": 0,
        "missed_stream_events": 0,
        "duplicate_stream_events": 0,
        "clock_drift_alerts": 0,
        "session_dropped_messages": 0,
        "bridge_dropped_messages": 0,
        "receiver_intake_lost": 0,
        "malformed_event_reasons": {},
        "rejected_event_reasons": {},
    }
    dq.update(quality)
    return {
        "session_id": session_id,
        "symbol": "MNQ",
        "source_mode": "delayed",
        "data_delay_minutes": 15,
        "is_delayed": True,
        "synthetic": False,
        "provenance": "REAL_DELAYED",
        "utc_start": "2026-08-01T00:00:00+00:00",
        "utc_end": "2026-08-01T01:00:00+00:00",
        "clean_shutdown": True,
        "continuity_status": "continuous",
        "addon_version": "1.4.0",
        "dropped_message_count": 0,
        "event_counts": {"depth_updates": 20, "trades": 10, "connection_events": 2},
        "data_quality": dq,
        "bridge_provenance": {
            "protocol_version": "1.2",
            "minimum_protocol_version": "1.2",
            "provider": "bookmap",
            "handshake_accepted": True,
            "declared_capabilities": [
                "aggregated_depth", "aggressor_side", "source_timestamps", "trades",
            ],
            "observed": {"depth_updates": 20, "trades": 10, "trades_with_aggressor_side": 10},
        },
        "storage": {"format": "closed_parquet_parts_v1", "trade_parts": 1, "depth_parts": 1},
    }


def _write_session(
    root: Path,
    session_id: str,
    *,
    trades: list[dict[str, object]],
    depth: list[dict[str, object]],
    quality: dict[str, object],
) -> Path:
    session_dir = root / "2026-08-01" / session_id
    session_dir.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(trades, schema=SESSION_TRADE_SCHEMA),
                   session_dir / "trades.parquet")
    pq.write_table(pa.Table.from_pylist(depth, schema=SESSION_DEPTH_SCHEMA),
                   session_dir / "depth.parquet")
    (session_dir / "session_manifest.json").write_text(
        json.dumps(_candidate_manifest(session_id, quality=quality)), encoding="utf-8",
    )
    return session_dir


def _clean_trades() -> list[dict[str, object]]:
    return [_trade(i, side="buy" if i % 2 else "sell") for i in range(1, 11)]


def _clean_depth() -> list[dict[str, object]]:
    return [_depth(i) for i in range(20)]


def test_recoverable_session_certifies_and_is_independently_eligible(tmp_path: Path) -> None:
    """On-disk trades are contiguous; the capture-time miss counter was noise."""
    root = tmp_path / "raw"
    session_dir = _write_session(
        root, "session_A",
        trades=_clean_trades(), depth=_clean_depth(),
        quality={"missed_trade_events": 6, "trade_sequence_gaps": 6},
    )

    assessment = assess_session(session_dir)
    assert assessment.recoverable is True
    assert assessment.disk_quality.trade_sequence_gaps == 0
    assert assessment.disk_quality.missed_trade_events == 0
    assert assessment.disk_quality.filtered_trades == 10

    result = reprocess_session(session_dir, output_root=root, apply=True)
    assert result.outcome == OUTCOME_RECOVERED
    assert result.recovered_trades == 10
    assert result.output_dir is not None

    # The written recovery is independently model-training eligible.
    manifest = json.loads((result.output_dir / "session_manifest.json").read_text())
    entry = classify_manifest(manifest, result.output_dir / "session_manifest.json")
    assert entry.eligible_for_model_training is True
    # Lineage is recorded; the source's original counters are preserved, not hidden.
    lineage = manifest["reprocessed_from"]
    assert lineage["source_session_id"] == "session_A"
    assert lineage["source_data_quality"]["missed_trade_events"] == 6


def test_original_session_is_never_modified(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    session_dir = _write_session(
        root, "session_A",
        trades=_clean_trades(), depth=_clean_depth(),
        quality={"missed_trade_events": 6, "trade_sequence_gaps": 6},
    )
    manifest_before = (session_dir / "session_manifest.json").read_bytes()
    trades_before = (session_dir / "trades.parquet").read_bytes()

    reprocess_session(session_dir, output_root=root, apply=True)

    assert (session_dir / "session_manifest.json").read_bytes() == manifest_before
    assert (session_dir / "trades.parquet").read_bytes() == trades_before


def test_unrecoverable_when_trades_are_gone_from_disk(tmp_path: Path) -> None:
    """A real on-disk gap means the missing trades were never written - unrecoverable."""
    root = tmp_path / "raw"
    # Sequence jumps 1,2,3 -> 50: 46 real trades are permanently absent from disk.
    broken = [_trade(1), _trade(2), _trade(3), _trade(50), _trade(51)]
    session_dir = _write_session(
        root, "session_B",
        trades=broken, depth=_clean_depth(),
        quality={"missed_trade_events": 46, "trade_sequence_gaps": 1},
    )

    assessment = assess_session(session_dir)
    assert assessment.recoverable is False
    assert assessment.disk_quality.missed_trade_events == 46
    assert assessment.reasons  # honest, non-empty blocking reasons

    result = reprocess_session(session_dir, output_root=root, apply=True)
    assert result.outcome == OUTCOME_UNRECOVERABLE
    assert result.output_dir is None
    # Nothing was written.
    assert not (root / "2026-08-01" / "session_B__reprocessed").exists()


def test_zero_size_artifact_is_filtered_and_restores_continuity(tmp_path: Path) -> None:
    """A zero-size marker sharing a sequence id breaks continuity until it is dropped."""
    root = tmp_path / "raw"
    trades = [
        _trade(1, side="buy"),
        _trade(1, size="0", side=""),   # zero-size artifact: duplicate seq, no aggressor
        _trade(2, side="sell"),
        _trade(3, side="buy"),
    ]
    session_dir = _write_session(
        root, "session_C",
        trades=trades, depth=_clean_depth(),
        quality={"malformed_events": 1, "duplicate_stream_events": 1},
    )

    dq = derive_disk_quality(session_dir)
    assert dq.zero_size_dropped == 1
    assert dq.filtered_trades == 3
    assert dq.duplicate_trade_sequences == 0          # dup vanished with the artifact
    assert dq.trades_with_aggressor == 3              # coverage complete after filter

    result = reprocess_session(session_dir, output_root=root, apply=True)
    assert result.outcome == OUTCOME_RECOVERED
    assert result.recovered_trades == 3
    assert result.disk_quality.zero_size_dropped == 1

    # The written trades exclude the zero-size row.
    written = pq.read_table(result.output_dir / "trades.parquet").to_pylist()
    assert len(written) == 3
    assert all(float(row["size"]) > 0 for row in written)


def test_out_of_order_depth_detected_across_parts(tmp_path: Path) -> None:
    """Out-of-order depth is counted across the part boundary (carried high-water)."""
    root = tmp_path / "raw"
    session_dir = root / "2026-08-01" / "session_D"
    parts = session_dir / "depth_parts"
    parts.mkdir(parents=True)
    # part 0 ascends to ts 1004; part 1 opens at 1002 (a regression) then ascends.
    pq.write_table(
        pa.Table.from_pylist([_depth(i) for i in range(5)], schema=SESSION_DEPTH_SCHEMA),
        parts / "part-000000.parquet",
    )
    lower = dict(_depth(0), timestamp=1002)
    pq.write_table(
        pa.Table.from_pylist(
            [lower] + [_depth(i) for i in range(5, 8)], schema=SESSION_DEPTH_SCHEMA,
        ),
        parts / "part-000001.parquet",
    )
    pq.write_table(pa.Table.from_pylist(_clean_trades(), schema=SESSION_TRADE_SCHEMA),
                   session_dir / "trades.parquet")
    (session_dir / "session_manifest.json").write_text(
        json.dumps(_candidate_manifest("session_D", quality={"malformed_events": 1})),
        encoding="utf-8",
    )

    dq = derive_disk_quality(session_dir)
    assert dq.filtered_depth == 9
    assert dq.out_of_order_depth == 1

    # Out-of-order depth is a real order-flow break -> not recoverable.
    assert assess_session(session_dir).recoverable is False


def test_reprocess_all_skips_non_candidates_and_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "raw"
    # A recoverable candidate.
    _write_session(root, "cand", trades=_clean_trades(), depth=_clean_depth(),
                   quality={"missed_trade_events": 2, "trade_sequence_gaps": 2})
    # An already-eligible session (clean quality from the start).
    _write_session(root, "good", trades=_clean_trades(), depth=_clean_depth(), quality={"ok": True})
    # A real-loss session: dropped messages -> not a reprocess candidate.
    _write_session(root, "lossy", trades=_clean_trades(), depth=_clean_depth(),
                   quality={"session_dropped_messages": 500, "bridge_dropped_messages": 500})

    first = {r.source_session_id: r for r in reprocess_all(
        build_catalog(root), output_root=root, apply=True)}
    assert first["good"].outcome == OUTCOME_ALREADY_ELIGIBLE
    assert first["lossy"].outcome == OUTCOME_NOT_CANDIDATE
    assert first["cand"].outcome == OUTCOME_RECOVERED

    # Re-running does not rewrite an existing recovery.
    second = {r.source_session_id: r for r in reprocess_all(
        build_catalog(root), output_root=root, apply=True)}
    assert second["cand"].outcome == OUTCOME_ALREADY_REPROCESSED
