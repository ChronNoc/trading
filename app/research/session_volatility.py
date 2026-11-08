"""Walk-forward volatility regime for recorded sessions, from the first RTH hour.

Why this exists: a strategy tuned with fixed point thresholds in one volatility
regime quietly stops fitting when the regime changes (a public MNQ study found
exactly that: thresholds tuned in a high-volatility stretch failed in a calm one).
Pooling every session into one number hides that, so each trading day gets a
volatility label and results can be reported per label.

The label is WALK-FORWARD: the regime going into trading day D is computed only
from the first-hour (09:30-10:30 New York) trade range of trading days BEFORE D,
so it is known before D opens and can be used for evaluation without look-ahead.
Every session that starts on day D inherits D's label (overnight and Asia
sessions included, because the label describes the regime they sit in).

Honesty rules:
  * a day is "unknown" until there is enough prior history (never guessed);
  * a session counts toward a day only if it covers the whole first hour;
  * heavy parquet reading never happens inside the live backend - a CLI
    (``python -m tools.session_volatility``) fills a small JSON cache and the
    catalog only reads that cache.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from statistics import median

from app.research.causal_context import NEW_YORK, RTH_OPEN, trading_day_for_timestamp

FIRST_HOUR_END = time(10, 30)
CACHE_VERSION = 1
DEFAULT_CACHE_NAME = "session_volatility.json"

LABEL_LOW = "low"
LABEL_NORMAL = "normal"
LABEL_HIGH = "high"
LABEL_UNKNOWN = "unknown"
LABELS = (LABEL_LOW, LABEL_NORMAL, LABEL_HIGH, LABEL_UNKNOWN)

_NS = 1_000_000_000


# --- first-hour sample ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FirstHourSample:
    """High/low of the trades printed during one day's first RTH hour."""

    trading_day: str  # ISO date of the New York trading day
    session_id: str
    ib_high: Decimal
    ib_low: Decimal
    trades: int

    @property
    def ib_range(self) -> Decimal:
        """First-hour range in price points."""
        return self.ib_high - self.ib_low


def first_hour_window_ns(trading_day: date) -> tuple[int, int]:
    """Return the [start, end) UTC nanoseconds of the day's New York first hour."""
    start = datetime.combine(trading_day, RTH_OPEN, tzinfo=NEW_YORK)
    end = datetime.combine(trading_day, FIRST_HOUR_END, tzinfo=NEW_YORK)
    return int(start.timestamp()) * _NS, int(end.timestamp()) * _NS


def trading_days_with_full_first_hour(
    utc_start: str | None,
    utc_end: str | None,
    *,
    tolerance_seconds: int = 60,
) -> list[date]:
    """Weekdays whose whole first hour lies inside a session's recorded span.

    A session that starts at 10:00 would understate that day's range, so partial
    coverage is rejected rather than estimated. ``tolerance_seconds`` forgives a
    start or stop a few seconds inside the window edge.
    """
    start_ns, end_ns = _iso_to_ns(utc_start), _iso_to_ns(utc_end)
    if start_ns is None or end_ns is None or end_ns <= start_ns:
        return []
    tolerance = tolerance_seconds * _NS
    day = _ns_to_datetime(start_ns).astimezone(NEW_YORK).date() - timedelta(days=1)
    last = _ns_to_datetime(end_ns).astimezone(NEW_YORK).date()
    found: list[date] = []
    while day <= last:
        if day.weekday() < 5:  # no RTH on weekends
            low, high = first_hour_window_ns(day)
            if start_ns <= low + tolerance and end_ns >= high - tolerance:
                found.append(day)
        day += timedelta(days=1)
    return found


