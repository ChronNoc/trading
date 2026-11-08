"""Import Databento MBP-10 CSV exports as replay sessions (offline; the source is only read).

A Databento ``mbp-10`` export (``mbp-1`` works too) becomes the folders the project already
replays: ``depth.parquet`` (per-price-level changes), ``trades.parquet`` (executions with
their aggressor side) and ``session_manifest.json``. One file may hold one day or a whole
month: **every UTC calendar day becomes its own session**, the same split Databento itself
uses for ``split_duration=day``, and the contract is chosen per day.

What it guarantees (each point has a test in ``tests/test_databento_import.py``):

* Trades come from the file's own ``T`` records, so a trade keeps its true place in the
  feed. MBP-10 already contains every trade, so a separate trades download is not needed.
* The clock is ``ts_recv``. Databento sorts records by it and calls ``ts_event`` possibly
  non-monotonic. Timestamps are kept non-decreasing and every regression is counted.
* One instrument per session. A parent symbol such as ``MNQ.FUT`` returns every expiry and
  the spreads in one file; the busiest outright contract of each day is chosen, the rest are
  dropped and the choice is recorded. Spreads and non-MNQ data are refused.
* The manifest records what was MEASURED (ordering, Databento's own gap flags, prices off
  the tick grid, trades without an aggressor); the project's own classifier decides
  eligibility from it. Nothing is asserted just to pass a gate.
* A session is built in a private folder outside ``data/raw`` and moved into place whole,
  manifest included, so a half-finished import is never visible to the app.
* Importing the same data twice is detected and skipped.
* No network, no orders, no live-trading code (see AGENTS.md).

Speed: the file is streamed through pyarrow and the book is diffed with numpy, so memory
stays flat and a multi-million-row day takes minutes, not hours.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
import shutil
import time
import uuid
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import numpy as np

from app.research.session_catalog import classify_manifest

IMPORTER = "app/importers/databento.py"
VENDOR = "databento"
SESSION_SUFFIX = "__databento"
UNDEF_PRICE = 9_223_372_036_854_775_807  # Databento's "no price" sentinel (INT64_MAX)
FLAG_MAYBE_BAD_BOOK = 4   # "an unrecoverable gap was detected in the channel"
FLAG_BAD_TS_RECV = 8      # "ts_recv is inaccurate due to clock issues or reordering"
FLAG_SNAPSHOT = 32        # record sourced from a snapshot/replay
NS = 1_000_000_000
DAY_NS = 86_400 * NS
_DIFF_BLOCK_ROWS = 20_000  # bounds the temporary (rows x levels x levels) arrays
_SUPPORTED_SUFFIXES = (".csv", ".csv.gz", ".csv.zst")
_PRICE_RANGE = (3_000.0, 200_000.0)  # sanity range for MNQ index-point prices
_MIN_PRICE_SHARE = 0.2   # an instrument trading below this share of the top price is a spread
_STALE_STAGING_SECONDS = 24 * 3600
_NOT_NEEDED_TRADES = "A separate trades file is not needed: the order-book (MBP) file already contains every trade."


# --- public data types ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ImportOptions:
    """How to import. Defaults are the safe ones: replay and analysis only, no ML training."""

    out_root: Path = Path("data/raw")
    symbol_family: str = "MNQ"
    tick_size: Decimal = Decimal("0.25")
    allow_model_training: bool = False
    instrument_id: int | None = None
    block_bytes: int = 8 * 1024 * 1024  # measured fastest: 8 MB blocks beat 1, 2, 4, 16, 32 and 64 MB


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Where an import is. ``total`` is 0 while it is not known (the scan)."""

    stage: str  # checking | scanning | converting | finishing
    done: int = 0
    total: int = 0
    message: str = ""
    file: str = ""
    file_index: int = 0
    file_count: int = 0


class ImportCancelled(Exception):
    """Raised inside an import when the caller asks it to stop."""


@dataclass(frozen=True, slots=True)
class FileInfo:
    """What a dropped file is, learned from its first block only (cheap)."""

    path: Path
    kind: str  # mbp | trades | dbn | metadata | unsupported | empty
    levels: int = 0
    compression: str = "none"
    pretty_ts: bool = False
    pretty_px: bool = False
    columns: tuple[str, ...] = ()
    size_bytes: int = 0
    day_hint: str = ""  # YYYY-MM-DD of the first row, when readable
    problem: str = ""   # a plain-language reason the file cannot be imported
    note: str = ""      # a plain-language reason the file is harmlessly skipped

    @property
    def importable(self) -> bool:
        return self.kind == "mbp" and not self.problem

    @property
    def clock_column(self) -> str:
        return "ts_recv" if "ts_recv" in self.columns else "ts_event"

    @property
    def has_symbol(self) -> bool:
        return "symbol" in self.columns

    @property
    def has_instrument_id(self) -> bool:
        return "instrument_id" in self.columns


@dataclass(slots=True)
class ImportResult:
    """The outcome for one session (or one file that produced none), written for a person."""

    status: str  # imported | duplicate | refused | skipped | failed | cancelled
    source: Path
    headline: str
    day: str = ""
    session_id: str = ""
    session_dir: Path | None = None
    notes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    stats: dict[str, object] = field(default_factory=dict)
    replay_ok: bool | None = None
    training_ok: bool | None = None
    replay_reasons: tuple[str, ...] = ()
    training_reasons: tuple[str, ...] = ()
    seconds: float = 0.0


# --- looking at a file ------------------------------------------------------------------


