"""Walk-forward volatility regime: first-hour ranges, cache, labels, per-label totals."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.research.session_volatility import (
    LABEL_HIGH,
    LABEL_LOW,
    LABEL_NORMAL,
    LABEL_UNKNOWN,
    FirstHourSample,
    SessionVolatilityCache,
    VolatilityRegimeConfig,
    VolatilityRegimeTable,
    first_hour_window_ns,
    label_for_session_start,
    load_cache,
    read_first_hour_sample,
    save_cache,
    totals_by_label,
    trading_days_with_full_first_hour,
    update_cache,
)


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()) * 1_000_000_000


# --- first-hour window and coverage ------------------------------------------------


def test_first_hour_window_follows_new_york_daylight_saving() -> None:
    assert first_hour_window_ns(date(2026, 11, 6)) == (_ns("2026-11-06T14:30:00Z"), _ns("2026-11-06T15:30:00Z"))
    assert first_hour_window_ns(date(2026, 7, 10)) == (_ns("2026-07-10T13:30:00Z"), _ns("2026-07-10T14:30:00Z"))


def test_only_sessions_covering_the_whole_first_hour_count() -> None:
    friday = date(2026, 11, 6)
    # Covers 09:30-10:30 New York (14:30-15:30Z).
    assert trading_days_with_full_first_hour("2026-11-05T23:00:00+00:00", "2026-11-06T21:00:00+00:00") == [friday]
    # Starts at 10:00 New York: the first half hour is missing, so the range would be understated.
    assert trading_days_with_full_first_hour("2026-11-06T15:00:00+00:00", "2026-11-06T21:00:00+00:00") == []
    # Stops before the hour ends.
    assert trading_days_with_full_first_hour("2026-11-06T13:00:00+00:00", "2026-11-06T15:00:00+00:00") == []


def test_coverage_tolerance_forgives_seconds_but_not_minutes() -> None:
    friday = date(2026, 11, 6)
    assert trading_days_with_full_first_hour("2026-11-06T14:30:30+00:00", "2026-11-06T16:00:00+00:00") == [friday]
    assert trading_days_with_full_first_hour("2026-11-06T14:32:00+00:00", "2026-11-06T16:00:00+00:00") == []


def test_weekends_and_unreadable_times_never_count() -> None:
    assert trading_days_with_full_first_hour("2026-11-07T14:00:00+00:00", "2026-11-07T16:00:00+00:00") == []  # Saturday
    assert trading_days_with_full_first_hour(None, "2026-11-06T21:00:00+00:00") == []
    assert trading_days_with_full_first_hour("not a time", "2026-11-06T21:00:00+00:00") == []
    assert trading_days_with_full_first_hour("2026-11-06T21:00:00+00:00", "2026-11-06T13:00:00+00:00") == []


# --- reading the first hour from parquet --------------------------------------------


def _write_trade_part(session: Path, index: int, rows: list[tuple[int, str]]) -> None:
    folder = session / "trade_parts"
    folder.mkdir(parents=True, exist_ok=True)
    table = pa.table({
        "timestamp_ns": pa.array([row[0] for row in rows], type=pa.int64()),
        "price": pa.array([row[1] for row in rows], type=pa.string()),
    })
    pq.write_table(table, folder / f"part-{index:06d}.parquet")


def test_sample_uses_only_trades_inside_the_window(tmp_path: Path) -> None:
    session = tmp_path / "s1"
    _write_trade_part(session, 0, [(_ns("2026-11-06T14:00:00Z"), "100.00")])  # before the hour
    _write_trade_part(session, 1, [
        (_ns("2026-11-06T14:30:00Z"), "200.00"),   # first instant is inside
        (_ns("2026-11-06T14:45:00Z"), "205.25"),
        (_ns("2026-11-06T15:29:59Z"), "199.50"),
    ])
    _write_trade_part(session, 2, [
        (_ns("2026-11-06T15:30:00Z"), "300.00"),   # the end instant is excluded
        (_ns("2026-11-06T16:00:00Z"), "50.00"),
    ])

    sample = read_first_hour_sample(session, "s1", date(2026, 11, 6))

    assert sample is not None
    assert (sample.ib_high, sample.ib_low, sample.trades) == (Decimal("205.25"), Decimal("199.50"), 3)
    assert sample.ib_range == Decimal("5.75")


def test_sample_is_none_when_no_trade_prints_in_the_hour(tmp_path: Path) -> None:
    session = tmp_path / "s1"
    _write_trade_part(session, 0, [(_ns("2026-11-06T16:00:00Z"), "100.00")])

    assert read_first_hour_sample(session, "s1", date(2026, 11, 6)) is None
    assert read_first_hour_sample(tmp_path / "missing", "x", date(2026, 11, 6)) is None


def test_files_outside_the_window_are_skipped_by_their_footers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cost must not grow with session length: only overlapping part files are read."""
    session = tmp_path / "s1"
    for index in range(30):  # 30 parts, only part 15 holds the first hour
        hour = "2026-11-06T14:40:00Z" if index == 15 else f"2026-11-0{(index % 5) + 1}T03:00:00Z"
        _write_trade_part(session, index, [(_ns(hour), f"{100 + index}.00")])
    reads: list[object] = []
    real_read_table = pq.read_table

    def counting_read_table(path: object, *args: object, **kwargs: object) -> object:
        reads.append(path)
        return real_read_table(path, *args, **kwargs)

    monkeypatch.setattr(pq, "read_table", counting_read_table)

    sample = read_first_hour_sample(session, "s1", date(2026, 11, 6))

    assert sample is not None and sample.ib_high == Decimal("115.00")
    assert len(reads) == 1


