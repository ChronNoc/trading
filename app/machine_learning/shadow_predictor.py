"""Shadow model loader: exact-approval scoring with paper-only policy evidence.

This is the ML-003 connection between the immutable offline challenger
registry (``app.machine_learning.registry``) and the shared causal feature
builder already running live on the analysis thread
(``app.machine_learning.feature_contract.ObserveOnlyFeatureSink``).

Activation is intentionally narrow: :class:`ObserveOnlyModelLoader` only ever
loads and scores an artifact when
``app.machine_learning.registry.validate_explicit_approval`` reports
``APPROVED_RUNTIME_DISABLED`` for an exact artifact ID + SHA256 named by a
human in ``config/model_approval.yaml``. The registry's own
``runtime_loading_enabled`` / ``shadow_scoring_enabled`` flags are a separate,
still-unimplemented safety boundary (D-001/D-006) — ``validate_explicit_approval``
continues to hard-reject both, unchanged by this module, and this loader does
not read or interpret them.

Every prediction produced here is written to an append-only
:class:`PredictionJournal` as durable evidence. This module has no import of,
or dependency on, ``app.strategy``, ``app.paper``, ``app.risk``,
``app.execution``, or any broker code. A paper/shadow caller may explicitly
consume the latest causally correlated score through :meth:`last_probability`
under the conservative ML policy, while broker execution remains unreachable.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable

from app.machine_learning.feature_contract import FeatureVector

PREDICTION_JOURNAL_SCHEMA_VERSION = 1
# Full registry/approval revalidation re-hashes model, dataset, and bundle
# bytes end-to-end (see app.machine_learning.registry.read_registry) - too
# costly to repeat on every scored vector. Approval changes are rare and
# human-driven, so a bounded cadence is safe; explicit refresh() calls (e.g.
# in tests) always run immediately regardless of this interval.
DEFAULT_REFRESH_INTERVAL_SECONDS = 30.0
DEFAULT_MAX_ARTIFACT_AGE_DAYS = 30


@dataclass(frozen=True, slots=True)
class ShadowProbabilityEvidence:
    """Latest direction score with complete atomic causal model identity."""

    prediction_id: str
    artifact_id: str
    artifact_sha256: str
    session_id: str
    direction: str
    timestamp_ns: int
    success_probability: float


@dataclass(frozen=True, slots=True)
class ShadowPrediction:
    """One scored prediction record with no direct broker-side effect."""

    prediction_id: str
    artifact_id: str
    artifact_sha256: str
    session_id: str
    timestamp_ns: int
    direction: str
    feature_vector_sha256: str
    success_probability: float
    schema_version: int = PREDICTION_JOURNAL_SCHEMA_VERSION

    def to_record(self) -> dict[str, object]:
        """Return the JSON record with the legacy direct-impact marker preserved."""
        return {
            "schema_version": self.schema_version,
            "prediction_id": self.prediction_id,
            "artifact_id": self.artifact_id,
            "artifact_sha256": self.artifact_sha256,
            "session_id": self.session_id,
            "timestamp_ns": self.timestamp_ns,
            "direction": self.direction,
            "feature_vector_sha256": self.feature_vector_sha256,
            "success_probability": self.success_probability,
            "decision_impact": "none",
        }


@dataclass(frozen=True, slots=True)
class JournalRecovery:
    """What was recovered from an existing prediction journal file."""

    records: tuple[dict[str, object], ...]
    damaged_tail: bool
    path: Path


class PredictionJournal:
    """Append-only JSONL sink for scored shadow predictions.

    Mirrors ``app.paper.ledger.PaperLedger``: a written record is a historical
    fact that is never rewritten, an existing file is user-owned data that is
    continued rather than overwritten, and a crash mid-write leaves at most one
    torn final line, which is tolerated on the next open.
    """

    def __init__(self, path: Path | str, *, fsync: bool = True) -> None:
        """Open (or create) a journal at ``path`` without disturbing its contents."""
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync
        self._lock = threading.Lock()
        self._recovery = self.recover(self._path)
        self._count = len(self._recovery.records)

    @property
    def path(self) -> Path:
        """Filesystem location of this journal."""
        return self._path

    @property
    def count(self) -> int:
        """Total records written plus recovered."""
        return self._count

    def append(self, prediction: ShadowPrediction) -> None:
        """Append one prediction durably. Never rewrites earlier lines."""
        line = json.dumps(prediction.to_record(), sort_keys=True, separators=(",", ":")) + "\n"
        with self._lock, self._path.open("a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            if self._fsync:
                os.fsync(handle.fileno())
            self._count += 1

    @staticmethod
    def recover(path: Path | str) -> JournalRecovery:
        """Read an existing journal, tolerating a torn final line from a crash."""
        target = Path(path)
        if not target.is_file():
            return JournalRecovery(records=(), damaged_tail=False, path=target)
        records: list[dict[str, object]] = []
        damaged = False
        for line in target.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                # Only a torn tail is tolerable; damage mid-file is still
                # reported rather than silently dropped.
                damaged = True
        return JournalRecovery(records=tuple(records), damaged_tail=damaged, path=target)


@dataclass(frozen=True, slots=True)
class ShadowPredictionSnapshot:
    """Thread-safe loader status; policy consumers may use correlated evidence."""

    state: str
    reason: str
    artifact_id: str
    artifact_sha256: str
    shadow_predictions: int
    load_failures: int
    scoring_failures: int
    last_prediction_timestamp_ns: int | None
    decision_impact: str = "none"


class ObserveOnlyModelLoader:
    """Load the exact human-approved artifact and produce paper-policy evidence.

    ``state`` progresses through: ``UNLOADED`` (before the first refresh),
    ``NOT_APPROVED`` (no exact artifact currently approved),
    ``INVALID`` (registry, approval, or model evidence failed integrity checks),
    ``STALE`` (approved evidence is older than the configured immutable-evaluation limit),
    ``LOAD_FAILED`` (an approved artifact exists but could not be loaded/scored),
    ``SCORING`` (an exact-approved artifact is loaded and producing scored
    evidence for journaling and optional paper-policy consumption).
    Only ``SCORING`` produces predictions; every other state makes
    :meth:`score` a no-op.
    """

    def __init__(
        self,
        *,
        models_root: Path,
        model_approval_path: Path,
        journal: PredictionJournal,
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        max_artifact_age_days: int = DEFAULT_MAX_ARTIFACT_AGE_DAYS,
        current_date_provider: Callable[[], date] | None = None,
        outcome_tracker: object | None = None,
    ) -> None:
        """Create a loader bound to a registry root, approval file, and journal.

        ``outcome_tracker`` is optional and defaults to ``None`` so existing
        callers are unaffected. When attached (an
        ``app.machine_learning.outcome_journal.PendingOutcomeTracker``), every
        journaled prediction is also handed to
        ``outcome_tracker.register(...)`` (ML-004) so it can be causally
        resolved against the same triple-barrier rule offline training uses.
        """
        self._models_root = Path(models_root)
        self._model_approval_path = Path(model_approval_path)
        self._journal = journal
        self._outcome_tracker = outcome_tracker
        self._refresh_interval_seconds = refresh_interval_seconds
        if max_artifact_age_days < 0:
            raise ValueError("max_artifact_age_days must be non-negative")
        self._max_artifact_age_days = max_artifact_age_days
        self._current_date_provider = current_date_provider or (lambda: datetime.now(UTC).date())
        self._lock = threading.Lock()
        self._model: object | None = None
        self._feature_columns: tuple[str, ...] = ()
        self._loaded_artifact_id = ""
        self._loaded_artifact_sha256 = ""
        self._session_id = "unbound"
        self._state = "UNLOADED"
        self._reason = "no refresh has run yet"
        self._shadow_predictions = 0
        self._load_failures = 0
        self._scoring_failures = 0
        self._last_prediction_timestamp_ns: int | None = None
        self._prediction_counter = 0
        self._last_refresh_monotonic: float | None = None
        # Last scored probability per direction ("long"/"short") for the
        # paper-only engine to correlate with a decision. This cache does not
        # mutate broker state; callers must supply complete causal correlation.
        self._last_probability_by_direction: dict[str, ShadowProbabilityEvidence] = {}
        self.refresh()

    def bind_session(self, session_id: str) -> None:
        """Bind a recording session and invalidate prior-session score evidence."""
        with self._lock:
            if session_id != self._session_id:
                self._last_probability_by_direction.clear()
            self._session_id = session_id
        if self._outcome_tracker is not None:
            try:
                self._outcome_tracker.bind_session(session_id)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001,S110 - a broken tracker must not stop scoring
                pass

    def refresh(self) -> None:
        """Revalidate registry/approval truth and (re)load only an exact-approved artifact.

        Never raises: any registry, approval, or model-loading failure is
        captured as state/reason so a corrupt or tampered evidence graph
        disables scoring rather than crashing the analysis thread. Always
        runs immediately when called directly (e.g. from tests); use
        :meth:`score`, which throttles refreshes to
        ``refresh_interval_seconds``, on the hot ingest path.
        """
        self._last_refresh_monotonic = time.monotonic()
        try:
            from app.machine_learning.registry import (
                is_artifact_stale,
                read_model_approval,
                read_registry,
                validate_explicit_approval,
            )

            validation = validate_explicit_approval(
                read_registry(self._models_root),
                read_model_approval(self._model_approval_path),
            )
        except Exception as error:  # noqa: BLE001 - integrity boundary
            with self._lock:
                self._unload_locked()
                self._state = "INVALID"
                self._reason = f"{type(error).__name__}: {error}"
            return

        record = validation.record
        if validation.approval_state != "APPROVED_RUNTIME_DISABLED" or record is None:
            with self._lock:
                self._unload_locked()
                self._state = "INVALID" if validation.approval_state == "INVALID" else "NOT_APPROVED"
                self._reason = validation.detail
            return

        try:
            as_of_date = self._current_date_provider()
            if not isinstance(as_of_date, date):
                raise TypeError("current_date_provider must return datetime.date")
            stale = is_artifact_stale(
                record,
                as_of_date,
                self._max_artifact_age_days,
                models_root=self._models_root,
            )
        except Exception as error:  # noqa: BLE001 - staleness evidence is an integrity boundary
            with self._lock:
                self._unload_locked()
                self._state = "INVALID"
                self._reason = f"artifact staleness validation failed: {type(error).__name__}: {error}"
            return

        if stale:
            with self._lock:
                self._unload_locked()
                self._state = "STALE"
                self._reason = (
                    "approved artifact exceeds maximum immutable evaluation age "
                    f"of {self._max_artifact_age_days} days"
                )
            return

        with self._lock:
            already_loaded = (
                self._model is not None
                and self._loaded_artifact_id == record.artifact_id
                and self._loaded_artifact_sha256 == record.artifact_sha256
            )
            if already_loaded:
                self._state = "SCORING"
                self._reason = "exact-approved artifact loaded; scoring paper-only policy evidence"
                return

        try:
            from app.machine_learning.train import load_model_artifact

            artifact_path = self._models_root / record.artifact_path
            payload = load_model_artifact(artifact_path)
            model = payload["model"]
            if not hasattr(model, "predict_proba"):
                raise ValueError("model artifact does not support predict_proba")
            feature_columns = tuple(str(column) for column in payload["feature_columns"])
        except Exception as error:  # noqa: BLE001 - a broken artifact must not crash scoring
            with self._lock:
                self._unload_locked()
                self._load_failures += 1
                self._state = "LOAD_FAILED"
                self._reason = f"{type(error).__name__}: {error}"
            return

        with self._lock:
            self._model = model
            self._feature_columns = feature_columns
            self._loaded_artifact_id = record.artifact_id
            self._loaded_artifact_sha256 = record.artifact_sha256
            self._state = "SCORING"
            self._reason = "exact-approved artifact loaded; scoring paper-only policy evidence"

    def score(
        self,
        vector: FeatureVector,
        *,
        session_id: str,
        timestamp_ns: int,
        direction: str,
        entry_price: Decimal | None = None,
        target_ticks: Decimal | None = None,
        stop_ticks: Decimal | None = None,
        tick_size: Decimal | None = None,
    ) -> None:
        """Revalidate approval (throttled) then score one already-built feature vector.

        A no-op unless an exact-approved artifact is loaded. Any scoring
        failure is caught, counted, and never propagated: one bad vector must
        not stop analysis, matching ``AnalysisFeed``'s sink contract.

        ``entry_price``/``target_ticks``/``stop_ticks``/``tick_size`` are
        optional (default ``None``) so existing callers are unaffected. When
        all four are provided AND an outcome tracker is attached (ML-004),
        the scored prediction is also registered for causal outcome
        resolution.
        """
        with self._lock:
            due = (
                self._last_refresh_monotonic is None
                or time.monotonic() - self._last_refresh_monotonic >= self._refresh_interval_seconds
            )
        if due:
            self.refresh()
        with self._lock:
            if self._state != "SCORING" or self._model is None:
                return
            try:
                from app.machine_learning.train import build_feature_matrix

                matrix = build_feature_matrix((vector.as_record(),), self._feature_columns)
                probabilities = self._model.predict_proba(matrix)  # type: ignore[union-attr]
                probability = float(probabilities[0][1])
                if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
                    raise ValueError(
                        "predict_proba returned a non-finite or out-of-range probability"
                    )
            except Exception as error:  # noqa: BLE001 - one bad vector must not stop scoring
                self._scoring_failures += 1
                self._reason = f"scoring error: {type(error).__name__}: {error}"
                return

            self._prediction_counter += 1
            prediction = ShadowPrediction(
                prediction_id=f"{self._loaded_artifact_id}-{self._prediction_counter:012d}",
                artifact_id=self._loaded_artifact_id,
                artifact_sha256=self._loaded_artifact_sha256,
                session_id=session_id,
                timestamp_ns=timestamp_ns,
                direction=direction,
                feature_vector_sha256=hashlib.sha256(vector.canonical_bytes()).hexdigest(),
                success_probability=probability,
            )

        # Journal I/O runs outside this loader's lock: it has its own lock and
        # can be comparatively slow (fsync). Holding this lock across disk I/O
        # would block refresh()/snapshot() readers for no reason.
        try:
            self._journal.append(prediction)
        except Exception as error:  # noqa: BLE001 - a full disk must not stop scoring
            with self._lock:
                self._scoring_failures += 1
                self._reason = f"journal write failed: {type(error).__name__}: {error}"
            return

        with self._lock:
            self._last_probability_by_direction[direction] = ShadowProbabilityEvidence(
                prediction_id=prediction.prediction_id,
                artifact_id=prediction.artifact_id,
                artifact_sha256=prediction.artifact_sha256,
                session_id=prediction.session_id,
                direction=prediction.direction,
                timestamp_ns=prediction.timestamp_ns,
                success_probability=prediction.success_probability,
            )
            self._shadow_predictions += 1
            self._last_prediction_timestamp_ns = timestamp_ns

        if (
            self._outcome_tracker is not None
            and entry_price is not None
            and target_ticks is not None
            and stop_ticks is not None
            and tick_size is not None
        ):
            try:
                self._outcome_tracker.register(  # type: ignore[attr-defined]
                    prediction,
                    entry_price=entry_price,
                    direction=direction,
                    target_ticks=target_ticks,
                    stop_ticks=stop_ticks,
                    tick_size=tick_size,
                )
            except Exception:  # noqa: BLE001,S110 - a broken tracker must not stop scoring
                pass

    def snapshot(self) -> ShadowPredictionSnapshot:
        """Return immutable status safe for GUI/status publication threads."""
        with self._lock:
            return ShadowPredictionSnapshot(
                state=self._state,
                reason=self._reason,
                artifact_id=self._loaded_artifact_id,
                artifact_sha256=self._loaded_artifact_sha256,
                shadow_predictions=self._shadow_predictions,
                load_failures=self._load_failures,
                scoring_failures=self._scoring_failures,
                last_prediction_timestamp_ns=self._last_prediction_timestamp_ns,
            )

    def last_probability(
        self,
        direction: str,
        *,
        session_id: str | None = None,
        as_of_timestamp_ns: int | None = None,
        max_age_ns: int | None = None,
    ) -> ShadowProbabilityEvidence | None:
        """Return fresh direction-specific score evidence, or ``None``.

        Callers that may affect a decision must supply ``session_id``, the
        decision event's ``as_of_timestamp_ns``, and a non-negative
        ``max_age_ns``. Omitting all three remains an observe-only inspection
        API; partial correlation arguments are rejected so stale evidence
        cannot accidentally look current.
        """
        correlation = (session_id, as_of_timestamp_ns, max_age_ns)
        if any(value is not None for value in correlation):
            if any(value is None for value in correlation):
                raise ValueError(
                    "session_id, as_of_timestamp_ns, and max_age_ns must be supplied together"
                )
            if session_id is None or as_of_timestamp_ns is None or max_age_ns is None:
                raise AssertionError("complete correlation parameters expected")
            if max_age_ns < 0:
                raise ValueError("max_age_ns must be non-negative")
        with self._lock:
            evidence = self._last_probability_by_direction.get(direction)
            if evidence is None or session_id is None:
                return evidence
            if session_id != self._session_id or session_id != evidence.session_id:
                return None
            if evidence.direction != direction:
                return None
            if (
                evidence.artifact_id != self._loaded_artifact_id
                or evidence.artifact_sha256 != self._loaded_artifact_sha256
            ):
                return None
            if as_of_timestamp_ns is None or max_age_ns is None:
                raise AssertionError("complete correlation parameters expected")
            age_ns = as_of_timestamp_ns - evidence.timestamp_ns
            if age_ns < 0 or age_ns > max_age_ns:
                return None
            return evidence

    def _unload_locked(self) -> None:
        """Clear the model and any score evidence. Caller holds ``self._lock``."""
        self._model = None
        self._feature_columns = ()
        self._loaded_artifact_id = ""
        self._loaded_artifact_sha256 = ""
        self._last_probability_by_direction.clear()