def inspect_file(path: Path) -> FileInfo:
    """Classify a file from its first block. Never reads the whole file."""
    name = path.name.lower()
    try:
        size = path.stat().st_size
    except OSError as error:
        return FileInfo(path, "unsupported", problem=f"The file cannot be read ({error.strerror or error}).")
    if name.endswith((".dbn", ".dbn.zst")):
        return FileInfo(
            path, "dbn", size_bytes=size,
            problem=(
                "This is Databento's binary format (DBN). Download it again and choose CSV as the "
                "encoding; zstd compression is fine."
            ),
        )
    if name.endswith(".json"):
        return FileInfo(path, "metadata", size_bytes=size, note="Databento's information file; there is nothing to import.")
    if not name.endswith(_SUPPORTED_SUFFIXES):
        return FileInfo(
            path, "unsupported", size_bytes=size,
            problem="Only Databento CSV files are supported (.csv, .csv.zst, .csv.gz) or a .zip of them.",
        )
    compression = "zstd" if name.endswith(".zst") else "gzip" if name.endswith(".gz") else "none"
    try:
        head = _read_head(path)
    except Exception as error:  # noqa: BLE001 - a damaged archive must give a friendly answer
        return FileInfo(
            path, "unsupported", compression=compression, size_bytes=size,
            problem=f"The file could not be opened; it may be damaged or incomplete ({type(error).__name__}).",
        )
    lines = [line for line in head.replace("\r\n", "\n").split("\n") if line.strip()]
    if not lines:
        return FileInfo(path, "empty", compression=compression, size_bytes=size, problem="The file is empty.")
    header = tuple(column.strip().lstrip("﻿") for column in lines[0].split(","))
    if "bid_px_00" in header:
        kind = "mbp"
    elif {"price", "size", "side"} <= set(header) and "ts_event" in header:
        return FileInfo(path, "trades", compression=compression, columns=header, size_bytes=size, note=_NOT_NEEDED_TRADES)
    else:
        return FileInfo(
            path, "unsupported", compression=compression, columns=header, size_bytes=size,
            problem="This is not an MBP file. Download the mbp-10 schema (order book updates with trades).",
        )
    levels = len([column for column in header if re.fullmatch(r"bid_px_\d\d", column)])
    required = ("ask_px_00", "bid_sz_00", "ask_sz_00", "action", "side", "price", "size")
    missing = [column for column in required if column not in header]
    if "ts_recv" not in header and "ts_event" not in header:
        missing.append("ts_recv")
    if missing:
        return FileInfo(
            path, kind, levels=levels, compression=compression, columns=header, size_bytes=size,
            problem="The file is missing columns this importer needs: " + ", ".join(missing) + ".",
        )
    if len(lines) < 2:
        return FileInfo(
            path, kind, levels=levels, compression=compression, columns=header, size_bytes=size,
            problem="The file has column names but no rows.",
        )
    rows = list(csv.DictReader(lines[:200]))
    clock = "ts_recv" if "ts_recv" in header else "ts_event"
    first = str(rows[0].get(clock, "")).strip()
    pretty_ts = not first.isdigit()
    pretty_px = any(
        "." in str(row.get(column) or "")
        for row in rows
        for column in ("price", "bid_px_00", "ask_px_00")
    )
    return FileInfo(
        path, kind, levels=levels, compression=compression, pretty_ts=pretty_ts, pretty_px=pretty_px,
        columns=header, size_bytes=size, day_hint=_day_of(first, pretty_ts),
    )


def _read_head(path: Path, nbytes: int = 1_048_576) -> str:
    import pyarrow as pa

    with pa.input_stream(str(path), compression="detect") as stream:
        return stream.read(nbytes).decode("utf-8", errors="replace")


def _day_of(value: str, pretty: bool) -> str:
    try:
        if pretty:
            return value[:10] if re.match(r"\d{4}-\d{2}-\d{2}", value) else ""
        return datetime.fromtimestamp(int(value) / NS, tz=UTC).date().isoformat()
    except (ValueError, OverflowError, OSError):
        return ""


def file_fingerprint(path: Path) -> str:
    """A fast identity for a file: its size plus its first and last megabyte."""
    size = path.stat().st_size
    digest = hashlib.sha256(str(size).encode())
    with path.open("rb") as handle:
        digest.update(handle.read(1 << 20))
        if size > (2 << 20):
            handle.seek(size - (1 << 20))
            digest.update(handle.read(1 << 20))
    return digest.hexdigest()


# --- dropped files, folders and zips -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InputFile:
    """One real file on disk plus what it is and where it came from."""

    path: Path
    origin: str
    info: FileInfo


def collect_inputs(paths: Iterable[Path], workdir: Path) -> list[InputFile]:
    """Expand dropped files, folders and .zip archives into inspected files.

    Zip members are extracted into ``workdir`` (a folder the caller removes afterwards) and
    only when their names are safe: nothing can land outside it.
    """
    found: list[InputFile] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            for root, _dirs, files in os.walk(path):
                for name in sorted(files):
                    found.extend(_expand(Path(root) / name, name, workdir))
        elif path.is_file():
            found.extend(_expand(path, path.name, workdir))
    return found


def _expand(path: Path, origin: str, workdir: Path) -> list[InputFile]:
    if path.suffix.lower() == ".zip":
        return _extract_zip(path, workdir)
    return [InputFile(path, origin, inspect_file(path))]


def _extract_zip(archive: Path, workdir: Path) -> list[InputFile]:
    folder = workdir / f"{archive.stem}_{uuid.uuid4().hex[:8]}"
    results: list[InputFile] = []
    wanted = _SUPPORTED_SUFFIXES + (".json", ".dbn", ".dbn.zst")
    try:
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                if member.is_dir() or not member.filename.lower().endswith(wanted):
                    continue
                folder.mkdir(parents=True, exist_ok=True)
                target = (folder / member.filename).resolve()
                if not target.is_relative_to(folder.resolve()):
                    continue  # a name that climbs out of the folder is never extracted
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as sink:
                    shutil.copyfileobj(source, sink, 1 << 20)
                results.append(InputFile(target, f"{archive.name} / {Path(member.filename).name}", inspect_file(target)))
    except zipfile.BadZipFile:
        info = FileInfo(archive, "unsupported", problem="The .zip file is damaged or incomplete.")
        results.append(InputFile(archive, archive.name, info))
    return results


# --- reading ----------------------------------------------------------------------------


def _open_reader(info: FileInfo, columns: list[str], block_bytes: int):
    """Stream a CSV through pyarrow with explicit types for the columns we need."""
    import pyarrow as pa
    import pyarrow.csv as pacsv

    price_columns = {"price"} | {f"{side}_px_{level:02d}" for side in ("bid", "ask") for level in range(20)}
    types: dict[str, object] = {}
    for column in columns:
        if column in ("ts_recv", "ts_event"):
            types[column] = pa.timestamp("ns", tz="UTC") if info.pretty_ts else pa.int64()
        elif column in price_columns:
            types[column] = pa.float64() if info.pretty_px else pa.int64()
        elif column in ("instrument_id", "flags", "size") or "_sz_" in column:
            types[column] = pa.int64()
        else:
            types[column] = pa.string()
    stream = pa.input_stream(str(info.path), compression="detect")
    return pacsv.open_csv(
        stream,
        read_options=pacsv.ReadOptions(block_size=block_bytes),
        convert_options=pacsv.ConvertOptions(
            include_columns=columns, column_types=types,
            null_values=["", "null", "NULL", "nan", "NaN"], strings_can_be_null=True,
        ),
    )


def _ints(array, fill: int = 0) -> np.ndarray:
    import pyarrow.compute as pc

    return pc.fill_null(array, fill).to_numpy(zero_copy_only=False).astype(np.int64, copy=False)


def _flags_of(array) -> np.ndarray:
    import pyarrow.compute as pc

    return pc.fill_null(array, False).to_numpy(zero_copy_only=False).astype(bool, copy=False)


def _clock_ns(batch, info: FileInfo) -> np.ndarray:
    import pyarrow as pa

    column = batch.column(info.clock_column)
    if info.pretty_ts:
        column = column.cast(pa.int64())
    return _ints(column)


