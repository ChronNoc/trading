"""Realistic Databento ``mbp-10`` CSV data for the importer tests.

The column layout follows Databento's MBP-10 documentation. The underlying book keeps 30
levels per side and each row shows the top 10, exactly like a real record. Trade records
(``T``) leave the book unchanged and are followed by the book-changing record (``C``/``M``),
as the documentation describes. Prices are Databento fixed-point integers (1e-9) and an
empty level is the INT64_MAX sentinel.

Nothing here touches the network or the user's data; every test writes into ``tmp_path``.
"""

from __future__ import annotations

import csv
import gzip
import random
import shutil
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

TICK = Decimal("0.25")
NS = 1_000_000_000
DAY_NS = 86_400 * NS
UNDEF = "9223372036854775807"
BASE_TS = 1_752_537_600_000_000_000  # 2025-07-15 00:00:00 UTC

COL_TS_RECV, COL_TS_EVENT, COL_ACTION, COL_SIDE, COL_PRICE, COL_FLAGS = 0, 1, 5, 6, 8, 10


def fp(price: object) -> str:
    """A price as Databento's fixed-point integer string."""
    return str(int(Decimal(str(price)) * NS))


def mbp_header() -> list[str]:
    header = [
        "ts_recv", "ts_event", "rtype", "publisher_id", "instrument_id", "action", "side",
        "depth", "price", "size", "flags", "ts_in_delta", "sequence",
    ]
    for level in range(10):
        header += [
            f"bid_px_{level:02d}", f"ask_px_{level:02d}", f"bid_sz_{level:02d}", f"ask_sz_{level:02d}",
            f"bid_ct_{level:02d}", f"ask_ct_{level:02d}",
        ]
    header.append("symbol")
    return header


TRADE_HEADER = [
    "ts_recv", "ts_event", "rtype", "publisher_id", "instrument_id", "action", "side",
    "depth", "price", "size", "flags", "ts_in_delta", "sequence", "symbol",
]


class Gen:
    """One instrument's feed. ``run`` returns (mbp rows, trade rows, expected top-10 books)."""

    def __init__(self, seed: int = 1, instrument_id: int = 1, symbol: str = "MNQU5", best_bid: str = "29500.00") -> None:
        self.r = random.Random(seed)
        self.iid, self.sym = instrument_id, symbol
        first = Decimal(best_bid)
        self.bids = [[first - TICK * i, self.r.randint(1, 60)] for i in range(30)]
        self.asks = [[first + TICK * (i + 1), self.r.randint(1, 60)] for i in range(30)]
        self.seq = 0

    def top10(self) -> tuple[list[tuple[Decimal, Decimal]], list[tuple[Decimal, Decimal]]]:
        return (
            [(price, Decimal(size)) for price, size in self.bids[:10]],
            [(price, Decimal(size)) for price, size in self.asks[:10]],
        )

    def _row(self, ts_recv: int, ts_event: int, action: str, side: str, price: Decimal, size: int, depth: int = 0) -> list[str]:
        self.seq += 1
        cells = [
            str(ts_recv), str(ts_event), "10", "1", str(self.iid), action, side, str(depth),
            fp(price), str(size), "130", "50000", str(self.seq),
        ]
        for level in range(10):
            (bid_price, bid_size), (ask_price, ask_size) = self.bids[level], self.asks[level]
            cells += [fp(bid_price), fp(ask_price), str(bid_size), str(ask_size), "1", "1"]
        cells.append(self.sym)
        return cells

    def _trade_row(self, ts_recv: int, ts_event: int, side: str, price: Decimal, size: int) -> list[str]:
        return [
            str(ts_recv), str(ts_event), "0", "1", str(self.iid), "T", side, "0", fp(price),
            str(size), "130", "50000", str(self.seq), self.sym,
        ]

    def run(self, n: int, mode: str = "clean", start: int = BASE_TS):
        """mode: ``clean`` (monotonic ts_event) | ``regress`` (ts_event runs backwards like the
        real feed; ts_recv never does) | ``tie`` (a trade and its book update share one ts_event,
        like one exchange packet)."""
        r = self.r
        ts_recv = start
        depth_rows: list[list[str]] = []
        trade_rows: list[list[str]] = []
        expected: dict[int, tuple] = {}
        for _ in range(n):
            ts_recv += r.randint(100_000, 2_000_000)
            latency = 100_000
            if mode == "regress":
                latency = r.randint(50_000, 400_000)
                if r.random() < 0.03:
                    latency += 900_000
            ts_event = ts_recv - latency
            x = r.random()
            if x < 0.40:
                side = r.choice("BA")
                book = self.bids if side == "B" else self.asks
                level = r.randrange(10)
                book[level][1] = max(1, book[level][1] + r.randint(-10, 15))
                depth_rows.append(self._row(ts_recv, ts_event, "M", side, book[level][0], book[level][1], level))
                expected[ts_recv] = self.top10()
            elif x < 0.55:
                side = r.choice("BA")
                if side == "B":
                    new_price = self.bids[0][0] + TICK
                    if new_price >= self.asks[0][0]:
                        continue
                    self.bids.insert(0, [new_price, r.randint(1, 40)])
                    self.bids.pop()
                else:
                    new_price = self.asks[0][0] - TICK
                    if new_price <= self.bids[0][0]:
                        continue
                    self.asks.insert(0, [new_price, r.randint(1, 40)])
                    self.asks.pop()
                depth_rows.append(self._row(ts_recv, ts_event, "A", side, new_price, 5, 0))
                expected[ts_recv] = self.top10()
            elif x < 0.70:
                side = r.choice("BA")
                book = self.bids if side == "B" else self.asks
                gone = book.pop(0)
                step = -TICK if side == "B" else TICK
                book.append([book[-1][0] + step, r.randint(1, 60)])
                depth_rows.append(self._row(ts_recv, ts_event, "C", side, gone[0], gone[1], 0))
                expected[ts_recv] = self.top10()
            else:
                aggressor = r.choice("BA")  # B = buy aggressor, lifts the ask
                book = self.asks if aggressor == "B" else self.bids
                resting_side = "A" if aggressor == "B" else "B"
                level_price, level_size = book[0]
                quantity = min(r.randint(1, level_size + 20), level_size)
                depth_rows.append(self._row(ts_recv, ts_event, "T", aggressor, level_price, quantity))
                trade_rows.append(self._trade_row(ts_recv, ts_event, aggressor, level_price, quantity))
                ts_recv += 1_000
                event_two = ts_event if mode == "tie" else ts_event + 1_000
                if quantity >= level_size:
                    book.pop(0)
                    step = TICK if aggressor == "B" else -TICK
                    book.append([book[-1][0] + step, r.randint(1, 60)])
                    action = "C"
                else:
                    book[0][1] -= quantity
                    action = "M"
                depth_rows.append(self._row(ts_recv, event_two, action, resting_side, level_price, quantity, 0))
                expected[ts_recv] = self.top10()
        return depth_rows, trade_rows, expected


