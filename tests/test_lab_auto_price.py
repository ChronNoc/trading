"""The Lab's automatic starting price must stay cheap on huge live sessions.

Regression: the helper used to replay the session on the GUI thread. Replay opens
every part file's footer to check the schema, and the newest session is usually the
one still recording (100k+ part files), so the window froze for minutes at startup
and the stall watchdog fired on every launch. The helper now reads only the first
few trade files, however large the session is.
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.gui.bidirectional_lab_screen import _auto_price_from_session
from app.labs.bidirectional.feed import first_trade_prices


def _trade_table(prices: list[str], first_timestamp_ns: int = 1) -> pa.Table:
    count = len(prices)
    return pa.table({
        "timestamp_ns": pa.array(range(first_timestamp_ns, first_timestamp_ns + count), type=pa.int64()),
        "price": pa.array(prices, type=pa.string()),
        "size": pa.array(["1"] * count, type=pa.string()),
        "aggressor_side": pa.array(["buy"] * count, type=pa.string()),
        "instrument": pa.array(["MNQ"] * count, type=pa.string()),
        "sequence_id": pa.array(range(count), type=pa.int64()),
        "receive_sequence": pa.array(range(count), type=pa.int64()),
    })


def _write_trade_parts(session: Path, parts: list[list[str]]) -> None:
    folder = session / "trade_parts"
    folder.mkdir(parents=True)
    timestamp_ns = 1
    for index, prices in enumerate(parts):
        pq.write_table(_trade_table(prices, timestamp_ns), folder / f"part-{index:06d}.parquet")
        timestamp_ns += len(prices)


def test_reads_the_earliest_trades_in_order_up_to_the_limit(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _write_trade_parts(session, [["100.00", "100.25"], ["100.50", "100.75"]])

    assert first_trade_prices(session, limit=3) == [
        Decimal("100.00"), Decimal("100.25"), Decimal("100.50"),
    ]


def test_reads_a_finalized_single_file_session(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    pq.write_table(_trade_table(["200.00", "200.25"]), session / "trades.parquet")

    assert first_trade_prices(session) == [Decimal("200.00"), Decimal("200.25")]


def test_opens_a_bounded_number_of_files_however_big_the_session_is(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bug: cost grew with the part-file count. It must be constant."""
    session = tmp_path / "session"
    _write_trade_parts(session, [["100.00"] for _ in range(200)])  # 200 tiny parts
    opened: list[object] = []
    real_parquet_file = pq.ParquetFile

    class CountingParquetFile(real_parquet_file):  # type: ignore[misc, valid-type]
        def __init__(self, source: object, **kwargs: object) -> None:
            opened.append(source)
            super().__init__(source, **kwargs)

    monkeypatch.setattr(pq, "ParquetFile", CountingParquetFile)

    prices = first_trade_prices(session, limit=600, max_files=4)

    assert len(prices) == 4
    assert len(opened) == 4


def test_never_touches_the_depth_stream(tmp_path: Path) -> None:
    """Corrupt depth parts would blow up any code that scanned them."""
    session = tmp_path / "session"
    _write_trade_parts(session, [["100.00", "100.25"]])
    depth = session / "depth_parts"
    depth.mkdir()
    for index in range(50):
        (depth / f"part-{index:06d}.parquet").write_bytes(b"not a parquet file")

    assert first_trade_prices(session) == [Decimal("100.00"), Decimal("100.25")]


def test_auto_price_is_the_median_snapped_to_the_tick(tmp_path: Path) -> None:
    session = tmp_path / "session"
    _write_trade_parts(session, [["100.00", "100.10", "100.40"]])
    # An unreadable depth stream must not matter: the old replay-based helper
    # opened every depth part to check its schema and failed (or, on a live
    # session, ran for minutes) - this one never looks at depth.
    depth = session / "depth_parts"
    depth.mkdir()
    (depth / "part-000000.parquet").write_bytes(b"not a parquet file")

    # Median 100.10 snaps to the nearest 0.25 tick: 100.00.
    assert _auto_price_from_session(session, Decimal("0.25")) == Decimal("100.00")


def test_auto_price_is_none_without_trades(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    assert _auto_price_from_session(empty, Decimal("0.25")) is None
    assert _auto_price_from_session(tmp_path / "missing", Decimal("0.25")) is None
