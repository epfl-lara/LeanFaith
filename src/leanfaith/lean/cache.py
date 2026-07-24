"""Fail-closed immutable cache for LF-020 symbolic evidence.

The cache stores :class:`~leanfaith.schemas.evidence.EvidenceRecord` objects,
never resolved labels.  In particular, ``not_proved`` and ``not_found`` are
cached as evidence outcomes only; interpreting evidence remains the resolver's
job.

Every semantic or execution-policy input that can change a result is part of
``EvidenceCacheKey``.  Cache files are canonical JSON, content checked on every
read, and installed with an atomic no-overwrite link.  An existing key can only
be reused when its bytes validate and are identical to the proposed entry.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.schemas.enums import EvidenceKind
from leanfaith.schemas.evidence import AuditValue, CounterexampleValue, EvidenceRecord
from leanfaith.schemas.ids import (
    CONTEXT_PREFIX,
    HEX64_PATTERN,
    PAIR_PREFIX,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    id_pattern,
)

_CACHE_FILE_SUFFIX = ".json"


class EvidenceCacheError(RuntimeError):
    """Base class for cache-integrity and immutability failures."""


class EvidenceCacheCorruptionError(EvidenceCacheError):
    """Raised when an existing cache entry is malformed or fails a hash check."""


class EvidenceCacheConflictError(EvidenceCacheError):
    """Raised when an immutable key already contains different valid evidence."""


class EvidenceCacheKey(StrictModel):
    """Complete identity of one symbolic-evidence computation (PLAN §16.9)."""

    schema_version: Literal[1] = 1
    pair_id: str = Field(pattern=id_pattern(PAIR_PREFIX))
    theorem_a_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    theorem_b_id: str = Field(pattern=id_pattern(THEOREM_PREFIX))
    theorem_a_statement_hash: str = Field(pattern=HEX64_PATTERN)
    theorem_b_statement_hash: str = Field(pattern=HEX64_PATTERN)
    representation_a_id: str = Field(pattern=id_pattern(REPRESENTATION_PREFIX))
    representation_b_id: str = Field(pattern=id_pattern(REPRESENTATION_PREFIX))
    representation_a_content_hash: str = Field(pattern=HEX64_PATTERN)
    representation_b_content_hash: str = Field(pattern=HEX64_PATTERN)
    representation_version: str = Field(min_length=1)
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    context_fingerprint: str = Field(pattern=HEX64_PATTERN)
    environment_schema_version: int = Field(ge=1)
    environment_hash: str = Field(pattern=HEX64_PATTERN)
    evidence_kind: EvidenceKind
    evidence_direction: Literal["none", "A_to_B", "B_to_A", "equivalence_only"]
    method_version: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    config_hash: str = Field(pattern=HEX64_PATTERN)
    semantic_policy_version: str = Field(min_length=1)
    semantic_policy_hash: str = Field(pattern=HEX64_PATTERN)
    lean_version: str = Field(min_length=1)
    lean_interact_version: str = Field(min_length=1)
    repl_revision: str = Field(min_length=1)
    project_revision: str = Field(min_length=1)


def compute_evidence_cache_key_hash(key: EvidenceCacheKey) -> str:
    """Return the content address for ``key``."""

    return hash_canonical(key.model_dump(mode="json"))


class EvidenceCacheEntry(StrictModel):
    """Canonical cache envelope with self-validating key and payload hashes."""

    schema_version: Literal[1] = 1
    cache_key_hash: str = Field(pattern=HEX64_PATTERN)
    cache_key: EvidenceCacheKey
    evidence_hash: str = Field(pattern=HEX64_PATTERN)
    evidence: EvidenceRecord
    auxiliary_evidence_hash: str = Field(pattern=HEX64_PATTERN)
    auxiliary_evidence: tuple[EvidenceRecord, ...] = ()
    generated_code_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    lean_request_hashes: tuple[str, ...] = ()
    certificate_dependency_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    artifact_hashes: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _hashes_and_lineage_match(self) -> EvidenceCacheEntry:
        expected_key_hash = compute_evidence_cache_key_hash(self.cache_key)
        if self.cache_key_hash != expected_key_hash:
            raise ValueError(
                f"cache_key_hash {self.cache_key_hash} does not match key {expected_key_hash}"
            )
        expected_evidence_hash = evidence_semantic_hash(self.evidence)
        if self.evidence_hash != expected_evidence_hash:
            raise ValueError(
                f"evidence_hash {self.evidence_hash} does not match payload "
                f"{expected_evidence_hash}"
            )
        expected_auxiliary_hash = hash_canonical(
            [evidence_semantic_payload(item) for item in self.auxiliary_evidence]
        )
        if self.auxiliary_evidence_hash != expected_auxiliary_hash:
            raise ValueError(
                f"auxiliary_evidence_hash {self.auxiliary_evidence_hash} does not "
                f"match payload {expected_auxiliary_hash}"
            )
        for auxiliary in self.auxiliary_evidence:
            if auxiliary.target_id != self.cache_key.pair_id:
                raise ValueError("auxiliary evidence must target the cache pair")
        evidence_ids = [self.evidence.evidence_id]
        evidence_ids.extend(item.evidence_id for item in self.auxiliary_evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("primary and auxiliary evidence IDs must be unique")
        if self.evidence.target_id != self.cache_key.pair_id:
            raise ValueError(
                f"evidence target {self.evidence.target_id} does not match cache pair "
                f"{self.cache_key.pair_id}"
            )
        if self.evidence.kind != self.cache_key.evidence_kind:
            raise ValueError(
                f"evidence kind {self.evidence.kind} does not match cache key kind "
                f"{self.cache_key.evidence_kind}"
            )
        if self.evidence.method_version != self.cache_key.method_version:
            raise ValueError(
                f"evidence method {self.evidence.method_version!r} does not match cache "
                f"method {self.cache_key.method_version!r}"
            )
        if self.evidence.config_hash != self.cache_key.config_hash:
            raise ValueError(
                f"evidence config hash {self.evidence.config_hash!r} does not match cache "
                f"config hash {self.cache_key.config_hash!r}"
            )
        for request_hash in self.lean_request_hashes:
            if len(request_hash) != 64 or any(c not in "0123456789abcdef" for c in request_hash):
                raise ValueError(f"invalid Lean request hash {request_hash!r}")
        for artifact, digest in self.artifact_hashes.items():
            if not artifact:
                raise ValueError("cache artifact paths must be nonempty")
            if not Path(artifact).is_absolute() and ".." in Path(artifact).parts:
                raise ValueError(
                    f"relative cache artifact path may not escape artifact_root: {artifact!r}"
                )
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"invalid cache artifact hash for {artifact!r}")
        referenced_artifacts: set[str] = set()
        for record in (self.evidence, *self.auxiliary_evidence):
            if record.raw_artifact:
                referenced_artifacts.add(record.raw_artifact)
            if isinstance(record.value, AuditValue) and record.value.detail_artifact:
                referenced_artifacts.add(record.value.detail_artifact)
            if isinstance(record.value, CounterexampleValue) and record.value.witness_artifact:
                referenced_artifacts.add(record.value.witness_artifact)
        missing_artifacts = referenced_artifacts - set(self.artifact_hashes)
        if missing_artifacts:
            raise ValueError(
                f"evidence artifact references are not content-bound: {sorted(missing_artifacts)}"
            )
        return self


def evidence_semantic_payload(evidence: EvidenceRecord) -> dict[str, object]:
    """Return semantic evidence independently of collection time/path.

    Concurrent identical computations may finish at different wall-clock
    times or store raw responses under different append-only paths.  Those
    operational fields must not turn one semantic cache key into a conflict.
    Exact artifact bytes are bound separately by ``artifact_hashes``.
    """

    value = evidence.model_dump(mode="json")
    value.pop("created_at", None)
    value.pop("raw_artifact", None)
    metadata = dict(value.get("metadata", {}))
    for key in (
        "cache_hit",
        "raw_artifact_sha256",
        "collected_at",
        "run_id",
    ):
        metadata.pop(key, None)
    value["metadata"] = metadata
    return value


def evidence_semantic_hash(evidence: EvidenceRecord) -> str:
    return hash_canonical(evidence_semantic_payload(evidence))


def make_evidence_cache_entry(
    key: EvidenceCacheKey,
    evidence: EvidenceRecord,
    *,
    auxiliary_evidence: tuple[EvidenceRecord, ...] = (),
    generated_code_hash: str | None = None,
    lean_request_hashes: tuple[str, ...] = (),
    certificate_dependency_hash: str | None = None,
    artifact_hashes: dict[str, str] | None = None,
) -> EvidenceCacheEntry:
    """Construct and fully validate one cache entry."""

    return EvidenceCacheEntry(
        cache_key_hash=compute_evidence_cache_key_hash(key),
        cache_key=key,
        evidence_hash=evidence_semantic_hash(evidence),
        evidence=evidence,
        auxiliary_evidence_hash=hash_canonical(
            [evidence_semantic_payload(item) for item in auxiliary_evidence]
        ),
        auxiliary_evidence=auxiliary_evidence,
        generated_code_hash=generated_code_hash,
        lean_request_hashes=lean_request_hashes,
        certificate_dependency_hash=certificate_dependency_hash,
        artifact_hashes=artifact_hashes or {},
    )


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> float:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _canonical_entry_bytes(entry: EvidenceCacheEntry) -> bytes:
    return canonical_json_bytes(entry.model_dump(mode="json")) + b"\n"


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class EvidenceCache:
    """Directory-backed, content-addressed and immutable evidence cache."""

    def __init__(self, root: Path, *, artifact_root: Path | None = None) -> None:
        self.root = root
        self.artifact_root = artifact_root

    def entry_path(self, key: EvidenceCacheKey) -> Path:
        digest = compute_evidence_cache_key_hash(key)
        return self.root / "v1" / digest[:2] / f"{digest}{_CACHE_FILE_SUFFIX}"

    def get(self, key: EvidenceCacheKey) -> EvidenceCacheEntry | None:
        """Return a validated entry, ``None`` on a clean miss, or fail closed."""

        path = self.entry_path(key)
        try:
            entry, _raw = self._read_path(path)
        except FileNotFoundError:
            return None
        expected = compute_evidence_cache_key_hash(key)
        if entry.cache_key_hash != expected or entry.cache_key != key:
            raise EvidenceCacheCorruptionError(
                f"cache entry {path} does not contain the requested key {expected}"
            )
        return entry

    def put(
        self,
        key: EvidenceCacheKey,
        evidence: EvidenceRecord,
        *,
        auxiliary_evidence: tuple[EvidenceRecord, ...] = (),
        generated_code_hash: str | None = None,
        lean_request_hashes: tuple[str, ...] = (),
        certificate_dependency_hash: str | None = None,
        artifact_hashes: dict[str, str] | None = None,
    ) -> EvidenceCacheEntry:
        """Atomically install an immutable entry.

        Concurrent identical writers converge on the same entry.  A writer
        proposing different evidence for an existing key receives a conflict;
        no cache file is overwritten.
        """

        entry = make_evidence_cache_entry(
            key,
            evidence,
            auxiliary_evidence=auxiliary_evidence,
            generated_code_hash=generated_code_hash,
            lean_request_hashes=lean_request_hashes,
            certificate_dependency_hash=certificate_dependency_hash,
            artifact_hashes=artifact_hashes,
        )
        path = self.entry_path(key)
        # A newly written entry must be usable immediately.  Validate every
        # referenced artifact before installing the immutable cache file,
        # rather than discovering a missing or stale artifact on the next get.
        self._verify_artifacts(entry, path)
        existing = self.get(key)
        if existing is not None:
            if (
                existing.evidence_hash == entry.evidence_hash
                and existing.auxiliary_evidence_hash == entry.auxiliary_evidence_hash
                and existing.generated_code_hash == entry.generated_code_hash
                and existing.lean_request_hashes == entry.lean_request_hashes
                and existing.certificate_dependency_hash == entry.certificate_dependency_hash
                and existing.artifact_hashes == entry.artifact_hashes
            ):
                return existing
            raise EvidenceCacheConflictError(
                f"immutable cache key {entry.cache_key_hash} already contains different evidence"
            )
        payload = _canonical_entry_bytes(entry)
        path.parent.mkdir(parents=True, exist_ok=True)

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{entry.cache_key_hash}.",
            suffix=".partial",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o444)
            try:
                # Hard-link installation is atomic and, unlike os.replace,
                # cannot overwrite a winner from a concurrent process.
                os.link(temporary, path)
            except FileExistsError:
                existing, _existing_raw = self._read_path(path)
                if (
                    existing.evidence_hash != entry.evidence_hash
                    or existing.auxiliary_evidence_hash != entry.auxiliary_evidence_hash
                    or existing.generated_code_hash != entry.generated_code_hash
                    or existing.lean_request_hashes != entry.lean_request_hashes
                    or existing.certificate_dependency_hash != entry.certificate_dependency_hash
                    or existing.artifact_hashes != entry.artifact_hashes
                ):
                    raise EvidenceCacheConflictError(
                        f"immutable cache key {entry.cache_key_hash} already contains "
                        "different evidence"
                    ) from None
                return existing
            _fsync_directory(path.parent)
            return entry
        finally:
            temporary.unlink(missing_ok=True)

    def _read_path(self, path: Path) -> tuple[EvidenceCacheEntry, bytes]:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            raise
        if path.is_symlink() or not path.is_file():
            raise EvidenceCacheCorruptionError(
                f"cache entry {path} is not a regular non-symlink file"
            )
        if metadata.st_size == 0:
            raise EvidenceCacheCorruptionError(f"cache entry {path} is empty")
        try:
            raw = path.read_bytes()
            document = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json,
            )
            entry = EvidenceCacheEntry.model_validate(document)
        except (
            OSError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValidationError,
            ValueError,
        ) as exc:
            raise EvidenceCacheCorruptionError(f"invalid cache entry {path}: {exc}") from exc
        expected_bytes = _canonical_entry_bytes(entry)
        if raw != expected_bytes:
            raise EvidenceCacheCorruptionError(
                f"cache entry {path} is not in canonical immutable encoding"
            )
        expected_name = f"{entry.cache_key_hash}{_CACHE_FILE_SUFFIX}"
        if path.name != expected_name or path.parent.name != entry.cache_key_hash[:2]:
            raise EvidenceCacheCorruptionError(
                f"cache entry {path} is stored under the wrong content address"
            )
        self._verify_artifacts(entry, path)
        return entry, raw

    def _verify_artifacts(self, entry: EvidenceCacheEntry, cache_path: Path) -> None:
        for raw_path, expected_hash in entry.artifact_hashes.items():
            artifact = Path(raw_path)
            if not artifact.is_absolute():
                if self.artifact_root is None:
                    raise EvidenceCacheCorruptionError(
                        f"cache entry {cache_path} references relative artifact {raw_path!r} "
                        "but no artifact_root was configured"
                    )
                artifact = self.artifact_root / artifact
            if not artifact.is_file():
                raise EvidenceCacheCorruptionError(
                    f"cache entry {cache_path} references missing artifact {artifact}"
                )
            observed = hash_file(artifact)
            if observed != expected_hash:
                raise EvidenceCacheCorruptionError(
                    f"cache entry {cache_path} artifact {artifact} hash {observed} "
                    f"does not match {expected_hash}"
                )


__all__ = [
    "EvidenceCache",
    "EvidenceCacheConflictError",
    "EvidenceCacheCorruptionError",
    "EvidenceCacheEntry",
    "EvidenceCacheError",
    "EvidenceCacheKey",
    "compute_evidence_cache_key_hash",
    "evidence_semantic_hash",
    "evidence_semantic_payload",
    "make_evidence_cache_entry",
]