def _effective_days(raw_ts: np.ndarray, carry: int) -> tuple[np.ndarray, int]:
    """The UTC day of each row, never going backwards: a stray early timestamp stays in its day."""
    days = raw_ts // DAY_NS
    effective = np.maximum.accumulate(np.concatenate(([carry], days)))[1:]
    return effective, int(effective[-1]) if len(effective) else carry


def _prices(array, info: FileInfo) -> tuple[np.ndarray, np.ndarray]:
    """Raw prices and a validity mask (no sentinel, not empty, positive)."""
    import pyarrow.compute as pc

    if info.pretty_px:
        values = pc.fill_null(array, float("nan")).to_numpy(zero_copy_only=False)
        return values, ~np.isnan(values) & (values > 0)
    values = _ints(array, UNDEF_PRICE)
    return values, (values != UNDEF_PRICE) & (values > 0)


def _to_ticks(values: np.ndarray, valid: np.ndarray, info: FileInfo, tick: Decimal) -> tuple[np.ndarray, int]:
    """Prices to whole ticks (-1 where invalid), plus how many valid prices sit off the grid."""
    if info.pretty_px:
        scaled = np.where(valid, values, 0.0) / float(tick)
        rounded = np.rint(scaled)
        off = valid & (np.abs(scaled - rounded) > 1e-6)
        return np.where(valid, rounded, -1).astype(np.int64), int(off.sum())
    unit = int(tick * NS)
    safe = np.where(valid, values, 0)
    off = valid & (safe % unit != 0)
    return np.where(valid, (safe + unit // 2) // unit, -1).astype(np.int64), int(off.sum())


# --- the survey pass (which contract, how many rows, on which day) --------------------------


@dataclass(slots=True)
class _Group:
    """What one instrument did on one UTC day."""

    day: int
    instrument_id: int
    rows: int = 0
    trades: int = 0
    price_sum: float = 0.0
    price_n: int = 0
    first_ts: int | None = None
    last_ts: int = 0
    symbol: str = ""

    @property
    def mean_price(self) -> float | None:
        return self.price_sum / self.price_n if self.price_n else None


def _survey(
    info: FileInfo,
    options: ImportOptions,
    progress: Callable[[ProgressEvent], None] | None,
    cancel: Callable[[], bool] | None,
) -> tuple[dict[tuple[int, int], _Group], int]:
    """One cheap pass: rows, trades and prices per (day, instrument). Returns the groups and total rows."""
    import pyarrow.compute as pc

    columns = [info.clock_column, "action", "price"]
    columns += [column for column in ("instrument_id", "symbol") if column in info.columns]
    groups: dict[tuple[int, int], _Group] = {}
    total_rows = 0
    carry = -1
    for batch in _open_reader(info, columns, options.block_bytes):
        _check_cancel(cancel)
        n = batch.num_rows
        if n == 0:
            continue
        raw_ts = _clock_ns(batch, info)
        days, carry = _effective_days(raw_ts, carry)
        ids = _ints(batch.column("instrument_id"), 0) if info.has_instrument_id else np.zeros(n, np.int64)
        key = (days << 32) | (ids & 0xFFFFFFFF)
        unique, first_index, inverse, counts = np.unique(
            key, return_index=True, return_inverse=True, return_counts=True,
        )
        inverse = inverse.reshape(-1)
        last_index = np.zeros(len(unique), np.int64)
        np.maximum.at(last_index, inverse, np.arange(n))
        is_trade = _flags_of(pc.equal(batch.column("action"), "T"))
        raw, ok = _prices(batch.column("price"), info)
        points = (raw if info.pretty_px else raw / NS).astype(np.float64)
        priced = is_trade & ok
        trade_counts = np.bincount(inverse, weights=is_trade.astype(np.float64), minlength=len(unique))
        price_counts = np.bincount(inverse[priced], minlength=len(unique))
        price_sums = np.bincount(inverse[priced], weights=points[priced], minlength=len(unique))
        symbols = batch.column("symbol") if info.has_symbol else None
        for slot, packed in enumerate(unique.tolist()):
            day, instrument = packed >> 32, packed & 0xFFFFFFFF
            group = groups.setdefault((day, instrument), _Group(day, instrument))
            group.rows += int(counts[slot])
            group.trades += int(trade_counts[slot])
            group.price_n += int(price_counts[slot])
            group.price_sum += float(price_sums[slot])
            if group.first_ts is None:
                group.first_ts = int(raw_ts[first_index[slot]])
            group.last_ts = int(raw_ts[last_index[slot]])
            if symbols is not None and not group.symbol:
                group.symbol = str(symbols[int(first_index[slot])].as_py() or "")
        total_rows += n
        if progress is not None:
            progress(ProgressEvent("scanning", total_rows, 0, f"Scanning: {total_rows:,} rows so far"))
    return groups, total_rows


def _is_spread_symbol(symbol: str) -> bool:
    return bool(symbol) and any(mark in symbol for mark in ("-", " ", ":"))


def _choose_instrument(candidates: list[_Group], options: ImportOptions) -> tuple[_Group | None, str, list[_Group]]:
    """Pick the one contract to import for a day. Returns (chosen, why, the ones left out)."""
    everyone = sorted(candidates, key=lambda group: (-group.rows, group.instrument_id))
    if options.instrument_id is not None:
        chosen = next((group for group in everyone if group.instrument_id == options.instrument_id), None)
        if chosen is None:
            return None, f"instrument {options.instrument_id} is not in this file on this day", everyone
        return chosen, f"you asked for instrument {chosen.instrument_id}", [g for g in everyone if g is not chosen]
    if len(everyone) == 1:
        return everyone[0], "the only instrument that day", []
    outright = [group for group in everyone if not _is_spread_symbol(group.symbol)]
    prices = [group.mean_price for group in outright if group.mean_price]
    if prices:
        top = max(prices)
        outright = [group for group in outright if not group.mean_price or group.mean_price >= top * _MIN_PRICE_SHARE]
    outright = [group for group in outright if group.trades > 0] or outright
    if not outright:
        return None, "every instrument that day looks like a spread", everyone
    chosen = max(outright, key=lambda group: (group.rows, group.trades))
    total = sum(group.rows for group in everyone)
    why = (
        f"the busiest outright contract: {chosen.rows:,} of {total:,} rows "
        f"({len(everyone)} instruments that day, spreads excluded)"
    )
    return chosen, why, [group for group in everyone if group is not chosen]


def _refusal_for(chosen: _Group, options: ImportOptions) -> str:
    """A plain-language reason this instrument must not be imported, or an empty string."""
    family = options.symbol_family
    if chosen.symbol:
        if _is_spread_symbol(chosen.symbol):
            return f"{chosen.symbol} is a calendar spread, not a contract."
        if not chosen.symbol.upper().startswith(family.upper()):
            return f"It is for {chosen.symbol}, and this project only works with {family}."
    mean = chosen.mean_price
    if mean is not None and not (_PRICE_RANGE[0] <= mean <= _PRICE_RANGE[1]):
        return f"The prices (around {mean:,.2f}) do not look like {family}. Check that you downloaded the right instrument."
    if chosen.trades == 0:
        return "It contains no trades, and a session needs both book updates and trades."
    return ""


# --- the conversion pass --------------------------------------------------------------------


@dataclass(slots=True)
class _Counters:
    rows_read: int = 0
    rows_other_instruments: int = 0
    rows_used: int = 0
    depth_changes: int = 0
    trades: int = 0
    trades_no_aggressor: int = 0
    trades_unusable: int = 0
    out_of_order: int = 0
    bad_book_rows: int = 0
    bad_ts_rows: int = 0
    snapshot_rows: int = 0
    crossed_rows: int = 0
    misaligned_prices: int = 0
    records: int = 0
    first_raw: int | None = None
    last_raw: int = 0
    first_ts: int | None = None
    last_ts: int = 0


def _depth_schema():
    import pyarrow as pa

    return pa.schema([
        ("timestamp", pa.int64()), ("symbol", pa.string()), ("side", pa.string()), ("price", pa.string()),
        ("previous_size", pa.string()), ("new_size", pa.string()), ("receive_sequence", pa.int64()),
    ])


def _trade_schema():
    import pyarrow as pa

    return pa.schema([
        ("timestamp_ns", pa.int64()), ("price", pa.string()), ("size", pa.string()),
        ("aggressor_side", pa.string()), ("instrument", pa.string()), ("sequence_id", pa.int64()),
        ("receive_sequence", pa.int64()),
    ])


def _diff_side(
    cur_t: np.ndarray, cur_s: np.ndarray, prev_t: np.ndarray, prev_s: np.ndarray,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Per-price-level changes between each row's book side and the row before it.

    Inputs are (rows, levels) tick indices (-1 = no level) and sizes. Returns parallel arrays:
    source row, price in ticks, previous size, new size (0 = the level is gone).

    Most updates only change a size at a level that keeps its price; those rows take a cheap
    direct comparison. Only rows where the price ladder moved (a level was added or removed)
    use the full all-levels match, in blocks so the temporary arrays stay small.
    """
    empty = np.zeros(0, np.int64)
    if cur_t.shape[0] == 0:
        return empty, empty, empty, empty
    rows_out: list[np.ndarray] = []
    price_out: list[np.ndarray] = []
    prev_out: list[np.ndarray] = []
    new_out: list[np.ndarray] = []

    same_ladder = (cur_t == prev_t).all(axis=1)
    fast = np.nonzero(same_ladder)[0]
    if len(fast):
        ct, cs, ps = cur_t[fast], cur_s[fast], prev_s[fast]
        row, level = np.nonzero((ct >= 0) & (cs != ps))
        rows_out.append(fast[row])
        price_out.append(ct[row, level])
        prev_out.append(ps[row, level])
        new_out.append(cs[row, level])

    slow = np.nonzero(~same_ladder)[0]
    for start in range(0, len(slow), _DIFF_BLOCK_ROWS):
        index = slow[start:start + _DIFF_BLOCK_ROWS]
        ct, cs, pt, ps = cur_t[index], cur_s[index], prev_t[index], prev_s[index]
        cur_ok, prev_ok = ct >= 0, pt >= 0
        same = (ct[:, :, None] == pt[:, None, :]) & cur_ok[:, :, None] & prev_ok[:, None, :]
        prev_size_for_cur = np.where(same, ps[:, None, :], 0).sum(axis=2)
        changed = cur_ok & (cs != prev_size_for_cur)
        removed = prev_ok & ~same.any(axis=1)
        rc, lc = np.nonzero(changed)
        rr, lr = np.nonzero(removed)
        rows_out.append(np.concatenate([index[rc], index[rr]]))
        price_out.append(np.concatenate([ct[rc, lc], pt[rr, lr]]))
        prev_out.append(np.concatenate([prev_size_for_cur[rc, lc], ps[rr, lr]]))
        new_out.append(np.concatenate([cs[rc, lc], np.zeros(len(rr), np.int64)]))
    if not rows_out:
        return empty, empty, empty, empty
    joined = [np.concatenate(part).astype(np.int64) for part in (rows_out, price_out, prev_out, new_out)]
    return joined[0], joined[1], joined[2], joined[3]


class _DayConverter:
    """Turns record batches of ONE day and ONE contract into depth and trade rows."""

    def __init__(self, info: FileInfo, options: ImportOptions, group: _Group, symbol: str, folder: Path) -> None:
        import pyarrow.parquet as pq

        self.info, self.options, self.group, self.symbol = info, options, group, symbol
        self.levels = max(1, info.levels)
        self.counters = _Counters()
        no_level = np.full((1, self.levels), -1, np.int64)
        no_size = np.zeros((1, self.levels), np.int64)
        self._carry = (no_level, no_size, no_level.copy(), no_size.copy())  # bid ticks/sizes, ask ticks/sizes
        self._depth_writer = pq.ParquetWriter(folder / "depth.parquet", _depth_schema(), compression="zstd")
        self._trade_writer = pq.ParquetWriter(folder / "trades.parquet", _trade_schema(), compression="zstd")

    def close(self) -> None:
        self._depth_writer.close()
        self._trade_writer.close()

    def feed(self, batch, raw_ts: np.ndarray) -> None:
        import pyarrow as pa

        counters, info = self.counters, self.info
        read = batch.num_rows
        counters.rows_read += read
        if info.has_instrument_id:
            keep = _ints(batch.column("instrument_id"), -1) == self.group.instrument_id
            batch = batch.filter(pa.array(keep))
            raw_ts = raw_ts[keep]
        n = batch.num_rows
        counters.rows_other_instruments += read - n
        if n == 0:
            return
        counters.rows_used += n
        if counters.first_raw is None:
            counters.first_raw = int(raw_ts[0])
        counters.last_raw = int(raw_ts[-1])

        previous_max = np.maximum.accumulate(np.concatenate(([counters.last_ts], raw_ts[:-1])))
        counters.out_of_order += int((raw_ts < previous_max).sum())
        ts = np.maximum(raw_ts, previous_max)
        counters.last_ts = int(ts[-1])
        if counters.first_ts is None:
            counters.first_ts = int(ts[0])

        if "flags" in info.columns:
            flags = _ints(batch.column("flags"))
            counters.bad_book_rows += int(((flags & FLAG_MAYBE_BAD_BOOK) != 0).sum())
            counters.bad_ts_rows += int(((flags & FLAG_BAD_TS_RECV) != 0).sum())
            counters.snapshot_rows += int(((flags & FLAG_SNAPSHOT) != 0).sum())

        bid_t, bid_s, ask_t, ask_s = self._book(batch)
        both = (bid_t[:, 0] >= 0) & (ask_t[:, 0] >= 0)
        counters.crossed_rows += int((both & (bid_t[:, 0] >= ask_t[:, 0])).sum())

        sides = []
        for (cur_t, cur_s), carry_t, carry_s, label in (
            ((ask_t, ask_s), self._carry[2], self._carry[3], 0),
            ((bid_t, bid_s), self._carry[0], self._carry[1], 1),
        ):
            prev_t = np.vstack([carry_t, cur_t[:-1]])
            prev_s = np.vstack([carry_s, cur_s[:-1]])
            row, price, prev_size, new_size = _diff_side(cur_t, cur_s, prev_t, prev_s)
            sides.append((row, np.full(len(row), label, np.int64), price, prev_size, new_size))
        self._carry = (bid_t[-1:], bid_s[-1:], ask_t[-1:], ask_s[-1:])
        d_row, d_side, d_ticks, d_prev, d_new = (np.concatenate([part[i] for part in sides]) for i in range(5))
        # Within one source row: asks by rising price, then bids by falling price.
        order = np.lexsort((np.where(d_side == 1, -d_ticks, d_ticks), d_side, d_row))
        d_row, d_side, d_ticks, d_prev, d_new = (a[order] for a in (d_row, d_side, d_ticks, d_prev, d_new))

        trades = self._trades(batch)
        self._write(ts, (d_row, d_side, d_ticks, d_prev, d_new), trades, n)

    def _book(self, batch):
        info, tick = self.info, self.options.tick_size
        out = []
        for side in ("bid", "ask"):
            ticks_columns, size_columns = [], []
            for level in range(self.levels):
                values, ok = _prices(batch.column(f"{side}_px_{level:02d}"), info)
                size = _ints(batch.column(f"{side}_sz_{level:02d}"))
                ok = ok & (size > 0)
                ticks, off = _to_ticks(values, ok, info, tick)
                self.counters.misaligned_prices += off
                ticks_columns.append(ticks)
                size_columns.append(np.where(ok, size, 0))
            out.append((np.column_stack(ticks_columns), np.column_stack(size_columns)))
        (bid_t, bid_s), (ask_t, ask_s) = out
        return bid_t, bid_s, ask_t, ask_s

    def _trades(self, batch):
        import pyarrow.compute as pc

        counters, info = self.counters, self.info
        is_trade = _flags_of(pc.equal(batch.column("action"), "T"))
        values, price_ok = _prices(batch.column("price"), info)
        size = _ints(batch.column("size"))
        ticks, off = _to_ticks(values, price_ok & is_trade, info, self.options.tick_size)
        counters.misaligned_prices += off
        is_buy = _flags_of(pc.equal(batch.column("side"), "B"))
        is_sell = _flags_of(pc.equal(batch.column("side"), "A"))
        usable = is_trade & price_ok & (size > 0)
        counters.trades_unusable += int((is_trade & ~usable).sum())
        counters.trades_no_aggressor += int((usable & ~(is_buy | is_sell)).sum())
        keep = usable & (is_buy | is_sell)
        rows = np.nonzero(keep)[0].astype(np.int64)
        return rows, ticks[rows], size[rows], is_buy[rows]

    def _ticks_to_strings(self, ticks: np.ndarray):
        """Prices as '29500.25' (two decimals, like the live recorder), one string per distinct tick."""
        import pyarrow as pa

        unique, inverse = np.unique(ticks, return_inverse=True)
        tick = self.options.tick_size
        places = max(0, -tick.as_tuple().exponent)
        labels = [format(Decimal(int(value)) * tick, f".{places}f") for value in unique.tolist()]
        indices = pa.array(inverse.reshape(-1).astype(np.int32))
        return pa.DictionaryArray.from_arrays(indices, pa.array(labels)).dictionary_decode()

    def _write(self, ts, deltas, trades, n_rows: int) -> None:
        import pyarrow as pa
        import pyarrow.compute as pc

        counters = self.counters
        d_row, d_side, d_ticks, d_prev, d_new = deltas
        t_row, t_ticks, t_size, t_buy = trades
        per_row_trades = np.bincount(t_row, minlength=n_rows)
        per_row_deltas = np.bincount(d_row, minlength=n_rows)
        per_row = per_row_trades + per_row_deltas
        first = counters.records + np.cumsum(per_row) - per_row  # records written before each row
        t_seq = first[t_row] + 1  # a trade is delivered before the book change it causes
        within = np.arange(len(d_row)) - np.searchsorted(d_row, d_row, side="left")
        d_seq = first[d_row] + per_row_trades[d_row] + 1 + within
        if counters.records == 0 and n_rows:
            # The session's very first row builds the whole starting book from nothing; that
            # book must exist before a trade in the same row can print against it.
            d_first = d_row == 0
            d_seq = np.where(d_first, 1 + within, d_seq)
            t_seq = np.where(t_row == 0, 1 + per_row_deltas[0], t_seq)
        counters.records += int(per_row.sum())

        if len(d_row):
            self._depth_writer.write_table(pa.table({
                "timestamp": pa.array(ts[d_row], pa.int64()),
                "symbol": pa.repeat(pa.scalar(self.symbol), len(d_row)),
                "side": pc.if_else(pa.array(d_side == 1), "bid", "ask"),
                "price": self._ticks_to_strings(d_ticks),
                "previous_size": pa.array(d_prev).cast(pa.string()),
                "new_size": pa.array(d_new).cast(pa.string()),
                "receive_sequence": pa.array(d_seq, pa.int64()),
            }, schema=_depth_schema()))
        if len(t_row):
            first_id = counters.trades + 1
            self._trade_writer.write_table(pa.table({
                "timestamp_ns": pa.array(ts[t_row], pa.int64()),
                "price": self._ticks_to_strings(t_ticks),
                "size": pa.array(t_size).cast(pa.string()),
                "aggressor_side": pc.if_else(pa.array(t_buy), "buy", "sell"),
                "instrument": pa.repeat(pa.scalar(self.symbol), len(t_row)),
                "sequence_id": pa.array(np.arange(first_id, first_id + len(t_row)), pa.int64()),
                "receive_sequence": pa.array(t_seq, pa.int64()),
            }, schema=_trade_schema()))
        counters.depth_changes += len(d_row)
        counters.trades += len(t_row)


def _check_cancel(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise ImportCancelled


# --- what is already imported ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Known:
    session_id: str
    content_key: str
    day: str
    first_ts: int
    last_ts: int


class _ImportIndex:
    """The earlier Databento imports under ``out_root``, read once, used to skip repeats."""

    def __init__(self, out_root: Path) -> None:
        self.known: list[_Known] = []
        if not out_root.is_dir():
            return
        for manifest in out_root.glob(f"*/*{SESSION_SUFFIX}/session_manifest.json"):
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                source = data["import_source"]
                self.known.append(_Known(
                    str(data.get("session_id") or manifest.parent.name), str(source["content_key"]),
                    manifest.parent.parent.name, int(source["first_ts_ns"]), int(source["last_ts_ns"]),
                ))
            except (OSError, ValueError, KeyError, TypeError):
                continue

    def add(self, known: _Known) -> None:
        self.known.append(known)

    def same_content(self, content_key: str) -> _Known | None:
        return next((item for item in self.known if item.content_key == content_key), None)

    def overlapping(self, day: str, first_ts: int, last_ts: int) -> _Known | None:
        return next(
            (item for item in self.known if item.day == day and item.first_ts <= last_ts and first_ts <= item.last_ts),
            None,
        )


# --- the whole import ---------------------------------------------------------------------


def staging_root_for(options: ImportOptions) -> Path:
    """Private working folder, beside (never inside) the recordings folder."""
    return options.out_root.parent / f".importing_{options.out_root.name}"


def _sweep_stale_staging(root: Path) -> None:
    """Remove working folders left behind by a crashed import (older than a day)."""
    if not root.is_dir():
        return
    cutoff = time.time() - _STALE_STAGING_SECONDS
    for child in root.iterdir():
        try:
            if child.is_dir() and child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
        except OSError:
            continue


def _remove_if_empty(path: Path) -> None:
    try:
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    except OSError:
        pass


def import_mbp_file(
    path: Path,
    options: ImportOptions | None = None,
    *,
    progress: Callable[[ProgressEvent], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    on_result: Callable[[ImportResult], None] | None = None,
) -> list[ImportResult]:
    """Import one MBP file: one result per UTC day in it. Never raises for bad input.

    ``on_result`` is called with each result as soon as it exists, so a screen can fill in
    day by day. Days that finished before a cancel or a failure stay imported.
    """
    options = options or ImportOptions()
    run = _Run(Path(path), options, progress, cancel, on_result)
    try:
        run.execute()
    except ImportCancelled:
        run.emit(ImportResult("cancelled", run.path, f"Stopped. Nothing more was saved from {run.path.name}."))
    except Exception as error:  # noqa: BLE001 - one bad file must never crash the app
        run.emit(ImportResult(
            "failed", run.path, f"{run.path.name} could not be fully imported.",
            problems=[f"{type(error).__name__}: {error}"],
        ))
    finally:
        _remove_if_empty(staging_root_for(options))
    return run.results


class _Run:
    """One file's import, kept in one object so its steps share their state."""

    def __init__(self, path, options, progress, cancel, on_result) -> None:
        self.path: Path = path
        self.options: ImportOptions = options
        self._progress = progress
        self._cancel = cancel
        self._on_result = on_result
        self.results: list[ImportResult] = []
        self.file_index = 0
        self.file_count = 0
        self.info: FileInfo = FileInfo(path, "unsupported")
        self.fingerprint = ""
        self.index = _ImportIndex(options.out_root)

    def emit(self, result: ImportResult) -> None:
        self.results.append(result)
        if self._on_result is not None:
            self._on_result(result)

    def report(self, stage: str, done: int = 0, total: int = 0, message: str = "") -> None:
        if self._progress is not None:
            self._progress(ProgressEvent(stage, done, total, message, self.path.name, self.file_index, self.file_count))

    def execute(self) -> None:
        self.report("checking", message=f"Checking {self.path.name}")
        self.info = inspect_file(self.path)
        if not self.info.importable:
            reason = self.info.problem or self.info.note or "This file is not a Databento MBP file."
            status = "skipped" if self.info.note and not self.info.problem else "refused"
            self.emit(ImportResult(status, self.path, f"{self.path.name} was not imported. {reason}"))
            return
        self.fingerprint = file_fingerprint(self.path)
        groups, total_rows = _survey(self.info, self.options, self._relay, self._cancel)
        if not groups:
            self.emit(ImportResult("refused", self.path, f"{self.path.name} has no rows to import."))
            return
        plans = self._plan(groups)
        if any(plan.action == "convert" for plan in plans.values()):
            self._convert(plans, total_rows)

    def _relay(self, event: ProgressEvent) -> None:
        self.report(event.stage, event.done, event.total, event.message)

    # -- deciding what to do with each day

    def _plan(self, groups: dict[tuple[int, int], _Group]) -> dict[int, _DayPlan]:
        by_day: dict[int, list[_Group]] = {}
        for group in groups.values():
            by_day.setdefault(group.day, []).append(group)
        plans: dict[int, _DayPlan] = {}
        for day in sorted(by_day):
            plan = self._plan_day(day, by_day[day])
            plans[day] = plan
            if plan.action != "convert":
                self.emit(plan.as_result(self.path))
        return plans

    def _plan_day(self, day: int, candidates: list[_Group]) -> _DayPlan:
        label = _day_label(day)
        chosen, why, others = _choose_instrument(candidates, self.options)
        if chosen is None:
            message = f"{label}: nothing imported. {why[:1].upper()}{why[1:]}."
            return _DayPlan(day, label, None, why, [], "refused", message)
        refusal = _refusal_for(chosen, self.options)
        if refusal:
            return _DayPlan(day, label, chosen, why, others, "refused", f"{label}: nothing imported. {refusal}")
        symbol = chosen.symbol or self.options.symbol_family
        first, last = chosen.first_ts or 0, chosen.last_ts
        key = f"{symbol}|{first}|{chosen.rows}|{chosen.trades}"
        twin = self.index.same_content(key) or self.index.overlapping(label, first, last)
        if twin is not None:
            if twin.content_key == key:
                message = f"{label}: already imported as {twin.session_id}. Skipped."
            else:
                message = (
                    f"{label}: overlaps {twin.session_id}, which is already imported. "
                    "Skipped so the same trades are not counted twice."
                )
            return _DayPlan(day, label, chosen, why, others, "duplicate", message, twin.session_id, symbol, key)
        return _DayPlan(day, label, chosen, why, others, "convert", "", "", symbol, key)

    # -- converting the days that need it

    def _convert(self, plans: dict[int, _DayPlan], total_rows: int) -> None:
        columns = _conversion_columns(self.info)
        staging_root = staging_root_for(self.options)
        staging_root.mkdir(parents=True, exist_ok=True)
        _sweep_stale_staging(staging_root)
        current: _ActiveDay | None = None
        done_rows = 0
        carry = -1
        try:
            for batch in _open_reader(self.info, columns, self.options.block_bytes):
                _check_cancel(self._cancel)
                n = batch.num_rows
                if n == 0:
                    continue
                raw_ts = _clock_ns(batch, self.info)
                days, carry = _effective_days(raw_ts, carry)
                cuts = np.concatenate(([0], np.nonzero(np.diff(days))[0] + 1, [n]))
                for start, stop in zip(cuts[:-1].tolist(), cuts[1:].tolist(), strict=True):
                    day = int(days[start])
                    if current is not None and current.plan.day != day:
                        finished, current = current, None
                        self._finish_day(finished)
                    plan = plans.get(day)
                    if plan is None or plan.action != "convert":
                        continue
                    if current is None:
                        current = self._open_day(plan, staging_root)
                    current.converter.feed(batch.slice(start, stop - start), raw_ts[start:stop])
                done_rows += n
                where = f" ({current.plan.label})" if current is not None else ""
                self.report("converting", done_rows, total_rows, f"Converting{where}: {done_rows:,} of {total_rows:,} rows")
            if current is not None:
                finished, current = current, None
                self._finish_day(finished)
        finally:
            if current is not None:  # cancelled or failed part-way: this day is thrown away, finished days stay
                current.converter.close()
                shutil.rmtree(current.folder, ignore_errors=True)

    def _open_day(self, plan: _DayPlan, staging_root: Path) -> _ActiveDay:
        folder = staging_root / uuid.uuid4().hex
        folder.mkdir(parents=True)
        assert plan.chosen is not None
        converter = _DayConverter(self.info, self.options, plan.chosen, plan.symbol, folder)
        return _ActiveDay(plan, converter, folder, time.perf_counter())

    def _finish_day(self, active: _ActiveDay) -> None:
        active.converter.close()
        self.report("finishing", message=f"Saving {active.plan.label}")
        self.emit(self._save_day(active))

    def _save_day(self, active: _ActiveDay) -> ImportResult:
        plan, converter, folder = active.plan, active.converter, active.folder
        c, options = converter.counters, self.options
        if c.first_ts is None or c.rows_used == 0:
            shutil.rmtree(folder, ignore_errors=True)
            return ImportResult("refused", self.path, f"{plan.label}: nothing imported. No usable rows.", day=plan.label)
        start = datetime.fromtimestamp(max(c.first_ts, plan.day * DAY_NS) / NS, tz=UTC)
        stamp = start.strftime("%Y%m%dT%H%M%SZ")
        session_id, number = f"session_{stamp}{SESSION_SUFFIX}", 1
        while (options.out_root / plan.label / session_id).exists():
            # Two different stretches of one day that begin in the same second must not collide.
            number += 1
            session_id = f"session_{stamp}_{number}{SESSION_SUFFIX}"
        session_dir = options.out_root / plan.label / session_id
        problems, warnings = _quality_findings(c, self.info)
        manifest = _manifest(self, plan, converter, session_id, start, problems, warnings)
        entry = classify_manifest(manifest, folder / "session_manifest.json")
        manifest["valid_for_analysis"] = entry.eligible_for_analysis
        manifest["valid_for_order_flow_replay"] = entry.eligible_for_order_flow_replay
        manifest["valid_for_real_training"] = entry.eligible_for_model_training
        manifest["valid_for_live_decisions"] = False
        _write_manifest(folder / "session_manifest.json", manifest)
        session_dir.parent.mkdir(parents=True, exist_ok=True)
        os.replace(folder, session_dir)  # the whole session, manifest included, appears at once
        self.index.add(_Known(session_id, plan.content_key, plan.label, c.first_raw or 0, c.last_raw))
        result = ImportResult(
            "imported", self.path,
            f"{plan.label}: imported {plan.symbol} with {c.depth_changes:,} order-book changes and {c.trades:,} trades.",
            day=plan.label, session_id=session_id, session_dir=session_dir,
            notes=_notes(plan, self.info), problems=problems,
            stats={
                "symbol": plan.symbol, "rows_read": c.rows_read, "rows_used": c.rows_used,
                "depth_changes": c.depth_changes, "trades": c.trades, "book_levels": converter.levels,
                "out_of_order": c.out_of_order, "bad_book_rows": c.bad_book_rows,
            },
            replay_ok=entry.eligible_for_order_flow_replay, training_ok=entry.eligible_for_model_training,
            replay_reasons=entry.reasons, training_reasons=entry.model_training_reasons,
            seconds=time.perf_counter() - active.started,
        )
        result.notes.extend(warnings)
        return result


@dataclass(slots=True)
class _DayPlan:
    day: int
    label: str  # YYYY-MM-DD
    chosen: _Group | None
    why: str
    others: list[_Group]
    action: str  # convert | duplicate | refused
    message: str
    twin: str = ""
    symbol: str = ""
    content_key: str = ""

    def as_result(self, source: Path) -> ImportResult:
        status = "duplicate" if self.action == "duplicate" else "refused"
        return ImportResult(status, source, self.message, day=self.label, session_id=self.twin)


@dataclass(slots=True)
class _ActiveDay:
    plan: _DayPlan
    converter: _DayConverter
    folder: Path
    started: float


def _day_label(day: int) -> str:
    return datetime.fromtimestamp(day * 86_400, tz=UTC).date().isoformat()


def _conversion_columns(info: FileInfo) -> list[str]:
    columns = [info.clock_column, "action", "side", "price", "size"]
    columns += [column for column in ("instrument_id", "flags") if column in info.columns]
    for level in range(max(1, info.levels)):
        columns += [f"bid_px_{level:02d}", f"ask_px_{level:02d}", f"bid_sz_{level:02d}", f"ask_sz_{level:02d}"]
    return columns


def _quality_findings(c: _Counters, info: FileInfo) -> tuple[list[str], list[str]]:
    """Problems make the session unfit for replay; warnings are only worth knowing."""
    problems: list[str] = []
    warnings: list[str] = []
    if c.out_of_order:
        problems.append(f"{c.out_of_order:,} rows had a timestamp earlier than the row before them.")
    if c.bad_book_rows:
        problems.append(
            f"Databento flagged {c.bad_book_rows:,} rows as having an unrecoverable gap in the feed, "
            "so the order book may be wrong after them."
        )
    if c.misaligned_prices:
        problems.append(
            f"{c.misaligned_prices:,} prices were not on the 0.25 tick grid, "
            "which suggests a spread or a different product."
        )
    seen_trades = c.trades + c.trades_no_aggressor
    if seen_trades and c.trades_no_aggressor / seen_trades > 0.001:
        problems.append(
            f"{c.trades_no_aggressor:,} trades had no aggressor side and were left out, "
            "which would skew buy/sell volume."
        )
    elif c.trades_no_aggressor:
        warnings.append(f"{c.trades_no_aggressor:,} trades had no aggressor side and were left out.")
    if c.trades_unusable:
        warnings.append(f"{c.trades_unusable:,} trade records had no usable price or size and were left out.")
    if c.bad_ts_rows:
        warnings.append(f"Databento flagged {c.bad_ts_rows:,} rows as having an unreliable receive time.")
    if c.crossed_rows:
        warnings.append(f"The best bid was at or above the best ask on {c.crossed_rows:,} rows (a locked or crossed book).")
    if "ts_recv" not in info.columns:
        problems.append("The file has no ts_recv column, so ordering was taken from ts_event, which can run backwards.")
    return problems, warnings


def _manifest(
    run: _Run, plan: _DayPlan, converter: _DayConverter, session_id: str, start: datetime,
    problems: list[str], warnings: list[str],
) -> dict[str, object]:
    c, options, info = converter.counters, run.options, run.info
    chosen = plan.chosen
    assert chosen is not None
    end = datetime.fromtimestamp(c.last_ts / NS, tz=UTC)
    verified = not problems
    return {
        "session_id": session_id,
        "symbol": plan.symbol,
        "synthetic": False,
        "provenance": "REAL_REPLAY",
        "source_mode": "replay",
        "is_delayed": False,
        "data_delay_minutes": 0,
        # For an import, "clean shutdown" means the import ran to completion; the folder appears whole.
        "clean_shutdown": True,
        "continuity_status": "continuous" if not (c.out_of_order or c.bad_book_rows) else "gap",
        "utc_start": start.isoformat(),
        "utc_end": end.isoformat(),
        "event_counts": {"depth_updates": c.depth_changes, "trades": c.trades},
        "dropped_message_count": c.bad_book_rows,
        # Only what was measured is recorded; what was not measured is listed, never claimed.
        "data_quality": {
            "ok": verified,
            "session_dropped_messages": c.bad_book_rows,
            "out_of_order_events": c.out_of_order,
            "malformed_events": 0,
            "rejected_events": 0,
            "not_measured": ["duplicate_stream_events", "trade_sequence_gaps"],
        },
        "receive_order": {"available": True, "field": "receive_sequence", "last_sequence": c.records},
        "storage": {
            "format": "closed_parquet_single_files_v1", "finalized_single_files": True,
            "depth_parts": 1, "trade_parts": 1, "compaction_deferred": False,
        },
        "import_source": {
            "importer": IMPORTER,
            "vendor": VENDOR,
            "schema": f"mbp-{converter.levels}",
            "file": run.path.name,
            "file_fingerprint": run.fingerprint,
            "content_key": plan.content_key,
            "first_ts_ns": c.first_raw,
            "last_ts_ns": c.last_raw,
            "session_split": "one session per UTC calendar day",
            "clock": info.clock_column,
            "book_depth_levels": converter.levels,
            "trades_from": "the file's own trade records (action T)",
            "instrument": {
                "instrument_id": chosen.instrument_id,
                "symbol": plan.symbol,
                "chosen_because": plan.why,
                "other_instruments_left_out": [
                    {"instrument_id": other.instrument_id, "symbol": other.symbol, "rows": other.rows}
                    for other in plan.others
                ],
                "rows_left_out": c.rows_other_instruments,
            },
            "rows": {
                "read": c.rows_read, "used": c.rows_used, "depth_changes": c.depth_changes, "trades": c.trades,
            },
            "trades_with_aggressor_side": c.trades,
            "trades_left_out_no_aggressor": c.trades_no_aggressor,
            "trade_records_unusable_left_out": c.trades_unusable,
            "quality": {
                "out_of_order_rows": c.out_of_order,
                "bad_book_flag_rows": c.bad_book_rows,
                "bad_ts_recv_flag_rows": c.bad_ts_rows,
                "snapshot_rows": c.snapshot_rows,
                "crossed_book_rows": c.crossed_rows,
                "prices_off_tick_grid": c.misaligned_prices,
                "problems": problems,
                "warnings": warnings,
            },
            "quality_verified": verified,
            "allow_model_training": bool(options.allow_model_training),
            "note": "offline historical import, not a live bridge capture",
        },
    }


def _write_manifest(path: Path, manifest: dict[str, object]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


def _notes(plan: _DayPlan, info: FileInfo) -> list[str]:
    notes = [f"Contract chosen: {plan.why}."]
    if plan.others:
        listed = ", ".join(f"{o.symbol or o.instrument_id} ({o.rows:,} rows)" for o in plan.others[:4])
        notes.append(f"Left out {len(plan.others)} other instrument(s): {listed}{'...' if len(plan.others) > 4 else ''}.")
    notes.append(
        f"Order book depth: {max(1, info.levels)} level(s). Live recordings show up to 20, so features that "
        "sum the whole book are not directly comparable."
    )
    return notes


# --- many files at once (the drop box and the command line use this) ------------------------


def import_many(
    paths: Iterable[Path],
    options: ImportOptions | None = None,
    *,
    progress: Callable[[ProgressEvent], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    on_result: Callable[[ImportResult], None] | None = None,
) -> list[ImportResult]:
    """Import whatever was dropped: files, folders and zips. Returns every result, in order."""
    options = options or ImportOptions()
    results: list[ImportResult] = []

    def emit(result: ImportResult) -> None:
        results.append(result)
        if on_result is not None:
            on_result(result)

    staging_root = staging_root_for(options)
    workdir = staging_root / f"drop_{uuid.uuid4().hex[:8]}"
    try:
        dropped = [Path(item) for item in paths]
        for missing in (item for item in dropped if not item.exists()):
            emit(ImportResult("refused", missing, f"{missing.name} was not found, so it was not imported."))
        inputs = collect_inputs([item for item in dropped if item.exists()], workdir)
        inputs.sort(key=lambda item: (item.info.day_hint or "9999", item.path.name))
        importable = [item for item in inputs if item.info.importable]
        for item in inputs:
            if item.info.importable:
                continue
            reason = item.info.problem or item.info.note
            status = "skipped" if item.info.note and not item.info.problem else "refused"
            emit(ImportResult(status, item.path, f"{item.origin} was not imported. {reason}"))
        if not dropped:
            emit(ImportResult("refused", Path("."), "Nothing to import: no files were dropped."))
        elif not inputs and all(item.exists() for item in dropped):
            emit(ImportResult("refused", dropped[0], "Nothing to import: no files were found in what you dropped."))
        for number, item in enumerate(importable, start=1):
            if cancel is not None and cancel():
                emit(ImportResult("cancelled", item.path, "Stopped before this file. Nothing was saved from it."))
                break
            run = _Run(item.path, options, progress, cancel, emit)
            run.file_index, run.file_count = number, len(importable)
            try:
                run.execute()
            except ImportCancelled:
                emit(ImportResult("cancelled", item.path, f"Stopped. Nothing more was saved from {item.origin}."))
                break
            except Exception as error:  # noqa: BLE001 - one bad file must never stop the rest
                emit(ImportResult(
                    "failed", item.path, f"{item.origin} could not be fully imported.",
                    problems=[f"{type(error).__name__}: {error}"],
                ))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        _remove_if_empty(staging_root)
    return results
