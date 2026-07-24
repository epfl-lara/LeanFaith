"""User-facing LF-020 symbolic-evidence collection.

The command consumes explicit, immutable JSONL partitions and emits only
``EvidenceRecord`` objects, evidence-linked ``PairRecord`` objects, explicit
failures, and manifests.  It deliberately does not import or construct a
resolved-label schema.
"""

from __future__ import annotations

import datetime
import json
import os
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.evidence.certificates import ClaimAlignmentSpec
from leanfaith.evidence.config import load_evidence_configs
from leanfaith.evidence.pipeline import (
    EvidenceCollectorSettings,
    EvidencePipelineError,
    PairEvidenceResult,
    SymbolicEvidenceCollector,
)
from leanfaith.lean.cache import (
    EvidenceCache,
    EvidenceCacheEntry,
    EvidenceCacheKey,
    compute_evidence_cache_key_hash,
)
from leanfaith.lean.leaninteract_backend import BackendSettings, LeanInteractBackend
from leanfaith.lean.protocol import LeanBackend, LeanRequest, LeanResult
from leanfaith.schemas import (
    ArtifactClass,
    ContextRecord,
    DataStage,
    EvidenceKind,
    EvidenceRecord,
    OutputManifest,
    PairRecord,
    RepresentationRecord,
    RunManifest,
    TheoremRecord,
    check_pair_groups,
    collect_code_state,
    new_run_id,
    run_manifest_path,
    write_manifest,
)
from leanfaith.schemas.migrations import CURRENT_RECORD_SCHEMA_VERSION

_ARTIFACT_CLASS_VALUES = {item.value for item in ArtifactClass}
_monotonic_ns = time.monotonic_ns


class EvidenceCollectionInputError(ValueError):
    """Input partitions are malformed, ambiguous, or lineage-inconsistent."""


class EvidenceCollectionFailure(StrictModel):
    """One pair that could not be submitted to the evidence collector."""

    schema_version: Literal[1] = 1
    failure_id: str = Field(pattern=r"^evidence-failure:[0-9a-f]{64}$")
    pair_id: str
    stage: Literal["lineage_validation", "evidence_collection"]
    failure_type: str
    detail: str
    created_at: datetime.datetime


