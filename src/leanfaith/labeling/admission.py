"""Artifact-backed verification for LF-020 evidence admissions.

This module is deliberately an adapter, not an admission factory.  It verifies
an existing :class:`EvidenceAdmissionRecord` against both clean LF-020 replay
trees, the persisted replay report, the active policies, and an independently
frozen target/runtime binding.  It never creates an admission, a resolution
candidate, or a label, and therefore does not relax the LF-024 production
guard.

The current ``EvidenceAdmissionRecord`` stores opaque artifact IDs and hashes,
but no artifact locations.  Callers must consequently supply explicit,
root-confined locators and the complete replay inputs.  The exact bytes and
the recomputed replay report are still cryptographically checked; the locator
is not treated as evidence by itself.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, ValidationError, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.evidence.replay import (
    EvidenceReplayInputError,
    EvidenceReplayReport,
    compare_lf020_replays,
)
from leanfaith.labeling.aggregation import EvidenceAdmissionRecord
from leanfaith.labeling.quality import (
    LabelResolutionPolicyError,
    load_active_label_resolution_policy,
)
from leanfaith.lean.cache import (
    EvidenceCacheEntry,
    evidence_semantic_hash,
)
from leanfaith.schemas.enums import (
    ArtifactClass,
    DataStage,
    EvidenceExecutionStatus,
    EvidenceKind,
    EvidenceTargetKind,
)
from leanfaith.schemas.evidence import EvidenceRecord
from leanfaith.schemas.ids import (
    CONTEXT_PREFIX,
    EVIDENCE_PREFIX,
    HEX64_PATTERN,
    PAIR_PREFIX,
    REPRESENTATION_PREFIX,
    THEOREM_PREFIX,
    id_pattern,
    make_id,
)
from leanfaith.schemas.manifest import MANIFEST_SCHEMA_VERSION, OutputManifest
from leanfaith.schemas.migrations import CURRENT_RECORD_SCHEMA_VERSION
from leanfaith.schemas.pair import PairRecord

_VERIFICATION_PREFIX = "evidence_admission_verification"


class EvidenceAdmissionVerificationError(ValueError):
    """An LF-020 admission artifact graph is missing, stale, or inconsistent."""


class AdmissionArtifactLocator(StrictModel):
    """Explicit location for one opaque artifact ID in an admission record."""

    artifact_id: str = Field(min_length=1)
    relative_path: str = Field(min_length=1)

    @model_validator(mode="after")
    def _safe_relative_path(self) -> Self:
        path = Path(self.relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("admission artifact paths must be root-relative and non-escaping")
        return self


class LF020TargetRuntimeBinding(StrictModel):
    """Frozen pair, representation, context, and environment identity.

    These fields are copied from the active input/context manifests, not
    inferred from the artifacts being admitted.  Every primary LF-020 cache
    key for the target must match them exactly.
    """

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
    semantic_policy_version: str = Field(min_length=1)
    semantic_policy_sha256: str = Field(pattern=HEX64_PATTERN)
    lean_version: str = Field(min_length=1)
    lean_interact_version: str = Field(min_length=1)
    repl_revision: str = Field(min_length=1)
    project_revision: str = Field(min_length=1)


class LF020ExpectedEvidenceBinding(StrictModel):
    """Frozen expected identity for one output or auxiliary evidence record."""

    evidence_id: str = Field(pattern=id_pattern(EVIDENCE_PREFIX))
    kind: EvidenceKind
    status: EvidenceExecutionStatus
    method_version: str = Field(min_length=1)
    config_hash: str = Field(pattern=HEX64_PATTERN)
    semantic_evidence_sha256: str = Field(pattern=HEX64_PATTERN)
    cache_key_hash: str = Field(pattern=HEX64_PATTERN)
    evidence_direction: Literal["none", "A_to_B", "B_to_A", "equivalence_only"]
    timeout_seconds: float = Field(gt=0)


class LF020ExpectedAdmissionBinding(StrictModel):
    """Independent allowlist against which LF-020 evidence is verified."""

    schema_version: Literal[1] = 1
    binding_id: str = Field(pattern=id_pattern("lf020_expected_admission"))
    target_kind: Literal[EvidenceTargetKind.LEAN_PAIR] = EvidenceTargetKind.LEAN_PAIR
    target_id: str = Field(pattern=id_pattern(PAIR_PREFIX))
    label_resolution_policy_sha256: str = Field(pattern=HEX64_PATTERN)
    output_manifest_config_hash: str = Field(pattern=HEX64_PATTERN)
    output_manifest_context_hash: str = Field(pattern=HEX64_PATTERN)
    source_pair_fingerprint: str = Field(pattern=HEX64_PATTERN)
    upstream_evidence_id_fingerprint: str = Field(pattern=HEX64_PATTERN)
    runtime: LF020TargetRuntimeBinding
    evidence: tuple[LF020ExpectedEvidenceBinding, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _canonical_and_content_addressed(self) -> Self:
        if self.runtime.pair_id != self.target_id:
            raise ValueError("runtime pair_id must equal target_id")
        keys = tuple(item.evidence_id for item in self.evidence)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("expected evidence bindings must be sorted and unique")
        expected_id = make_id(
            "lf020_expected_admission",
            self.model_dump(mode="json", exclude={"binding_id"}),
        )
        if self.binding_id != expected_id:
            raise ValueError("binding_id does not match expected admission content")
        return self


def build_lf020_expected_admission_binding(
    *,
    target_id: str,
    label_resolution_policy_sha256: str,
    output_manifest_config_hash: str,
    output_manifest_context_hash: str,
    source_pair_fingerprint: str,
    upstream_evidence_id_fingerprint: str,
    runtime: LF020TargetRuntimeBinding,
    evidence: Sequence[LF020ExpectedEvidenceBinding],
) -> LF020ExpectedAdmissionBinding:
    """Build the canonical expected binding from an independent frozen source."""

    ordered = tuple(sorted(evidence, key=lambda item: item.evidence_id))
    if len(ordered) != len({item.evidence_id for item in ordered}):
        raise EvidenceAdmissionVerificationError("duplicate expected evidence IDs")
    values: dict[str, object] = {
        "schema_version": 1,
        "target_kind": EvidenceTargetKind.LEAN_PAIR,
        "target_id": target_id,
        "label_resolution_policy_sha256": label_resolution_policy_sha256,
        "output_manifest_config_hash": output_manifest_config_hash,
        "output_manifest_context_hash": output_manifest_context_hash,
        "source_pair_fingerprint": source_pair_fingerprint,
        "upstream_evidence_id_fingerprint": upstream_evidence_id_fingerprint,
        "runtime": runtime,
        "evidence": ordered,
    }
    id_payload: dict[str, object] = {
        "schema_version": 1,
        "target_kind": EvidenceTargetKind.LEAN_PAIR.value,
        "target_id": target_id,
        "label_resolution_policy_sha256": label_resolution_policy_sha256,
        "output_manifest_config_hash": output_manifest_config_hash,
        "output_manifest_context_hash": output_manifest_context_hash,
        "source_pair_fingerprint": source_pair_fingerprint,
        "upstream_evidence_id_fingerprint": upstream_evidence_id_fingerprint,
        "runtime": runtime.model_dump(mode="json"),
        "evidence": [item.model_dump(mode="json") for item in ordered],
    }
    return LF020ExpectedAdmissionBinding.model_validate(
        {
            "binding_id": make_id("lf020_expected_admission", id_payload),
            **values,
        }
    )


@dataclass(frozen=True, slots=True)
class LF020ReplayInputs:
    """All artifacts required to recompute an LF-020 replay report."""

    left_output_dir: Path
    left_cache_root: Path
    left_artifact_root: Path
    right_output_dir: Path
    right_cache_root: Path
    right_artifact_root: Path
    source_pairs: tuple[PairRecord, ...]
    upstream_evidence_ids: tuple[str, ...] = ()


class DiagnosticLF020EvidenceAdmissionVerification(StrictModel):
    """Content-addressed diagnostic receipt, never production authority."""

    schema_version: Literal[1] = 1
    verification_id: str = Field(pattern=id_pattern(_VERIFICATION_PREFIX))
    admission_id: str
    expected_binding_id: str
    target_kind: Literal[EvidenceTargetKind.LEAN_PAIR] = EvidenceTargetKind.LEAN_PAIR
    target_id: str = Field(pattern=id_pattern(PAIR_PREFIX))
    evidence_ids: tuple[str, ...]
    manifest_artifact_id: str
    manifest_artifact_sha256: str = Field(pattern=HEX64_PATTERN)
    replay_artifact_id: str
    replay_artifact_sha256: str = Field(pattern=HEX64_PATTERN)
    replay_report_hash: str = Field(pattern=HEX64_PATTERN)
    matched_replay_side: Literal["left", "right"]
    output_run_id: str
    context_id: str = Field(pattern=id_pattern(CONTEXT_PREFIX))
    context_fingerprint: str = Field(pattern=HEX64_PATTERN)
    semantic_policy_sha256: str = Field(pattern=HEX64_PATTERN)
    label_resolution_policy_sha256: str = Field(pattern=HEX64_PATTERN)
    cache_key_hashes: tuple[str, ...]
    production_guard_removed: Literal[False] = False
    production_authority_established: Literal[False] = False
    admissions_created: Literal[0] = 0
    labels_created: Literal[0] = 0

    @model_validator(mode="after")
    def _content_addressed(self) -> Self:
        if self.evidence_ids != tuple(sorted(set(self.evidence_ids))):
            raise ValueError("verified evidence_ids must be sorted and unique")
        if self.cache_key_hashes != tuple(sorted(set(self.cache_key_hashes))):
            raise ValueError("verified cache_key_hashes must be sorted and unique")
        expected = make_id(
            _VERIFICATION_PREFIX,
            self.model_dump(mode="json", exclude={"verification_id"}),
        )
        if self.verification_id != expected:
            raise ValueError("verification_id does not match receipt content")
        return self


@dataclass(frozen=True, slots=True)
class LF020EvidenceAdmissionDiagnosticResult:
    """Diagnostic receipt plus exact output evidence records."""

    receipt: DiagnosticLF020EvidenceAdmissionVerification
    evidence_records: tuple[EvidenceRecord, ...]


class _CacheCatalogEntry(StrictModel):
    cache_key_hash: str = Field(pattern=HEX64_PATTERN)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)


class _CacheCatalog(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    entries: tuple[_CacheCatalogEntry, ...]

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        keys = tuple(item.cache_key_hash for item in self.entries)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("cache catalog keys must be sorted and unique")
        return self


class _ArtifactCatalogEntry(StrictModel):
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)
    kind: Literal["raw_response", "evidence_artifact"]


class _ArtifactCatalog(StrictModel):
    schema_version: Literal[1] = 1
    run_id: str
    entries: tuple[_ArtifactCatalogEntry, ...]

    @model_validator(mode="after")
    def _canonical(self) -> Self:
        keys = tuple((item.kind, item.path, item.sha256) for item in self.entries)
        if keys != tuple(sorted(keys)):
            raise ValueError("artifact catalog entries must be canonically sorted")
        if len({item.path for item in self.entries}) != len(self.entries):
            raise ValueError("artifact catalog paths must be unique")
        return self


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> float:
    raise ValueError(f"non-finite JSON constant {value!r}")


def _reject_symlink_components(path: Path, *, role: str) -> None:
    """Reject a symlink at any lexical component of an absolute path."""

    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.is_symlink():
            raise EvidenceAdmissionVerificationError(
                f"{role} traverses symlink component {current}"
            )


def _path_within_root(root: Path, path: Path, *, role: str) -> Path:
    """Resolve ``path`` while rejecting symlinks in every root-local component."""

    lexical_root = root.absolute()
    if lexical_root.is_symlink():
        raise EvidenceAdmissionVerificationError("verification_root must not be a symlink")
    lexical = path.absolute() if path.is_absolute() else (lexical_root / path)
    try:
        relative = lexical.relative_to(lexical_root)
    except ValueError as exc:
        raise EvidenceAdmissionVerificationError(f"{role} escapes verification root") from exc
    current = lexical_root
    for component in relative.parts:
        current /= component
        if current.is_symlink():
            raise EvidenceAdmissionVerificationError(
                f"{role} traverses symlink component {current}"
            )
    resolved_root = lexical_root.resolve()
    resolved = lexical.resolve()
    if not resolved.is_relative_to(resolved_root):
        raise EvidenceAdmissionVerificationError(f"{role} escapes verification root")
    return resolved


def _regular_path(root: Path, relative_path: str, *, role: str) -> Path:
    relative = Path(relative_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise EvidenceAdmissionVerificationError(f"{role} path escapes verification root")
    resolved = _path_within_root(root, relative, role=role)
    if not resolved.is_file():
        raise EvidenceAdmissionVerificationError(f"{role} is missing or not a regular file")
    return resolved


def _directory_within(root: Path, path: Path, *, role: str) -> Path:
    resolved = _path_within_root(root, path, role=role)
    if not resolved.is_dir():
        raise EvidenceAdmissionVerificationError(f"{role} is missing or not a directory")
    return resolved


def _load_canonical_model[ModelT: StrictModel](
    path: Path,
    model_type: type[ModelT],
    *,
    role: str,
) -> ModelT:
    try:
        raw = path.read_bytes()
        document = json.loads(
            raw,
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite,
        )
        model = model_type.model_validate(document)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        raise EvidenceAdmissionVerificationError(f"invalid {role}: {exc}") from exc
    expected = canonical_json_bytes(model.model_dump(mode="json")) + b"\n"
    if raw != expected:
        raise EvidenceAdmissionVerificationError(f"{role} is not canonical JSON")
    return model


def _load_canonical_jsonl[ModelT: StrictModel](
    path: Path,
    model_type: type[ModelT],
    *,
    role: str,
) -> tuple[ModelT, ...]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise EvidenceAdmissionVerificationError(f"cannot read {role}: {exc}") from exc
    records: list[ModelT] = []
    canonical_parts: list[bytes] = []
    for line_number, line in enumerate(raw.splitlines(), start=1):
        if not line.strip():
            raise EvidenceAdmissionVerificationError(
                f"{role}:{line_number} contains a blank noncanonical record"
            )
        try:
            document = json.loads(
                line,
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_reject_nonfinite,
            )
            record = model_type.model_validate(document)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            raise EvidenceAdmissionVerificationError(
                f"invalid {role}:{line_number}: {exc}"
            ) from exc
        records.append(record)
        canonical_parts.append(canonical_json_bytes(record.model_dump(mode="json")) + b"\n")
    if raw != b"".join(canonical_parts):
        raise EvidenceAdmissionVerificationError(f"{role} is not canonical JSONL")
    return tuple(records)


def _active_semantic_policy_hash(repo_root: Path) -> str:
    """Return the semantic-policy hash bound by the already-validated Gate 0."""

    gate_path = _regular_path(
        repo_root,
        "reports/gates/gate_0.json",
        role="Gate-0 report",
    )
    try:
        gate = json.loads(
            gate_path.read_bytes(),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite,
        )
        inputs = gate["inputs"]
        expected = inputs["semantic_policy"]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise EvidenceAdmissionVerificationError(
            f"Gate 0 does not expose a semantic-policy binding: {exc}"
        ) from exc
    if not isinstance(expected, str) or len(expected) != 64:
        raise EvidenceAdmissionVerificationError("Gate-0 semantic-policy hash is invalid")
    semantic_path = _regular_path(
        repo_root,
        "policies/semantic_policy_v1.md",
        role="active semantic policy",
    )
    if hash_file(semantic_path) != expected:
        raise EvidenceAdmissionVerificationError(
            "stale semantic policy: Gate 0 does not bind its exact active bytes"
        )
    return expected


def _catalog_artifact_paths(
    *,
    verification_root: Path,
    output_dir: Path,
) -> tuple[Path, ...]:
    catalog_path = _path_within_root(
        verification_root,
        output_dir / "artifact_catalog.json",
        role="artifact catalog",
    )
    catalog = _load_canonical_model(catalog_path, _ArtifactCatalog, role="artifact catalog")
    paths: list[Path] = []
    for entry in catalog.entries:
        paths.append(
            _path_within_root(
                verification_root,
                Path(entry.path),
                role=f"cataloged evidence artifact {entry.path}",
            )
        )
    return tuple(paths)


def _stability_snapshot(
    *,
    verification_root: Path,
    replay_inputs: LF020ReplayInputs,
    direct_paths: Iterable[Path],
) -> dict[str, str]:
    """Hash the exact replay/policy graph before and after verification."""

    files: set[Path] = set()
    for direct in direct_paths:
        resolved = _path_within_root(verification_root, direct, role="verification input")
        if not resolved.is_file():
            raise EvidenceAdmissionVerificationError("verification input is not a regular file")
        files.add(resolved)
    for role, directory in (
        ("left output", replay_inputs.left_output_dir),
        ("left cache", replay_inputs.left_cache_root),
        ("right output", replay_inputs.right_output_dir),
        ("right cache", replay_inputs.right_cache_root),
    ):
        resolved_dir = _directory_within(verification_root, directory, role=role)
        for candidate in resolved_dir.rglob("*"):
            resolved = _path_within_root(
                verification_root,
                candidate,
                role=f"{role} member",
            )
            if resolved.is_file():
                files.add(resolved)
    for output_dir in (replay_inputs.left_output_dir, replay_inputs.right_output_dir):
        files.update(
            _catalog_artifact_paths(
                verification_root=verification_root,
                output_dir=output_dir,
            )
        )
    return {
        path.relative_to(verification_root.resolve()).as_posix(): hash_file(path)
        for path in sorted(files)
    }


def _unique_checksum_path(
    root: Path,
    checksums: dict[str, str],
    *,
    filename: str,
    role: str,
    allowed_root: Path,
) -> Path:
    matches = [(path, digest) for path, digest in checksums.items() if Path(path).name == filename]
    if len(matches) != 1:
        raise EvidenceAdmissionVerificationError(
            f"{role} must bind exactly one {filename}, found {len(matches)}"
        )
    raw_path, expected = matches[0]
    path = Path(raw_path)
    resolved = _path_within_root(root, path, role=filename)
    if not resolved.is_relative_to(allowed_root.resolve()):
        raise EvidenceAdmissionVerificationError(
            f"{filename} is not contained by the matched LF-020 output side"
        )
    if not resolved.is_file() or resolved.is_symlink():
        raise EvidenceAdmissionVerificationError(f"{filename} is missing or not regular")
    if hash_file(resolved) != expected:
        raise EvidenceAdmissionVerificationError(f"{filename} hash differs from manifest")
    return resolved


def _cache_entries(
    *,
    verification_root: Path,
    manifest: OutputManifest,
    output_dir: Path,
    cache_root: Path,
) -> tuple[EvidenceCacheEntry, ...]:
    catalog_path = _unique_checksum_path(
        verification_root,
        manifest.output_partition_checksums,
        filename="cache_catalog.json",
        role="output manifest",
        allowed_root=output_dir,
    )
    catalog = _load_canonical_model(catalog_path, _CacheCatalog, role="cache catalog")
    if catalog.run_id != manifest.run_id:
        raise EvidenceAdmissionVerificationError("cache catalog run_id differs from manifest")
    root = cache_root.resolve()
    records: list[EvidenceCacheEntry] = []
    for catalog_entry in catalog.entries:
        relative = Path(catalog_entry.path)
        if relative.is_absolute() or ".." in relative.parts:
            raise EvidenceAdmissionVerificationError("cache catalog path escapes cache root")
        resolved = _path_within_root(verification_root, cache_root / relative, role="cache entry")
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise EvidenceAdmissionVerificationError("cache entry is missing or escapes root")
        if hash_file(resolved) != catalog_entry.sha256:
            raise EvidenceAdmissionVerificationError("cache entry hash differs from catalog")
        record = _load_canonical_model(
            resolved,
            EvidenceCacheEntry,
            role=f"cache entry {catalog_entry.cache_key_hash}",
        )
        if record.cache_key_hash != catalog_entry.cache_key_hash:
            raise EvidenceAdmissionVerificationError("cache entry key differs from catalog")
        records.append(record)
    return tuple(records)


def _runtime_projection(entry: EvidenceCacheEntry) -> dict[str, object]:
    key = entry.cache_key
    return {
        "schema_version": 1,
        "pair_id": key.pair_id,
        "theorem_a_id": key.theorem_a_id,
        "theorem_b_id": key.theorem_b_id,
        "theorem_a_statement_hash": key.theorem_a_statement_hash,
        "theorem_b_statement_hash": key.theorem_b_statement_hash,
        "representation_a_id": key.representation_a_id,
        "representation_b_id": key.representation_b_id,
        "representation_a_content_hash": key.representation_a_content_hash,
        "representation_b_content_hash": key.representation_b_content_hash,
        "representation_version": key.representation_version,
        "context_id": key.context_id,
        "context_fingerprint": key.context_fingerprint,
        "environment_schema_version": key.environment_schema_version,
        "environment_hash": key.environment_hash,
        "semantic_policy_version": key.semantic_policy_version,
        "semantic_policy_sha256": key.semantic_policy_hash,
        "lean_version": key.lean_version,
        "lean_interact_version": key.lean_interact_version,
        "repl_revision": key.repl_revision,
        "project_revision": key.project_revision,
    }


def _record_bindings(
    entries: Iterable[EvidenceCacheEntry],
) -> dict[str, tuple[EvidenceRecord, EvidenceCacheEntry]]:
    bindings: dict[str, tuple[EvidenceRecord, EvidenceCacheEntry]] = {}
    for entry in entries:
        for record in (entry.evidence, *entry.auxiliary_evidence):
            if record.evidence_id in bindings:
                raise EvidenceAdmissionVerificationError(
                    f"evidence {record.evidence_id} appears in multiple cache entries"
                )
            bindings[record.evidence_id] = (record, entry)
    return bindings


def _validate_expected_binding(
    *,
    expected: LF020ExpectedAdmissionBinding,
    output_records: tuple[EvidenceRecord, ...],
    cache_bindings: dict[str, tuple[EvidenceRecord, EvidenceCacheEntry]],
) -> tuple[str, ...]:
    expected_by_id = {item.evidence_id: item for item in expected.evidence}
    output_by_id = {item.evidence_id: item for item in output_records}
    if len(output_by_id) != len(output_records):
        raise EvidenceAdmissionVerificationError("output evidence contains duplicate IDs")
    if set(output_by_id) != set(expected_by_id):
        raise EvidenceAdmissionVerificationError(
            "output target evidence IDs differ from the frozen expected binding"
        )
    if set(cache_bindings) != set(expected_by_id):
        raise EvidenceAdmissionVerificationError(
            "cache target evidence IDs differ from the frozen expected binding"
        )

    cache_keys: set[str] = set()
    for evidence_id, expected_record in expected_by_id.items():
        output = output_by_id[evidence_id]
        cached, entry = cache_bindings[evidence_id]
        if evidence_semantic_hash(output) != evidence_semantic_hash(cached):
            raise EvidenceAdmissionVerificationError(
                f"output evidence {evidence_id} differs from its cache payload"
            )
        actual = {
            "evidence_id": output.evidence_id,
            "kind": output.kind,
            "status": output.status,
            "method_version": output.method_version,
            "config_hash": output.config_hash,
            "semantic_evidence_sha256": evidence_semantic_hash(output),
            "cache_key_hash": entry.cache_key_hash,
            "evidence_direction": entry.cache_key.evidence_direction,
            "timeout_seconds": entry.cache_key.timeout_seconds,
        }
        if expected_record.model_dump(mode="python") != actual:
            raise EvidenceAdmissionVerificationError(
                f"evidence {evidence_id} differs from its frozen method/config binding"
            )
        if _runtime_projection(entry) != expected.runtime.model_dump(mode="python"):
            raise EvidenceAdmissionVerificationError(
                f"evidence {evidence_id} differs from its frozen target/context binding"
            )
        cache_keys.add(entry.cache_key_hash)
    return tuple(sorted(cache_keys))


def verify_lf020_evidence_admission(
    *,
    verification_root: Path,
    admission: EvidenceAdmissionRecord,
    manifest_locator: AdmissionArtifactLocator,
    replay_locator: AdmissionArtifactLocator,
    replay_inputs: LF020ReplayInputs,
    expected: LF020ExpectedAdmissionBinding,
) -> LF020EvidenceAdmissionDiagnosticResult:
    """Verify one production LF-020 evidence admission without promoting it.

    Both replay sides are inspected again through ``compare_lf020_replays``.
    The persisted report must be canonical and byte-bound by the admission,
    and the recomputed report must be identical.  The selected output side is
    then checked against an independent exact evidence/runtime binding.
    """

    _reject_symlink_components(verification_root, role="verification_root")
    root = verification_root.resolve()
    if not root.is_dir():
        raise EvidenceAdmissionVerificationError("verification_root is not a directory")
    if admission.target_kind is not EvidenceTargetKind.LEAN_PAIR:
        raise EvidenceAdmissionVerificationError(
            "the first typed adapter supports LF-020 lean_pair evidence only"
        )
    if not (
        admission.artifact_class is ArtifactClass.PRODUCTION
        and admission.replay_passed
        and admission.production_eligible
    ):
        raise EvidenceAdmissionVerificationError(
            "typed production verification requires a replay-passed production admission"
        )
    if (manifest_locator.artifact_id, replay_locator.artifact_id) != (
        admission.manifest_artifact_id,
        admission.replay_artifact_id,
    ):
        raise EvidenceAdmissionVerificationError("artifact locator ID differs from admission")

    manifest_path = _regular_path(
        root,
        manifest_locator.relative_path,
        role="admission manifest",
    )
    replay_path = _regular_path(
        root,
        replay_locator.relative_path,
        role="admission replay report",
    )
    for role, path in (
        ("left output", replay_inputs.left_output_dir),
        ("left cache", replay_inputs.left_cache_root),
        ("left artifact", replay_inputs.left_artifact_root),
        ("right output", replay_inputs.right_output_dir),
        ("right cache", replay_inputs.right_cache_root),
        ("right artifact", replay_inputs.right_artifact_root),
    ):
        _directory_within(root, path, role=role)

    stable_direct_paths = (
        manifest_path,
        replay_path,
        root / "policies/label_resolution_v1.yaml",
        root / "policies/semantic_policy_v1.md",
        root / "reports/gates/gate_0.json",
    )
    before_snapshot = _stability_snapshot(
        verification_root=root,
        replay_inputs=replay_inputs,
        direct_paths=stable_direct_paths,
    )
    if hash_file(manifest_path) != admission.manifest_artifact_sha256:
        raise EvidenceAdmissionVerificationError("admission manifest hash mismatch")
    if hash_file(replay_path) != admission.replay_artifact_sha256:
        raise EvidenceAdmissionVerificationError("admission replay hash mismatch")

    try:
        active_policy = load_active_label_resolution_policy(root)
    except LabelResolutionPolicyError as exc:
        raise EvidenceAdmissionVerificationError(
            f"active Gate-0 label policy is invalid: {exc}"
        ) from exc
    label_policy_hash = active_policy.policy_file_sha256
    semantic_policy_hash = _active_semantic_policy_hash(root)
    if admission.policy_sha256 != label_policy_hash:
        raise EvidenceAdmissionVerificationError("admission references a stale label policy")
    if expected.label_resolution_policy_sha256 != label_policy_hash:
        raise EvidenceAdmissionVerificationError("expected binding references a stale label policy")
    if expected.runtime.semantic_policy_sha256 != semantic_policy_hash:
        raise EvidenceAdmissionVerificationError(
            "expected binding references a stale semantic policy"
        )

    stored_report = _load_canonical_model(
        replay_path,
        EvidenceReplayReport,
        role="LF-020 replay report",
    )
    try:
        recomputed_report = compare_lf020_replays(
            left_output_dir=replay_inputs.left_output_dir,
            left_cache_root=replay_inputs.left_cache_root,
            left_artifact_root=replay_inputs.left_artifact_root,
            right_output_dir=replay_inputs.right_output_dir,
            right_cache_root=replay_inputs.right_cache_root,
            right_artifact_root=replay_inputs.right_artifact_root,
            source_pairs=replay_inputs.source_pairs,
            upstream_evidence_ids=replay_inputs.upstream_evidence_ids,
        )
    except (EvidenceReplayInputError, ValidationError, OSError, ValueError) as exc:
        raise EvidenceAdmissionVerificationError(
            f"independent LF-020 replay recomputation failed: {exc}"
        ) from exc
    if recomputed_report != stored_report:
        raise EvidenceAdmissionVerificationError(
            "persisted replay report differs from independent replay recomputation"
        )
    if not stored_report.passed:
        raise EvidenceAdmissionVerificationError("LF-020 replay report did not pass")
    if (
        expected.source_pair_fingerprint != stored_report.source_pair_fingerprint
        or expected.upstream_evidence_id_fingerprint
        != stored_report.upstream_evidence_id_fingerprint
    ):
        raise EvidenceAdmissionVerificationError(
            "replay source-pair/upstream lineage differs from the frozen expected binding"
        )

    matching_sides = tuple(
        side
        for side, summary in (("left", stored_report.left), ("right", stored_report.right))
        if summary.output_manifest_sha256 == admission.manifest_artifact_sha256
    )
    if len(matching_sides) != 1:
        raise EvidenceAdmissionVerificationError(
            "admission manifest must match exactly one independently replayed side"
        )
    matched_side = matching_sides[0]
    output_dir = (
        replay_inputs.left_output_dir if matched_side == "left" else replay_inputs.right_output_dir
    )
    cache_root = (
        replay_inputs.left_cache_root if matched_side == "left" else replay_inputs.right_cache_root
    )
    expected_manifest_path = (output_dir / "manifest.json").resolve()
    if manifest_path != expected_manifest_path:
        raise EvidenceAdmissionVerificationError(
            "manifest locator does not point to the matched replay side"
        )

    manifest = _load_canonical_model(
        manifest_path,
        OutputManifest,
        role="LF-020 output manifest",
    )
    if manifest.schema_version != MANIFEST_SCHEMA_VERSION:
        raise EvidenceAdmissionVerificationError("unsupported output-manifest schema")
    if not (
        manifest.stage is DataStage.EVIDENCE_COLLECTED
        and manifest.source == "lf020_symbolic_evidence"
        and manifest.record_schema_version == CURRENT_RECORD_SCHEMA_VERSION
        and manifest.artifact_class is ArtifactClass.PRODUCTION
    ):
        raise EvidenceAdmissionVerificationError(
            "manifest is not a current production LF-020 evidence artifact"
        )
    if manifest.code.git_dirty or manifest.code.code_tree_hash is None:
        raise EvidenceAdmissionVerificationError(
            "production evidence manifest must bind a clean content-addressed code tree"
        )
    if manifest.config_hash != expected.output_manifest_config_hash:
        raise EvidenceAdmissionVerificationError("manifest configuration hash is stale")
    if manifest.environment_hash != expected.runtime.environment_hash:
        raise EvidenceAdmissionVerificationError("manifest environment hash is stale")
    if manifest.context_hash != expected.output_manifest_context_hash:
        raise EvidenceAdmissionVerificationError("manifest context hash is stale")

    evidence_path = _unique_checksum_path(
        root,
        manifest.output_partition_checksums,
        filename="evidence.jsonl",
        role="output manifest",
        allowed_root=output_dir,
    )
    all_output_records = _load_canonical_jsonl(
        evidence_path,
        EvidenceRecord,
        role="LF-020 evidence partition",
    )
    target_records = tuple(
        sorted(
            (record for record in all_output_records if record.target_id == admission.target_id),
            key=lambda record: record.evidence_id,
        )
    )
    if not target_records:
        raise EvidenceAdmissionVerificationError(
            "manifest contains no evidence for admission target"
        )
    target_ids = tuple(record.evidence_id for record in target_records)
    if target_ids != admission.evidence_ids:
        raise EvidenceAdmissionVerificationError(
            "admission evidence IDs are not the exact manifest set for the target"
        )
    if (
        expected.target_id != admission.target_id
        or expected.target_kind is not admission.target_kind
    ):
        raise EvidenceAdmissionVerificationError("expected binding targets a different record")

    entries = _cache_entries(
        verification_root=root,
        manifest=manifest,
        output_dir=output_dir,
        cache_root=cache_root,
    )
    target_entries = tuple(
        entry for entry in entries if entry.cache_key.pair_id == admission.target_id
    )
    cache_bindings = _record_bindings(target_entries)
    cache_key_hashes = _validate_expected_binding(
        expected=expected,
        output_records=target_records,
        cache_bindings=cache_bindings,
    )

    values: dict[str, object] = {
        "schema_version": 1,
        "admission_id": admission.admission_id,
        "expected_binding_id": expected.binding_id,
        "target_kind": admission.target_kind,
        "target_id": admission.target_id,
        "evidence_ids": admission.evidence_ids,
        "manifest_artifact_id": admission.manifest_artifact_id,
        "manifest_artifact_sha256": admission.manifest_artifact_sha256,
        "replay_artifact_id": admission.replay_artifact_id,
        "replay_artifact_sha256": admission.replay_artifact_sha256,
        "replay_report_hash": stored_report.report_hash,
        "matched_replay_side": matched_side,
        "output_run_id": manifest.run_id,
        "context_id": expected.runtime.context_id,
        "context_fingerprint": expected.runtime.context_fingerprint,
        "semantic_policy_sha256": semantic_policy_hash,
        "label_resolution_policy_sha256": label_policy_hash,
        "cache_key_hashes": cache_key_hashes,
        "production_guard_removed": False,
        "production_authority_established": False,
        "admissions_created": 0,
        "labels_created": 0,
    }
    receipt = DiagnosticLF020EvidenceAdmissionVerification.model_validate(
        {
            "verification_id": make_id(_VERIFICATION_PREFIX, values),
            **values,
        }
    )
    after_snapshot = _stability_snapshot(
        verification_root=root,
        replay_inputs=replay_inputs,
        direct_paths=stable_direct_paths,
    )
    if before_snapshot != after_snapshot:
        raise EvidenceAdmissionVerificationError(
            "admission artifact graph changed during verification"
        )
    return LF020EvidenceAdmissionDiagnosticResult(
        receipt=receipt,
        evidence_records=target_records,
    )


__all__ = [
    "AdmissionArtifactLocator",
    "DiagnosticLF020EvidenceAdmissionVerification",
    "EvidenceAdmissionVerificationError",
    "LF020EvidenceAdmissionDiagnosticResult",
    "LF020ExpectedAdmissionBinding",
    "LF020ExpectedEvidenceBinding",
    "LF020ReplayInputs",
    "LF020TargetRuntimeBinding",
    "build_lf020_expected_admission_binding",
    "verify_lf020_evidence_admission",
]
