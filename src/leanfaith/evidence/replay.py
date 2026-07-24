"""Deterministic LF-020 semantic-replay audit.

The comparator is deliberately narrower than a byte-for-byte run comparison:
wall-clock times and evidence artifact paths are operational details, while
terminal evidence semantics, certificate audits, and the code/request/
dependency hashes bound by each cache key must replay exactly.

This module never resolves labels or promotes evidence.  It also verifies that
every evidence reference on an output pair resolves against the explicitly
supplied upstream IDs plus the evidence emitted by that LF-020 run.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, ValidationError, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.lean.cache import EvidenceCacheEntry
from leanfaith.schemas.enums import ArtifactClass, DataStage, EvidenceKind
from leanfaith.schemas.evidence import AuditValue, CounterexampleValue, EvidenceRecord
from leanfaith.schemas.manifest import OutputManifest
from leanfaith.schemas.pair import PairRecord

_COMPARISON_VERSION = "lf020_semantic_replay_v1"
_REQUEST_HASH_POLICY = "ordered_exact_by_cache_key_v1"
_ARTIFACT_PATH_POLICY = "ignored_for_semantics_v1"
_REQUIRED_OUTPUT_FILES = frozenset(
    {
        "evidence.jsonl",
        "pairs.jsonl",
        "failures.jsonl",
        "artifact_catalog.json",
        "cache_catalog.json",
        "manifest.json",
    }
)
_FORBIDDEN_OUTPUT_NAMES = frozenset(
    {
        "labels.jsonl",
        "resolved_labels.jsonl",
        "promotions.jsonl",
        "labels.json",
        "resolved_labels.json",
        "promotions.json",
    }
)
_TERMINAL_KINDS = frozenset(
    {
        EvidenceKind.DEFEQ,
        EvidenceKind.PROOF_A_IMPLIES_B,
        EvidenceKind.PROOF_B_IMPLIES_A,
        EvidenceKind.CLAIM_ALIGNMENT,
        EvidenceKind.COUNTEREXAMPLE,
    }
)
_NEW_EVIDENCE_KINDS = _TERMINAL_KINDS | {EvidenceKind.AXIOM_AUDIT}


class EvidenceReplayInputError(ValueError):
    """A replay tree is malformed and cannot be compared safely."""


class EvidenceReplayChecks(StrictModel):
    """Mechanically checkable replay claims."""

    left_accounting_closed: bool
    right_accounting_closed: bool
    no_labels_or_promotions: bool
    pair_semantics_match: bool
    terminal_job_semantics_match: bool
    audit_semantics_match: bool
    cache_keys_match: bool
    cache_payload_semantics_match: bool
    cache_execution_hashes_match: bool


class EvidenceReplaySideSummary(StrictModel):
    """Path-independent accounting for one completed LF-020 run."""

    run_id: str = Field(pattern=r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
    artifact_class: ArtifactClass
    output_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_file_count: int = Field(ge=0)
    artifact_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_catalog_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_snapshot_catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    pair_count: int = Field(ge=0)
    new_evidence_count: int = Field(ge=0)
    upstream_evidence_id_count: int = Field(ge=0)
    terminal_job_count: int = Field(ge=0)
    audit_count: int = Field(ge=0)
    cache_entry_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    unresolved_pair_evidence_id_count: int = Field(ge=0)
    unreferenced_new_evidence_count: int = Field(ge=0)
    label_or_promotion_violation_count: int = Field(ge=0)
    semantic_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    cache_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvidenceReplayReport(StrictModel):
    """Self-hashed JSON-ready LF-020 replay evidence."""

    schema_version: Literal[1] = 1
    comparison_version: Literal["lf020_semantic_replay_v1"] = "lf020_semantic_replay_v1"
    request_hash_policy: Literal["ordered_exact_by_cache_key_v1"] = "ordered_exact_by_cache_key_v1"
    artifact_path_policy: Literal["ignored_for_semantics_v1"] = "ignored_for_semantics_v1"
    source_pair_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    upstream_evidence_id_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    passed: bool
    left: EvidenceReplaySideSummary
    right: EvidenceReplaySideSummary
    checks: EvidenceReplayChecks
    errors: tuple[str, ...] = ()
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _self_consistent(self) -> EvidenceReplayReport:
        expected_pass = all(self.checks.model_dump().values()) and not self.errors
        if self.passed != expected_pass:
            raise ValueError("passed must equal all(checks) with no errors")
        payload = self.model_dump(mode="json", exclude={"report_hash"})
        expected_hash = hash_canonical(payload)
        if self.report_hash != expected_hash:
            raise ValueError(
                f"report_hash {self.report_hash} does not match report payload {expected_hash}"
            )
        return self


@dataclass(frozen=True, slots=True)
class _Side:
    summary: EvidenceReplaySideSummary
    pair_projection: tuple[dict[str, object], ...]
    terminal_projection: tuple[dict[str, object], ...]
    audit_projection: tuple[dict[str, object], ...]
    cache_payload_projection: dict[str, dict[str, object]]
    cache_execution_projection: dict[str, dict[str, object]]
    cache_keys: tuple[str, ...]
    accounting_errors: tuple[str, ...]
    scope_errors: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _CacheTree:
    entries: tuple[EvidenceCacheEntry, ...]
    snapshot_hashes: dict[str, str]
    snapshot_paths: dict[str, Path]


class _ArtifactCatalogEntry(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["raw_response", "evidence_artifact"]


class _ArtifactCatalog(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
    entries: tuple[_ArtifactCatalogEntry, ...]

    @model_validator(mode="after")
    def _canonical_entries(self) -> _ArtifactCatalog:
        keys = [(entry.kind, entry.path, entry.sha256) for entry in self.entries]
        if keys != sorted(keys):
            raise ValueError("artifact catalog entries are not canonically sorted")
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("artifact catalog contains duplicate paths")
        return self


class _CacheCatalogEntry(StrictModel):
    cache_key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class _CacheCatalog(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
    entries: tuple[_CacheCatalogEntry, ...]

    @model_validator(mode="after")
    def _canonical_entries(self) -> _CacheCatalog:
        keys = [entry.cache_key_hash for entry in self.entries]
        if keys != sorted(keys) or len(keys) != len(set(keys)):
            raise ValueError("cache catalog keys are not sorted and unique")
        paths = [entry.path for entry in self.entries]
        if len(paths) != len(set(paths)):
            raise ValueError("cache catalog contains duplicate paths")
        return self


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> float:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _load_json(path: Path) -> object:
    try:
        raw = path.read_text(encoding="utf-8")
        return json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise EvidenceReplayInputError(f"invalid JSON artifact {path}: {exc}") from exc


def _load_jsonl[ModelT: StrictModel](path: Path, model: type[ModelT]) -> tuple[ModelT, ...]:
    records: list[ModelT] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceReplayInputError(f"cannot read JSONL artifact {path}: {exc}") from exc
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            document = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json,
            )
            records.append(model.model_validate(document))
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise EvidenceReplayInputError(
                f"invalid {model.__name__} at {path}:{line_number}: {exc}"
            ) from exc
    return tuple(records)


def _failure_count(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise EvidenceReplayInputError(f"cannot read failure partition {path}: {exc}") from exc
    count = 0
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite_json,
            )
        except (json.JSONDecodeError, ValueError) as exc:
            raise EvidenceReplayInputError(
                f"invalid failure JSON at {path}:{line_number}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise EvidenceReplayInputError(
                f"failure record at {path}:{line_number} is not an object"
            )
        count += 1
    return count


def _terminal_projection(record: EvidenceRecord) -> dict[str, object]:
    return {
        "evidence_id": record.evidence_id,
        "target_kind": record.target_kind.value,
        "target_id": record.target_id,
        "kind": record.kind.value,
        "status": record.status.value,
        "value": None if record.value is None else record.value.model_dump(mode="json"),
    }


def _audit_projection(record: EvidenceRecord) -> dict[str, object]:
    if not isinstance(record.value, AuditValue):
        raise EvidenceReplayInputError(
            f"audit evidence {record.evidence_id} does not contain AuditValue"
        )
    return {
        "evidence_id": record.evidence_id,
        "target_kind": record.target_kind.value,
        "target_id": record.target_id,
        "kind": record.kind.value,
        "status": record.status.value,
        "method_version": record.method_version,
        "config_hash": record.config_hash,
        # raw_artifact and detail_artifact are deliberately excluded.
        "checks": dict(sorted(record.value.checks.items())),
        "violation_codes": tuple(sorted(record.value.violation_codes)),
    }


def _pair_projection(pair: PairRecord) -> dict[str, object]:
    return {
        "pair_id": pair.pair_id,
        "theorem_a_id": pair.theorem_a_id,
        "theorem_b_id": pair.theorem_b_id,
        "evidence_ids": tuple(sorted(pair.evidence_ids)),
        "resolved_label_id": pair.resolved_label_id,
    }


def _source_pair_projection(pair: PairRecord) -> dict[str, object]:
    """Fields LF-020 must preserve exactly from its input pair partition."""

    return pair.model_dump(
        mode="json",
        exclude={"evidence_ids"},
    )


def _find_manifest_checksum(
    checksums: Mapping[str, str],
    *,
    filename: str,
) -> str | None:
    matches = [digest for name, digest in checksums.items() if Path(name).name == filename]
    if len(matches) != 1:
        return None
    return matches[0]


def _load_manifest(path: Path) -> OutputManifest:
    document = _load_json(path)
    try:
        return OutputManifest.model_validate(document)
    except ValidationError as exc:
        raise EvidenceReplayInputError(f"invalid OutputManifest {path}: {exc}") from exc


def _load_cache(cache_root: Path) -> _CacheTree:
    if not cache_root.is_dir():
        raise EvidenceReplayInputError(f"cache root does not exist: {cache_root}")
    paths = sorted(cache_root.rglob("*.json"))
    if not paths:
        raise EvidenceReplayInputError(f"cache root contains no entries: {cache_root}")
    records: list[EvidenceCacheEntry] = []
    snapshot_hashes: dict[str, str] = {}
    snapshot_paths: dict[str, Path] = {}
    seen: set[str] = set()
    for path in paths:
        document = _load_json(path)
        try:
            entry = EvidenceCacheEntry.model_validate(document)
        except ValidationError as exc:
            raise EvidenceReplayInputError(f"invalid EvidenceCacheEntry {path}: {exc}") from exc
        expected_name = f"{entry.cache_key_hash}.json"
        if path.name != expected_name or path.parent.name != entry.cache_key_hash[:2]:
            raise EvidenceReplayInputError(
                f"cache entry {path} is stored under the wrong content address"
            )
        expected_bytes = canonical_json_bytes(entry.model_dump(mode="json")) + b"\n"
        if path.read_bytes() != expected_bytes:
            raise EvidenceReplayInputError(f"cache entry {path} is not canonical JSON")
        if entry.cache_key_hash in seen:
            raise EvidenceReplayInputError(
                f"duplicate cache key {entry.cache_key_hash} under {cache_root}"
            )
        seen.add(entry.cache_key_hash)
        records.append(entry)
        snapshot_hashes[entry.cache_key_hash] = hash_file(path)
        snapshot_paths[entry.cache_key_hash] = path
    return _CacheTree(
        entries=tuple(records),
        snapshot_hashes=snapshot_hashes,
        snapshot_paths=snapshot_paths,
    )


def _record_artifact_refs(record: EvidenceRecord) -> tuple[tuple[str, str], ...]:
    refs: list[tuple[str, str]] = []
    if record.raw_artifact:
        refs.append(("raw_artifact", record.raw_artifact))
    if isinstance(record.value, AuditValue) and record.value.detail_artifact:
        refs.append(("detail_artifact", record.value.detail_artifact))
    if isinstance(record.value, CounterexampleValue) and record.value.witness_artifact:
        refs.append(("witness_artifact", record.value.witness_artifact))
    return tuple(refs)


def _resolve_artifact(
    raw_path: str,
    *,
    artifact_root: Path,
) -> tuple[Path | None, str | None, str | None]:
    """Return resolved path, digest, and a fail-closed error."""

    artifact = Path(raw_path)
    root = artifact_root.resolve()
    if artifact.is_absolute():
        return None, None, f"artifact path must be repository-relative: {raw_path!r}"
    else:
        if ".." in artifact.parts:
            return None, None, f"artifact path escapes root: {raw_path!r}"
        candidate = artifact_root / artifact
    if candidate.is_symlink():
        return candidate, None, f"artifact is a symlink: {raw_path!r}"
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        return None, None, f"artifact path escapes root: {raw_path!r}"
    if not resolved.is_file():
        return (
            resolved,
            None,
            f"artifact is missing or not a regular non-symlink file: {raw_path!r}",
        )
    return resolved, hash_file(resolved), None


def _validate_artifacts(
    *,
    evidence: tuple[EvidenceRecord, ...],
    cache: tuple[EvidenceCacheEntry, ...],
    artifact_root: Path,
) -> tuple[tuple[dict[str, object], ...], int, tuple[str, ...]]:
    """Validate persisted evidence/cache artifacts and build a path-free catalog."""

    rows: list[dict[str, object]] = []
    errors: list[str] = []
    declared_paths: set[str] = set()

    for record in sorted(evidence, key=lambda item: item.evidence_id):
        for role, raw_path in _record_artifact_refs(record):
            declared_paths.add(raw_path)
            _resolved, observed, error = _resolve_artifact(
                raw_path,
                artifact_root=artifact_root,
            )
            if error is not None:
                errors.append(f"evidence {record.evidence_id} {role}: {error}")
            expected: str | None = None
            if role == "raw_artifact":
                raw_expected = record.metadata.get("raw_artifact_sha256")
                if raw_expected is not None:
                    if (
                        not isinstance(raw_expected, str)
                        or len(raw_expected) != 64
                        or any(c not in "0123456789abcdef" for c in raw_expected)
                    ):
                        errors.append(
                            f"evidence {record.evidence_id} has invalid "
                            "raw_artifact_sha256 metadata"
                        )
                    else:
                        expected = raw_expected
                        if observed != expected:
                            errors.append(
                                f"evidence {record.evidence_id} raw artifact digest mismatch"
                            )
            rows.append(
                {
                    "owner": "output_evidence",
                    "evidence_id": record.evidence_id,
                    "role": role,
                    "declared_sha256": expected,
                    "observed_sha256": observed,
                }
            )

    for entry in sorted(cache, key=lambda item: item.cache_key_hash):
        referenced: set[str] = set()
        records = (entry.evidence, *entry.auxiliary_evidence)
        for record in records:
            for role, raw_path in _record_artifact_refs(record):
                referenced.add(raw_path)
                declared_paths.add(raw_path)
                expected = entry.artifact_hashes.get(raw_path)
                _resolved, observed, error = _resolve_artifact(
                    raw_path,
                    artifact_root=artifact_root,
                )
                if error is not None:
                    errors.append(
                        f"cache {entry.cache_key_hash} evidence {record.evidence_id} "
                        f"{role}: {error}"
                    )
                if expected is None:
                    # EvidenceCacheEntry normally rejects this before replay,
                    # but retain the explicit check in the persisted audit.
                    errors.append(
                        f"cache {entry.cache_key_hash} does not bind {role} for "
                        f"{record.evidence_id}"
                    )
                elif observed != expected:
                    errors.append(
                        f"cache {entry.cache_key_hash} artifact digest mismatch for "
                        f"{record.evidence_id} {role}"
                    )
                rows.append(
                    {
                        "owner": "cache_evidence",
                        "cache_key_hash": entry.cache_key_hash,
                        "evidence_id": record.evidence_id,
                        "role": role,
                        "declared_sha256": expected,
                        "observed_sha256": observed,
                    }
                )

        extra_digests: list[tuple[str, str | None]] = []
        for raw_path, expected in entry.artifact_hashes.items():
            if raw_path in referenced:
                continue
            declared_paths.add(raw_path)
            _resolved, observed, error = _resolve_artifact(
                raw_path,
                artifact_root=artifact_root,
            )
            if error is not None:
                errors.append(f"cache {entry.cache_key_hash} extra artifact: {error}")
            if observed != expected:
                errors.append(f"cache {entry.cache_key_hash} extra artifact digest mismatch")
            extra_digests.append((expected, observed))
        for ordinal, (expected, observed) in enumerate(sorted(extra_digests)):
            rows.append(
                {
                    "owner": "cache_extra",
                    "cache_key_hash": entry.cache_key_hash,
                    "ordinal": ordinal,
                    "declared_sha256": expected,
                    "observed_sha256": observed,
                }
            )

    ordered = tuple(
        sorted(
            rows,
            key=lambda row: canonical_json_bytes(row),
        )
    )
    return ordered, len(declared_paths), tuple(errors)


def _validate_persisted_catalogs(
    *,
    output_dir: Path,
    artifact_root: Path,
    cache_root: Path,
    manifest: OutputManifest,
    cache_tree: _CacheTree,
) -> tuple[_ArtifactCatalog, _CacheCatalog, tuple[str, ...]]:
    """Require the persisted catalogs to enumerate exactly the touched tree."""

    artifact_document = _load_json(output_dir / "artifact_catalog.json")
    cache_document = _load_json(output_dir / "cache_catalog.json")
    try:
        artifact_catalog = _ArtifactCatalog.model_validate(artifact_document)
        cache_catalog = _CacheCatalog.model_validate(cache_document)
    except ValidationError as exc:
        raise EvidenceReplayInputError(f"invalid LF-020 persisted catalog: {exc}") from exc

    errors: list[str] = []
    for label, catalog_run_id in (
        ("artifact", artifact_catalog.run_id),
        ("cache", cache_catalog.run_id),
    ):
        if catalog_run_id != manifest.run_id:
            errors.append(f"{label} catalog run_id {catalog_run_id} != manifest {manifest.run_id}")

    expected_artifacts: dict[str, str] = {}
    for cache_entry in cache_tree.entries:
        for raw_path, digest in cache_entry.artifact_hashes.items():
            existing = expected_artifacts.setdefault(raw_path, digest)
            if existing != digest:
                errors.append(f"cache entries bind conflicting hashes for artifact {raw_path!r}")
    observed_artifacts = {
        catalog_entry.path: catalog_entry.sha256 for catalog_entry in artifact_catalog.entries
    }
    missing_artifacts = sorted(set(expected_artifacts) - set(observed_artifacts))
    extra_artifacts = sorted(set(observed_artifacts) - set(expected_artifacts))
    if missing_artifacts:
        errors.append(
            f"artifact catalog misses {len(missing_artifacts)} cache-bound paths; "
            f"first={missing_artifacts[0]}"
        )
    if extra_artifacts:
        errors.append(
            f"artifact catalog has {len(extra_artifacts)} extra paths; first={extra_artifacts[0]}"
        )
    for catalog_entry in artifact_catalog.entries:
        expected = expected_artifacts.get(catalog_entry.path)
        if expected is not None and catalog_entry.sha256 != expected:
            errors.append(f"artifact catalog hash differs from cache binding: {catalog_entry.path}")
        resolved, observed, error = _resolve_artifact(
            catalog_entry.path,
            artifact_root=artifact_root,
        )
        if error is not None:
            errors.append(f"artifact catalog {catalog_entry.path!r}: {error}")
            continue
        if observed != catalog_entry.sha256:
            errors.append(f"artifact catalog digest mismatch: {catalog_entry.path}")
        assert resolved is not None
        expected_kind = "raw_response" if "raw_responses" in resolved.parts else "evidence_artifact"
        if catalog_entry.kind != expected_kind:
            errors.append(
                f"artifact catalog kind mismatch for {catalog_entry.path}: "
                f"{catalog_entry.kind}!={expected_kind}"
            )

    observed_cache = {
        cache_catalog_entry.cache_key_hash: cache_catalog_entry
        for cache_catalog_entry in cache_catalog.entries
    }
    expected_cache_keys = set(cache_tree.snapshot_hashes)
    missing_cache = sorted(expected_cache_keys - set(observed_cache))
    extra_cache = sorted(set(observed_cache) - expected_cache_keys)
    if missing_cache:
        errors.append(
            f"cache catalog misses {len(missing_cache)} touched keys; first={missing_cache[0]}"
        )
    if extra_cache:
        errors.append(f"cache catalog has {len(extra_cache)} extra keys; first={extra_cache[0]}")
    root = cache_root.resolve()
    for cache_key, cache_catalog_entry in observed_cache.items():
        expected_hash = cache_tree.snapshot_hashes.get(cache_key)
        expected_path = cache_tree.snapshot_paths.get(cache_key)
        catalog_path = Path(cache_catalog_entry.path)
        if catalog_path.is_absolute():
            resolved_path = catalog_path.resolve()
        else:
            if ".." in catalog_path.parts:
                errors.append(f"cache catalog path escapes root: {cache_catalog_entry.path!r}")
                continue
            resolved_path = (cache_root / catalog_path).resolve()
            if not resolved_path.is_relative_to(root):
                errors.append(f"cache catalog path escapes root: {cache_catalog_entry.path!r}")
                continue
        if expected_path is not None and resolved_path != expected_path.resolve():
            errors.append(f"cache catalog path mismatch for key {cache_key}")
        if expected_hash is not None and cache_catalog_entry.sha256 != expected_hash:
            errors.append(f"cache catalog hash mismatch for key {cache_key}")
        if not resolved_path.is_file() or resolved_path.is_symlink():
            errors.append(f"cache catalog snapshot missing for key {cache_key}")
        elif hash_file(resolved_path) != cache_catalog_entry.sha256:
            errors.append(f"cache catalog persisted digest mismatch for key {cache_key}")

    for filename in ("artifact_catalog.json", "cache_catalog.json"):
        actual_hash = hash_file(output_dir / filename)
        file_hash = _find_manifest_checksum(manifest.file_checksums, filename=filename)
        output_hash = _find_manifest_checksum(
            manifest.output_partition_checksums,
            filename=filename,
        )
        if file_hash != actual_hash:
            errors.append(f"manifest file checksum mismatch for {filename}")
        if output_hash != actual_hash:
            errors.append(f"manifest output checksum mismatch for {filename}")

    return artifact_catalog, cache_catalog, tuple(errors)


def _cache_payload_projection(entry: EvidenceCacheEntry) -> dict[str, object]:
    primary = (
        _audit_projection(entry.evidence)
        if entry.evidence.kind == EvidenceKind.AXIOM_AUDIT
        else _terminal_projection(entry.evidence)
    )
    auxiliary = tuple(
        sorted(
            (_audit_projection(record) for record in entry.auxiliary_evidence),
            key=lambda item: str(item["evidence_id"]),
        )
    )
    return {
        "cache_key": entry.cache_key.model_dump(mode="json"),
        "primary_evidence": primary,
        "auxiliary_audits": auxiliary,
    }


def _cache_execution_projection(entry: EvidenceCacheEntry) -> dict[str, object]:
    return {
        "generated_code_hash": entry.generated_code_hash,
        # Request order is semantic here: it binds deterministic preflight,
        # portfolio, replay, and audit execution order for this cache job.
        "lean_request_hashes": entry.lean_request_hashes,
        "certificate_dependency_hash": entry.certificate_dependency_hash,
    }


def _inspect_side(
    *,
    output_dir: Path,
    cache_root: Path,
    artifact_root: Path,
    upstream_evidence_ids: tuple[str, ...],
    source_pair_by_id: Mapping[str, PairRecord],
) -> _Side:
    if not output_dir.is_dir():
        raise EvidenceReplayInputError(f"output directory does not exist: {output_dir}")
    missing = sorted(name for name in _REQUIRED_OUTPUT_FILES if not (output_dir / name).is_file())
    if missing:
        raise EvidenceReplayInputError(
            f"output directory {output_dir} is missing required files: {missing}"
        )

    pairs = _load_jsonl(output_dir / "pairs.jsonl", PairRecord)
    evidence = _load_jsonl(output_dir / "evidence.jsonl", EvidenceRecord)
    failures = _failure_count(output_dir / "failures.jsonl")
    manifest = _load_manifest(output_dir / "manifest.json")
    cache_tree = _load_cache(cache_root)
    cache = cache_tree.entries

    accounting_errors: list[str] = []
    scope_errors: list[str] = []
    _artifact_semantic_rows, artifact_file_count, artifact_errors = _validate_artifacts(
        evidence=evidence,
        cache=cache,
        artifact_root=artifact_root,
    )
    accounting_errors.extend(artifact_errors)
    _persisted_artifact_catalog, _persisted_cache_catalog, catalog_errors = (
        _validate_persisted_catalogs(
            output_dir=output_dir,
            artifact_root=artifact_root,
            cache_root=cache_root,
            manifest=manifest,
            cache_tree=cache_tree,
        )
    )
    accounting_errors.extend(catalog_errors)
    if manifest.stage != DataStage.EVIDENCE_COLLECTED:
        accounting_errors.append(
            f"manifest stage is {manifest.stage.value}, expected evidence_collected"
        )
    if manifest.row_count != len(evidence):
        accounting_errors.append(
            f"manifest row_count {manifest.row_count} != evidence count {len(evidence)}"
        )
    if manifest.attempted_row_count != len(pairs) + failures:
        accounting_errors.append(
            "manifest attempted_row_count does not equal enriched pairs plus failures"
        )
    expected_counts = {
        "enriched_pairs": len(pairs),
        "evidence_records": len(evidence),
        "pair_failures": failures,
        "resolved_labels_created": 0,
    }
    for name, expected in expected_counts.items():
        if manifest.terminal_outcome_counts.get(name) != expected:
            accounting_errors.append(f"manifest terminal_outcome_counts[{name!r}] != {expected}")
    for filename in ("evidence.jsonl", "pairs.jsonl", "failures.jsonl"):
        expected_hash = _find_manifest_checksum(manifest.file_checksums, filename=filename)
        actual_hash = hash_file(output_dir / filename)
        if expected_hash != actual_hash:
            accounting_errors.append(f"manifest checksum mismatch for {filename}")
    if failures:
        accounting_errors.append(f"failure partition contains {failures} records")

    pair_by_id: dict[str, PairRecord] = {}
    for pair in pairs:
        if pair.pair_id in pair_by_id:
            accounting_errors.append(f"duplicate output pair_id {pair.pair_id}")
        pair_by_id[pair.pair_id] = pair
        if len(pair.evidence_ids) != len(set(pair.evidence_ids)):
            accounting_errors.append(f"pair {pair.pair_id} repeats an evidence_id")
        source_pair = source_pair_by_id.get(pair.pair_id)
        if source_pair is None:
            accounting_errors.append(f"output contains unexpected pair {pair.pair_id}")
            continue
        if _source_pair_projection(pair) != _source_pair_projection(source_pair):
            if pair.resolved_label_id != source_pair.resolved_label_id:
                scope_errors.append(f"pair {pair.pair_id} changed preexisting resolved_label_id")
            else:
                accounting_errors.append(
                    f"pair {pair.pair_id} changed source fields other than evidence_ids"
                )
        missing_source_evidence = set(source_pair.evidence_ids) - set(pair.evidence_ids)
        if missing_source_evidence:
            accounting_errors.append(
                f"pair {pair.pair_id} dropped {len(missing_source_evidence)} upstream evidence IDs"
            )

    missing_output_pairs = sorted(set(source_pair_by_id) - set(pair_by_id))
    if missing_output_pairs:
        accounting_errors.append(
            f"{len(missing_output_pairs)} source pairs are absent from output; "
            f"first={missing_output_pairs[0]}"
        )

    evidence_by_id: dict[str, EvidenceRecord] = {}
    for record in evidence:
        if record.evidence_id in evidence_by_id:
            accounting_errors.append(f"duplicate new evidence_id {record.evidence_id}")
        evidence_by_id[record.evidence_id] = record
        if record.kind not in _NEW_EVIDENCE_KINDS:
            scope_errors.append(
                f"new evidence {record.evidence_id} has out-of-scope kind {record.kind.value}"
            )
        if record.target_id not in pair_by_id:
            accounting_errors.append(
                f"new evidence {record.evidence_id} targets absent pair {record.target_id}"
            )

    upstream_set = set(upstream_evidence_ids)
    source_evidence_ids = {
        evidence_id
        for source_pair in source_pair_by_id.values()
        for evidence_id in source_pair.evidence_ids
    }
    missing_bound_upstream = source_evidence_ids - upstream_set
    if missing_bound_upstream:
        accounting_errors.append(
            f"{len(missing_bound_upstream)} source-pair evidence IDs were not supplied "
            f"as upstream evidence; first={sorted(missing_bound_upstream)[0]}"
        )
    new_set = set(evidence_by_id)
    overlap = sorted(upstream_set & new_set)
    if overlap:
        accounting_errors.append(f"upstream and new evidence IDs overlap; first={overlap[0]}")
    resolvable = upstream_set | new_set
    referenced: set[str] = set()
    unresolved: set[str] = set()
    for pair in pairs:
        for evidence_id in pair.evidence_ids:
            referenced.add(evidence_id)
            if evidence_id not in resolvable:
                unresolved.add(evidence_id)
        for record in evidence:
            if record.target_id == pair.pair_id and record.evidence_id not in pair.evidence_ids:
                accounting_errors.append(
                    f"pair {pair.pair_id} does not reference new evidence {record.evidence_id}"
                )
    if unresolved:
        accounting_errors.append(
            f"{len(unresolved)} pair evidence IDs do not resolve; first={sorted(unresolved)[0]}"
        )
    unreferenced_new = new_set - referenced
    if unreferenced_new:
        accounting_errors.append(
            f"{len(unreferenced_new)} new evidence IDs are unreferenced; "
            f"first={sorted(unreferenced_new)[0]}"
        )

    forbidden_files = sorted(
        path.relative_to(output_dir).as_posix()
        for path in output_dir.rglob("*")
        if path.is_file() and path.name in _FORBIDDEN_OUTPUT_NAMES
    )
    if forbidden_files:
        scope_errors.append(f"label/promotion artifacts present: {forbidden_files}")

    terminal = tuple(record for record in evidence if record.kind in _TERMINAL_KINDS)
    audits = tuple(record for record in evidence if record.kind == EvidenceKind.AXIOM_AUDIT)
    terminal_job_keys: set[tuple[str, EvidenceKind]] = set()
    for record in terminal:
        key = (record.target_id, record.kind)
        if key in terminal_job_keys:
            accounting_errors.append(
                f"duplicate terminal job for pair={record.target_id}, kind={record.kind.value}"
            )
        terminal_job_keys.add(key)

    terminal_ids = {record.evidence_id for record in terminal}
    audit_ids = {record.evidence_id for record in audits}
    cache_primary_ids = {entry.evidence.evidence_id for entry in cache}
    cache_auxiliary_ids = {
        record.evidence_id for entry in cache for record in entry.auxiliary_evidence
    }
    if cache_primary_ids != terminal_ids:
        accounting_errors.append("cache primary evidence IDs do not equal terminal output IDs")
    if cache_auxiliary_ids != audit_ids:
        accounting_errors.append("cache auxiliary audit IDs do not equal audit output IDs")
    for entry in cache:
        if entry.cache_key.pair_id not in pair_by_id:
            accounting_errors.append(
                f"cache key {entry.cache_key_hash} targets absent pair {entry.cache_key.pair_id}"
            )
        if entry.evidence.kind not in _TERMINAL_KINDS:
            accounting_errors.append(
                f"cache key {entry.cache_key_hash} primary evidence is not a terminal job"
            )
        for auxiliary in entry.auxiliary_evidence:
            if auxiliary.kind != EvidenceKind.AXIOM_AUDIT:
                accounting_errors.append(
                    f"cache key {entry.cache_key_hash} has non-audit auxiliary evidence"
                )

    pair_projection = tuple(
        _pair_projection(pair) for pair in sorted(pairs, key=lambda item: item.pair_id)
    )
    terminal_projection = tuple(
        _terminal_projection(record)
        for record in sorted(
            terminal,
            key=lambda item: (item.target_id, item.kind.value, item.evidence_id),
        )
    )
    audit_projection = tuple(
        _audit_projection(record)
        for record in sorted(
            audits,
            key=lambda item: (item.target_id, item.evidence_id),
        )
    )
    cache_payload = {
        entry.cache_key_hash: _cache_payload_projection(entry)
        for entry in sorted(cache, key=lambda item: item.cache_key_hash)
    }
    cache_execution = {
        entry.cache_key_hash: _cache_execution_projection(entry)
        for entry in sorted(cache, key=lambda item: item.cache_key_hash)
    }
    semantic_fingerprint = hash_canonical(
        {
            "pairs": pair_projection,
            "terminal_jobs": terminal_projection,
            "audits": audit_projection,
        }
    )
    cache_fingerprint = hash_canonical(
        {
            "payload": cache_payload,
            "execution": cache_execution,
        }
    )
    artifact_catalog_hash = hash_canonical(
        [entry.model_dump(mode="json") for entry in _persisted_artifact_catalog.entries]
    )
    cache_catalog_hash = hash_canonical(
        [entry.model_dump(mode="json") for entry in _persisted_cache_catalog.entries]
    )
    cache_snapshot_catalog_hash = hash_canonical(
        [
            {
                "cache_key_hash": key,
                "snapshot_sha256": cache_tree.snapshot_hashes[key],
            }
            for key in sorted(cache_tree.snapshot_hashes)
        ]
    )
    summary = EvidenceReplaySideSummary(
        run_id=manifest.run_id,
        artifact_class=manifest.artifact_class,
        output_manifest_sha256=hash_file(output_dir / "manifest.json"),
        artifact_file_count=artifact_file_count,
        artifact_catalog_sha256=hash_file(output_dir / "artifact_catalog.json"),
        artifact_catalog_hash=artifact_catalog_hash,
        cache_catalog_sha256=hash_file(output_dir / "cache_catalog.json"),
        cache_catalog_hash=cache_catalog_hash,
        cache_snapshot_catalog_hash=cache_snapshot_catalog_hash,
        pair_count=len(pairs),
        new_evidence_count=len(evidence),
        upstream_evidence_id_count=len(upstream_evidence_ids),
        terminal_job_count=len(terminal),
        audit_count=len(audits),
        cache_entry_count=len(cache),
        failure_count=failures,
        unresolved_pair_evidence_id_count=len(unresolved),
        unreferenced_new_evidence_count=len(unreferenced_new),
        label_or_promotion_violation_count=len(scope_errors),
        semantic_fingerprint=semantic_fingerprint,
        cache_fingerprint=cache_fingerprint,
    )
    return _Side(
        summary=summary,
        pair_projection=pair_projection,
        terminal_projection=terminal_projection,
        audit_projection=audit_projection,
        cache_payload_projection=cache_payload,
        cache_execution_projection=cache_execution,
        cache_keys=tuple(sorted(cache_payload)),
        accounting_errors=tuple(accounting_errors),
        scope_errors=tuple(scope_errors),
    )


def _comparison_error(
    label: str,
    left: object,
    right: object,
) -> str:
    return f"{label} differs: left_hash={hash_canonical(left)}, right_hash={hash_canonical(right)}"


def compare_lf020_replays(
    *,
    left_output_dir: Path,
    left_cache_root: Path,
    left_artifact_root: Path,
    right_output_dir: Path,
    right_cache_root: Path,
    right_artifact_root: Path,
    source_pairs: Iterable[PairRecord],
    upstream_evidence_ids: Iterable[str] = (),
) -> EvidenceReplayReport:
    """Compare two clean-cache LF-020 runs using path-independent semantics.

    ``source_pairs`` is the exact pre-LF-020 pair partition supplied to both
    runs.  A preexisting ``resolved_label_id`` is allowed only when preserved
    byte-for-byte from that source record; LF-020 cannot create or replace one.
    ``upstream_evidence_ids`` must be the complete upstream evidence lineage.
    The comparator does not infer either input from unrelated directories.
    """

    upstream = tuple(upstream_evidence_ids)
    if len(upstream) != len(set(upstream)):
        raise EvidenceReplayInputError("upstream_evidence_ids contains duplicates")
    source_pair_items = tuple(source_pairs)
    source_pair_by_id = {pair.pair_id: pair for pair in source_pair_items}
    if len(source_pair_by_id) != len(source_pair_items):
        raise EvidenceReplayInputError("source_pairs contains duplicate pair_id values")
    source_pair_fingerprint = hash_canonical(
        [
            pair.model_dump(mode="json")
            for pair in sorted(source_pair_items, key=lambda item: item.pair_id)
        ]
    )
    upstream_evidence_id_fingerprint = hash_canonical(tuple(sorted(upstream)))
    left = _inspect_side(
        output_dir=left_output_dir,
        cache_root=left_cache_root,
        artifact_root=left_artifact_root,
        upstream_evidence_ids=upstream,
        source_pair_by_id=source_pair_by_id,
    )
    right = _inspect_side(
        output_dir=right_output_dir,
        cache_root=right_cache_root,
        artifact_root=right_artifact_root,
        upstream_evidence_ids=upstream,
        source_pair_by_id=source_pair_by_id,
    )

    pair_match = left.pair_projection == right.pair_projection
    terminal_match = left.terminal_projection == right.terminal_projection
    audit_match = left.audit_projection == right.audit_projection
    cache_keys_match = left.cache_keys == right.cache_keys
    cache_payload_match = left.cache_payload_projection == right.cache_payload_projection
    cache_execution_match = left.cache_execution_projection == right.cache_execution_projection
    no_scope_violations = not left.scope_errors and not right.scope_errors
    checks = EvidenceReplayChecks(
        left_accounting_closed=not left.accounting_errors,
        right_accounting_closed=not right.accounting_errors,
        no_labels_or_promotions=no_scope_violations,
        pair_semantics_match=pair_match,
        terminal_job_semantics_match=terminal_match,
        audit_semantics_match=audit_match,
        cache_keys_match=cache_keys_match,
        cache_payload_semantics_match=cache_payload_match,
        cache_execution_hashes_match=cache_execution_match,
    )
    errors = [
        *(f"left: {error}" for error in left.accounting_errors),
        *(f"right: {error}" for error in right.accounting_errors),
        *(f"left scope: {error}" for error in left.scope_errors),
        *(f"right scope: {error}" for error in right.scope_errors),
    ]
    if not pair_match:
        errors.append(
            _comparison_error("pair semantics", left.pair_projection, right.pair_projection)
        )
    if not terminal_match:
        errors.append(
            _comparison_error(
                "terminal job semantics",
                left.terminal_projection,
                right.terminal_projection,
            )
        )
    if not audit_match:
        errors.append(
            _comparison_error("audit semantics", left.audit_projection, right.audit_projection)
        )
    if not cache_keys_match:
        errors.append(_comparison_error("cache key sets", left.cache_keys, right.cache_keys))
    if not cache_payload_match:
        errors.append(
            _comparison_error(
                "cache payload semantics",
                left.cache_payload_projection,
                right.cache_payload_projection,
            )
        )
    if not cache_execution_match:
        errors.append(
            _comparison_error(
                "cache execution hashes",
                left.cache_execution_projection,
                right.cache_execution_projection,
            )
        )

    base: dict[str, Any] = {
        "schema_version": 1,
        "comparison_version": _COMPARISON_VERSION,
        "request_hash_policy": _REQUEST_HASH_POLICY,
        "artifact_path_policy": _ARTIFACT_PATH_POLICY,
        "source_pair_fingerprint": source_pair_fingerprint,
        "upstream_evidence_id_fingerprint": upstream_evidence_id_fingerprint,
        "passed": all(checks.model_dump().values()) and not errors,
        "left": left.summary.model_dump(mode="json"),
        "right": right.summary.model_dump(mode="json"),
        "checks": checks.model_dump(mode="json"),
        "errors": tuple(errors),
    }
    return EvidenceReplayReport(
        **base,
        report_hash=hash_canonical(base),
    )


__all__ = [
    "EvidenceReplayChecks",
    "EvidenceReplayInputError",
    "EvidenceReplayReport",
    "EvidenceReplaySideSummary",
    "compare_lf020_replays",
]
