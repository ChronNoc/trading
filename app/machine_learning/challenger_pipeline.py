"""Reproducible, catalog-gated dataset construction for pooled challengers.

This module deliberately stops before runtime loading or prediction. It turns
only finalized sessions that pass the strict model-training catalog gate into a
content-addressed dataset with enough provenance to reproduce or reject it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Mapping

from app.machine_learning.feature_contract import (
    FEATURE_COLUMNS,
    FEATURE_CONTRACT_VERSION,
    feature_contract_descriptor,
    feature_contract_sha256,
)
from app.machine_learning.session_training import (
    SessionTrainingConfig,
    build_session_training_rows,
)
from app.research.session_catalog import SessionEntry, build_catalog

DATASET_SCHEMA_VERSION = 1
LABEL_CONTRACT_VERSION = "causal-triple-barrier-v2-resolution-provenance"


@dataclass(frozen=True, slots=True)
class DatasetBuildConfig:
    """Inputs that materially define one deterministic pooled dataset."""

    training: SessionTrainingConfig = SessionTrainingConfig()

    def to_json_dict(self) -> dict[str, object]:
        cfg = self.training
        return {
            "target_ticks": str(cfg.target_ticks),
            "stop_ticks": str(cfg.stop_ticks),
            "horizon_seconds": cfg.horizon_seconds,
            "sample_interval_seconds": cfg.sample_interval_seconds,
            "window_span_seconds": cfg.window_span_seconds,
            "sample_interval_ms": cfg.sample_interval_ms,
            "warmup_seconds": cfg.warmup_seconds,
            "tick_size": str(cfg.tick_size),
            "directions": list(cfg.directions),
        }


@dataclass(frozen=True, slots=True)
class SourceSessionRecord:
    """Immutable source evidence for one included session."""

    session_id: str
    manifest_path: str
    manifest_sha256: str
    input_files: tuple[dict[str, object], ...]
    rows: int
    wins: int
    losses: int
    dropped_incomplete: int
    dropped_invalid_features: int


@dataclass(frozen=True, slots=True)
class ExcludedSessionRecord:
    """One catalogued session rejected before dataset construction."""

    session_id: str
    manifest_path: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DatasetBuildResult:
    """A deterministic dataset and its machine-readable provenance."""

    dataset_id: str
    rows: tuple[dict[str, object], ...]
    manifest: dict[str, object]
    dataset_path: Path | None = None
    manifest_path: Path | None = None


def build_challenger_dataset(
    raw_root: Path,
    *,
    output_root: Path | None = None,
    config: DatasetBuildConfig | None = None,
) -> DatasetBuildResult:
    """Build a deterministic dataset from strictly eligible recorded sessions.

    Source files are hashed before and after row construction. Any mutation
    aborts the build, so an active or externally modified session can never be
    published under misleading provenance.
    """
    build_config = config or DatasetBuildConfig()
    build_config.training.validate()
    feature_descriptor = feature_contract_descriptor(
        window_span_seconds=build_config.training.window_span_seconds,
        sample_interval_ms=build_config.training.sample_interval_ms,
        tick_size=build_config.training.tick_size,
    )
    feature_hash = feature_contract_sha256(
        window_span_seconds=build_config.training.window_span_seconds,
        sample_interval_ms=build_config.training.sample_interval_ms,
        tick_size=build_config.training.tick_size,
    )

    included: list[SourceSessionRecord] = []
    excluded: list[ExcludedSessionRecord] = []
    pooled_rows: list[dict[str, object]] = []

    entries = sorted(
        build_catalog(raw_root),
        key=lambda entry: (entry.utc_start or "", entry.session_id, str(entry.manifest_path)),
    )
    for entry in entries:
        if not entry.eligible_for_model_training:
            excluded.append(_excluded(entry, raw_root=raw_root))
            continue

        session_dir = entry.manifest_path.parent
        source_paths = _source_paths(session_dir, entry.manifest_path)
        before = _hash_paths(source_paths, base=raw_root)
        rows, summary = build_session_training_rows(
            session_dir,
            config=build_config.training,
        )
        after_paths = _source_paths(session_dir, entry.manifest_path)
        after = _hash_paths(after_paths, base=raw_root)
        if source_paths != after_paths or before != after:
            raise RuntimeError(f"source session changed during dataset build: {entry.session_id}")
        if not rows:
            excluded.append(
                ExcludedSessionRecord(
                    session_id=entry.session_id,
                    manifest_path=_relative(entry.manifest_path, raw_root),
                    reasons=(summary.note or "session produced no completed labels",),
                )
            )
            continue

        stable_rows = _annotate_rows(rows, entry.session_id)
        pooled_rows.extend(stable_rows)
        manifest_hash = next(
            str(record["sha256"])
            for record in before
            if str(record["path"]) == _relative(entry.manifest_path, raw_root)
        )
        included.append(
            SourceSessionRecord(
                session_id=entry.session_id,
                manifest_path=_relative(entry.manifest_path, raw_root),
                manifest_sha256=manifest_hash,
                input_files=tuple(before),
                rows=summary.rows,
                wins=summary.wins,
                losses=summary.losses,
                dropped_incomplete=summary.dropped_incomplete,
                dropped_invalid_features=summary.dropped_invalid_features,
            )
        )

    ordered_rows = tuple(sorted(pooled_rows, key=_row_key))
    rows_payload = b"".join(_canonical_json(row) + b"\n" for row in ordered_rows)
    rows_sha256 = _sha256_bytes(rows_payload)
    labels = [int(str(row["label"])) for row in ordered_rows]
    timestamps = [int(str(row["timestamp_ns"])) for row in ordered_rows]

    core: dict[str, object] = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_columns": list(FEATURE_COLUMNS),
        "feature_contract": feature_descriptor,
        "feature_contract_sha256": feature_hash,
        "label_contract_version": LABEL_CONTRACT_VERSION,
        "label_config": build_config.to_json_dict(),
        "included_sessions": [asdict(record) for record in included],
        "excluded_sessions": [asdict(record) for record in excluded],
        "row_count": len(ordered_rows),
        "positive_labels": sum(labels),
        "negative_labels": len(labels) - sum(labels),
        "minimum_timestamp_ns": min(timestamps) if timestamps else None,
        "maximum_timestamp_ns": max(timestamps) if timestamps else None,
        "rows_sha256": rows_sha256,
    }
    dataset_id = "dataset-" + _sha256_bytes(_canonical_json(core))[:24]
    manifest = {"dataset_id": dataset_id, **core}

    dataset_path: Path | None = None
    manifest_path: Path | None = None
    if output_root is not None:
        destination = output_root / "datasets" / dataset_id
        dataset_path = destination / "dataset.jsonl"
        manifest_path = destination / "dataset_manifest.json"
        dataset_text = rows_payload.decode("utf-8")
        manifest_text = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        _publish_identical_or_new(dataset_path, dataset_text)
        _publish_identical_or_new(manifest_path, manifest_text)

    return DatasetBuildResult(
        dataset_id=dataset_id,
        rows=ordered_rows,
        manifest=manifest,
        dataset_path=dataset_path,
        manifest_path=manifest_path,
    )


def build_validated_challenger(
    raw_root: Path,
    models_root: Path,
    *,
    config: DatasetBuildConfig | None = None,
    model_version: str = "0.1.0",
    target_ticks: float = 12.0,
    stop_ticks: float = 8.0,
    cost_ticks: float = 2.0,
    min_train_days: int = 3,
    min_oos_predictions: int = 50,
) -> dict[str, object]:
    """Build, validate, package, and register one offline-only challenger.

    A failed gate writes an attempt report and returns without publishing a
    model. Passing creates one immutable logistic-regression bundle. Nothing in
    this function approves or loads the model at runtime.
    """
    from joblib import dump
    from sklearn.linear_model import LogisticRegression

    from app.machine_learning.pooled_training import walk_forward_evaluate
    from app.machine_learning.registry import (
        ModelRegistryRecord,
        register_challenger,
        sha256_file,
    )
    from app.machine_learning.train import (
        FEATURE_COLUMNS as TRAIN_FEATURE_COLUMNS,
        build_feature_matrix,
        build_label_array,
        load_model_artifact,
    )

    build_config = config or DatasetBuildConfig()
    if target_ticks != float(build_config.training.target_ticks):
        raise ValueError("validation target_ticks must match the dataset label contract")
    if stop_ticks != float(build_config.training.stop_ticks):
        raise ValueError("validation stop_ticks must match the dataset label contract")
    if min_train_days < 1 or min_oos_predictions < 1:
        raise ValueError("walk-forward minimums must be positive")
    if cost_ticks < 0:
        raise ValueError("cost_ticks must be non-negative")
    dataset = build_challenger_dataset(raw_root, output_root=models_root, config=build_config)
    validation = walk_forward_evaluate(
        list(dataset.rows), target_ticks=target_ticks, stop_ticks=stop_ticks,
        cost_ticks=cost_ticks, min_train_days=min_train_days,
    )
    gates = {
        "has_rows": bool(dataset.rows),
        "both_label_classes": (
            int(str(dataset.manifest["positive_labels"])) > 0
            and int(str(dataset.manifest["negative_labels"])) > 0
        ),
        "minimum_oos_predictions": validation.oos_predictions >= min_oos_predictions,
        "minimum_evaluated_days": validation.evaluated_days >= min_train_days,
        "positive_incremental_expectancy": validation.beats_baseline,
    }
    validation_state = "PASSED" if all(gates.values()) else "REJECTED"
    validation_payload: dict[str, object] = {
        **validation.to_json(),
        "target_ticks": str(target_ticks),
        "stop_ticks": str(stop_ticks),
        "cost_ticks": str(cost_ticks),
        "minimum_train_days": min_train_days,
        "minimum_oos_predictions": min_oos_predictions,
        "gates": gates,
        "validation_state": validation_state,
        "scope": "offline walk-forward only; no runtime loading or decision impact",
    }
    attempt_core = {
        "dataset_id": dataset.dataset_id,
        "validation": validation_payload,
        "model_type": "logistic_regression",
        "model_version": model_version,
    }
    attempt_id = "attempt-" + _sha256_bytes(_canonical_json(attempt_core))[:24]
    attempt_path = models_root / "attempts" / f"{attempt_id}.json"
    _publish_identical_or_new(
        attempt_path,
        json.dumps({"attempt_id": attempt_id, **attempt_core}, indent=2, sort_keys=True) + "\n",
    )
    if validation_state != "PASSED":
        return {
            "status": "rejected",
            "attempt_id": attempt_id,
            "attempt_path": str(attempt_path),
            "dataset_id": dataset.dataset_id,
            "validation": validation_payload,
        }

    features = build_feature_matrix(dataset.rows)
    labels = build_label_array(dataset.rows)
    model = LogisticRegression(max_iter=1_000, solver="liblinear", random_state=7)
    model.fit(features, labels)

    staging = models_root / ".staging" / attempt_id
    staging.mkdir(parents=True, exist_ok=True)
    model_path = staging / "model.joblib"
    dump({
        "model": model,
        "model_type": "logistic_regression",
        "version": model_version,
        "feature_columns": tuple(TRAIN_FEATURE_COLUMNS),
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": str(dataset.manifest["feature_contract_sha256"]),
    }, model_path)
    loaded = load_model_artifact(model_path)
    smoke_probability = float(loaded["model"].predict_proba(features[:1])[0][1])  # type: ignore[union-attr]
    model_sha256 = sha256_file(model_path)
    artifact_core = {
        "dataset_id": dataset.dataset_id,
        "dataset_sha256": str(dataset.manifest["rows_sha256"]),
        "model_sha256": model_sha256,
        "model_type": "logistic_regression",
        "model_version": model_version,
        "feature_contract_version": FEATURE_CONTRACT_VERSION,
        "feature_contract_sha256": str(dataset.manifest["feature_contract_sha256"]),
        "validation": validation_payload,
        "smoke_probability": smoke_probability,
        "runtime_loaded": False,
        "shadow_predictions": 0,
        "decision_impact": "none",
    }
    artifact_id = "challenger-" + _sha256_bytes(_canonical_json(artifact_core))[:24]
    destination = models_root / "challengers" / artifact_id
    destination.mkdir(parents=True, exist_ok=True)
    final_model_path = destination / "model.joblib"
    if final_model_path.exists():
        if sha256_file(final_model_path) != model_sha256:
            raise FileExistsError(f"immutable challenger model differs: {final_model_path}")
    else:
        from app.database.recorder import replace_with_retry, unique_temp_path

        temporary_model = unique_temp_path(final_model_path, ".tmp")
        temporary_model.parent.mkdir(parents=True, exist_ok=True)
        try:
            temporary_model.write_bytes(model_path.read_bytes())
            if sha256_file(temporary_model) != model_sha256:
                raise ValueError("staged challenger model SHA changed before publication")
            replace_with_retry(temporary_model, final_model_path)
        finally:
            temporary_model.unlink(missing_ok=True)
    validation_path = destination / "validation.json"
    bundle_path = destination / "bundle_manifest.json"
    _publish_identical_or_new(
        validation_path, json.dumps(validation_payload, indent=2, sort_keys=True) + "\n",
    )
    bundle = {"artifact_id": artifact_id, **artifact_core}
    _publish_identical_or_new(
        bundle_path, json.dumps(bundle, indent=2, sort_keys=True) + "\n",
    )
    included_manifest = dataset.manifest["included_sessions"]
    excluded_manifest = dataset.manifest["excluded_sessions"]
    if not isinstance(included_manifest, list) or not isinstance(excluded_manifest, list):
        raise ValueError("dataset manifest session lists are malformed")
    record = ModelRegistryRecord(
        artifact_id=artifact_id,
        artifact_sha256=model_sha256,
        dataset_id=dataset.dataset_id,
        dataset_sha256=str(dataset.manifest["rows_sha256"]),
        model_type="logistic_regression",
        model_version=model_version,
        feature_contract_sha256=str(dataset.manifest["feature_contract_sha256"]),
        included_sessions=len(included_manifest),
        excluded_sessions=len(excluded_manifest),
        validation_state="PASSED",
        validation_detail=validation.note,
        oos_predictions=validation.oos_predictions,
        brier_score=validation.brier_score,
        beats_baseline=validation.beats_baseline,
        artifact_path=final_model_path.relative_to(models_root).as_posix(),
    )
    registry_path = register_challenger(models_root, record)
    return {
        "status": "challenger",
        "attempt_id": attempt_id,
        "artifact_id": artifact_id,
        "artifact_sha256": model_sha256,
        "dataset_id": dataset.dataset_id,
        "model_path": str(final_model_path),
        "registry_path": str(registry_path),
        "validation": validation_payload,
    }


def _excluded(entry: SessionEntry, *, raw_root: Path) -> ExcludedSessionRecord:
    """Return deterministic exclusion evidence independent of raw-root location."""
    reasons = entry.model_training_reasons or ("session did not pass model-training eligibility",)
    return ExcludedSessionRecord(
        session_id=entry.session_id,
        manifest_path=_relative(entry.manifest_path, raw_root),
        reasons=tuple(reasons),
    )


def _source_paths(session_dir: Path, manifest_path: Path) -> tuple[Path, ...]:
    paths = [manifest_path]
    for stem, parts_name in (("depth", "depth_parts"), ("trades", "trade_parts")):
        compacted = session_dir / f"{stem}.parquet"
        if compacted.is_file():
            paths.append(compacted)
            continue
        parts = sorted((session_dir / parts_name).glob("part-*.parquet"))
        if not parts:
            raise ValueError(f"eligible session has no closed {stem} input: {session_dir}")
        paths.extend(parts)
    return tuple(paths)


def _hash_paths(paths: tuple[Path, ...], *, base: Path) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for path in sorted(paths, key=lambda item: str(item)):
        stat = path.stat()
        records.append(
            {
                "path": _relative(path, base),
                "size_bytes": stat.st_size,
                "sha256": _sha256_file(path),
            }
        )
    return records


def _annotate_rows(
    rows: list[dict[str, object]],
    session_id: str,
) -> list[dict[str, object]]:
    annotated: list[dict[str, object]] = []
    for index, row in enumerate(rows):
        item = dict(row)
        item["source_session_id"] = session_id
        item["source_row_index"] = index
        annotated.append(item)
    return annotated


def _row_key(row: Mapping[str, object]) -> tuple[int, str, str, int]:
    return (
        int(str(row["timestamp_ns"])),
        str(row["source_session_id"]),
        str(row.get("direction", "")),
        int(str(row["source_row_index"])),
    )


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _relative(path: Path, base: Path) -> str:
    try:
        return path.resolve().relative_to(base.resolve()).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _publish_identical_or_new(path: Path, payload: str) -> None:
    encoded = payload.encode("utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise FileExistsError(f"immutable dataset path already differs: {path}")
        return
    from app.database.recorder import replace_with_retry, unique_temp_path

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = unique_temp_path(path, ".tmp")
    try:
        temporary.write_bytes(encoded)
        replace_with_retry(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