# --- cache ---------------------------------------------------------------------------


def test_cache_round_trips_and_survives_damage(tmp_path: Path) -> None:
    cache = SessionVolatilityCache()
    cache.put("a", "key1", [FirstHourSample("2026-11-06", "a", Decimal("205.25"), Decimal("199.50"), 3)])
    path = tmp_path / "nested" / "cache.json"

    save_cache(path, cache)
    loaded = load_cache(path)

    assert loaded.is_current("a", "key1") and not loaded.is_current("a", "other-key")
    assert loaded.all_samples() == [FirstHourSample("2026-11-06", "a", Decimal("205.25"), Decimal("199.50"), 3)]
    assert not list(path.parent.glob("*.tmp"))  # atomic write leaves no temp file

    path.write_text("{not json", encoding="utf-8")
    assert load_cache(path).sessions == {}
    path.write_text(json.dumps({"version": 999, "sessions": {"a": {}}}), encoding="utf-8")
    assert load_cache(path).sessions == {}
    assert load_cache(tmp_path / "absent.json").sessions == {}


def test_one_malformed_cache_record_does_not_poison_the_rest() -> None:
    cache = SessionVolatilityCache(sessions={
        "bad": {"key": "k", "samples": [{"trading_day": "2026-11-06"}]},
        "good": {"key": "k", "samples": [
            {"trading_day": "2026-11-06", "ib_high": "10", "ib_low": "5", "trades": 4},
        ]},
    })

    assert [sample.session_id for sample in cache.all_samples()] == ["good"]


def _manifest(session_id: str, start: str, end: str | None, trades: int) -> dict[str, object]:
    manifest: dict[str, object] = {
        "session_id": session_id, "symbol": "MNQ", "provenance": "REAL_REPLAY",
        "source_mode": "replay", "clean_shutdown": True, "continuity_status": "continuous",
        "utc_start": start, "event_counts": {"depth_updates": 10, "trades": trades},
    }
    if end is not None:
        manifest["utc_end"] = end
    return manifest