# --- writing and reshaping ----------------------------------------------------------------


def write_csv(path: Path, rows: list[list[str]], header: list[str] | None = None) -> Path:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header or mbp_header())
        writer.writerows(rows)
    return path


def write_compressed(path: Path, rows: list[list[str]], codec: str, header: list[str] | None = None) -> Path:
    """Write the same CSV through ``zstd`` (Databento's default) or ``gzip``."""
    plain = write_csv(path.with_suffix(".plain.tmp"), rows, header)
    if codec == "gzip":
        with plain.open("rb") as source, gzip.open(path, "wb") as sink:
            shutil.copyfileobj(source, sink)
    else:
        import pyarrow as pa

        with plain.open("rb") as source, pa.CompressedOutputStream(str(path), "zstd") as sink:
            shutil.copyfileobj(source, sink)
    plain.unlink()
    return path


def pretty(rows: list[list[str]], header: list[str] | None = None) -> list[list[str]]:
    """The same rows as ``pretty_ts``/``pretty_px`` CSV: ISO timestamps and decimal prices."""
    names = header or mbp_header()
    price_columns = [i for i, name in enumerate(names) if name == "price" or "_px_" in name]

    def iso(ns: str) -> str:
        seconds, nanos = divmod(int(ns), NS)  # integer maths: a float would lose nanoseconds
        moment = datetime.fromtimestamp(seconds, tz=UTC)
        return moment.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"

    out = []
    for row in rows:
        cells = list(row)
        cells[COL_TS_RECV], cells[COL_TS_EVENT] = iso(cells[COL_TS_RECV]), iso(cells[COL_TS_EVENT])
        for index in price_columns:
            cells[index] = "" if cells[index] == UNDEF else format(Decimal(cells[index]) / Decimal(NS), "f")
        out.append(cells)
    return out


def without_symbol(rows: list[list[str]]) -> tuple[list[list[str]], list[str]]:
    """Rows and header with the trailing ``symbol`` column removed."""
    return [row[:-1] for row in rows], mbp_header()[:-1]


def set_flag(rows: list[list[str]], indexes: list[int], flag: int) -> None:
    for index in indexes:
        rows[index][COL_FLAGS] = str(int(rows[index][COL_FLAGS]) | flag)


def trade_indexes(rows: list[list[str]]) -> list[int]:
    return [i for i, row in enumerate(rows) if row[COL_ACTION] == "T"]


def day_rows(days: int, rows_per_day: int = 300, seed: int = 100, symbol: str = "MNQU5") -> list[list[str]]:
    """``days`` consecutive UTC days of one contract in one list, sorted by ts_recv."""
    rows: list[list[str]] = []
    for k in range(days):
        rows += Gen(seed=seed + k, symbol=symbol).run(rows_per_day, "clean", start=BASE_TS + k * DAY_NS)[0]
    rows.sort(key=lambda row: int(row[COL_TS_RECV]))
    return rows
