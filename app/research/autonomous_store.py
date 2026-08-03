"""Crash-safe persistence for autonomous intelligence evidence."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import secrets
import time
from typing import Any, Mapping

from app.research.autonomous_models import CandidateRecord, canonical_json


@dataclass(frozen=True, slots=True)
class Lease:
    """Fenced lease proving which worker may publish a candidate update."""

    candidate_id: str
    owner: str
    token: str
    acquired_at_ns: int
    heartbeat_at_ns: int

    def to_dict(self) -> dict[str, str | int]:
        """Return a JSON-safe lease representation."""
        return {
            "candidate_id": self.candidate_id,
            "owner": self.owner,
            "token": self.token,
            "acquired_at_ns": self.acquired_at_ns,
            "heartbeat_at_ns": self.heartbeat_at_ns,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Lease":
        """Decode a persisted lease."""
        return cls(
            candidate_id=str(payload["candidate_id"]),
            owner=str(payload["owner"]),
            token=str(payload["token"]),
            acquired_at_ns=int(payload["acquired_at_ns"]),
            heartbeat_at_ns=int(payload["heartbeat_at_ns"]),
        )


class AutonomousStore:
    """Versioned records, fenced leases, checkpoints, and append-only activity."""

    def __init__(self, root: str | Path, *, lease_seconds: float = 120.0) -> None:
        self.root = Path(root)
        self.candidates_dir = self.root / "candidates"
        self.leases_dir = self.root / "leases"
        self.checkpoint_path = self.root / "checkpoint.json"
        self.activity_path = self.root / "activity.jsonl"
        self.lease_ns = int(lease_seconds * 1_000_000_000)
        if self.lease_ns <= 0:
            raise ValueError("lease_seconds must be positive")
        self.candidates_dir.mkdir(parents=True, exist_ok=True)
        self.leases_dir.mkdir(parents=True, exist_ok=True)

    def candidate_path(self, candidate_id: str) -> Path:
        """Return the canonical path for a candidate record."""
        self._validate_identifier(candidate_id)
        return self.candidates_dir / f"{candidate_id}.json"

    def publish(self, record: CandidateRecord, *, lease: Lease | None = None) -> Path:
        """Atomically publish evidence after validating identity and fencing."""
        if lease is not None:
            self.assert_lease(lease)
            if lease.candidate_id != record.candidate_id:
                raise PermissionError("lease does not belong to candidate")
        path = self.candidate_path(record.candidate_id)
        payload = canonical_json(record.to_dict()) + "\n"
        if path.is_file() and path.read_text(encoding="utf-8") == payload:
            return path
        temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
        temp.write_text(payload, encoding="utf-8")
        try:
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
        return path

    def load(self, candidate_id: str) -> CandidateRecord | None:
        """Load and validate one record, returning None when absent."""
        path = self.candidate_path(candidate_id)
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("candidate record must be a JSON object")
        return CandidateRecord.from_dict(payload)

    def list_candidates(self) -> tuple[CandidateRecord, ...]:
        """Load all valid candidate records in deterministic order."""
        records = []
        for path in sorted(self.candidates_dir.glob("*.json")):
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError(f"candidate record is not an object: {path.name}")
            records.append(CandidateRecord.from_dict(payload))
        return tuple(records)

    def try_acquire(self, candidate_id: str, owner: str, *, now_ns: int | None = None) -> Lease | None:
        """Atomically acquire a candidate lease or recover one that is stale."""
        self._validate_identifier(candidate_id)
        if not owner.strip():
            raise ValueError("lease owner is required")
        now = time.time_ns() if now_ns is None else now_ns
        path = self.leases_dir / f"{candidate_id}.lease"
        current = self._read_lease(path)
        if current is not None and now - current.heartbeat_at_ns <= self.lease_ns:
            return None
        if current is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        lease = Lease(candidate_id, owner, secrets.token_hex(16), now, now)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return None
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(canonical_json(lease.to_dict()) + "\n")
        return lease

    def heartbeat(self, lease: Lease, *, now_ns: int | None = None) -> Lease:
        """Refresh a lease while retaining its fencing token."""
        self.assert_lease(lease)
        now = time.time_ns() if now_ns is None else now_ns
        refreshed = Lease(lease.candidate_id, lease.owner, lease.token, lease.acquired_at_ns, now)
        path = self.leases_dir / f"{lease.candidate_id}.lease"
        self._atomic_json(path, refreshed.to_dict())
        return refreshed

    def assert_lease(self, lease: Lease) -> None:
        """Reject stale owners whose fencing token no longer owns the lease."""
        path = self.leases_dir / f"{lease.candidate_id}.lease"
        current = self._read_lease(path)
        if current is None or current.token != lease.token or current.owner != lease.owner:
            raise PermissionError("candidate lease is missing or fenced")

    def release(self, lease: Lease) -> None:
        """Release a lease only when its fencing token still owns it."""
        self.assert_lease(lease)
        (self.leases_dir / f"{lease.candidate_id}.lease").unlink(missing_ok=True)

    def mark_complete(self, candidate_id: str) -> None:
        """Checkpoint only a candidate whose evidence is already durable."""
        if not self.candidate_path(candidate_id).is_file():
            raise FileNotFoundError("candidate evidence must exist before checkpoint completion")
        completed = self.completed_ids()
        completed.add(candidate_id)
        self._atomic_json(self.checkpoint_path, {"schema_version": 1, "completed": sorted(completed)})

    def completed_ids(self) -> set[str]:
        """Return checkpointed candidate IDs, tolerating absent state."""
        if not self.checkpoint_path.is_file():
            return set()
        try:
            payload = json.loads(self.checkpoint_path.read_text(encoding="utf-8"))
            return {str(value) for value in payload.get("completed", ())}
        except (json.JSONDecodeError, OSError, AttributeError):
            return set()

    def append_activity(self, event: Mapping[str, Any]) -> None:
        """Append one durable activity event without rewriting prior history."""
        self.activity_path.parent.mkdir(parents=True, exist_ok=True)
        line = canonical_json(dict(event)) + "\n"
        fd = os.open(self.activity_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def attempt_count_for_utc_day(self, now_ns: int) -> int:
        """Count durable gate-attempt starts for the UTC day containing now_ns."""
        day_ns = 86_400_000_000_000
        day_start_ns = (now_ns // day_ns) * day_ns
        day_end_ns = day_start_ns + day_ns
        try:
            lines = self.activity_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return 0
        count = 0
        for line in lines:
            try:
                event = json.loads(line)
                recorded_at_ns = int(event.get("recorded_at_ns", -1))
            except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
                continue
            if event.get("event") == "gate_attempt_started" and day_start_ns <= recorded_at_ns < day_end_ns:
                count += 1
        return count

    def disk_usage_bytes(self) -> int:
        """Return total autonomous-state disk use for quota enforcement."""
        total = 0
        for path in self.root.rglob("*"):
            if path.is_file():
                total += path.stat().st_size
        return total

    def _read_lease(self, path: Path) -> Lease | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return Lease.from_dict(payload)
        except (FileNotFoundError, json.JSONDecodeError, OSError, KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _validate_identifier(value: str) -> None:
        if not value or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("candidate_id must be a lowercase hexadecimal digest")

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp")
        temp.write_text(canonical_json(payload) + "\n", encoding="utf-8")
        try:
            temp.replace(path)
        finally:
            temp.unlink(missing_ok=True)