class EvidenceArtifactCatalogEntry(StrictModel):
    """One content-bound artifact referenced by a touched evidence cache entry."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    kind: Literal["raw_response", "evidence_artifact"]


class EvidenceArtifactCatalog(StrictModel):
    """Canonical catalog of artifacts used by one LF-020 run."""

    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
    entries: tuple[EvidenceArtifactCatalogEntry, ...]


class EvidenceCacheCatalogEntry(StrictModel):
    """One touched cache entry, addressed relative to the explicit cache root."""

    cache_key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class EvidenceCacheCatalog(StrictModel):
    """Canonical catalog of cache entries touched by one LF-020 run."""

    schema_version: Literal[1] = 1
    run_id: str = Field(pattern=r"^run_[0-9]{8}T[0-9]{6}Z_[0-9a-f]{8}$")
    entries: tuple[EvidenceCacheCatalogEntry, ...]


@dataclass(frozen=True, slots=True)
class EvidenceCollectionArtifacts:
    """Paths and accounting for one completed LF-020 collection run."""

    run_id: str
    artifact_class: ArtifactClass
    output_dir: Path
    evidence_path: Path
    pair_path: Path
    failure_path: Path
    artifact_catalog_path: Path
    cache_catalog_path: Path
    output_manifest_path: Path
    run_manifest_path: Path
    evidence_count: int
    pair_count: int
    failure_count: int
    cache_hits: int
    cache_misses: int
    lean_request_attempts: int
    retry_count: int
    wall_elapsed_seconds: float


class _MeasuredLeanBackend:
    """Transparent backend proxy with attempt-level measurements."""

    def __init__(self, delegate: LeanBackend) -> None:
        self.delegate = delegate
        self.request_attempt_count = 0
        self.retry_count = 0
        self.elapsed_ms = 0
        self.request_hashes: set[str] = set()
        self.status_counts: dict[str, int] = {}

    def _record(self, request: LeanRequest, result: LeanResult) -> None:
        self.request_attempt_count += 1
        attempt = str(request.metadata.get("attempt", "0"))
        if attempt not in {"", "0"}:
            self.retry_count += 1
        self.elapsed_ms += result.elapsed_ms
        self.request_hashes.add(result.request_hash)
        status = result.status.value
        self.status_counts[status] = self.status_counts.get(status, 0) + 1

    def run(self, request: LeanRequest) -> LeanResult:
        result = self.delegate.run(request)
        self._record(request, result)
        return result

    def run_batch(self, requests: Sequence[LeanRequest]) -> list[LeanResult]:
        results = self.delegate.run_batch(requests)
        if len(results) != len(requests):
            raise EvidenceCollectionInputError(
                "Lean backend returned a different number of results than requests"
            )
        for request, result in zip(requests, results, strict=True):
            self._record(request, result)
        return results

    def close(self) -> None:
        self.delegate.close()


class _MeasuredEvidenceCache:
    """Transparent cache proxy that records only entries touched by this run."""

    def __init__(self, delegate: EvidenceCache) -> None:
        self.delegate = delegate
        self.hit_count = 0
        self.miss_count = 0
        self.put_count = 0
        self.entries: dict[str, EvidenceCacheEntry] = {}

    def get(self, key: EvidenceCacheKey) -> EvidenceCacheEntry | None:
        entry = self.delegate.get(key)
        if entry is None:
            self.miss_count += 1
        else:
            self.hit_count += 1
            self.entries[entry.cache_key_hash] = entry
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
        entry = self.delegate.put(
            key,
            evidence,
            auxiliary_evidence=auxiliary_evidence,
            generated_code_hash=generated_code_hash,
            lean_request_hashes=lean_request_hashes,
            certificate_dependency_hash=certificate_dependency_hash,
            artifact_hashes=artifact_hashes,
        )
        expected = compute_evidence_cache_key_hash(key)
        if entry.cache_key_hash != expected:
            raise EvidenceCollectionInputError(
                f"cache returned key {entry.cache_key_hash}, expected {expected}"
            )
        self.put_count += 1
        self.entries[entry.cache_key_hash] = entry
        return entry


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> float:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _load_jsonl[ModelT: StrictModel](
    path: Path,
    model_type: type[ModelT],
) -> tuple[ModelT, ...]:
    """Load strict JSON objects with path/line diagnostics."""

    records: list[ModelT] = []
    try:
        handle = path.open(encoding="utf-8")
    except OSError as exc:
        raise EvidenceCollectionInputError(f"cannot read {path}: {exc}") from exc
    with handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(
                    line,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_nonfinite,
                )
                if not isinstance(value, dict):
                    raise ValueError("expected a JSON object")
                records.append(model_type.model_validate(value))
            except (json.JSONDecodeError, ValidationError, ValueError) as exc:
                raise EvidenceCollectionInputError(f"{path}:{line_number}: {exc}") from exc
    return tuple(records)


def _index_unique[ModelT: StrictModel](
    records: Iterable[ModelT],
    *,
    id_field: str,
    record_kind: str,
) -> dict[str, ModelT]:
    indexed: dict[str, ModelT] = {}
    for record in records:
        identifier = getattr(record, id_field)
        if identifier in indexed:
            raise EvidenceCollectionInputError(
                f"duplicate {record_kind} ID {identifier!r} across explicit input partitions"
            )
        indexed[identifier] = record
    return indexed


def _artifact_class_markers(
    theorems: Iterable[TheoremRecord],
    pairs: Iterable[PairRecord],
    upstream_evidence: Iterable[EvidenceRecord],
) -> set[ArtifactClass]:
    markers: set[ArtifactClass] = set()
    identified_metadata = (
        *((theorem.theorem_id, theorem.metadata) for theorem in theorems),
        *((pair.pair_id, pair.metadata) for pair in pairs),
        *((record.evidence_id, record.metadata) for record in upstream_evidence),
    )
    for identifier, metadata in identified_metadata:
        raw = metadata.get("artifact_class")
        if raw is None:
            continue
        if not isinstance(raw, str) or raw not in _ARTIFACT_CLASS_VALUES:
            raise EvidenceCollectionInputError(
                f"record {identifier} has invalid artifact_class metadata {raw!r}"
            )
        markers.add(ArtifactClass(raw))
    return markers


def resolve_artifact_class(
    *,
    requested: str,
    theorems: Iterable[TheoremRecord],
    pairs: Iterable[PairRecord],
    upstream_evidence: Iterable[EvidenceRecord] = (),
) -> ArtifactClass:
    """Propagate smoke inputs and reject smoke-to-production promotion."""

    if requested not in {"auto", *_ARTIFACT_CLASS_VALUES}:
        raise EvidenceCollectionInputError(
            "--artifact-class must be auto, production, smoke, or diagnostic"
        )
    markers = _artifact_class_markers(theorems, pairs, upstream_evidence)
    if ArtifactClass.SMOKE in markers:
        if requested == ArtifactClass.PRODUCTION.value:
            raise EvidenceCollectionInputError(
                "smoke input cannot be collected into a production artifact"
            )
        return ArtifactClass.SMOKE
    if requested == "auto":
        if ArtifactClass.DIAGNOSTIC in markers:
            return ArtifactClass.DIAGNOSTIC
        return ArtifactClass.PRODUCTION
    return ArtifactClass(requested)


def _relative_or_absolute(path: Path, root: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def _write_jsonl(records: Sequence[StrictModel], path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_bytes(
        b"".join(canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records)
    )
    os.replace(partial, path)
    return hash_file(path)


def _write_canonical_model(model: StrictModel, path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    partial.write_bytes(canonical_json_bytes(model.model_dump(mode="json")) + b"\n")
    os.replace(partial, path)
    return hash_file(path)


def _catalog_artifact_kind(path: str) -> Literal["raw_response", "evidence_artifact"]:
    return "raw_response" if "raw_responses" in Path(path).parts else "evidence_artifact"


def _write_evidence_catalogs(
    *,
    paths: RepoPaths,
    run_id: str,
    output_dir: Path,
    cache_dir: Path,
    cache_entries: dict[str, EvidenceCacheEntry],
) -> tuple[Path, str, EvidenceArtifactCatalog, Path, str, EvidenceCacheCatalog]:
    """Write canonical catalogs for exactly the cache entries touched by this run."""

    artifact_by_path: dict[str, EvidenceArtifactCatalogEntry] = {}
    cache_items: list[EvidenceCacheCatalogEntry] = []
    root = paths.root.resolve()
    cache_root = cache_dir.resolve()

    for cache_key_hash, entry in sorted(cache_entries.items()):
        if entry.cache_key_hash != cache_key_hash:
            raise EvidenceCollectionInputError(
                f"touched cache entry key mismatch: {cache_key_hash} != {entry.cache_key_hash}"
            )
        cache_path = cache_root / "v1" / cache_key_hash[:2] / f"{cache_key_hash}.json"
        try:
            relative_cache_path = cache_path.relative_to(cache_root).as_posix()
        except ValueError as exc:
            raise EvidenceCollectionInputError(
                f"cache entry escapes explicit cache root: {cache_path}"
            ) from exc
        if cache_path.is_symlink() or not cache_path.is_file():
            raise EvidenceCollectionInputError(
                f"touched cache entry is missing or not a regular file: {cache_path}"
            )
        expected_cache_bytes = canonical_json_bytes(entry.model_dump(mode="json")) + b"\n"
        if cache_path.read_bytes() != expected_cache_bytes:
            raise EvidenceCollectionInputError(
                f"touched cache entry changed before cataloging: {cache_path}"
            )
        cache_items.append(
            EvidenceCacheCatalogEntry(
                cache_key_hash=cache_key_hash,
                path=relative_cache_path,
                sha256=hash_file(cache_path),
            )
        )

        for raw_path, expected_hash in entry.artifact_hashes.items():
            artifact_path = Path(raw_path)
            if artifact_path.is_absolute() or ".." in artifact_path.parts:
                raise EvidenceCollectionInputError(
                    f"LF-020 cache artifacts must use repository-relative paths: {raw_path!r}"
                )
            resolved_artifact = (root / artifact_path).resolve()
            try:
                resolved_artifact.relative_to(root)
            except ValueError as exc:
                raise EvidenceCollectionInputError(
                    f"cache artifact escapes repository root: {raw_path!r}"
                ) from exc
            if resolved_artifact.is_symlink() or not resolved_artifact.is_file():
                raise EvidenceCollectionInputError(
                    f"cache artifact is missing or not a regular file: {raw_path!r}"
                )
            observed_hash = hash_file(resolved_artifact)
            if observed_hash != expected_hash:
                raise EvidenceCollectionInputError(
                    f"cache artifact hash mismatch for {raw_path!r}: "
                    f"{observed_hash} != {expected_hash}"
                )
            candidate = EvidenceArtifactCatalogEntry(
                path=artifact_path.as_posix(),
                sha256=expected_hash,
                kind=_catalog_artifact_kind(raw_path),
            )
            existing = artifact_by_path.get(candidate.path)
            if existing is not None and existing != candidate:
                raise EvidenceCollectionInputError(
                    f"conflicting artifact catalog entries for {candidate.path!r}"
                )
            artifact_by_path[candidate.path] = candidate

    artifact_catalog = EvidenceArtifactCatalog(
        run_id=run_id,
        entries=tuple(
            sorted(
                artifact_by_path.values(),
                key=lambda item: (item.kind, item.path, item.sha256),
            )
        ),
    )
    cache_catalog = EvidenceCacheCatalog(
        run_id=run_id,
        entries=tuple(sorted(cache_items, key=lambda item: item.cache_key_hash)),
    )
    artifact_catalog_path = output_dir / "artifact_catalog.json"
    cache_catalog_path = output_dir / "cache_catalog.json"
    artifact_catalog_hash = _write_canonical_model(
        artifact_catalog,
        artifact_catalog_path,
    )
    cache_catalog_hash = _write_canonical_model(cache_catalog, cache_catalog_path)
    return (
        artifact_catalog_path,
        artifact_catalog_hash,
        artifact_catalog,
        cache_catalog_path,
        cache_catalog_hash,
        cache_catalog,
    )


def _failure(
    *,
    pair_id: str,
    stage: Literal["lineage_validation", "evidence_collection"],
    exc: Exception,
    created_at: datetime.datetime,
) -> EvidenceCollectionFailure:
    failure_type = type(exc).__name__
    detail = str(exc)
    failure_id = "evidence-failure:" + hash_canonical(
        {
            "schema": "lf020_evidence_failure_v1",
            "pair_id": pair_id,
            "stage": stage,
            "failure_type": failure_type,
            "detail": detail,
        }
    )
    return EvidenceCollectionFailure(
        failure_id=failure_id,
        pair_id=pair_id,
        stage=stage,
        failure_type=failure_type,
        detail=detail,
        created_at=created_at,
    )


def _validate_context_compatibility(contexts: Iterable[ContextRecord]) -> int:
    schema_versions = {context.environment_schema_version for context in contexts}
    if not schema_versions:
        raise EvidenceCollectionInputError("context partition is empty")
    if len(schema_versions) != 1:
        raise EvidenceCollectionInputError(
            "all contexts in one run must use one environment_schema_version"
        )
    return next(iter(schema_versions))


def _representation_by_theorem(
    representations: Iterable[RepresentationRecord],
) -> dict[str, RepresentationRecord]:
    indexed: dict[str, RepresentationRecord] = {}
    for representation in representations:
        if representation.theorem_id in indexed:
            previous = indexed[representation.theorem_id]
            raise EvidenceCollectionInputError(
                "explicit representation inputs contain more than one representation "
                f"for theorem {representation.theorem_id!r}: "
                f"{previous.representation_id!r}, {representation.representation_id!r}"
            )
        indexed[representation.theorem_id] = representation
    return indexed


def _effective_argv(
    *,
    paths: RepoPaths,
    context_paths: Sequence[Path],
    theorem_paths: Sequence[Path],
    representation_paths: Sequence[Path],
    pair_path: Path,
    project_dir: Path,
    upstream_evidence_paths: Sequence[Path],
    out_dir: Path,
    cache_dir: Path,
    artifact_dir: Path,
    alignment_path: Path | None,
    artifact_class: ArtifactClass,
    memory_hard_limit_mb: int | None,
    limit: int | None,
) -> tuple[str, ...]:
    """Serialize the complete effective invocation for exact run provenance."""

    argv: list[str] = ["leanfaith", "collect-evidence"]
    for path in context_paths:
        argv.extend(("--contexts", str(path.resolve())))
    for path in theorem_paths:
        argv.extend(("--theorems", str(path.resolve())))
    for path in representation_paths:
        argv.extend(("--representations", str(path.resolve())))
    argv.extend(("--pairs", str(pair_path.resolve())))
    argv.extend(("--project-dir", str(project_dir.resolve())))
    for path in upstream_evidence_paths:
        argv.extend(("--upstream-evidence", str(path.resolve())))
    argv.extend(("--out-dir", str(out_dir)))
    argv.extend(("--cache-dir", str(cache_dir)))
    argv.extend(("--artifact-dir", str(artifact_dir)))
    if alignment_path is not None:
        argv.extend(("--alignments", str(alignment_path.resolve())))
    argv.extend(("--artifact-class", artifact_class.value))
    if memory_hard_limit_mb is not None:
        argv.extend(("--memory-hard-limit-mb", str(memory_hard_limit_mb)))
    if limit is not None:
        argv.extend(("--limit", str(limit)))
    argv.extend(("--root", str(paths.root.resolve())))
    return tuple(argv)


def run_collect_evidence(
    *,
    paths: RepoPaths,
    context_paths: Sequence[Path],
    theorem_paths: Sequence[Path],
    representation_paths: Sequence[Path],
    pair_path: Path,
    project_dir: Path,
    upstream_evidence_paths: Sequence[Path] = (),
    out_dir: Path | None = None,
    cache_dir: Path | None = None,
    artifact_dir: Path | None = None,
    alignment_path: Path | None = None,
    artifact_class: str = "auto",
    memory_hard_limit_mb: int | None = None,
    limit: int | None = None,
    created_at: datetime.datetime | None = None,
) -> EvidenceCollectionArtifacts:
    """Collect LF-020 evidence from explicit partitions without resolving labels."""

    wall_started_ns = _monotonic_ns()
    if not context_paths:
        raise EvidenceCollectionInputError("at least one --contexts partition is required")
    if not theorem_paths:
        raise EvidenceCollectionInputError("at least one --theorems partition is required")
    if not representation_paths:
        raise EvidenceCollectionInputError("at least one --representations partition is required")
    if limit is not None and limit < 1:
        raise EvidenceCollectionInputError("limit must be positive")

    contexts = tuple(
        record for path in context_paths for record in _load_jsonl(path, ContextRecord)
    )
    theorems = tuple(
        record for path in theorem_paths for record in _load_jsonl(path, TheoremRecord)
    )
    representations = tuple(
        record
        for path in representation_paths
        for record in _load_jsonl(path, RepresentationRecord)
    )
    pairs = _load_jsonl(pair_path, PairRecord)
    if limit is not None:
        pairs = pairs[:limit]
    upstream_evidence = tuple(
        record for path in upstream_evidence_paths for record in _load_jsonl(path, EvidenceRecord)
    )
    alignments = () if alignment_path is None else _load_jsonl(alignment_path, ClaimAlignmentSpec)

    context_by_id = _index_unique(
        contexts,
        id_field="context_id",
        record_kind="context",
    )
    theorem_by_id = _index_unique(
        theorems,
        id_field="theorem_id",
        record_kind="theorem",
    )
    _index_unique(
        representations,
        id_field="representation_id",
        record_kind="representation",
    )
    pair_by_id = _index_unique(pairs, id_field="pair_id", record_kind="pair")
    upstream_evidence_by_id = _index_unique(
        upstream_evidence,
        id_field="evidence_id",
        record_kind="upstream-evidence",
    )
    alignment_by_pair = _index_unique(
        alignments,
        id_field="pair_id",
        record_kind="claim-alignment",
    )
    representation_by_theorem = _representation_by_theorem(representations)
    for theorem in theorems:
        if theorem.context_id not in context_by_id:
            raise EvidenceCollectionInputError(
                f"theorem {theorem.theorem_id} references missing context {theorem.context_id}"
            )
    for representation in representations:
        matched_theorem = theorem_by_id.get(representation.theorem_id)
        if matched_theorem is None:
            raise EvidenceCollectionInputError(
                f"representation {representation.representation_id} references missing "
                f"theorem {representation.theorem_id}"
            )
        if representation.context_id != matched_theorem.context_id:
            raise EvidenceCollectionInputError(
                f"representation {representation.representation_id} context "
                f"{representation.context_id} does not match theorem context "
                f"{matched_theorem.context_id}"
            )
    unknown_alignment_pairs = sorted(set(alignment_by_pair) - set(pair_by_id))
    if unknown_alignment_pairs:
        raise EvidenceCollectionInputError(
            "claim-alignment input targets absent pairs: " + ", ".join(unknown_alignment_pairs)
        )
    for pair in pairs:
        missing_evidence = sorted(set(pair.evidence_ids) - set(upstream_evidence_by_id))
        if missing_evidence:
            raise EvidenceCollectionInputError(
                f"pair {pair.pair_id} has unresolved preexisting evidence links: "
                + ", ".join(missing_evidence)
                + "; pass their canonical records with --upstream-evidence"
            )
        for evidence_id in pair.evidence_ids:
            upstream = upstream_evidence_by_id[evidence_id]
            if upstream.target_kind.value != "lean_pair" or upstream.target_id != pair.pair_id:
                raise EvidenceCollectionInputError(
                    f"upstream evidence {evidence_id} does not target pair {pair.pair_id}"
                )
    environment_schema_version = _validate_context_compatibility(contexts)
    effective_class = resolve_artifact_class(
        requested=artifact_class,
        theorems=theorems,
        pairs=pairs,
        upstream_evidence=upstream_evidence,
    )

    now = created_at or datetime.datetime.now(tz=datetime.UTC)
    if now.tzinfo is None or now.utcoffset() != datetime.timedelta(0):
        raise EvidenceCollectionInputError("created_at must be timezone-aware UTC")
    run_id = new_run_id(now)
    resolved_output = (out_dir or paths.data / "evidence" / "lf020_symbolic_v1" / run_id).resolve()
    if resolved_output.exists() and any(resolved_output.iterdir()):
        raise EvidenceCollectionInputError(f"output directory is not empty: {resolved_output}")
    resolved_output.mkdir(parents=True, exist_ok=True)
    resolved_cache = (
        cache_dir or paths.data / "cache" / "evidence" / "lf020_symbolic_v1"
    ).resolve()
    resolved_artifacts = (
        artifact_dir or paths.artifacts / "evidence" / "lf020_symbolic_v1" / run_id
    ).resolve()

    configs = load_evidence_configs(paths)
    environment_path = paths.configs / "environment.lock.yaml"
    semantic_policy_path = paths.policies / "semantic_policy_v1.md"
    evidence_policy_path = paths.policies / "evidence_policy_v1.yaml"
    for required in (environment_path, semantic_policy_path, evidence_policy_path):
        if not required.is_file():
            raise EvidenceCollectionInputError(f"required policy/config is missing: {required}")
    environment_hash = hash_file(environment_path)
    semantic_policy_hash = hash_file(semantic_policy_path)
    evidence_policy_hash = hash_file(evidence_policy_path)
    settings = EvidenceCollectorSettings(
        root=paths.root.resolve(),
        artifact_dir=resolved_artifacts,
        environment_hash=environment_hash,
        semantic_policy_version="semantic_policy_v1",
        semantic_policy_hash=semantic_policy_hash,
        created_at=now,
    )
    measured_cache = _MeasuredEvidenceCache(
        EvidenceCache(resolved_cache, artifact_root=paths.root.resolve())
    )

    backends: dict[str, _MeasuredLeanBackend] = {}
    collectors: dict[str, SymbolicEvidenceCollector] = {}
    evidence: list[EvidenceRecord] = []
    enriched_pairs: list[PairRecord] = []
    failures: list[EvidenceCollectionFailure] = []

    def collector_for(context: ContextRecord) -> SymbolicEvidenceCollector:
        existing = collectors.get(context.context_id)
        if existing is not None:
            return existing
        backend = _MeasuredLeanBackend(
            LeanInteractBackend(
                BackendSettings(
                    project_dir=project_dir.resolve(),
                    context_fingerprint=context.context_fingerprint,
                    environment_schema_version=context.environment_schema_version,
                    raw_response_dir=resolved_output
                    / "raw_responses"
                    / context.context_fingerprint,
                    memory_hard_limit_mb=memory_hard_limit_mb,
                )
            )
        )
        collector = SymbolicEvidenceCollector(
            backend=backend,
            configs=configs,
            cache=measured_cache,  # type: ignore[arg-type]
            settings=settings,
        )
        backends[context.context_id] = backend
        collectors[context.context_id] = collector
        return collector

    try:
        for pair in pair_by_id.values():
            try:
                theorem_a = theorem_by_id[pair.theorem_a_id]
                theorem_b = theorem_by_id[pair.theorem_b_id]
                representation_a = representation_by_theorem[pair.theorem_a_id]
                representation_b = representation_by_theorem[pair.theorem_b_id]
                context_a = context_by_id[theorem_a.context_id]
                context_b = context_by_id[theorem_b.context_id]
                group_violations = check_pair_groups(pair, theorem_a, theorem_b)
                if group_violations:
                    raise EvidenceCollectionInputError("; ".join(group_violations))
                for theorem, representation in (
                    (theorem_a, representation_a),
                    (theorem_b, representation_b),
                ):
                    if representation.theorem_id != theorem.theorem_id:
                        raise EvidenceCollectionInputError(
                            f"representation {representation.representation_id} targets "
                            f"{representation.theorem_id}, expected {theorem.theorem_id}"
                        )
                    if representation.context_id != theorem.context_id:
                        raise EvidenceCollectionInputError(
                            f"theorem/representation context mismatch for {theorem.theorem_id}"
                        )
            except (KeyError, EvidenceCollectionInputError) as exc:
                failures.append(
                    _failure(
                        pair_id=pair.pair_id,
                        stage="lineage_validation",
                        exc=exc,
                        created_at=now,
                    )
                )
                continue

            try:
                result: PairEvidenceResult = collector_for(context_a).collect_pair(
                    pair=pair,
                    theorem_a=theorem_a,
                    theorem_b=theorem_b,
                    representation_a=representation_a,
                    representation_b=representation_b,
                    context_a=context_a,
                    context_b=context_b,
                    alignment=alignment_by_pair.get(pair.pair_id),
                )
            except (EvidencePipelineError, RuntimeError, ValidationError, ValueError) as exc:
                failures.append(
                    _failure(
                        pair_id=pair.pair_id,
                        stage="evidence_collection",
                        exc=exc,
                        created_at=now,
                    )
                )
                continue
            evidence.extend(result.evidence)
            enriched_pairs.append(result.pair)
    finally:
        for backend in backends.values():
            backend.close()

    if len({record.evidence_id for record in evidence}) != len(evidence):
        raise EvidenceCollectionInputError("duplicate evidence IDs across pair results")

    evidence_path = resolved_output / "evidence.jsonl"
    enriched_pair_path = resolved_output / "pairs.jsonl"
    failure_path = resolved_output / "failures.jsonl"
    evidence_hash = _write_jsonl(evidence, evidence_path)
    pair_hash = _write_jsonl(enriched_pairs, enriched_pair_path)
    failure_hash = _write_jsonl(failures, failure_path)
    (
        artifact_catalog_path,
        artifact_catalog_hash,
        artifact_catalog,
        cache_catalog_path,
        cache_catalog_hash,
        cache_catalog,
    ) = _write_evidence_catalogs(
        paths=paths,
        run_id=run_id,
        output_dir=resolved_output,
        cache_dir=resolved_cache,
        cache_entries=measured_cache.entries,
    )
    emitted_cache_keys = {
        raw_key
        for record in evidence
        if isinstance((raw_key := record.metadata.get("cache_key")), str)
    }
    uncataloged_cache_keys = emitted_cache_keys - set(measured_cache.entries)
    if uncataloged_cache_keys:
        raise EvidenceCollectionInputError(
            "emitted evidence references cache keys not touched by this run: "
            + ", ".join(sorted(uncataloged_cache_keys))
        )

    input_paths = (
        *context_paths,
        *theorem_paths,
        *representation_paths,
        pair_path,
        *upstream_evidence_paths,
        *((alignment_path,) if alignment_path is not None else ()),
    )
    input_hashes = {
        _relative_or_absolute(path, paths.root): hash_file(path) for path in input_paths
    }
    config_hashes = {
        _relative_or_absolute(configs.portfolio.path, paths.root): configs.portfolio.config_hash,
        _relative_or_absolute(
            configs.counterexample.path, paths.root
        ): configs.counterexample.config_hash,
        _relative_or_absolute(configs.sampling.path, paths.root): configs.sampling.config_hash,
        _relative_or_absolute(semantic_policy_path, paths.root): semantic_policy_hash,
        _relative_or_absolute(evidence_policy_path, paths.root): evidence_policy_hash,
    }
    combined_config_hash = hash_canonical(config_hashes)
    source_revision = hash_canonical(input_hashes)
    context_hash = hash_canonical(
        [
            context.model_dump(mode="json")
            for context in sorted(contexts, key=lambda item: item.context_id)
        ]
    )
    code_state = collect_code_state(paths.root)
    output_checksums = {
        _relative_or_absolute(evidence_path, paths.root): evidence_hash,
        _relative_or_absolute(enriched_pair_path, paths.root): pair_hash,
        _relative_or_absolute(artifact_catalog_path, paths.root): artifact_catalog_hash,
        _relative_or_absolute(cache_catalog_path, paths.root): cache_catalog_hash,
    }
    failure_checksums = {
        _relative_or_absolute(failure_path, paths.root): failure_hash,
    }
    all_checksums = {**output_checksums, **failure_checksums}
    terminal_jobs = sum(record.kind != EvidenceKind.AXIOM_AUDIT for record in evidence)
    axiom_audits = sum(record.kind == EvidenceKind.AXIOM_AUDIT for record in evidence)
    evidence_jobs = 5 * len(pairs)
    backend_request_attempts = sum(backend.request_attempt_count for backend in backends.values())
    backend_elapsed_ms = sum(backend.elapsed_ms for backend in backends.values())
    retry_count = sum(backend.retry_count for backend in backends.values())
    unique_request_hashes = {
        request_hash for backend in backends.values() for request_hash in backend.request_hashes
    }
    lean_request_attempts = sum(
        len(entry.lean_request_hashes) for entry in measured_cache.entries.values()
    )
    wall_elapsed_seconds = max(
        (_monotonic_ns() - wall_started_ns) / 1_000_000_000,
        1e-9,
    )
    measurements: dict[str, int | float] = {
        "wall_elapsed_seconds": wall_elapsed_seconds,
        "input_pairs": len(pairs),
        "evidence_jobs": evidence_jobs,
        "terminal_jobs_emitted": terminal_jobs,
        "axiom_audits": axiom_audits,
        "evidence_records": len(evidence),
        "lean_request_attempts": lean_request_attempts,
        "lean_backend_calls": backend_request_attempts,
        "lean_unique_request_hashes": len(unique_request_hashes),
        "lean_backend_elapsed_ms": backend_elapsed_ms,
        "retries": retry_count,
        "cache_hits": measured_cache.hit_count,
        "cache_misses": measured_cache.miss_count,
        "cache_puts": measured_cache.put_count,
        "artifact_catalog_entries": len(artifact_catalog.entries),
        "cache_catalog_entries": len(cache_catalog.entries),
        "pairs_per_second": len(pairs) / wall_elapsed_seconds,
        "evidence_records_per_second": len(evidence) / wall_elapsed_seconds,
    }
    status_counts: dict[str, int] = {
        "input_pairs": len(pairs),
        "evidence_jobs": evidence_jobs,
        "terminal_jobs_emitted": terminal_jobs,
        "axiom_audits": axiom_audits,
        "enriched_pairs": len(enriched_pairs),
        "evidence_records": len(evidence),
        "pair_failures": len(failures),
        "cache_hits": measured_cache.hit_count,
        "cache_misses": measured_cache.miss_count,
        "cache_puts": measured_cache.put_count,
        "lean_request_attempts": lean_request_attempts,
        "lean_backend_calls": backend_request_attempts,
        "lean_request_retries": retry_count,
        "artifact_catalog_entries": len(artifact_catalog.entries),
        "cache_catalog_entries": len(cache_catalog.entries),
        "upstream_evidence_records": len(upstream_evidence),
        "resolved_labels_created": 0,
    }
    for record in evidence:
        key = f"evidence_status_{record.status.value}"
        status_counts[key] = status_counts.get(key, 0) + 1

    output_manifest = OutputManifest(
        stage=DataStage.EVIDENCE_COLLECTED,
        artifact_class=effective_class,
        run_id=run_id,
        source="lf020_symbolic_evidence",
        source_revision=source_revision,
        config_hash=combined_config_hash,
        record_schema_version=CURRENT_RECORD_SCHEMA_VERSION,
        row_count=len(evidence),
        attempted_row_count=len(pairs),
        terminal_outcome_counts=status_counts,
        file_checksums=all_checksums,
        input_partition_checksums=input_hashes,
        output_partition_checksums=output_checksums,
        failure_partition_checksums=failure_checksums,
        environment_hash=environment_hash,
        context_hash=context_hash,
        code_tree_hash=code_state.code_tree_hash,
        code=code_state,
        created_at=now,
        notes=(
            "Evidence-only LF-020 output. No labels were created or emitted; "
            "not_proved/not_found remain evidence outcomes. Preexisting pair evidence "
            f"was validated from {len(upstream_evidence_paths)} bound partition(s) "
            f"containing {len(upstream_evidence)} record(s), and was not duplicated "
            "into the LF-020 evidence partition."
        ),
    )
    output_manifest_path = resolved_output / "manifest.json"
    output_manifest_hash = write_manifest(output_manifest, output_manifest_path)

    output_hashes = {
        **all_checksums,
        _relative_or_absolute(output_manifest_path, paths.root): output_manifest_hash,
    }
    run_manifest = RunManifest(
        run_id=run_id,
        artifact_class=effective_class,
        command="leanfaith collect-evidence",
        argv=_effective_argv(
            paths=paths,
            context_paths=context_paths,
            theorem_paths=theorem_paths,
            representation_paths=representation_paths,
            pair_path=pair_path,
            project_dir=project_dir,
            upstream_evidence_paths=upstream_evidence_paths,
            out_dir=resolved_output,
            cache_dir=resolved_cache,
            artifact_dir=resolved_artifacts,
            alignment_path=alignment_path,
            artifact_class=effective_class,
            memory_hard_limit_mb=memory_hard_limit_mb,
            limit=limit,
        ),
        code=code_state,
        environment_schema_version=environment_schema_version,
        environment={
            "project_dir": str(project_dir.resolve()),
            "environment_lock_sha256": environment_hash,
        },
        config_hashes=config_hashes,
        input_hashes=input_hashes,
        output_hashes=output_hashes,
        execution={
            "context_count": len(contexts),
            "memory_hard_limit_mb": memory_hard_limit_mb,
            "artifact_class": effective_class.value,
            "label_resolution": False,
            "upstream_evidence_partition_count": len(upstream_evidence_paths),
            "upstream_evidence_record_count": len(upstream_evidence),
            "artifact_catalog": _relative_or_absolute(artifact_catalog_path, paths.root),
            "cache_catalog": _relative_or_absolute(cache_catalog_path, paths.root),
        },
        status_counts=status_counts,
        retry_count=retry_count,
        measurements=measurements,
        created_at=now,
        notes="LF-020 symbolic evidence collection; label resolution is out of scope.",
    )
    resolved_run_manifest_path = run_manifest_path(paths, run_id)
    write_manifest(run_manifest, resolved_run_manifest_path)

    return EvidenceCollectionArtifacts(
        run_id=run_id,
        artifact_class=effective_class,
        output_dir=resolved_output,
        evidence_path=evidence_path,
        pair_path=enriched_pair_path,
        failure_path=failure_path,
        artifact_catalog_path=artifact_catalog_path,
        cache_catalog_path=cache_catalog_path,
        output_manifest_path=output_manifest_path,
        run_manifest_path=resolved_run_manifest_path,
        evidence_count=len(evidence),
        pair_count=len(enriched_pairs),
        failure_count=len(failures),
        cache_hits=measured_cache.hit_count,
        cache_misses=measured_cache.miss_count,
        lean_request_attempts=lean_request_attempts,
        retry_count=retry_count,
        wall_elapsed_seconds=wall_elapsed_seconds,
    )
