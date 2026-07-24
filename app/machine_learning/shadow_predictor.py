"""Observe-only shadow model loader: exact-approval scoring, zero decision effect.

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
:class:`PredictionJournal` as memory/disk evidence only. This module has no
import of, or dependency on, ``app.strategy``, ``app.paper``, ``app.risk``,
``app.execution``, or any broker code — a scored probability can be observed
and audited, but it cannot reach a trading decision.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.machine_learning.feature_contract import FeatureVector

PREDICTION_JOURNAL_SCHEMA_VERSION = 1
# Full registry/approval revalidation re-hashes model, dataset, and bundle
# bytes end-to-end (see app.machine_learning.registry.read_registry) - too
# costly to repeat on every scored vector. Approval changes are rare and
# human-driven, so a bounded cadence is safe; explicit refresh() calls (e.g.
# in tests) always run immediately regardless of this interval.
DEFAULT_REFRESH_INTERVAL_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ShadowPrediction:
    """One observe-only prediction record. Carries no decision effect."""

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
        """Return the JSON record. ``decision_impact`` is structural, not stored state."""
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
    """Append-only JSONL sink for observe-only shadow predictions.

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
    """Thread-safe truth about the observe-only loader; never a decision."""

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
    """Load the exact human-approved artifact and score vectors; no decision effect.

    ``state`` progresses through: ``UNLOADED`` (before the first refresh),
    ``NOT_APPROVED`` (no exact artifact currently approved),
    ``INVALID`` (registry, approval, or model evidence failed integrity checks),
    ``LOAD_FAILED`` (an approved artifact exists but could not be loaded/scored),
    ``SCORING`` (an exact-approved artifact is loaded and scoring observe-only
    evidence). Only ``SCORING`` produces predictions; every other state makes
    :meth:`score` a no-op.
    """

    def __init__(
        self,
        *,
        models_root: Path,
        model_approval_path: Path,
        journal: PredictionJournal,
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        """Create a loader bound to a registry root, approval file, and journal."""
        self._models_root = Path(models_root)
        self._model_approval_path = Path(model_approval_path)
        self._journal = journal
        self._refresh_interval_seconds = refresh_interval_seconds
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
        self.refresh()

    def bind_session(self, session_id: str) -> None:
        """Record the active recording session; scoring needs no session-scoped state."""
        with self._lock:
            self._session_id = session_id

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

        with self._lock:
            already_loaded = (
                self._model is not None
                and self._loaded_artifact_id == record.artifact_id
                and self._loaded_artifact_sha256 == record.artifact_sha256
            )
            if already_loaded:
                self._state = "SCORING"
                self._reason = "exact-approved artifact loaded; scoring is observe-only evidence"
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
            self._reason = "exact-approved artifact loaded; scoring is observe-only evidence"

    def score(
        self,
        vector: FeatureVector,
        *,
        session_id: str,
        timestamp_ns: int,
        direction: str,
    ) -> None:
        """Revalidate approval (throttled) then score one already-built feature vector.

        A no-op unless an exact-approved artifact is loaded. Any scoring
        failure is caught, counted, and never propagated: one bad vector must
        not stop analysis, matching ``AnalysisFeed``'s sink contract.
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
            self._shadow_predictions += 1
            self._last_prediction_timestamp_ns = timestamp_ns

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

    def _unload_locked(self) -> None:
        """Clear any loaded model. Caller must hold ``self._lock``."""
        self._model = None
        self._feature_columns = ()
        self._loaded_artifact_id = ""
        self._loaded_artifact_sha256 = ""