def _write_session(raw: Path, day: str, manifest: dict[str, object]) -> Path:
    directory = raw / day / str(manifest["session_id"])
    directory.mkdir(parents=True)
    (directory / "session_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def test_update_cache_analyses_new_sessions_once_and_skips_unfinished(tmp_path: Path) -> None:
    raw = tmp_path / "data" / "raw"
    covering = _write_session(raw, "2026-11-06", _manifest(
        "session_cover", "2026-11-05T23:00:00+00:00", "2026-11-06T21:00:00+00:00", 3))
    _write_trade_part(covering, 0, [
        (_ns("2026-11-06T14:35:00Z"), "200.00"), (_ns("2026-11-06T15:00:00Z"), "210.00"),
        (_ns("2026-11-06T15:20:00Z"), "190.00"),
    ])
    _write_session(raw, "2026-11-06", _manifest(
        "session_evening", "2026-11-06T22:00:00+00:00", "2026-11-06T23:00:00+00:00", 5))
    _write_session(raw, "2026-11-06", _manifest(
        "session_live", "2026-11-06T23:30:00+00:00", None, 5))
    cache_path = tmp_path / "data" / "processed" / "session_volatility.json"

    first = update_cache(raw, cache_path)
    second = update_cache(raw, cache_path)

    assert (first.analysed, first.already_cached, first.no_first_hour, first.skipped_unfinished) == (2, 0, 1, 1)
    assert first.errors == ()
    assert (second.analysed, second.already_cached) == (0, 2)  # nothing is re-read
    samples = load_cache(cache_path).all_samples()
    assert [(s.session_id, s.ib_range) for s in samples] == [("session_cover", Decimal("20.00"))]


def test_update_cache_reports_a_damaged_session_and_keeps_going(tmp_path: Path) -> None:
    raw = tmp_path / "data" / "raw"
    broken = _write_session(raw, "2026-11-06", _manifest(
        "session_broken", "2026-11-05T23:00:00+00:00", "2026-11-06T21:00:00+00:00", 3))
    folder = broken / "trade_parts"
    folder.mkdir()
    (folder / "part-000000.parquet").write_bytes(b"not parquet")
    cache_path = tmp_path / "data" / "processed" / "session_volatility.json"

    summary = update_cache(raw, cache_path)

    assert len(summary.errors) == 1 and "session_broken" in summary.errors[0]
    assert load_cache(cache_path).sessions == {}  # not cached, so the next run retries it


# --- the walk-forward regime -----------------------------------------------------------

START = date(2026, 1, 1)


def _table(values: list[int], config: VolatilityRegimeConfig | None = None) -> VolatilityRegimeTable:
    return VolatilityRegimeTable({START + timedelta(days=i): Decimal(v) for i, v in enumerate(values)}, config)


def _day(index: int) -> date:
    return START + timedelta(days=index)


def test_label_is_unknown_until_enough_history_exists() -> None:
    table = _table(list(range(1, 30)))  # 29 prior days, 30 are required

    unknown = table.label_for(_day(29))
    assert unknown.label == LABEL_UNKNOWN and unknown.typical_range is None
    assert "needs 30" in unknown.reason and unknown.prior_days == 29

    assert _table(list(range(1, 31))).label_for(_day(30)).label != LABEL_UNKNOWN


def test_rising_falling_and_flat_history_map_to_high_low_and_normal() -> None:
    rising = _table(list(range(1, 61))).label_for(_day(60))
    falling = _table(list(range(60, 0, -1))).label_for(_day(60))
    flat = _table([100] * 60).label_for(_day(60))

    assert (rising.label, falling.label, flat.label) == (LABEL_HIGH, LABEL_LOW, LABEL_NORMAL)
    assert rising.typical_range == Decimal("50.5")  # median of the last 20 prior days (41..60)
    assert flat.percentile == Decimal("50.0")       # ties rank in the middle, never at an extreme


def test_label_uses_only_strictly_earlier_days() -> None:
    """The core promise: nothing on or after the asked day can change its label."""
    base = list(range(1, 61))
    target = _day(60)

    with_future = _table(base + [999, 1, 999, 1, 500])  # extra days AFTER the target
    own_day_changed = _table(base + [1_000_000])         # the target day's own range is huge
    plain = _table(base)

    assert with_future.label_for(target) == plain.label_for(target)
    assert own_day_changed.label_for(target) == plain.label_for(target)


def test_stale_history_is_unknown_not_guessed() -> None:
    table = _table(list(range(1, 41)))  # last observed day is index 39

    assert table.label_for(_day(39 + 14)).label != LABEL_UNKNOWN
    stale = table.label_for(_day(39 + 15))
    assert stale.label == LABEL_UNKNOWN and "days old" in stale.reason


def test_thresholds_are_configurable_and_percentiles_not_points() -> None:
    values = list(range(1, 61))
    strict = VolatilityRegimeConfig(low_percentile=10, high_percentile=95)

    assert _table(values, strict).label_for(_day(60)).label == LABEL_NORMAL  # 83rd pct < 95
    # Scaling every range by 1000 (a different price level) cannot change the label.
    assert _table([v * 1000 for v in values]).label_for(_day(60)).label == LABEL_HIGH


def test_one_day_with_two_sessions_uses_the_fullest_recording() -> None:
    samples = [
        FirstHourSample("2026-11-06", "partial", Decimal("110"), Decimal("100"), 50),
        FirstHourSample("2026-11-06", "full", Decimal("150"), Decimal("100"), 5000),
    ]

    table = VolatilityRegimeTable.from_samples(samples)

    assert table.observed_days == 1
    assert table._ranges[date(2026, 11, 6)] == Decimal("50")


def test_a_session_inherits_the_label_of_the_trading_day_it_starts_in() -> None:
    table = _table(list(range(1, 91)))
    monday = date(2026, 1, 1) + timedelta(days=60)  # a day with enough prior history
    # 18:30 New York the evening before belongs to the NEXT trading day.
    evening_before = datetime.combine(monday - timedelta(days=1), datetime.min.time(), tzinfo=UTC).replace(hour=23, minute=30)

    label = label_for_session_start(table, evening_before.isoformat())

    assert label == table.label_for(monday)
    assert label_for_session_start(table, "garbage").label == LABEL_UNKNOWN
    assert label_for_session_start(table, None).label == LABEL_UNKNOWN


# --- per-label totals --------------------------------------------------------------------


def test_totals_sum_decimal_money_per_label_and_never_hide_an_empty_bucket() -> None:
    totals = totals_by_label([
        (LABEL_LOW, 2, "-10.50"),
        (LABEL_LOW, 1, "4.25"),
        (LABEL_HIGH, 0, ""),
        (LABEL_UNKNOWN, 5, "100.00"),
        ("mystery-label", 1, "1.00"),   # an unexpected label is counted as unknown
        (LABEL_UNKNOWN, 1, "not money"),  # malformed money is skipped, never guessed
    ])

    assert (totals[LABEL_LOW].sessions, totals[LABEL_LOW].paper_trades, totals[LABEL_LOW].net_pnl) == (2, 3, Decimal("-6.25"))
    assert totals[LABEL_HIGH].sessions == 1 and totals[LABEL_HIGH].net_pnl == Decimal("0")
    assert totals[LABEL_NORMAL].sessions == 0  # present although empty
    assert totals[LABEL_UNKNOWN].sessions == 3
    assert totals[LABEL_UNKNOWN].net_pnl == Decimal("101.00")
    assert list(totals) == [LABEL_LOW, LABEL_NORMAL, LABEL_HIGH, LABEL_UNKNOWN]


# --- the catalog reads the cache and totals every session ---------------------------------


def _source(tmp_path: Path, raw: Path, cache_path: Path, ledger: Path | None = None):
    from app.gui.snapshot_source import SnapshotSource

    return SnapshotSource(raw_root=raw, paper_ledger_path=ledger, volatility_cache_path=cache_path)


def test_catalog_labels_sessions_from_the_cache_and_totals_every_session(tmp_path: Path) -> None:
    """Rows are bounded, but each label's totals cover ALL sessions; fixtures never count."""
    raw = tmp_path / "data" / "raw"
    cache_path = tmp_path / "data" / "processed" / "session_volatility.json"
    cache = SessionVolatilityCache()
    for index in range(40):  # 40 earlier days whose first-hour range keeps rising
        day = date(2026, 6, 1) + timedelta(days=index)
        cache.put(f"hist{index}", "k", [
            FirstHourSample(day.isoformat(), f"hist{index}", Decimal(100 + 5 * index), Decimal(100), 1000),
        ])
    save_cache(cache_path, cache)
    _write_session(raw, "2026-07-20", _manifest(
        "session_recent", "2026-07-20T15:00:00+00:00", "2026-07-20T16:00:00+00:00", 5))
    _write_session(raw, "2026-12-31", _manifest(
        "session_stale", "2026-12-31T15:00:00+00:00", "2026-12-31T16:00:00+00:00", 5))
    ledger = tmp_path / "paper_trades.jsonl"
    ledger.write_text("\n".join(json.dumps(row) for row in [
        {"session_id": "session_recent", "net_pnl": "10.50"},
        {"session_id": "session_recent", "net_pnl": "-3.25"},
        {"session_id": "session_stale", "net_pnl": "-100.00"},
        {"session_id": "session_stale", "net_pnl": "999", "is_synthetic_fixture": True},
    ]), encoding="utf-8")

    rows, total, buckets = _source(tmp_path, raw, cache_path, ledger)._build_sessions(limit=1)

    assert total == 2 and len(rows) == 1  # only the newest is listed ...
    assert (rows[0].session_id, rows[0].volatility) == ("session_stale", "unknown")  # ... 5+ months after the history
    by_label = {bucket.label: bucket for bucket in buckets}
    assert list(by_label) == [LABEL_LOW, LABEL_NORMAL, LABEL_HIGH, LABEL_UNKNOWN]
    high, unknown, low = by_label[LABEL_HIGH], by_label[LABEL_UNKNOWN], by_label[LABEL_LOW]
    assert (high.sessions, high.paper_trades, high.net_pnl) == (1, 2, "7.25")  # ... yet the listed-out one is counted
    assert (unknown.sessions, unknown.paper_trades, unknown.net_pnl) == (1, 1, "-100.00")
    assert (low.sessions, low.paper_trades, low.net_pnl) == (0, 0, "")  # empty bucket: no invented P&L


def test_catalog_survives_a_damaged_or_missing_volatility_cache(tmp_path: Path) -> None:
    raw = tmp_path / "data" / "raw"
    _write_session(raw, "2026-07-20", _manifest(
        "session_a", "2026-07-20T15:00:00+00:00", "2026-07-20T16:00:00+00:00", 5))
    damaged = tmp_path / "damaged.json"
    damaged.write_text("{broken", encoding="utf-8")

    for cache_path in (damaged, tmp_path / "does_not_exist.json"):
        rows, total, buckets = _source(tmp_path, raw, cache_path)._build_sessions(limit=10)
        assert total == 1 and rows[0].volatility == LABEL_UNKNOWN
        assert {bucket.label: bucket.sessions for bucket in buckets}[LABEL_UNKNOWN] == 1


def test_a_failing_volatility_lookup_degrades_only_the_column_not_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The volatility labels are an add-on: if they break, every session must still be listed."""
    from app.research import session_volatility

    raw = tmp_path / "data" / "raw"
    _write_session(raw, "2026-07-20", _manifest(
        "session_a", "2026-07-20T15:00:00+00:00", "2026-07-20T16:00:00+00:00", 5))
    _write_session(raw, "2026-07-21", _manifest(
        "session_b", "2026-07-21T15:00:00+00:00", "2026-07-21T16:00:00+00:00", 5))

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("volatility lookup exploded")

    monkeypatch.setattr(session_volatility, "label_for_session_start", broken)

    rows, total, buckets = _source(tmp_path, raw, tmp_path / "cache.json")._build_sessions(limit=10)

    assert total == 2 and [row.session_id for row in rows] == ["session_b", "session_a"]
    assert all(row.volatility == LABEL_UNKNOWN for row in rows)  # a failed lookup is "unknown", never a guess
    assert {bucket.label: bucket.sessions for bucket in buckets} == {
        LABEL_LOW: 0, LABEL_NORMAL: 0, LABEL_HIGH: 0, LABEL_UNKNOWN: 2,
    }


def test_a_missing_volatility_module_blanks_the_column_but_keeps_the_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    raw = tmp_path / "data" / "raw"
    _write_session(raw, "2026-07-20", _manifest(
        "session_a", "2026-07-20T15:00:00+00:00", "2026-07-20T16:00:00+00:00", 5))
    monkeypatch.setitem(sys.modules, "app.research.session_volatility", None)  # makes the import fail

    rows, total, buckets = _source(tmp_path, raw, tmp_path / "cache.json")._build_sessions(limit=10)

    assert total == 1 and rows[0].session_id == "session_a"
    assert rows[0].volatility == ""  # not computed, so blank
    assert buckets == ()             # and no totals; the page then says they are unavailable


def test_a_failing_totals_step_drops_only_the_breakdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.research import session_volatility

    raw = tmp_path / "data" / "raw"
    _write_session(raw, "2026-07-20", _manifest(
        "session_a", "2026-07-20T15:00:00+00:00", "2026-07-20T16:00:00+00:00", 5))

    def broken(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("totals exploded")

    monkeypatch.setattr(session_volatility, "totals_by_label", broken)

    rows, total, buckets = _source(tmp_path, raw, tmp_path / "cache.json")._build_sessions(limit=10)

    assert total == 1 and rows[0].volatility == LABEL_UNKNOWN  # labels still fine
    assert buckets == ()                                         # only the breakdown is lost


def test_with_no_sessions_the_four_buckets_are_all_zero(tmp_path: Path) -> None:
    raw = tmp_path / "data" / "raw"
    raw.mkdir(parents=True)

    rows, total, buckets = _source(tmp_path, raw, tmp_path / "cache.json")._build_sessions(limit=10)

    assert (rows, total) == ((), 0)
    assert [(bucket.label, bucket.sessions, bucket.paper_trades, bucket.net_pnl) for bucket in buckets] == [
        (LABEL_LOW, 0, 0, ""), (LABEL_NORMAL, 0, 0, ""), (LABEL_HIGH, 0, 0, ""), (LABEL_UNKNOWN, 0, 0, ""),
    ]
