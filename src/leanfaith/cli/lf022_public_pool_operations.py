"""Strict offline orchestration for the public-only LF-022 source pool.

The pure materializer in :mod:`leanfaith.generation.lf022_public_pool` accepts
validated records.  This module is the operator boundary: it reads only exact
repository-local regular files, rejects symlinks and duplicate keys, binds the
active benchmark registry to its exact bytes, invokes the materializer, and
then verifies that no input drifted while the operation ran.

Nothing here performs network I/O, creates semantic labels, or authorizes
execution of the resulting allocation scaffold.
"""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Self, cast

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.config.paths import RepoPaths
from leanfaith.datasets.denylist import FrozenRegistry
from leanfaith.generation.lf022_production import (
    LF022ArtifactBinding,
    LF022JSONLArtifactBinding,
    LF022PlanProfile,
    LF022ProductionFamilyMatrix,
)
from leanfaith.generation.lf022_public_pool import (
    LF022ApprovedPublicSource,
    LF022PublicPoolCapacityError,
    LF022PublicPoolError,
    MaterializedLF022PublicPool,
    materialize_lf022_public_pool,
)
from leanfaith.schemas.ids import id_pattern
from leanfaith.schemas.manifest import OutputManifest
from leanfaith.sources.mathlib_frame import MathlibFileFrame


class LF022PublicPoolOperationCode(StrEnum):
    """Stable operator-facing error categories."""

    INVALID_REQUEST = "invalid_request"
    UNSAFE_INPUT_PATH = "unsafe_input_path"
    INVALID_INPUT_SYNTAX = "invalid_input_syntax"
    INVALID_INPUT_SCHEMA = "invalid_input_schema"
    INPUT_HASH_DRIFT = "input_hash_drift"
    INSUFFICIENT_CAPACITY = "insufficient_capacity"
    MATERIALIZATION_FAILED = "materialization_failed"


class LF022PublicPoolOperationFailure(StrictModel):
    """Deterministic, value-redacted failure information for a CLI adapter."""

    schema_version: Literal[1] = 1
    operation: Literal["materialize_lf022_public_pool"]
    status: Literal["error"]
    code: LF022PublicPoolOperationCode
    message: str = Field(min_length=1)
    requested_count: int | None = Field(default=None, ge=1, strict=True)
    eligible_count: int | None = Field(default=None, ge=0, strict=True)
    eligible_unique_ancestry_count: int | None = Field(default=None, ge=0, strict=True)
    network_execution_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False


class LF022PublicPoolOperationError(ValueError):
    """A public-pool operation failed with structured, safe information."""

    def __init__(self, failure: LF022PublicPoolOperationFailure) -> None:
        self.failure = failure
        super().__init__(failure.message)


