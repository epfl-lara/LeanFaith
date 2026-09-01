"""Append-only run journal and cross-run semantic cache for the sprint runner."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical

PROVENANCE_FIELDS = frozenset({"render", "process_request_hash", "elapsed_ms", "engine"})


class StoreError(RuntimeError):
    """Raised on malformed durable state."""


def write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.partial")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def read_json_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise StoreError(f"expected JSON object at {path}")
    return value


class Journal:
    """Append-only JSONL journal with fsync per append."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, record: Mapping[str, object]) -> None:
        line = canonical_json_bytes(record) + b"\n"
        with self.path.open("ab") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())

    def append_many(self, records: Sequence[Mapping[str, object]]) -> None:
        if not records:
            return
        body = b"".join(canonical_json_bytes(record) + b"\n" for record in records)
        with self.path.open("ab") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.is_file():
            return
        with self.path.open("rb") as handle:
            for raw in handle:
                raw = raw.rstrip(b"\n")
                if not raw:
                    continue
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    # A torn final line from an interrupted append is ignored; every
                    # complete record before it remains authoritative.
                    continue
                if isinstance(value, dict):
                    yield value


class SemanticCache:
    """Content-addressed root and operation records shared across runs.

    Root records are keyed by project revision, Lean version, import/options
    context, engine semantic version, and root name.  Operation records are
    keyed by the root's structural closed-Expr hash, operation ID, engine
    semantic version, Lean/project revision, and import/options context, as
    the sprint contract requires.  Runner and config bytes are provenance only.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        (self.root / "roots").mkdir(parents=True, exist_ok=True)
        (self.root / "ops").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def root_key(
        *,
        project_revision: str,
        lean_version: str,
        import_options_fingerprint: str,
        engine_semantic_version: str,
        name: str,
    ) -> str:
        return hash_canonical(
            {
                "kind": "sprint_root",
                "cache_schema": 2,
                "project_revision": project_revision,
                "lean_version": lean_version,
                "import_options_fingerprint": import_options_fingerprint,
                "engine_semantic_version": engine_semantic_version,
                "name": name,
            }
        )

    @staticmethod
    def op_key(
        *,
        reference_alpha_hash: str,
        operation_id: str,
        engine_semantic_version: str,
        lean_version: str,
        project_revision: str,
        import_options_fingerprint: str,
        name: str,
    ) -> str:
        """Operation record key.

        Alias theorems with alpha-identical statements share the reference
        hash but not their source constant, and negative evidence cites that
        constant, so the root name is part of the key (cache schema 2).
        """

        return hash_canonical(
            {
                "kind": "sprint_operation",
                "cache_schema": 2,
                "name": name,
                "reference_alpha_hash": reference_alpha_hash,
                "operation_id": operation_id,
                "engine_semantic_version": engine_semantic_version,
                "lean_version": lean_version,
                "project_revision": project_revision,
                "import_options_fingerprint": import_options_fingerprint,
            }
        )

    def _path(self, kind: str, key: str) -> Path:
        return self.root / kind / key[:2] / f"{key}.json"

    def get_root(self, key: str) -> dict[str, Any] | None:
        path = self._path("roots", key)
        return read_json_object(path) if path.is_file() else None

    def put_root(self, key: str, record: Mapping[str, object]) -> None:
        write_atomic(self._path("roots", key), canonical_json_bytes(record) + b"\n")

    def get_op(self, key: str) -> dict[str, Any] | None:
        path = self._path("ops", key)
        return read_json_object(path) if path.is_file() else None

    def put_op(self, key: str, record: Mapping[str, object]) -> None:
        """Write an operation record.

        Lean-derived fields must agree with any existing record; request
        hashes and timings are provenance and may differ.  An existing
        unrendered record is upgraded when the incoming one carries a render;
        an existing rendered record is kept as the first writer.
        """

        path = self._path("ops", key)
        data = canonical_json_bytes(record) + b"\n"
        if path.is_file():
            existing = read_json_object(path)
            if existing == dict(record):
                return
            semantic = {k: v for k, v in existing.items() if k not in PROVENANCE_FIELDS}
            incoming = {k: v for k, v in record.items() if k not in PROVENANCE_FIELDS}
            if semantic != incoming:
                differing = sorted(
                    field
                    for field in set(semantic) | set(incoming)
                    if semantic.get(field) != incoming.get(field)
                )
                raise StoreError(
                    f"semantic cache conflict for operation record {key}: fields {differing}"
                )
            if existing.get("render") is not None:
                return
        write_atomic(path, data)
