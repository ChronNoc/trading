"""The Databento importer: what it reads, what it refuses, and what it promises about the result.

Every test writes only into ``tmp_path``. The data comes from ``tests/databento_fixtures.py``,
which follows Databento's documented MBP-10 layout, and the imported sessions are replayed
through the project's own loader and ``MarketState``, so the checks do not trust the importer.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import zipfile
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from app.importers import databento as db
from app.importers.databento import (
    ImportCancelled,
    ImportOptions,
    ImportResult,
    collect_inputs,
    import_many,
    import_mbp_file,
    inspect_file,
    staging_root_for,
)
from app.market.state import MarketState
from app.research.replay_loader import stream_session_events_with_stats
from app.research.session_catalog import build_catalog
from tests.databento_fixtures import (
    BASE_TS,
    COL_ACTION,
    COL_PRICE,
    COL_SIDE,
    COL_TS_EVENT,
    DAY_NS,
    NS,
    TRADE_HEADER,
    Gen,
    day_rows,
    fp,
    mbp_header,
    pretty,
    set_flag,
    trade_indexes,
    without_symbol,
    write_compressed,
    write_csv,
)


def _options(tmp_path: Path, **kwargs) -> ImportOptions:
    return ImportOptions(out_root=tmp_path / "raw", **kwargs)


def _import(tmp_path: Path, rows: list[list[str]], name: str = "day.mbp-10.csv", **kwargs) -> list[ImportResult]:
    return import_mbp_file(write_csv(tmp_path / name, rows), _options(tmp_path, **kwargs))


def _only(results: list[ImportResult]) -> ImportResult:
    assert len(results) == 1, [r.headline for r in results]
    return results[0]


def _replay(session_dir: Path):
    events, stats = stream_session_events_with_stats(session_dir)
    state, out = MarketState(), []
    for event in events:
        state = state.update(event.payload)
        out.append((event.kind, event.timestamp_ns, state, event.payload))
    return out, stats


def _manifest(result: ImportResult) -> dict:
    assert result.session_dir is not None
    return json.loads((result.session_dir / "session_manifest.json").read_text(encoding="utf-8"))


# --- what a dropped file is -----------------------------------------------------------------


def test_a_databento_mbp_csv_is_recognised_with_its_book_depth_and_formats(tmp_path: Path) -> None:
    rows = Gen(seed=1).run(50)[0]
    plain = inspect_file(write_csv(tmp_path / "a.mbp-10.csv", rows))
    assert plain.kind == "mbp" and plain.importable
    assert plain.levels == 10 and not plain.pretty_ts and not plain.pretty_px
    assert plain.clock_column == "ts_recv" and plain.has_symbol and plain.has_instrument_id
    assert plain.day_hint == "2025-07-15"

    shown = inspect_file(write_csv(tmp_path / "b.mbp-10.csv", pretty(rows)))
    assert shown.pretty_ts and shown.pretty_px and shown.day_hint == "2025-07-15"

    assert inspect_file(write_compressed(tmp_path / "c.mbp-10.csv.zst", rows, "zstd")).compression == "zstd"
    assert inspect_file(write_compressed(tmp_path / "d.mbp-10.csv.gz", rows, "gzip")).compression == "gzip"


@pytest.mark.parametrize(
    ("name", "content", "kind", "fragment"),
    [
        ("x.dbn.zst", b"DBN\x01", "dbn", "choose CSV"),
        ("x.dbn", b"DBN\x01", "dbn", "choose CSV"),
        ("empty.csv", b"", "empty", "empty"),
        ("garbage.csv", b"a,b,c\n1,2,3\n", "unsupported", "not an MBP file"),
        ("notes.txt", b"hello", "unsupported", "Only Databento CSV"),
        ("headeronly.csv", (",".join(mbp_header()) + "\n").encode(), "mbp", "no rows"),
    ],
)
def test_files_that_cannot_be_imported_say_why_in_plain_words(
    tmp_path: Path, name: str, content: bytes, kind: str, fragment: str,
) -> None:
    path = tmp_path / name
    path.write_bytes(content)
    info = inspect_file(path)
    assert info.kind == kind and not info.importable
    assert fragment in info.problem


def test_a_trades_file_and_the_info_file_are_skipped_not_failed(tmp_path: Path) -> None:
    trades = tmp_path / "t.trades.csv"
    trades.write_text(",".join(TRADE_HEADER) + "\n", encoding="utf-8")
    info = inspect_file(trades)
    assert info.kind == "trades" and not info.importable and not info.problem
    assert "already contains every trade" in info.note
    meta = tmp_path / "metadata.json"
    meta.write_text("{}", encoding="utf-8")
    assert inspect_file(meta).kind == "metadata" and not inspect_file(meta).problem


def test_an_mbp_file_missing_needed_columns_names_them(tmp_path: Path) -> None:
    header = [name for name in mbp_header() if name not in ("action", "ask_sz_00")]
    path = tmp_path / "m.csv"
    path.write_text(",".join(header) + "\n" + ",".join("1" for _ in header) + "\n", encoding="utf-8")
    info = inspect_file(path)
    assert not info.importable and "action" in info.problem and "ask_sz_00" in info.problem


def test_a_damaged_compressed_file_gets_a_friendly_message(tmp_path: Path) -> None:
    path = tmp_path / "bad.mbp-10.csv.zst"
    path.write_bytes(b"this is not zstd data at all")
    info = inspect_file(path)
    assert not info.importable and "damaged or incomplete" in info.problem


# --- dropped folders and zips ---------------------------------------------------------------


def test_folders_and_zips_are_expanded_into_their_files(tmp_path: Path) -> None:
    rows = Gen(seed=2).run(50)[0]
    folder = tmp_path / "drop"
    (folder / "sub").mkdir(parents=True)
    write_csv(folder / "one.mbp-10.csv", rows)
    write_csv(folder / "sub" / "two.mbp-10.csv", rows)
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(write_csv(tmp_path / "inzip.mbp-10.csv", rows), "inzip.mbp-10.csv")
        bundle.writestr("metadata.json", "{}")
        bundle.writestr("readme.pdf", "ignored")
    work = tmp_path / "work"
    found = collect_inputs([folder, archive], work)
    names = sorted(item.path.name for item in found)
    assert names == ["inzip.mbp-10.csv", "metadata.json", "one.mbp-10.csv", "two.mbp-10.csv"]
    assert all(item.path.parent == work or work in item.path.parents for item in found if item.path.name in names[:2] and "inzip" in item.path.name)


def test_a_zip_member_that_climbs_out_of_the_folder_is_never_extracted(tmp_path: Path) -> None:
    rows = Gen(seed=2).run(30)[0]
    good = write_csv(tmp_path / "good.mbp-10.csv", rows)
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(good, "good.mbp-10.csv")
        bundle.writestr("../escaped.csv", "ts_recv\n1\n")
        bundle.writestr("..\\escaped2.csv", "ts_recv\n1\n")
    work = tmp_path / "work" / "inner"
    found = collect_inputs([archive], work)
    assert [item.path.name for item in found if item.info.importable] == ["good.mbp-10.csv"]
    assert not (tmp_path / "work" / "escaped.csv").exists()
    assert not (tmp_path / "escaped.csv").exists()
    assert not list(tmp_path.rglob("escaped2.csv")) or all(work in p.parents for p in tmp_path.rglob("escaped2.csv"))


def test_a_damaged_zip_is_reported_not_raised(tmp_path: Path) -> None:
    archive = tmp_path / "broken.zip"
    archive.write_bytes(b"PK\x03\x04 not really a zip")
    found = collect_inputs([archive], tmp_path / "work")
    assert len(found) == 1 and "damaged" in found[0].info.problem


# --- fidelity -------------------------------------------------------------------------------


def test_the_replayed_book_matches_the_files_own_top_ten_levels_exactly(tmp_path: Path) -> None:
    rows, _trades, expected = Gen(seed=7).run(1500)
    result = _only(_import(tmp_path, rows))
    assert result.status == "imported"
    out, stats = _replay(result.session_dir)
    last = {ts: state for kind, ts, state, _ in out if kind == "depth"}
    checked = 0
    for ts, (bids, asks) in expected.items():
        state = last.get(ts)
        if state is None:
            continue
        checked += 1
        assert [(lv.price, lv.size) for lv in state.bid_depth[:10]] == bids
        assert [(lv.price, lv.size) for lv in state.ask_depth[:10]] == asks
    assert checked > 1000
    assert stats.continuity_ok and stats.ordering_mode == "receive_sequence"


def test_every_trade_in_the_file_is_kept_with_its_price_size_and_aggressor(tmp_path: Path) -> None:
    rows, trade_rows, _ = Gen(seed=8).run(1500)
    result = _only(_import(tmp_path, rows))
    table = pq.read_table(result.session_dir / "trades.parquet").to_pylist()
    assert len(table) == len(trade_rows) > 100
    assert [Decimal(t["price"]) for t in table] == [Decimal(r[COL_PRICE]) / NS for r in trade_rows]
    assert [t["size"] for t in table] == [r[9] for r in trade_rows]
    assert [t["aggressor_side"] for t in table] == ["buy" if r[COL_SIDE] == "B" else "sell" for r in trade_rows]
    assert [t["sequence_id"] for t in table] == list(range(1, len(table) + 1))
    assert {t["instrument"] for t in table} == {"MNQU5"}


def test_prices_are_written_like_the_live_recorder_with_two_decimals(tmp_path: Path) -> None:
    result = _only(_import(tmp_path, Gen(seed=9).run(300)[0]))
    for name, column in (("depth.parquet", "price"), ("trades.parquet", "price")):
        prices = pq.read_table(result.session_dir / name).column(column).to_pylist()
        assert prices and all(re.fullmatch(r"\d+\.\d{2}", price) for price in prices)


def test_the_clock_is_ts_recv_so_a_backwards_ts_event_never_reorders_the_session(tmp_path: Path) -> None:
    rows, _, _ = Gen(seed=11).run(2000, "regress")
    event_times = [int(row[COL_TS_EVENT]) for row in rows]
    assert sum(b < a for a, b in zip(event_times, event_times[1:])) > 0  # the fixture really does run backwards
    result = _only(_import(tmp_path, rows))
    assert result.stats["out_of_order"] == 0 and result.replay_ok
    _, stats = _replay(result.session_dir)
    assert stats.timestamp_regressions == 0 and stats.continuity_ok


def test_a_trade_never_prints_inside_the_spread_when_it_shares_a_timestamp_with_its_book_update(tmp_path: Path) -> None:
    rows, _, _ = Gen(seed=21).run(2000, "tie")
    result = _only(_import(tmp_path, rows))
    out, _ = _replay(result.session_dir)
    trades = [(state, payload) for kind, _ts, state, payload in out if kind == "trade"]
    assert len(trades) > 100
    for state, payload in trades:
        price = Decimal(str(payload["price"]))
        assert not (state.best_bid is not None and state.best_ask is not None and state.best_bid < price < state.best_ask)


def test_the_first_event_of_a_session_builds_the_book_before_a_trade_prints_against_it(tmp_path: Path) -> None:
    rows, _, _ = Gen(seed=3).run(600)
    rows = rows[trade_indexes(rows)[0]:]  # start the file on a trade record
    assert rows[0][COL_ACTION] == "T"
    result = _only(_import(tmp_path, rows))
    out, _ = _replay(result.session_dir)
    assert out[0][0] == "depth"
    assert any(kind == "trade" for kind, *_ in out)


def test_a_tiny_read_block_gives_exactly_the_same_session_as_one_big_block(tmp_path: Path) -> None:
    """The book is carried across blocks; if that carry were wrong, small blocks would differ."""
    rows = Gen(seed=12).run(2500)[0]
    big = _only(import_mbp_file(write_csv(tmp_path / "f.mbp-10.csv", rows), ImportOptions(out_root=tmp_path / "a" / "raw")))
    small = _only(import_mbp_file(
        tmp_path / "f.mbp-10.csv", ImportOptions(out_root=tmp_path / "b" / "raw", block_bytes=64 * 1024),
    ))
    for name in ("depth.parquet", "trades.parquet"):
        assert pq.read_table(big.session_dir / name).equals(pq.read_table(small.session_dir / name))


def test_every_file_format_databento_can_produce_gives_the_same_session(tmp_path: Path) -> None:
    rows = Gen(seed=13).run(800)[0]
    sources = {
        "plain": write_csv(tmp_path / "plain.mbp-10.csv", rows),
        "pretty": write_csv(tmp_path / "pretty.mbp-10.csv", pretty(rows)),
        "zstd": write_compressed(tmp_path / "z.mbp-10.csv.zst", rows, "zstd"),
        "gzip": write_compressed(tmp_path / "g.mbp-10.csv.gz", rows, "gzip"),
    }
    tables = {}
    for label, path in sources.items():
        result = _only(import_mbp_file(path, ImportOptions(out_root=tmp_path / label / "raw")))
        assert result.status == "imported", (label, result.headline)
        tables[label] = [pq.read_table(result.session_dir / name) for name in ("depth.parquet", "trades.parquet")]
    for label in ("pretty", "zstd", "gzip"):
        assert tables[label][0].equals(tables["plain"][0]), label
        assert tables[label][1].equals(tables["plain"][1]), label


def test_a_book_deeper_than_ten_levels_is_not_assumed_and_a_shallower_one_works(tmp_path: Path) -> None:
    rows = Gen(seed=14).run(300)[0]
    header = mbp_header()
    keep = [i for i, name in enumerate(header) if not re.search(r"_(0[1-9])$", name)]
    shallow_rows = [[row[i] for i in keep] for row in rows]
    shallow_header = [header[i] for i in keep]
    result = _only(import_mbp_file(
        write_csv(tmp_path / "mbp1.csv", shallow_rows, shallow_header), _options(tmp_path),
    ))
    assert result.status == "imported" and result.stats["book_levels"] == 1
    assert _manifest(result)["import_source"]["schema"] == "mbp-1"


# --- which contract, which days -------------------------------------------------------------


def _mixed_day(start: int = BASE_TS, front: int = 800, back: int = 300, seed: int = 31) -> list[list[str]]:
    rows = Gen(seed, 1, "MNQU5", "29500.00").run(front, "clean", start=start)[0]
    rows += Gen(seed + 1, 2, "MNQU5-MNQZ5", "12.00").run(front, "clean", start=start + 50_000)[0]
    rows += Gen(seed + 2, 3, "MNQZ5", "29600.00").run(back, "clean", start=start + 70_000)[0]
    rows.sort(key=lambda row: int(row[0]))
    return rows


def test_a_parent_symbol_file_imports_only_the_busiest_outright_contract(tmp_path: Path) -> None:
    result = _only(_import(tmp_path, _mixed_day()))
    assert result.status == "imported" and result.stats["symbol"] == "MNQU5"
    instrument = _manifest(result)["import_source"]["instrument"]
    left_out = {item["symbol"] for item in instrument["other_instruments_left_out"]}
    assert left_out == {"MNQU5-MNQZ5", "MNQZ5"}
    assert "spreads excluded" in instrument["chosen_because"]
    out, _ = _replay(result.session_dir)
    mids = [(s.best_bid + s.best_ask) / 2 for kind, _, s, _ in out if kind == "depth" and s.best_bid and s.best_ask]
    assert 29_400 < min(mids) and max(mids) < 29_600  # never the 12-point spread, never the other expiry


def test_the_contract_is_chosen_for_each_day_so_a_roll_is_followed(tmp_path: Path) -> None:
    rows = _mixed_day(BASE_TS, front=800, back=300) + _mixed_day(BASE_TS + DAY_NS, front=300, back=800, seed=41)
    rows.sort(key=lambda row: int(row[0]))
    results = _import(tmp_path, rows)
    assert [(r.day, r.stats["symbol"]) for r in results if r.status == "imported"] == [
        ("2025-07-15", "MNQU5"), ("2025-07-16", "MNQZ5"),
    ]


def test_an_instrument_can_be_forced_when_the_choice_is_wrong_for_you(tmp_path: Path) -> None:
    result = _only(_import(tmp_path, _mixed_day(), instrument_id=3))
    assert result.status == "imported" and result.stats["symbol"] == "MNQZ5"
    missing = _only(import_mbp_file(tmp_path / "day.mbp-10.csv", ImportOptions(out_root=tmp_path / "x" / "raw", instrument_id=99)))
    assert missing.status == "refused" and "Instrument 99" in missing.headline


def test_a_spread_a_different_product_and_a_file_without_trades_are_refused(tmp_path: Path) -> None:
    spread = _only(_import(tmp_path, Gen(5, 2, "MNQU5-MNQZ5", "12.00").run(300)[0], "spread.csv"))
    assert spread.status == "refused" and "calendar spread" in spread.headline

    other = _only(import_mbp_file(
        write_csv(tmp_path / "es.csv", Gen(6, 5, "ESU5", "6000.00").run(300)[0]),
        ImportOptions(out_root=tmp_path / "es" / "raw"),
    ))
    assert other.status == "refused" and "only works with MNQ" in other.headline

    rows = [row for row in Gen(7).run(300)[0] if row[COL_ACTION] != "T"]
    none = _only(import_mbp_file(write_csv(tmp_path / "nt.csv", rows), ImportOptions(out_root=tmp_path / "nt" / "raw")))
    assert none.status == "refused" and "no trades" in none.headline
    assert not (tmp_path / "raw").exists() and not (tmp_path / "es" / "raw").exists()


def test_without_a_symbol_column_the_prices_still_have_to_look_like_mnq(tmp_path: Path) -> None:
    rows, header = without_symbol(Gen(8).run(300)[0])
    ok = _only(import_mbp_file(write_csv(tmp_path / "ok.csv", rows, header), ImportOptions(out_root=tmp_path / "ok" / "raw")))
    assert ok.status == "imported" and ok.stats["symbol"] == "MNQ"
    rows, header = without_symbol(Gen(9, 2, "x", "12.00").run(300)[0])
    bad = _only(import_mbp_file(write_csv(tmp_path / "bad.csv", rows, header), ImportOptions(out_root=tmp_path / "bad" / "raw")))
    assert bad.status == "refused" and "do not look like MNQ" in bad.headline


# --- many days in one file ------------------------------------------------------------------


def test_a_file_of_several_days_becomes_one_session_per_utc_day(tmp_path: Path) -> None:
    results = _import(tmp_path, day_rows(3), "month.mbp-10.csv")
    assert [(r.status, r.day) for r in results] == [
        ("imported", "2025-07-15"), ("imported", "2025-07-16"), ("imported", "2025-07-17"),
    ]
    raw = tmp_path / "raw"
    assert sorted(p.parent.parent.name for p in raw.glob("*/*/session_manifest.json")) == [
        "2025-07-15", "2025-07-16", "2025-07-17",
    ]
    for result in results:  # each day is a complete, independent session
        out, stats = _replay(result.session_dir)
        assert out[0][0] == "depth" and out[0][3]["previous_size"] == "0"
        assert stats.continuity_ok


def test_a_chunk_that_crosses_midnight_is_split_without_losing_or_repeating_a_trade(tmp_path: Path) -> None:
    rows, trade_rows, _ = Gen(seed=77).run(2500, start=BASE_TS + DAY_NS - 1_500_000_000)
    results = _import(tmp_path, rows)
    assert [r.day for r in results] == ["2025-07-15", "2025-07-16"]
    assert sum(r.stats["trades"] for r in results) == len(trade_rows)
    second, _ = _replay(results[1].session_dir)
    assert second[0][0] == "depth" and second[0][3]["previous_size"] == "0"  # the new day starts from a fresh book


# --- what is measured and what the gate decides ---------------------------------------------


def test_a_clean_import_is_usable_for_replay_but_not_for_training_until_you_opt_in(tmp_path: Path) -> None:
    rows = Gen(seed=15).run(1000)[0]
    result = _only(_import(tmp_path, rows))
    assert result.replay_ok is True and result.training_ok is False
    assert result.training_reasons == ("imported session: model training was not enabled for this import (opt-in)",)
    manifest = _manifest(result)
    assert manifest["valid_for_order_flow_replay"] is True
    assert manifest["valid_for_real_training"] is False
    assert manifest["valid_for_live_decisions"] is False

    opted = _only(import_mbp_file(
        tmp_path / "day.mbp-10.csv", ImportOptions(out_root=tmp_path / "opt" / "raw", allow_model_training=True),
    ))
    assert opted.replay_ok is True and opted.training_ok is True
    assert _manifest(opted)["valid_for_real_training"] is True


def test_training_can_be_allowed_later_for_a_day_already_imported(tmp_path: Path) -> None:
    from app.importers.databento import set_training_permission
    from tools.databento_training import main as training_cli

    result = _only(_import(tmp_path, Gen(seed=15).run(1000)[0]))
    raw = tmp_path / "raw"
    data_before = {p.name: p.read_bytes() for p in result.session_dir.glob("*.parquet")}
    manifest_path = result.session_dir / "session_manifest.json"

    shown = set_training_permission(raw, None)                    # report only: nothing written
    assert [(r.trainable, r.changed) for r in shown] == [(False, False)]
    assert _manifest(result)["import_source"]["allow_model_training"] is False

    assert training_cli(["--on", "--raw", str(raw)]) == 0
    after = _manifest(result)
    assert after["import_source"]["allow_model_training"] is True
    assert after["import_source"]["training_permission_changed"]["to"] is True
    assert after["valid_for_real_training"] is True
    assert [e.eligible_for_model_training for e in build_catalog(raw)] == [True]
    assert {p.name: p.read_bytes() for p in result.session_dir.glob("*.parquet")} == data_before  # data untouched

    again = set_training_permission(raw, True)
    assert [r.changed for r in again] == [False]                   # already on: no rewrite
    assert set_training_permission(raw, True, days=["2000-01-01"]) == []
    off = set_training_permission(raw, False)
    assert [(r.trainable, r.changed) for r in off] == [(False, True)]
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["valid_for_real_training"] is False


def test_allowing_training_later_never_overrides_a_failed_quality_check(tmp_path: Path) -> None:
    from app.importers.databento import set_training_permission

    rows = Gen(seed=19).run(600)[0]
    for index in (5, 6, 7):
        rows[index][mbp_header().index("bid_px_00")] = fp("29499.10")
    _only(_import(tmp_path, rows))
    results = set_training_permission(tmp_path / "raw", True)
    assert [r.trainable for r in results] == [False]
    assert any("quality problems" in reason for reason in results[0].reasons)


def test_the_manifest_records_what_was_measured_and_never_invents_a_live_bridge(tmp_path: Path) -> None:
    result = _only(_import(tmp_path, Gen(seed=16).run(1000)[0]))
    manifest = _manifest(result)
    assert "bridge_provenance" not in manifest
    source = manifest["import_source"]
    assert source["vendor"] == "databento" and source["importer"] == db.IMPORTER
    assert source["schema"] == "mbp-10" and source["clock"] == "ts_recv" and source["book_depth_levels"] == 10
    assert source["quality_verified"] is True and source["quality"]["problems"] == []
    assert source["trades_with_aggressor_side"] == manifest["event_counts"]["trades"] > 0
    assert source["trades_left_out_no_aggressor"] == 0
    assert "duplicate_stream_events" in manifest["data_quality"]["not_measured"]
    assert manifest["provenance"] == "REAL_REPLAY" and manifest["synthetic"] is False
    assert manifest["utc_start"].startswith("2025-07-15T00:") and manifest["receive_order"]["available"] is True


def test_the_project_catalog_sees_the_import_and_agrees_with_the_importer(tmp_path: Path) -> None:
    result = _only(_import(tmp_path, Gen(seed=17).run(800)[0]))
    [entry] = build_catalog(tmp_path / "raw")
    assert entry.session_id == result.session_id
    assert entry.eligible_for_analysis and entry.eligible_for_order_flow_replay
    assert entry.eligible_for_model_training is False
    assert entry.provenance == "REAL_REPLAY" and not entry.is_delayed and not entry.valid_for_live_decisions


def test_databentos_own_gap_flag_makes_the_session_unfit_for_replay_and_says_so(tmp_path: Path) -> None:
    rows = Gen(seed=18).run(1000)[0]
    set_flag(rows, [10, 20, 30], 4)  # F_MAYBE_BAD_BOOK
    result = _only(_import(tmp_path, rows))
    assert result.status == "imported" and result.replay_ok is False
    assert any("unrecoverable gap" in problem for problem in result.problems)
    manifest = _manifest(result)
    assert manifest["continuity_status"] == "gap" and manifest["dropped_message_count"] == 3
    assert manifest["data_quality"]["ok"] is False and manifest["valid_for_order_flow_replay"] is False
    assert result.training_ok is False


def test_prices_off_the_tick_grid_are_reported_and_block_replay_and_training(tmp_path: Path) -> None:
    rows = Gen(seed=19).run(600)[0]
    for index in (5, 6, 7):
        rows[index][mbp_header().index("bid_px_00")] = fp("29499.10")
    result = _only(_import(tmp_path, rows, allow_model_training=True))
    assert any("tick grid" in problem for problem in result.problems)
    assert result.replay_ok is False and result.training_ok is False
    assert "imported session: the importer found quality problems with this data" in result.replay_reasons


def test_a_trade_without_an_aggressor_is_left_out_and_blocks_training_even_after_opt_in(tmp_path: Path) -> None:
    rows = Gen(seed=20).run(5000)[0]
    first_trade = trade_indexes(rows)[0]
    rows[first_trade][COL_SIDE] = "N"
    result = _only(_import(tmp_path, rows, allow_model_training=True))
    assert result.stats["trades"] == len(trade_indexes(rows)) - 1
    assert any("no aggressor side" in note for note in result.notes)  # one in ~1400: a warning, not a problem
    assert result.problems == [] and result.replay_ok is True
    assert result.training_ok is False
    assert any("aggressor-side coverage incomplete" in reason for reason in result.training_reasons)


def test_many_trades_without_an_aggressor_make_the_session_a_problem(tmp_path: Path) -> None:
    rows = Gen(seed=20).run(1000)[0]
    for index in trade_indexes(rows)[:10]:
        rows[index][COL_SIDE] = "N"
    result = _only(_import(tmp_path, rows))
    assert any("would skew buy/sell volume" in problem for problem in result.problems)
    assert result.replay_ok is False


def test_the_source_file_is_only_ever_read(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "keep.mbp-10.csv", Gen(seed=21).run(400)[0])
    before = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns)
    _only(import_mbp_file(path, _options(tmp_path)))
    assert (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns) == before


# --- never twice, never half --------------------------------------------------------------


def test_importing_the_same_data_again_is_skipped_even_under_another_name(tmp_path: Path) -> None:
    rows = Gen(seed=22).run(600)[0]
    first = _only(_import(tmp_path, rows, "first.mbp-10.csv"))
    again = _only(_import(tmp_path, rows, "first.mbp-10.csv"))
    renamed = _only(_import(tmp_path, rows, "other-name.mbp-10.csv"))
    assert first.status == "imported"
    assert again.status == renamed.status == "duplicate"
    assert again.session_id == renamed.session_id == first.session_id
    # Recognised as the very same data, not merely as something that overlaps it.
    assert "already imported as" in again.headline and "already imported as" in renamed.headline
    assert len(list((tmp_path / "raw").glob("*/*/session_manifest.json"))) == 1


def test_a_partial_file_overlapping_an_imported_day_is_skipped_so_trades_are_not_counted_twice(tmp_path: Path) -> None:
    rows = Gen(seed=23).run(1500)[0]
    _only(_import(tmp_path, rows, "full.mbp-10.csv"))
    partial = _only(_import(tmp_path, rows[400:900], "part.mbp-10.csv"))
    assert partial.status == "duplicate" and "overlaps" in partial.headline
    assert len(list((tmp_path / "raw").glob("*/*/session_manifest.json"))) == 1


def test_two_different_stretches_of_one_day_become_two_sessions(tmp_path: Path) -> None:
    rows = Gen(seed=24).run(1500)[0]
    _only(_import(tmp_path, rows[:600], "am.mbp-10.csv"))
    second = _only(_import(tmp_path, rows[600:], "pm.mbp-10.csv"))
    assert second.status == "imported"
    assert len(list((tmp_path / "raw").glob("2025-07-15/*/session_manifest.json"))) == 2


def test_a_cancel_keeps_the_days_already_finished_and_throws_away_the_one_in_progress(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "three.mbp-10.csv", day_rows(3, rows_per_day=1500))
    options = _options(tmp_path, block_bytes=256 * 1024)
    seen = {"converting": 0}

    def progress(event: db.ProgressEvent) -> None:
        seen["converting"] += event.stage == "converting"

    results = import_mbp_file(path, options, progress=progress, cancel=lambda: seen["converting"] >= 5)
    assert [r.status for r in results][-1] == "cancelled"
    done = [r for r in results if r.status == "imported"]
    assert 1 <= len(done) < 3
    on_disk = sorted(p.parent.parent.name for p in (tmp_path / "raw").glob("*/*/session_manifest.json"))
    assert on_disk == [r.day for r in done]
    assert not staging_root_for(options).exists()


def test_a_crash_part_way_leaves_finished_days_and_nothing_half_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = write_csv(tmp_path / "three.mbp-10.csv", day_rows(3))
    options = _options(tmp_path)
    real_feed = db._DayConverter.feed  # noqa: SLF001
    calls = {"n": 0}

    def exploding(self, batch, raw_ts):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk full")
        return real_feed(self, batch, raw_ts)

    monkeypatch.setattr(db._DayConverter, "feed", exploding)  # noqa: SLF001
    results = import_mbp_file(path, options)
    assert [r.status for r in results] == ["imported", "failed"]
    assert "disk full" in results[-1].problems[0]
    assert sorted(p.parent.parent.name for p in (tmp_path / "raw").glob("*/*/session_manifest.json")) == ["2025-07-15"]
    assert not staging_root_for(options).exists()


def test_a_session_under_construction_is_never_visible_inside_the_recordings_folder(tmp_path: Path) -> None:
    path = write_csv(tmp_path / "day.mbp-10.csv", Gen(seed=25).run(2000)[0])
    options = _options(tmp_path, block_bytes=128 * 1024)
    raw = tmp_path / "raw"
    working_seen = {"yes": False}

    def progress(event: db.ProgressEvent) -> None:
        if event.stage == "converting":
            assert not list(raw.rglob("*.parquet")) if raw.exists() else True
            working_seen["yes"] |= bool(list(staging_root_for(options).rglob("depth.parquet")))

    _only(import_mbp_file(path, options, progress=progress))
    assert working_seen["yes"], "the working folder should hold the session while it is being built"
    assert staging_root_for(options).parent == tmp_path and staging_root_for(options).parent != raw


def test_working_folders_left_by_a_crashed_import_are_swept_but_a_fresh_one_is_left(tmp_path: Path) -> None:
    options = _options(tmp_path)
    root = staging_root_for(options)
    old, fresh = root / "old", root / "fresh"
    old.mkdir(parents=True)
    fresh.mkdir()
    long_ago = time.time() - 3 * 24 * 3600
    os.utime(old, (long_ago, long_ago))
    _only(import_mbp_file(write_csv(tmp_path / "d.mbp-10.csv", Gen(seed=26).run(300)[0]), options))
    assert not old.exists() and fresh.exists()


# --- dropping a mix --------------------------------------------------------------------------


def test_a_mixed_drop_imports_what_it_can_and_explains_the_rest(tmp_path: Path) -> None:
    rows = Gen(seed=27).run(400)[0]
    good = write_csv(tmp_path / "good.mbp-10.csv", rows)
    trades = tmp_path / "t.trades.csv"
    trades.write_text(",".join(TRADE_HEADER) + "\n", encoding="utf-8")
    meta = tmp_path / "metadata.json"
    meta.write_text("{}", encoding="utf-8")
    binary = tmp_path / "x.dbn.zst"
    binary.write_bytes(b"DBN")
    streamed: list[str] = []
    results = import_many(
        [good, trades, meta, binary, tmp_path / "nope.csv"], _options(tmp_path),
        on_result=lambda r: streamed.append(r.status),
    )
    by_status = {r.status for r in results}
    assert by_status == {"imported", "skipped", "refused"}
    assert streamed == [r.status for r in results]  # reported as they happen
    assert sum(r.status == "imported" for r in results) == 1
    assert any("was not found" in r.headline for r in results)
    assert any("choose CSV" in r.headline for r in results)
    assert not staging_root_for(_options(tmp_path)).exists()


def test_a_zip_of_several_months_files_imports_every_day_and_cleans_up(tmp_path: Path) -> None:
    archive = tmp_path / "databento.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.write(write_csv(tmp_path / "a.mbp-10.csv", day_rows(2)), "a.mbp-10.csv")
        bundle.writestr("metadata.json", "{}")
        bundle.writestr("symbology.json", "{}")
    results = import_many([archive], _options(tmp_path))
    assert sum(r.status == "imported" for r in results) == 2
    assert sum(r.status == "skipped" for r in results) == 2
    assert not staging_root_for(_options(tmp_path)).exists()


def test_a_dropped_folder_is_imported_in_date_order_with_file_progress(tmp_path: Path) -> None:
    folder = tmp_path / "downloads"
    folder.mkdir()
    write_csv(folder / "z-later.mbp-10.csv", Gen(seed=28).run(300, start=BASE_TS + DAY_NS)[0])
    write_csv(folder / "a-earlier.mbp-10.csv", Gen(seed=29).run(300, start=BASE_TS)[0])
    events: list[db.ProgressEvent] = []
    results = import_many([folder], _options(tmp_path), progress=events.append)
    assert [r.day for r in results if r.status == "imported"] == ["2025-07-15", "2025-07-16"]
    assert {(e.file_index, e.file_count) for e in events if e.stage == "converting"} == {(1, 2), (2, 2)}


def test_dropping_nothing_says_so(tmp_path: Path) -> None:
    [result] = import_many([], _options(tmp_path))
    assert result.status == "refused" and "no files were dropped" in result.headline
    empty = tmp_path / "emptyfolder"
    empty.mkdir()
    [result] = import_many([empty], _options(tmp_path))
    assert result.status == "refused" and "no files were found" in result.headline


def test_the_importer_has_no_network_and_no_order_code_in_it() -> None:
    source = Path("app/importers/databento.py").read_text(encoding="utf-8")
    imports = set(re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, flags=re.MULTILINE))
    forbidden = {"socket", "requests", "urllib", "http", "httpx", "aiohttp", "ftplib", "databento"}
    assert not {name.split(".")[0] for name in imports} & forbidden
    assert not any("execution" in name for name in imports)


def test_the_cancel_exception_is_the_one_that_stops_a_scan() -> None:
    assert issubclass(ImportCancelled, Exception)