class LF022ApprovedPublicSourcesFile(StrictModel):
    """Reviewed, public-only source authorization input.

    The explicit false flags make accidental reuse of an execution or label
    document fail closed rather than being silently ignored.
    """

    schema_version: Literal[1] = 1
    approved_sources: tuple[LF022ApprovedPublicSource, ...] = Field(min_length=1)
    public_sources_only: Literal[True] = True
    network_execution_authorized: Literal[False] = False
    semantic_labels_included: Literal[False] = False

    @field_validator("schema_version", mode="before")
    @classmethod
    def _schema_version_is_exact_integer(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("schema_version must be JSON integer 1")
        return value

    @field_validator("public_sources_only", mode="before")
    @classmethod
    def _public_only_flag_is_exact(cls, value: object) -> object:
        if value is not True:
            raise ValueError("public_sources_only must be true")
        return value

    @field_validator(
        "network_execution_authorized",
        "semantic_labels_included",
        mode="before",
    )
    @classmethod
    def _safety_flags_are_exact(cls, value: object) -> object:
        if value is not False:
            raise ValueError("approved-source safety flags must be false")
        return value

    @model_validator(mode="after")
    def _sources_are_sorted_unique(self) -> Self:
        keys = tuple((item.source, item.source_revision) for item in self.approved_sources)
        if len(keys) != len(set(keys)):
            raise ValueError("approved source/revision entries must be unique")
        if keys != tuple(sorted(keys)):
            raise ValueError("approved source/revision entries must be sorted")
        return self


class LF022PublicPoolOperationSuccess(StrictModel):
    """Stable JSON-serializable result of one offline materialization."""

    schema_version: Literal[1] = 1
    operation: Literal["materialize_lf022_public_pool"]
    status: Literal["materialized"]
    profile: LF022PlanProfile
    requested_count: int = Field(ge=1, strict=True)
    eligible_count: int = Field(ge=1, strict=True)
    eligible_unique_ancestry_count: int = Field(ge=1, strict=True)
    selected_count: int = Field(ge=1, strict=True)
    audit_id: str = Field(pattern=id_pattern("lf022_public_pool_audit"))
    audit: LF022ArtifactBinding
    admission_id: str = Field(pattern=id_pattern("lf022_production_admission"))
    plan_id: str = Field(pattern=id_pattern("lf022_production_plan"))
    output_directory: str = Field(min_length=1)
    active_registry: LF022ArtifactBinding
    extraction_output_manifest_input: LF022ArtifactBinding
    representation_output_manifest_input: LF022ArtifactBinding
    mathlib_source_frame_input: LF022ArtifactBinding
    family_matrix_input: LF022ArtifactBinding
    approved_sources_input: LF022ArtifactBinding
    public_sources_only: Literal[True] = True
    non_executable_allocation_only: Literal[True] = True
    network_execution_authorized: Literal[False] = False
    semantic_labels_created: Literal[False] = False

    @model_validator(mode="after")
    def _counts_reconcile(self) -> Self:
        if self.selected_count != self.requested_count:
            raise ValueError("selected_count must equal requested_count")
        if self.eligible_count < self.selected_count:
            raise ValueError("eligible_count cannot be below selected_count")
        if self.eligible_unique_ancestry_count < self.selected_count:
            raise ValueError("eligible unique ancestry count cannot be below selected_count")
        normalized = PurePosixPath(self.output_directory)
        if (
            self.output_directory.startswith("/")
            or ".." in normalized.parts
            or normalized.as_posix() != self.output_directory
        ):
            raise ValueError("output_directory must be normalized and repository-relative")
        return self


@dataclass(frozen=True, slots=True)
class LF022PublicPoolOperationRun:
    """Structured summary plus the revalidated in-memory materialization."""

    summary: LF022PublicPoolOperationSuccess
    materialized: MaterializedLF022PublicPool


@dataclass(frozen=True, slots=True)
class _ReadInput:
    path: Path
    raw: bytes
    sha256: str


class _StrictYAMLLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _StrictYAMLLoader,
    node: yaml.MappingNode,
) -> dict[object, object]:
    result: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ValueError("YAML mapping keys must be strings")
        if key in result:
            raise ValueError(f"duplicate YAML key {key!r}")
        result[key] = loader.construct_object(value_node, deep=True)
    return result


_StrictYAMLLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _failure(
    *,
    code: LF022PublicPoolOperationCode,
    message: str,
    requested_count: int | None,
    eligible_count: int | None = None,
    eligible_unique_ancestry_count: int | None = None,
) -> LF022PublicPoolOperationError:
    return LF022PublicPoolOperationError(
        LF022PublicPoolOperationFailure(
            operation="materialize_lf022_public_pool",
            status="error",
            code=code,
            message=message,
            requested_count=requested_count,
            eligible_count=eligible_count,
            eligible_unique_ancestry_count=eligible_unique_ancestry_count,
        )
    )


def _repo_relative(root: Path, path: Path) -> str:
    return PurePosixPath(path.relative_to(root).as_posix()).as_posix()


