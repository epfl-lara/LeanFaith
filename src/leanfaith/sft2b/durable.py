"""Atomic caches, append-only journals, and deterministic SFT2B compaction."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.sft2b.schemas import JournalEvent, stable_id


class DurableStoreError(RuntimeError):
    """Raised when an immutable cache or journal has conflicting content."""


def atomic_write(path: Path, payload: bytes) -> None:
    """Durably replace one exact file without following a target symlink."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise DurableStoreError(f"refusing symlink target: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def immutable_write(path: Path, payload: bytes) -> None:
    """Create an immutable-by-contract artifact or verify identical replay."""

    if path.exists():
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise DurableStoreError(f"immutable artifact conflicts: {path}")
        return
    atomic_write(path, payload)


def model_bytes(model: BaseModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def write_model(path: Path, model: BaseModel) -> None:
    immutable_write(path, model_bytes(model))


def read_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT:
    value = json.loads(path.read_text(encoding="utf-8"))
    return model_type.model_validate(value)


def write_json(path: Path, value: object) -> None:
    immutable_write(path, canonical_json_bytes(value) + b"\n")


def write_jsonl(path: Path, rows: list[object]) -> None:
    payload = b"".join(canonical_json_bytes(row) + b"\n" for row in rows)
    immutable_write(path, payload)


class AppendOnlyJournal:
    """Locked JSONL journal with terminal-key duplicate suppression."""

    def __init__(self, path: Path, *, run_id: str, source_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.source_id = source_id
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    def _events(self) -> list[JournalEvent]:
        if not self.path.exists():
            return []
        events: list[JournalEvent] = []
        with self.path.open(encoding="utf-8") as handle:
            for number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    events.append(JournalEvent.model_validate_json(line))
                except Exception as exc:
                    raise DurableStoreError(
                        f"invalid journal event at {self.path}:{number}: {exc}"
                    ) from exc
        if [event.sequence for event in events] != list(range(len(events))):
            raise DurableStoreError("journal sequence is not contiguous")
        if any(
            event.run_id != self.run_id or event.source_id != self.source_id for event in events
        ):
            raise DurableStoreError("journal contains a different run/source identity")
        keys = [event.terminal_key for event in events]
        if len(keys) != len(set(keys)):
            raise DurableStoreError("journal contains duplicate terminal keys")
        return events

    def append(
        self,
        *,
        stage: str,
        terminal_key: str,
        artifact_path: Path,
        candidate_id: str | None = None,
    ) -> bool:
        """Append once; return False when the terminal key already exists."""

        if not artifact_path.is_file():
            raise DurableStoreError(f"journal artifact does not exist: {artifact_path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                events = self._events()
                existing = {event.terminal_key: event for event in events}
                if terminal_key in existing:
                    prior = existing[terminal_key]
                    if prior.artifact_sha256 != hash_file(artifact_path):
                        raise DurableStoreError(
                            f"terminal replay points to changed artifact: {terminal_key}"
                        )
                    return False
                payload = {
                    "run_id": self.run_id,
                    "source_id": self.source_id,
                    "candidate_id": candidate_id,
                    "stage": stage,
                    "terminal_key": terminal_key,
                    "artifact_path": str(artifact_path),
                    "artifact_sha256": hash_file(artifact_path),
                }
                event = JournalEvent(
                    event_id=stable_id("sft2b_event", payload),
                    sequence=len(events),
                    run_id=self.run_id,
                    source_id=self.source_id,
                    candidate_id=candidate_id,
                    stage=stage,  # type: ignore[arg-type]
                    terminal_key=terminal_key,
                    artifact_path=str(artifact_path),
                    artifact_sha256=hash_file(artifact_path),
                )
                with self.path.open("ab") as handle:
                    handle.write(model_bytes(event))
                    handle.flush()
                    os.fsync(handle.fileno())
                return True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def events(self) -> tuple[JournalEvent, ...]:
        return tuple(self._events())