def read_first_hour_sample(
    session_dir: Path,
    session_id: str,
    trading_day: date,
) -> FirstHourSample | None:
    """Read one session's trades inside the day's first hour (read-only).

    Only the ``timestamp_ns`` and ``price`` columns of trade files whose footer
    time range can overlap the window are read, so even a session with thousands
    of part files costs one footer read each plus the window's rows.
    """
    import pyarrow.parquet as pq

    from app.research.replay_loader import _stream_paths

    low_ns, high_ns = first_hour_window_ns(trading_day)
    high_price: Decimal | None = None
    low_price: Decimal | None = None
    count = 0
    for path in _stream_paths(session_dir, "trades"):
        if not _file_may_overlap(path, low_ns, high_ns):
            continue
        table = pq.read_table(
            path,
            columns=["timestamp_ns", "price"],
            filters=[("timestamp_ns", ">=", low_ns), ("timestamp_ns", "<", high_ns)],
        )
        for raw in table.column("price").to_pylist():
            try:
                price = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                continue  # a malformed recorded price is skipped, never guessed
            count += 1
            high_price = price if high_price is None else max(high_price, price)
            low_price = price if low_price is None else min(low_price, price)
    if high_price is None or low_price is None:
        return None
    return FirstHourSample(trading_day.isoformat(), session_id, high_price, low_price, count)


def _file_may_overlap(path: Path, low_ns: int, high_ns: int) -> bool:
    """Use the parquet footer's min/max timestamps to skip files outside the window."""
    import pyarrow.parquet as pq

    try:
        metadata = pq.ParquetFile(path).metadata
        column = metadata.schema.names.index("timestamp_ns")
        file_min = file_max = None
        for group in range(metadata.num_row_groups):
            stats = metadata.row_group(group).column(column).statistics
            if stats is None or not stats.has_min_max:
                return True  # no statistics: cannot prove it is outside, so read it
            file_min = stats.min if file_min is None else min(file_min, stats.min)
            file_max = stats.max if file_max is None else max(file_max, stats.max)
        if file_min is None or file_max is None:
            return False
        return file_max >= low_ns and file_min < high_ns
    except Exception:  # noqa: BLE001 - when in doubt, read the file
        return True


# --- on-disk cache -------------------------------------------------------------


@dataclass(slots=True)
class SessionVolatilityCache:
    """First-hour samples per session, keyed so a changed manifest is re-read."""

    sessions: dict[str, dict[str, object]] = field(default_factory=dict)

    def is_current(self, session_id: str, key: str) -> bool:
        record = self.sessions.get(session_id)
        return record is not None and record.get("key") == key

    def put(self, session_id: str, key: str, samples: Iterable[FirstHourSample]) -> None:
        self.sessions[session_id] = {
            "key": key,
            "samples": [
                {
                    "trading_day": sample.trading_day,
                    "ib_high": str(sample.ib_high),
                    "ib_low": str(sample.ib_low),
                    "trades": sample.trades,
                }
                for sample in samples
            ],
        }

    def all_samples(self) -> list[FirstHourSample]:
        found: list[FirstHourSample] = []
        for session_id, record in self.sessions.items():
            for raw in record.get("samples", []) if isinstance(record.get("samples"), list) else []:
                try:
                    found.append(
                        FirstHourSample(
                            trading_day=str(raw["trading_day"]),
                            session_id=session_id,
                            ib_high=Decimal(str(raw["ib_high"])),
                            ib_low=Decimal(str(raw["ib_low"])),
                            trades=int(raw["trades"]),
                        ),
                    )
                except (KeyError, TypeError, ValueError, InvalidOperation):
                    continue  # one malformed record never poisons the rest
        return found