def _read_repo_regular_file(
    *,
    repo_root: Path,
    path: Path,
    owner: str,
) -> _ReadInput:
    """Read one repository-local regular file through an ``O_NOFOLLOW`` fd."""

    try:
        root = repo_root.resolve(strict=True)
    except OSError as exc:
        raise _failure(
            code=LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH,
            message="repository root is unavailable",
            requested_count=None,
        ) from exc
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise _failure(
            code=LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH,
            message=f"{owner} must be inside the repository",
            requested_count=None,
        ) from exc
    if not relative.parts or "." in relative.parts or ".." in relative.parts:
        raise _failure(
            code=LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH,
            message=f"{owner} path is not normalized",
            requested_count=None,
        )
    current = root
    for part in relative.parts:
        current /= part
        try:
            metadata = current.lstat()
        except OSError as exc:
            raise _failure(
                code=LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH,
                message=f"{owner} is missing or inaccessible",
                requested_count=None,
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise _failure(
                code=LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH,
                message=f"{owner} contains a symlinked path component",
                requested_count=None,
            )
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise _failure(
            code=LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH,
            message=f"{owner} is missing or inaccessible",
            requested_count=None,
        ) from exc
    if not resolved.is_relative_to(root):
        raise _failure(
            code=LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH,
            message=f"{owner} resolves outside the repository",
            requested_count=None,
        )
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise _failure(
            code=LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH,
            message=f"{owner} cannot be opened as a regular file",
            requested_count=None,
        ) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise _failure(
                code=LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH,
                message=f"{owner} is not a regular file",
                requested_count=None,
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise _failure(
                code=LF022PublicPoolOperationCode.INPUT_HASH_DRIFT,
                message=f"{owner} changed while it was read",
                requested_count=None,
            )
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    return _ReadInput(path=resolved, raw=raw, sha256=sha256_hex(raw))


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite_json(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value}")


def _parse_json(raw: bytes, *, owner: str) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
            parse_constant=_reject_nonfinite_json,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise _failure(
            code=LF022PublicPoolOperationCode.INVALID_INPUT_SYNTAX,
            message=f"{owner} is not strict UTF-8 JSON",
            requested_count=None,
        ) from exc


def _validate_jsonl_syntax(value: _ReadInput, *, owner: str) -> int:
    rows = value.raw.splitlines()
    if not rows or any(not row for row in rows):
        raise _failure(
            code=LF022PublicPoolOperationCode.INVALID_INPUT_SYNTAX,
            message=f"{owner} must be nonempty JSONL without blank rows",
            requested_count=None,
        )
    for row in rows:
        _parse_json(row, owner=owner)
    return len(rows)


def _validate_model[ModelT: StrictModel](
    document: object,
    *,
    model_type: type[ModelT],
    owner: str,
) -> ModelT:
    try:
        return model_type.model_validate(document)
    except ValidationError as exc:
        fields = sorted(
            {
                ".".join(str(part) for part in error["loc"]) or "<root>"
                for error in exc.errors(include_input=False, include_url=False)
            }
        )
        raise _failure(
            code=LF022PublicPoolOperationCode.INVALID_INPUT_SCHEMA,
            message=f"{owner} failed schema validation at {', '.join(fields)}",
            requested_count=None,
        ) from exc


def _json_object(value: object, *, owner: str) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _failure(
            code=LF022PublicPoolOperationCode.INVALID_INPUT_SCHEMA,
            message=f"{owner} must contain one JSON object",
            requested_count=None,
        )
    return cast(dict[str, object], value)


def _parse_approved_sources(
    value: _ReadInput,
) -> LF022ApprovedPublicSourcesFile:
    suffix = value.path.suffix.lower()
    if suffix == ".json":
        document = _parse_json(value.raw, owner="approved public sources")
    elif suffix in {".yaml", ".yml"}:
        try:
            document = yaml.load(
                value.raw.decode("utf-8"),
                Loader=_StrictYAMLLoader,
            )
        except (UnicodeDecodeError, yaml.YAMLError, ValueError) as exc:
            raise _failure(
                code=LF022PublicPoolOperationCode.INVALID_INPUT_SYNTAX,
                message="approved public sources is not strict UTF-8 YAML",
                requested_count=None,
            ) from exc
    else:
        raise _failure(
            code=LF022PublicPoolOperationCode.INVALID_REQUEST,
            message="approved public sources must use .json, .yaml, or .yml",
            requested_count=None,
        )
    return _validate_model(
        _json_object(document, owner="approved public sources"),
        model_type=LF022ApprovedPublicSourcesFile,
        owner="approved public sources",
    )


def _binding(root: Path, value: _ReadInput) -> LF022ArtifactBinding:
    return LF022ArtifactBinding(
        path=_repo_relative(root, value.path),
        sha256=value.sha256,
    )


def _jsonl_binding(
    root: Path,
    value: _ReadInput,
    *,
    record_count: int,
) -> LF022JSONLArtifactBinding:
    return LF022JSONLArtifactBinding(
        path=_repo_relative(root, value.path),
        sha256=value.sha256,
        record_count=record_count,
    )


def _ensure_unchanged(
    *,
    root: Path,
    original: _ReadInput,
    owner: str,
    requested_count: int,
) -> None:
    try:
        observed = _read_repo_regular_file(
            repo_root=root,
            path=original.path,
            owner=owner,
        )
    except LF022PublicPoolOperationError:
        raise
    if observed.sha256 != original.sha256:
        raise _failure(
            code=LF022PublicPoolOperationCode.INPUT_HASH_DRIFT,
            message=f"{owner} changed during materialization",
            requested_count=requested_count,
        )


def run_materialize_lf022_public_pool(
    *,
    paths: RepoPaths,
    theorem_records_path: Path,
    representation_records_path: Path,
    context_records_path: Path,
    extraction_output_manifest_path: Path,
    representation_output_manifest_path: Path,
    mathlib_source_frame_path: Path,
    active_registry_path: Path,
    family_matrix_path: Path,
    approved_sources_path: Path,
    output_directory: Path,
    requested_count: int = 15_000,
    profile: LF022PlanProfile = "scientific_production_scaffold",
) -> LF022PublicPoolOperationRun:
    """Validate exact offline inputs and materialize a non-executable pool."""

    if type(requested_count) is not int or requested_count < 1:
        raise _failure(
            code=LF022PublicPoolOperationCode.INVALID_REQUEST,
            message="requested_count must be a positive integer",
            requested_count=None,
        )
    if profile not in {
        "diagnostic_scaffold",
        "pilot_scaffold",
        "scientific_production_scaffold",
    }:
        raise _failure(
            code=LF022PublicPoolOperationCode.INVALID_REQUEST,
            message="profile is not a supported LF-022 allocation profile",
            requested_count=requested_count,
        )

    try:
        root = paths.root.resolve(strict=True)
    except OSError as exc:
        raise _failure(
            code=LF022PublicPoolOperationCode.UNSAFE_INPUT_PATH,
            message="repository root is unavailable",
            requested_count=requested_count,
        ) from exc
    inputs = {
        "theorem records": _read_repo_regular_file(
            repo_root=root,
            path=theorem_records_path,
            owner="theorem records",
        ),
        "representation records": _read_repo_regular_file(
            repo_root=root,
            path=representation_records_path,
            owner="representation records",
        ),
        "context records": _read_repo_regular_file(
            repo_root=root,
            path=context_records_path,
            owner="context records",
        ),
        "extraction output manifest": _read_repo_regular_file(
            repo_root=root,
            path=extraction_output_manifest_path,
            owner="extraction output manifest",
        ),
        "representation output manifest": _read_repo_regular_file(
            repo_root=root,
            path=representation_output_manifest_path,
            owner="representation output manifest",
        ),
        "mathlib source frame": _read_repo_regular_file(
            repo_root=root,
            path=mathlib_source_frame_path,
            owner="mathlib source frame",
        ),
        "active benchmark registry": _read_repo_regular_file(
            repo_root=root,
            path=active_registry_path,
            owner="active benchmark registry",
        ),
        "family matrix": _read_repo_regular_file(
            repo_root=root,
            path=family_matrix_path,
            owner="family matrix",
        ),
        "approved public sources": _read_repo_regular_file(
            repo_root=root,
            path=approved_sources_path,
            owner="approved public sources",
        ),
    }
    theorem_count = _validate_jsonl_syntax(
        inputs["theorem records"],
        owner="theorem records",
    )
    representation_count = _validate_jsonl_syntax(
        inputs["representation records"],
        owner="representation records",
    )
    context_count = _validate_jsonl_syntax(
        inputs["context records"],
        owner="context records",
    )
    active_registry = _validate_model(
        _json_object(
            _parse_json(
                inputs["active benchmark registry"].raw,
                owner="active benchmark registry",
            ),
            owner="active benchmark registry",
        ),
        model_type=FrozenRegistry,
        owner="active benchmark registry",
    )
    extraction_output_manifest = _validate_model(
        _json_object(
            _parse_json(
                inputs["extraction output manifest"].raw,
                owner="extraction output manifest",
            ),
            owner="extraction output manifest",
        ),
        model_type=OutputManifest,
        owner="extraction output manifest",
    )
    representation_output_manifest = _validate_model(
        _json_object(
            _parse_json(
                inputs["representation output manifest"].raw,
                owner="representation output manifest",
            ),
            owner="representation output manifest",
        ),
        model_type=OutputManifest,
        owner="representation output manifest",
    )
    mathlib_source_frame = _validate_model(
        _json_object(
            _parse_json(
                inputs["mathlib source frame"].raw,
                owner="mathlib source frame",
            ),
            owner="mathlib source frame",
        ),
        model_type=MathlibFileFrame,
        owner="mathlib source frame",
    )
    expected_frame_bytes = (
        canonical_json_bytes(mathlib_source_frame.model_dump(mode="json")) + b"\n"
    )
    if inputs["mathlib source frame"].raw != expected_frame_bytes:
        raise _failure(
            code=LF022PublicPoolOperationCode.INVALID_INPUT_SCHEMA,
            message="mathlib source frame is not canonical JSON",
            requested_count=requested_count,
        )
    family_matrix = _validate_model(
        _json_object(
            _parse_json(inputs["family matrix"].raw, owner="family matrix"),
            owner="family matrix",
        ),
        model_type=LF022ProductionFamilyMatrix,
        owner="family matrix",
    )
    approved_sources = _parse_approved_sources(inputs["approved public sources"])

    expected_theorems = _jsonl_binding(
        root,
        inputs["theorem records"],
        record_count=theorem_count,
    )
    expected_representations = _jsonl_binding(
        root,
        inputs["representation records"],
        record_count=representation_count,
    )
    expected_contexts = _jsonl_binding(
        root,
        inputs["context records"],
        record_count=context_count,
    )
    active_registry_binding = _binding(root, inputs["active benchmark registry"])
    extraction_output_manifest_binding = _binding(
        root,
        inputs["extraction output manifest"],
    )
    representation_output_manifest_binding = _binding(
        root,
        inputs["representation output manifest"],
    )
    mathlib_source_frame_binding = _binding(root, inputs["mathlib source frame"])
    family_matrix_binding = _binding(root, inputs["family matrix"])
    approved_sources_binding = _binding(root, inputs["approved public sources"])

    try:
        materialized = materialize_lf022_public_pool(
            repo_root=root,
            theorem_records_path=inputs["theorem records"].path,
            representation_records_path=inputs["representation records"].path,
            context_records_path=inputs["context records"].path,
            active_registry=active_registry,
            active_registry_binding=active_registry_binding,
            extraction_output_manifest=extraction_output_manifest,
            extraction_output_manifest_binding=extraction_output_manifest_binding,
            representation_output_manifest=representation_output_manifest,
            representation_output_manifest_binding=(representation_output_manifest_binding),
            mathlib_source_frame=mathlib_source_frame,
            mathlib_source_frame_binding=mathlib_source_frame_binding,
            family_matrix=family_matrix,
            approved_sources=approved_sources.approved_sources,
            output_directory=output_directory,
            requested_count=requested_count,
            profile=profile,
        )
    except LF022PublicPoolCapacityError as exc:
        raise _failure(
            code=LF022PublicPoolOperationCode.INSUFFICIENT_CAPACITY,
            message=("eligible public source capacity is below the exact requested count"),
            requested_count=exc.requested_count,
            eligible_count=exc.eligible_count,
            eligible_unique_ancestry_count=exc.eligible_unique_ancestry_count,
        ) from exc
    except (LF022PublicPoolError, ValidationError, ValueError, OSError) as exc:
        raise _failure(
            code=LF022PublicPoolOperationCode.MATERIALIZATION_FAILED,
            message="LF-022 public-pool materialization failed closed",
            requested_count=requested_count,
        ) from exc

    audit = materialized.audit
    if (
        audit.input_theorems != expected_theorems
        or audit.input_representations != expected_representations
        or audit.input_contexts != expected_contexts
        or audit.input_extraction_output_manifest != extraction_output_manifest_binding
        or audit.input_representation_output_manifest != representation_output_manifest_binding
        or audit.input_mathlib_source_frame != mathlib_source_frame_binding
        or audit.active_benchmark_registry != active_registry_binding
    ):
        raise _failure(
            code=LF022PublicPoolOperationCode.INPUT_HASH_DRIFT,
            message="materializer input bindings differ from preflight bindings",
            requested_count=requested_count,
        )
    for owner, original in inputs.items():
        _ensure_unchanged(
            root=root,
            original=original,
            owner=owner,
            requested_count=requested_count,
        )
    if (
        materialized.admission.network_execution_authorized
        or materialized.admission.semantic_labels_created
        or materialized.plan.network_execution_authorized
        or materialized.plan.semantic_labels_created
        or materialized.plan.execution_bindings_present
    ):
        raise _failure(
            code=LF022PublicPoolOperationCode.MATERIALIZATION_FAILED,
            message="materialized allocation violated non-executable safety invariants",
            requested_count=requested_count,
        )
    output = (
        output_directory if output_directory.is_absolute() else root / output_directory
    ).resolve(strict=True)
    if not output.is_relative_to(root):
        raise _failure(
            code=LF022PublicPoolOperationCode.MATERIALIZATION_FAILED,
            message="materialized output escaped the repository",
            requested_count=requested_count,
        )
    summary = LF022PublicPoolOperationSuccess(
        operation="materialize_lf022_public_pool",
        status="materialized",
        profile=profile,
        requested_count=requested_count,
        eligible_count=audit.eligible_count,
        eligible_unique_ancestry_count=audit.eligible_unique_ancestry_count,
        selected_count=audit.selected_count,
        audit_id=audit.audit_id,
        audit=materialized.audit_binding,
        admission_id=materialized.admission.admission_id,
        plan_id=materialized.plan.manifest_id,
        output_directory=_repo_relative(root, output),
        active_registry=active_registry_binding,
        extraction_output_manifest_input=extraction_output_manifest_binding,
        representation_output_manifest_input=representation_output_manifest_binding,
        mathlib_source_frame_input=mathlib_source_frame_binding,
        family_matrix_input=family_matrix_binding,
        approved_sources_input=approved_sources_binding,
    )
    return LF022PublicPoolOperationRun(summary=summary, materialized=materialized)


__all__ = [
    "LF022ApprovedPublicSourcesFile",
    "LF022PublicPoolOperationCode",
    "LF022PublicPoolOperationError",
    "LF022PublicPoolOperationFailure",
    "LF022PublicPoolOperationRun",
    "LF022PublicPoolOperationSuccess",
    "run_materialize_lf022_public_pool",
]