def load_cache(path: Path) -> SessionVolatilityCache:
    """Load the cache; a missing or damaged file just means an empty cache."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != CACHE_VERSION or not isinstance(payload.get("sessions"), dict):
            return SessionVolatilityCache()
        return SessionVolatilityCache(sessions=dict(payload["sessions"]))
    except (OSError, ValueError, AttributeError):
        return SessionVolatilityCache()


def save_cache(path: Path, cache: SessionVolatilityCache) -> None:
    """Write the cache atomically so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps({"version": CACHE_VERSION, "sessions": cache.sessions}, indent=1, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def default_cache_path(raw_root: Path) -> Path:
    """Where the cache lives for a recordings root: ``<data>/processed/``."""
    return raw_root.parent / "processed" / DEFAULT_CACHE_NAME


@dataclass(frozen=True, slots=True)
class UpdateSummary:
    """What one cache refresh did."""

    analysed: int
    already_cached: int
    no_first_hour: int
    skipped_unfinished: int
    errors: tuple[str, ...]


def update_cache(
    raw_root: Path,
    cache_path: Path,
    *,
    progress: Callable[[str], None] | None = None,
) -> UpdateSummary:
    """Fill the cache for every finished real session that is not yet current."""
    from app.research.session_catalog import PROVENANCE_SYNTHETIC, build_catalog

    cache = load_cache(cache_path)
    analysed = cached = no_hour = unfinished = 0
    errors: list[str] = []
    for entry in sorted(build_catalog(raw_root), key=lambda item: item.session_id):
        if entry.provenance == PROVENANCE_SYNTHETIC or not entry.finalized or entry.active:
            unfinished += 1
            continue
        key = f"{entry.utc_end}|{entry.trades}"
        if cache.is_current(entry.session_id, key):
            cached += 1
            continue
        days = trading_days_with_full_first_hour(entry.utc_start, entry.utc_end)
        samples: list[FirstHourSample] = []
        try:
            for day in days:
                sample = read_first_hour_sample(entry.manifest_path.parent, entry.session_id, day)
                if sample is not None:
                    samples.append(sample)
        except Exception as error:  # noqa: BLE001 - report it, retry next run, keep going
            errors.append(f"{entry.session_id}: {type(error).__name__}: {error}")
            continue
        cache.put(entry.session_id, key, samples)
        analysed += 1
        no_hour += 0 if samples else 1
        if progress is not None:
            progress(f"{entry.session_id}: {len(samples)} first-hour day(s)")
    save_cache(cache_path, cache)
    return UpdateSummary(analysed, cached, no_hour, unfinished, tuple(errors))


# --- the walk-forward regime ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class VolatilityRegimeConfig:
    """How the walk-forward label is formed. All values are about HISTORY SIZE and
    rank cut-offs - none is a price or tick threshold, so none depends on the
    current price level or volatility."""

    window_days: int = 20  # recent days summarised by their median
    min_window_days: int = 10  # recent days required before labelling
    min_history_days: int = 30  # total prior days required (the baseline)
    low_percentile: int = 33  # recent median at/below this rank of history = low
    high_percentile: int = 67  # at/above this rank = high
    max_staleness_days: int = 14  # newest prior day may be at most this old


@dataclass(frozen=True, slots=True)
class RegimeLabel:
    """The regime going into one trading day, with the evidence behind it."""

    label: str
    typical_range: Decimal | None
    percentile: Decimal | None
    prior_days: int
    reason: str


class VolatilityRegimeTable:
    """Daily first-hour ranges, queried walk-forward (only days before the asked day)."""

    def __init__(
        self,
        daily_ranges: Mapping[date, Decimal],
        config: VolatilityRegimeConfig | None = None,
    ) -> None:
        self._config = config or VolatilityRegimeConfig()
        self._days = sorted(daily_ranges)
        self._ranges = {day: daily_ranges[day] for day in self._days}

    @classmethod
    def from_samples(
        cls,
        samples: Iterable[FirstHourSample],
        config: VolatilityRegimeConfig | None = None,
    ) -> VolatilityRegimeTable:
        """One value per day: the sample that saw the most trades (the fullest recording)."""
        best: dict[date, FirstHourSample] = {}
        for sample in samples:
            day = date.fromisoformat(sample.trading_day)
            if day not in best or sample.trades > best[day].trades:
                best[day] = sample
        return cls({day: sample.ib_range for day, sample in best.items()}, config)

    @property
    def observed_days(self) -> int:
        """Number of trading days that have a first-hour range."""
        return len(self._days)

    def typical_range_for(self, trading_day: date) -> Decimal | None:
        """Median first-hour range of the recent prior days (points), or None.

        Available all day, including overnight, because it uses only earlier days -
        which makes it a safe reference to scale a threshold by.
        """
        return self.label_for(trading_day).typical_range

    def label_for(self, trading_day: date) -> RegimeLabel:
        """The regime going into ``trading_day`` using only strictly earlier days."""
        config = self._config
        prior = [day for day in self._days if day < trading_day]
        if len(prior) < config.min_history_days:
            return _unknown(len(prior), f"needs {config.min_history_days} prior first-hour days, have {len(prior)}")
        if (trading_day - prior[-1]).days > config.max_staleness_days:
            return _unknown(len(prior), f"newest prior first-hour day is more than {config.max_staleness_days} days old")
        recent = prior[-config.window_days :]
        if len(recent) < config.min_window_days:
            return _unknown(len(prior), f"needs {config.min_window_days} recent first-hour days, have {len(recent)}")
        typical = median(self._ranges[day] for day in recent)
        history = [self._ranges[day] for day in prior]
        percentile = _mid_rank_percentile(typical, history)
        if percentile <= config.low_percentile:
            label = LABEL_LOW
        elif percentile >= config.high_percentile:
            label = LABEL_HIGH
        else:
            label = LABEL_NORMAL
        return RegimeLabel(
            label=label,
            typical_range=typical,
            percentile=percentile,
            prior_days=len(prior),
            reason=(
                f"median first-hour range {typical} pts over the last {len(recent)} prior days "
                f"= {percentile}th percentile of {len(prior)} prior days"
            ),
        )

    def label_for_timestamp_ns(self, timestamp_ns: int) -> RegimeLabel:
        """The label of the trading day an instant belongs to."""
        return self.label_for(date.fromisoformat(trading_day_for_timestamp(timestamp_ns)))


def load_regime_table(cache_path: Path, config: VolatilityRegimeConfig | None = None) -> VolatilityRegimeTable:
    """Build the table from a cache file (empty table if there is none)."""
    return VolatilityRegimeTable.from_samples(load_cache(cache_path).all_samples(), config)


def label_for_session_start(table: VolatilityRegimeTable, utc_start: str | None) -> RegimeLabel:
    """The label for the trading day a session started in ("unknown" if unparseable)."""
    start_ns = _iso_to_ns(utc_start)
    if start_ns is None:
        return _unknown(0, "session start time is missing or unreadable")
    return table.label_for_timestamp_ns(start_ns)


# --- per-regime results ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RegimeTotals:
    """Sessions, paper trades, and simulated net P&L inside one regime label."""

    sessions: int
    paper_trades: int
    net_pnl: Decimal


def totals_by_label(rows: Iterable[tuple[str, int, str]]) -> dict[str, RegimeTotals]:
    """Sum (label, paper trades, net P&L string) rows per label, in label order.

    Money stays Decimal; a malformed P&L string is skipped, never guessed, so each
    total is only ever the sum of well-formed values. Every label is present, even
    with zero sessions, so the output never hides an empty bucket.
    """
    sessions = {label: 0 for label in LABELS}
    trades = {label: 0 for label in LABELS}
    money = {label: Decimal("0") for label in LABELS}
    for label, paper_trades, net_pnl in rows:
        bucket = label if label in sessions else LABEL_UNKNOWN
        sessions[bucket] += 1
        trades[bucket] += max(0, int(paper_trades))
        if net_pnl:
            try:
                money[bucket] += Decimal(net_pnl)
            except InvalidOperation:
                continue
    return {label: RegimeTotals(sessions[label], trades[label], money[label]) for label in LABELS}


# --- helpers ---------------------------------------------------------------------


def _unknown(prior_days: int, reason: str) -> RegimeLabel:
    return RegimeLabel(LABEL_UNKNOWN, None, None, prior_days, reason)


def _mid_rank_percentile(value: Decimal, history: list[Decimal]) -> Decimal:
    below = sum(1 for item in history if item < value)
    equal = sum(1 for item in history if item == value)
    return (Decimal(2 * below + equal) * Decimal(50) / Decimal(len(history))).quantize(Decimal("0.1"))


def _iso_to_ns(value: str | None) -> int | None:
    if not value:
        return None
    try:
        instant = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=UTC)
    return int(instant.timestamp()) * _NS + instant.microsecond * 1_000


def _ns_to_datetime(timestamp_ns: int) -> datetime:
    seconds, nanoseconds = divmod(timestamp_ns, _NS)
    return datetime.fromtimestamp(seconds, tz=UTC).replace(microsecond=nanoseconds // 1_000)
