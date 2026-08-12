"""Exact Lean-meta semantic-section derivation for the tokenizer audit.

The scientific tokenizer audit must not infer binders or hypotheses from
surface punctuation.  This module executes the pinned Lean helper through the
project's sole LeanInteract backend, persists one restartable item per frozen
theorem, then assembles an immutable private section partition in denominator
order.  It creates no semantic-faithfulness label.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import stat
import subprocess
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.lean.leaninteract_backend import (
    METHOD_VERSION as BACKEND_METHOD_VERSION,
)
from leanfaith.lean.leaninteract_backend import (
    BackendSettings,
    LeanInteractBackend,
)
from leanfaith.lean.project_registry import (
    EnvironmentLock,
    ProjectSpec,
    check_project_revision,
    check_project_toolchain,
)
from leanfaith.lean.protocol import LeanRequest, LeanStatus, compute_request_hash
from leanfaith.lean.session_policy import ServerMode
from leanfaith.representations.pipeline import (
    _hoist_inline_imports,
    _imports_with_lean,
    declaration_environment_lookup_name,
    inline_replay_environment_lookup_name,
)
from leanfaith.schemas.theorem import ContextRecord, TheoremRecord

METHOD_VERSION: Literal["lean_meta_tokenizer_sections_v1"] = "lean_meta_tokenizer_sections_v1"
FROZEN_PROFILE_ID = "gate3_tokenizer_sections_v1"
_HEX64 = r"^[0-9a-f]{64}$"
_MESSAGE_PREFIX = "LFTOKSECTIONSJSON "


class TokenizerSectionDerivationError(RuntimeError):
    """The exact semantic-section derivation failed closed."""


class SectionDerivationConfig(StrictModel):
    schema_version: Literal[4]
    profile_id: str = Field(min_length=1)
    method_version: Literal["lean_meta_tokenizer_sections_v1"]
    theorem_partition: str
    theorem_partition_sha256: str = Field(pattern=_HEX64)
    expected_records: int = Field(ge=1)
    expected_per_source: dict[str, int]
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")
    import_header: Literal["import Mathlib"]
    context_record_path: str
    context_record_sha256: str = Field(pattern=_HEX64)
    project_registry_key: Literal["mathlib"]
    project_registry_path: str
    project_registry_sha256: str = Field(pattern=_HEX64)
    environment_lock_path: str
    environment_lock_sha256: str = Field(pattern=_HEX64)
    expected_project_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    expected_lean_toolchain: str = Field(min_length=1)
    lean_toolchain_sha256: str = Field(pattern=_HEX64)
    lake_manifest_sha256: str = Field(pattern=_HEX64)
    project_dir: str
    raw_response_dir: str
    workers: int = Field(ge=1)
    chunk_size: int = Field(ge=1)
    memory_hard_limit_mb: int | None = Field(default=None, ge=1)
    timeout_seconds: float = Field(gt=0)
    lean_num_threads: Literal[1]
    enable_incremental_optimization: Literal[True]
    enable_parallel_elaboration: Literal[False]
    isolate_incremental_commands: Literal[True]
    confirm_invalid_on_fresh_process: Literal[True]
    prepare_environment_once: Literal[True]
    preflight_records_per_source: int = Field(ge=0)
    contains_private_source: Literal[True]
    redistribution: Literal[False]
    external_transmission: Literal[False]
    release_eligible: Literal[False]

    @model_validator(mode="after")
    def _exact_profile(self) -> SectionDerivationConfig:
        if sum(self.expected_per_source.values()) != self.expected_records:
            raise ValueError("section-derivation source counts do not reconcile")
        for value in (self.theorem_partition, self.project_dir, self.raw_response_dir):
            if not Path(value).is_absolute():
                raise ValueError("section-derivation runtime/input paths must be absolute")
        for value in (
            self.context_record_path,
            self.project_registry_path,
            self.environment_lock_path,
        ):
            path = Path(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("section-derivation pin paths must be repository-relative")
        if self.profile_id == FROZEN_PROFILE_ID:
            if self.expected_records != 10_000 or self.expected_per_source != {
                "mathlib": 5_000,
                "sft_classic": 5_000,
            }:
                raise ValueError("frozen section profile requires exactly 5,000 records per source")
            if self.workers != 4 or self.memory_hard_limit_mb != 16_384:
                raise ValueError("frozen section profile requires four 16,384-MB Lean workers")
            if self.preflight_records_per_source != 2:
                raise ValueError("frozen section profile requires a two-per-source pool preflight")
        return self


class SectionEnvironmentBinding(StrictModel):
    project_registry_key: Literal["mathlib"]
    project_registry_sha256: str = Field(pattern=_HEX64)
    environment_lock_sha256: str = Field(pattern=_HEX64)
    context_record_sha256: str = Field(pattern=_HEX64)
    project_revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    project_worktree_clean: Literal[True]
    lean_toolchain: str
    lean_toolchain_sha256: str = Field(pattern=_HEX64)
    lake_manifest_sha256: str = Field(pattern=_HEX64)
    lean_interact_version: str
    context_id: str = Field(pattern=r"^ctx:[0-9a-f]{64}$")


class SemanticSectionUnit(StrictModel):
    ordinal: int = Field(ge=0)
    kind: Literal["ordinary_binder", "instance_binder", "prop_hypothesis"]
    binder_info: Literal["default", "implicit", "strictImplicit", "instImplicit"]
    domain_is_prop: bool
    text: str = Field(min_length=1)

    @model_validator(mode="after")
    def _classification(self) -> SemanticSectionUnit:
        expected = (
            "instance_binder"
            if self.binder_info == "instImplicit"
            else "prop_hypothesis"
            if self.domain_is_prop
            else "ordinary_binder"
        )
        if self.kind != expected:
            raise ValueError("section kind differs from exact Lean-meta classification")
        return self


class SemanticSectionRecord(StrictModel):
    theorem_id: str
    source: str
    method_version: Literal["lean_meta_tokenizer_sections_v1"]
    units: tuple[SemanticSectionUnit, ...]
    conclusion: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> SemanticSectionRecord:
        if [unit.ordinal for unit in self.units] != list(range(len(self.units))):
            raise ValueError("section unit ordinals are not contiguous")
        return self


class SemanticSectionItem(StrictModel):
    schema_version: Literal[1]
    derivation_binding_sha256: str = Field(pattern=_HEX64)
    request_hash: str = Field(pattern=_HEX64)
    record: SemanticSectionRecord


class SectionDerivationManifest(StrictModel):
    schema_version: Literal[4]
    method_version: Literal["lean_meta_tokenizer_sections_v1"]
    derivation_id: str = Field(pattern=r"^tokenizer_sections:[0-9a-f]{64}$")
    derivation_binding_sha256: str = Field(pattern=_HEX64)
    config_hash: str = Field(pattern=_HEX64)
    config: SectionDerivationConfig
    repository_root: str
    theorem_partition_sha256: str = Field(pattern=_HEX64)
    helper_sha256: str = Field(pattern=_HEX64)
    environment: SectionEnvironmentBinding
    preflight_theorem_ids: tuple[str, ...]
    record_count: int
    per_source: dict[str, int]
    context_id: str
    contains_private_source: Literal[True]
    redistribution: Literal[False]
    external_transmission: Literal[False]
    release_eligible: Literal[False]
    output_sha256: dict[Literal["sections.jsonl"], str]

    @field_validator("output_sha256")
    @classmethod
    def _digest_values(
        cls, value: dict[Literal["sections.jsonl"], str]
    ) -> dict[Literal["sections.jsonl"], str]:
        if set(value) != {"sections.jsonl"}:
            raise ValueError("section derivation has unexpected output hash keys")
        if any(
            len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
            for digest in value.values()
        ):
            raise ValueError("section derivation output hash is invalid")
        return value

    @model_validator(mode="after")
    def _exact_outputs(self) -> SectionDerivationManifest:
        if sum(self.per_source.values()) != self.record_count:
            raise ValueError("section derivation source counts do not reconcile")
        expected_preflight = self.config.preflight_records_per_source * len(
            self.config.expected_per_source
        )
        if len(self.preflight_theorem_ids) != expected_preflight:
            raise ValueError("section derivation preflight count differs")
        return self


def _helper(repo_root: Path) -> tuple[str, str]:
    path = repo_root / "LeanFaith" / "Meta" / "TokenizerSections.lean"
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TokenizerSectionDerivationError(
            "tokenizer-section Lean helper is unavailable"
        ) from exc
    body = "\n".join(line for line in raw.splitlines() if not line.startswith("import "))
    return body, hash_file(path)


def load_tokenizer_section_config(path: Path) -> LoadedConfig[SectionDerivationConfig]:
    """Load and strictly validate a tokenizer-section derivation config."""

    return load_config(path, SectionDerivationConfig)


def _reject_symlink_components(path: Path, *, allow_missing: bool) -> Path:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        if not cursor.exists():
            if allow_missing:
                continue
            raise TokenizerSectionDerivationError(f"private path is unavailable: {cursor}")
        if cursor.is_symlink():
            raise TokenizerSectionDerivationError(
                f"private tokenizer-section path contains symlink: {cursor}"
            )
    return absolute


def _private_directory(path: Path, *, create: bool) -> Path:
    absolute = _reject_symlink_components(path, allow_missing=create)
    if create:
        absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        mode = absolute.stat().st_mode
    except OSError as exc:
        raise TokenizerSectionDerivationError(
            f"private tokenizer-section directory is unavailable: {absolute}"
        ) from exc
    if not stat.S_ISDIR(mode) or absolute.is_symlink():
        raise TokenizerSectionDerivationError(
            f"private tokenizer-section path is not a real directory: {absolute}"
        )
    absolute.chmod(0o700)
    return absolute


def _pinned_repository_file(repo: Path, relative: str, expected_hash: str) -> Path:
    path = repo / relative
    try:
        path.relative_to(repo)
    except ValueError as exc:  # pragma: no cover - config validation prevents this
        raise TokenizerSectionDerivationError("repository pin escapes the repository") from exc
    if path.is_symlink() or not path.is_file():
        raise TokenizerSectionDerivationError(f"repository pin is unavailable: {relative}")
    if hash_file(path) != expected_hash:
        raise TokenizerSectionDerivationError(f"repository pin hash differs: {relative}")
    return path


def _verify_environment(repo: Path, config: SectionDerivationConfig) -> SectionEnvironmentBinding:
    """Fail closed unless the live Lean project equals every frozen environment pin."""

    registry_path = _pinned_repository_file(
        repo, config.project_registry_path, config.project_registry_sha256
    )
    lock_path = _pinned_repository_file(
        repo, config.environment_lock_path, config.environment_lock_sha256
    )
    context_path = _pinned_repository_file(
        repo, config.context_record_path, config.context_record_sha256
    )
    try:
        spec = load_config(registry_path, ProjectSpec).config
        lock = load_config(lock_path, EnvironmentLock).config
        context = ContextRecord.model_validate(json.loads(context_path.read_bytes()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenizerSectionDerivationError("invalid pinned Lean environment metadata") from exc
    if spec.registry_key != config.project_registry_key:
        raise TokenizerSectionDerivationError("project registry key differs")

    project = _reject_symlink_components(Path(config.project_dir), allow_missing=False)
    if not project.is_dir():
        raise TokenizerSectionDerivationError("pinned Lean project is not a directory")
    try:
        revision = check_project_revision(spec, project)
        lean_version = str(check_project_toolchain(spec, project, lock.toolchain_lock))
        status = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=project,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError, RuntimeError, ValueError) as exc:
        raise TokenizerSectionDerivationError("cannot validate the pinned Lean project") from exc
    if revision != config.expected_project_revision or revision != spec.revision:
        raise TokenizerSectionDerivationError("live project revision differs from the frozen pin")
    if status:
        raise TokenizerSectionDerivationError("live Lean project worktree is not clean")
    if lean_version != config.expected_lean_toolchain:
        raise TokenizerSectionDerivationError("live Lean toolchain differs from the frozen pin")

    toolchain = project / "lean-toolchain"
    lake_manifest = project / "lake-manifest.json"
    for path, expected, label in (
        (toolchain, config.lean_toolchain_sha256, "lean-toolchain"),
        (lake_manifest, config.lake_manifest_sha256, "lake-manifest"),
    ):
        if path.is_symlink() or not path.is_file() or hash_file(path) != expected:
            raise TokenizerSectionDerivationError(f"live {label} differs from the frozen pin")

    try:
        lean_interact_version = importlib.metadata.version("lean-interact")
    except importlib.metadata.PackageNotFoundError as exc:
        raise TokenizerSectionDerivationError("lean-interact runtime is unavailable") from exc
    if lean_interact_version != lock.lean_interact.version:
        raise TokenizerSectionDerivationError("lean-interact runtime differs from environment lock")
    if (
        context.context_id != config.context_id
        or context.project_registry_key != config.project_registry_key
        or context.project_revision != revision
        or context.lean_version != lean_version
        or context.lean_interact_version != lean_interact_version
        or context.header_text != config.import_header + "\n"
    ):
        raise TokenizerSectionDerivationError(
            "context record differs from the live pinned environment"
        )
    return SectionEnvironmentBinding(
        project_registry_key=config.project_registry_key,
        project_registry_sha256=config.project_registry_sha256,
        environment_lock_sha256=config.environment_lock_sha256,
        context_record_sha256=config.context_record_sha256,
        project_revision=revision,
        project_worktree_clean=True,
        lean_toolchain=lean_version,
        lean_toolchain_sha256=config.lean_toolchain_sha256,
        lake_manifest_sha256=config.lake_manifest_sha256,
        lean_interact_version=lean_interact_version,
        context_id=config.context_id,
    )


def _write_private_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _normalize_private_tree(root: Path) -> None:
    root = _private_directory(root, create=False)
    for path in root.rglob("*"):
        if path.is_symlink():
            raise TokenizerSectionDerivationError(
                f"private tokenizer-section tree contains symlink: {path}"
            )
        mode = path.stat().st_mode
        if stat.S_ISDIR(mode):
            path.chmod(0o700)
        elif stat.S_ISREG(mode):
            path.chmod(0o600)
        else:
            raise TokenizerSectionDerivationError(
                f"private tokenizer-section tree contains special file: {path}"
            )


def _theorems(config: SectionDerivationConfig) -> list[TheoremRecord]:
    path = Path(config.theorem_partition)
    if hash_file(path) != config.theorem_partition_sha256:
        raise TokenizerSectionDerivationError("section theorem partition hash differs")
    records: list[TheoremRecord] = []
    with path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                row = json.loads(raw)
                payload = row.get("theorem", row)
                record = TheoremRecord.model_validate(payload)
            except (json.JSONDecodeError, ValueError, AttributeError) as exc:
                raise TokenizerSectionDerivationError(
                    f"invalid theorem partition row {line_number}"
                ) from exc
            records.append(record)
    if len(records) != config.expected_records:
        raise TokenizerSectionDerivationError("section theorem denominator differs")
    if Counter(record.source for record in records) != Counter(config.expected_per_source):
        raise TokenizerSectionDerivationError("section theorem per-source denominator differs")
    if any(record.context_id != config.context_id for record in records):
        raise TokenizerSectionDerivationError("section theorem context differs")
    return records


def _command(theorem: TheoremRecord, *, helper: str, import_header: str) -> tuple[str, str]:
    name = theorem.declaration_full_name or theorem.declaration_name
    if not name:
        raise TokenizerSectionDerivationError(f"theorem {theorem.theorem_id} has no name")
    if theorem.source == "mathlib":
        lookup = declaration_environment_lookup_name(name, theorem.source_file)
        command = f"lfDumpTokenizerSections {json.dumps(lookup, ensure_ascii=False)}"
        return "\n".join((_imports_with_lean(import_header), helper, command)), lookup
    if theorem.inline_elaboration_source is None:
        raise TokenizerSectionDerivationError(
            f"inline theorem {theorem.theorem_id} lacks exact elaboration source"
        )
    imports, body = _hoist_inline_imports(theorem.inline_elaboration_source)
    lookup = inline_replay_environment_lookup_name(name, theorem.inline_elaboration_source)
    code = "\n".join(
        (
            _imports_with_lean("\n".join((import_header, imports))),
            helper,
            body,
            f"lfDumpTokenizerSections {json.dumps(lookup, ensure_ascii=False)}",
        )
    )
    return code, lookup


def _parse_result(
    theorem: TheoremRecord,
    lookup: str,
    messages: Sequence[Mapping[str, Any]],
) -> SemanticSectionRecord:
    selected: dict[str, Any] | None = None
    for message in messages:
        for line in str(message.get("data", "")).splitlines():
            if _MESSAGE_PREFIX not in line:
                continue
            try:
                payload = json.loads(line.split(_MESSAGE_PREFIX, 1)[1])
            except json.JSONDecodeError as exc:
                raise TokenizerSectionDerivationError("malformed Lean section payload") from exc
            if payload.get("name") == lookup:
                selected = payload
    if selected is None or selected.get("notfound") is True:
        raise TokenizerSectionDerivationError(
            f"Lean section payload missing for {theorem.theorem_id}"
        )
    if selected.get("method_version") != METHOD_VERSION:
        raise TokenizerSectionDerivationError("Lean section method version differs")
    sections = selected.get("sections")
    if not isinstance(sections, dict):
        raise TokenizerSectionDerivationError("Lean section payload lacks sections")
    return SemanticSectionRecord.model_validate(
        {
            "theorem_id": theorem.theorem_id,
            "source": theorem.source,
            "method_version": METHOD_VERSION,
            "units": sections.get("units"),
            "conclusion": sections.get("conclusion"),
        }
    )


def _request_hash(request: LeanRequest, *, context_id: str) -> str:
    return compute_request_hash(
        request,
        context_fingerprint=context_id.removeprefix("ctx:"),
        environment_schema_version=1,
        method_version=BACKEND_METHOD_VERSION,
    )


def _derivation_binding(
    *,
    config_hash: str,
    theorem_partition_sha256: str,
    helper_sha256: str,
    environment: SectionEnvironmentBinding,
) -> str:
    return hash_canonical(
        {
            "method_version": METHOD_VERSION,
            "config_hash": config_hash,
            "theorem_partition_sha256": theorem_partition_sha256,
            "helper_sha256": helper_sha256,
            "environment": environment.model_dump(mode="json"),
        }
    )


def _write_item(
    path: Path,
    record: SemanticSectionRecord,
    *,
    derivation_binding_sha256: str,
    request_hash: str,
) -> None:
    item = SemanticSectionItem(
        schema_version=1,
        derivation_binding_sha256=derivation_binding_sha256,
        request_hash=request_hash,
        record=record,
    )
    payload = canonical_json_bytes(item.model_dump(mode="json")) + b"\n"
    if path.exists():
        if path.read_bytes() != payload:
            raise TokenizerSectionDerivationError(f"section resume item differs: {path.name}")
        return
    _write_private_file(path, payload)


def _load_item(
    path: Path,
    *,
    theorem: TheoremRecord,
    derivation_binding_sha256: str,
    expected_request_hash: str,
) -> SemanticSectionRecord:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise TokenizerSectionDerivationError(
            f"section resume item is not a private regular file: {path.name}"
        )
    path.chmod(0o600)
    try:
        item = SemanticSectionItem.model_validate(json.loads(path.read_bytes()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenizerSectionDerivationError(f"invalid section resume item: {path.name}") from exc
    if (
        item.derivation_binding_sha256 != derivation_binding_sha256
        or item.request_hash != expected_request_hash
        or item.record.theorem_id != theorem.theorem_id
        or item.record.source != theorem.source
    ):
        raise TokenizerSectionDerivationError(f"section resume item binding differs: {path.name}")
    return item.record


def _preflight_theorems(
    theorems: Sequence[TheoremRecord], config: SectionDerivationConfig
) -> tuple[TheoremRecord, ...]:
    if config.preflight_records_per_source == 0:
        return ()
    selected: list[TheoremRecord] = []
    for source in sorted(config.expected_per_source):
        candidates = [theorem for theorem in theorems if theorem.source == source]
        if len(candidates) < config.preflight_records_per_source:
            raise TokenizerSectionDerivationError(
                f"insufficient {source} records for the frozen pool preflight"
            )
        selected.extend(candidates[: config.preflight_records_per_source])
    return tuple(selected)


def _backend_settings(config: SectionDerivationConfig) -> BackendSettings:
    """Translate every frozen execution choice without relying on defaults."""

    return BackendSettings(
        project_dir=Path(config.project_dir),
        context_fingerprint=config.context_id.removeprefix("ctx:"),
        environment_schema_version=1,
        raw_response_dir=Path(config.raw_response_dir),
        server_mode=ServerMode.STABLE if config.workers == 1 else ServerMode.POOL,
        workers=None if config.workers == 1 else config.workers,
        memory_hard_limit_mb=config.memory_hard_limit_mb,
        enable_incremental_optimization=config.enable_incremental_optimization,
        enable_parallel_elaboration=config.enable_parallel_elaboration,
        isolate_incremental_commands=config.isolate_incremental_commands,
        confirm_invalid_on_fresh_process=config.confirm_invalid_on_fresh_process,
        environment_is_prepared=True,
    )


def run_tokenizer_section_derivation(
    *,
    repo_root: Path,
    output_dir: Path,
    config: SectionDerivationConfig,
) -> SectionDerivationManifest:
    """Run/resume exact per-theorem derivation and freeze the ordered partition."""

    repo = repo_root.resolve(strict=True)
    if os.environ.get("LEAN_NUM_THREADS") != str(config.lean_num_threads):
        raise TokenizerSectionDerivationError(
            "set LEAN_NUM_THREADS=1 before tokenizer-section derivation"
        )
    environment = _verify_environment(repo, config)
    theorems = _theorems(config)
    helper, helper_hash = _helper(repo)
    config_hash = hash_canonical(config.model_dump(mode="json"))
    derivation_binding_sha256 = _derivation_binding(
        config_hash=config_hash,
        theorem_partition_sha256=config.theorem_partition_sha256,
        helper_sha256=helper_hash,
        environment=environment,
    )
    output = _reject_symlink_components(output_dir, allow_missing=True)
    output_parent = _private_directory(output.parent, create=True)
    if output.exists() and (output.is_symlink() or not output.is_dir()):
        raise TokenizerSectionDerivationError("section output is not a real directory")
    work = _private_directory(
        output_parent / f".{output.name}.items-{derivation_binding_sha256}", create=True
    )
    raw_response_dir = _private_directory(Path(config.raw_response_dir), create=True)
    # Reject pre-existing links and special files before either the resume
    # reader or LeanInteract can touch them.  Checking only after execution
    # would detect a bad tree too late: a predictable raw-response filename
    # could already have followed a planted symlink.
    _normalize_private_tree(work)
    _normalize_private_tree(raw_response_dir)
    settings = _backend_settings(config)
    if settings.raw_response_dir != raw_response_dir:
        raise TokenizerSectionDerivationError("section raw-response path differs")
    if config.prepare_environment_once:
        # Build/validate the immutable project and LeanInteract REPL exactly
        # once. Pool workers must be build-disabled; otherwise all four race
        # through full Mathlib and REPL setup before processing any theorem.
        LeanInteractBackend.prepare_environment(replace(settings, environment_is_prepared=False))
    backend = LeanInteractBackend(settings)

    def pending_for(
        theorem: TheoremRecord,
    ) -> tuple[TheoremRecord, Path, LeanRequest, str, str]:
        item = work / f"{theorem.theorem_id.removeprefix('thm:')}.json"
        code, lookup = _command(
            theorem,
            helper=helper,
            import_header=config.import_header,
        )
        request = LeanRequest(
            request_id=f"toksec-{theorem.theorem_id.removeprefix('thm:')[:24]}",
            context_id=theorem.context_id,
            code=code,
            allow_sorry=theorem.source != "mathlib",
            timeout_seconds=config.timeout_seconds,
        )
        return theorem, item, request, lookup, _request_hash(request, context_id=config.context_id)

    def execute(
        pending: Sequence[tuple[TheoremRecord, Path, LeanRequest, str, str]],
    ) -> None:
        if not pending:
            return
        results = backend.run_batch([row[2] for row in pending])
        _normalize_private_tree(raw_response_dir)
        for (theorem, item, _request, lookup, request_hash), result in zip(
            pending, results, strict=True
        ):
            if result.request_hash != request_hash:
                raise TokenizerSectionDerivationError(
                    f"Lean request identity differs for {theorem.theorem_id}"
                )
            if result.status not in (LeanStatus.VALID, LeanStatus.VALID_WITH_SORRY):
                raise TokenizerSectionDerivationError(
                    f"Lean section derivation failed for {theorem.theorem_id}: {result.status}"
                )
            _write_item(
                item,
                _parse_result(theorem, lookup, result.messages),
                derivation_binding_sha256=derivation_binding_sha256,
                request_hash=request_hash,
            )

    preflight = _preflight_theorems(theorems, config)
    try:
        # This deliberately re-executes on every launch, even on a completed
        # resume.  The frozen profile therefore proves that the configured
        # four-worker LeanInteract pool can start and process both source paths
        # before the 10,000-record job is allowed to continue.
        execute([pending_for(theorem) for theorem in preflight])
        for start in range(0, len(theorems), config.chunk_size):
            chunk = theorems[start : start + config.chunk_size]
            pending: list[tuple[TheoremRecord, Path, LeanRequest, str, str]] = []
            for theorem in chunk:
                row = pending_for(theorem)
                item = row[1]
                if item.exists():
                    _load_item(
                        item,
                        theorem=theorem,
                        derivation_binding_sha256=derivation_binding_sha256,
                        expected_request_hash=row[4],
                    )
                    continue
                pending.append(row)
            execute(pending)
    finally:
        backend.close()
        _normalize_private_tree(raw_response_dir)

    records: list[SemanticSectionRecord] = []
    for theorem in theorems:
        row = pending_for(theorem)
        item = row[1]
        if not item.exists():
            raise TokenizerSectionDerivationError("section derivation is incomplete")
        records.append(
            _load_item(
                item,
                theorem=theorem,
                derivation_binding_sha256=derivation_binding_sha256,
                expected_request_hash=row[4],
            )
        )
    partition = b"".join(
        canonical_json_bytes(record.model_dump(mode="json")) + b"\n" for record in records
    )
    partition_hash = hashlib.sha256(partition).hexdigest()
    per_source = dict(sorted(Counter(record.source for record in records).items()))
    identity = {
        "derivation_binding_sha256": derivation_binding_sha256,
        "record_count": len(records),
        "per_source": per_source,
        "context_id": config.context_id,
        "sections_sha256": partition_hash,
    }
    manifest = SectionDerivationManifest(
        schema_version=4,
        method_version=METHOD_VERSION,
        derivation_id="tokenizer_sections:" + hash_canonical(identity),
        derivation_binding_sha256=derivation_binding_sha256,
        config_hash=config_hash,
        config=config,
        repository_root=str(repo),
        theorem_partition_sha256=config.theorem_partition_sha256,
        helper_sha256=helper_hash,
        environment=environment,
        preflight_theorem_ids=tuple(theorem.theorem_id for theorem in preflight),
        record_count=len(records),
        per_source=per_source,
        context_id=config.context_id,
        contains_private_source=True,
        redistribution=False,
        external_transmission=False,
        release_eligible=False,
        output_sha256={"sections.jsonl": partition_hash},
    )
    payloads = {
        "sections.jsonl": partition,
        "manifest.json": canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    }
    if output.exists():
        output = _private_directory(output, create=False)
        if {path.name for path in output.iterdir()} != set(payloads):
            raise TokenizerSectionDerivationError("section output file set differs")
        for name, payload in payloads.items():
            path = output / name
            if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
                raise TokenizerSectionDerivationError(f"section output is not regular: {name}")
            path.chmod(0o600)
            if path.read_bytes() != payload:
                raise TokenizerSectionDerivationError(f"section output differs: {name}")
        return verify_tokenizer_section_derivation(output, repo_root=repo)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output_parent))
    temporary.chmod(0o700)
    try:
        for name, payload in payloads.items():
            _write_private_file(temporary / name, payload)
        os.rename(temporary, output)
    finally:
        if temporary.exists():
            temporary.rmdir()
    return verify_tokenizer_section_derivation(output, repo_root=repo)


def verify_tokenizer_section_derivation(
    output_dir: Path,
    *,
    repo_root: Path,
) -> SectionDerivationManifest:
    """Verify the immutable section artifact and every live frozen input pin."""

    repo = repo_root.resolve(strict=True)
    output = _reject_symlink_components(output_dir, allow_missing=False)
    output = _private_directory(output, create=False)
    if {path.name for path in output.iterdir()} != {"manifest.json", "sections.jsonl"}:
        raise TokenizerSectionDerivationError("section output file set differs")
    manifest_path = output / "manifest.json"
    section_path = output / "sections.jsonl"
    for path in (manifest_path, section_path):
        if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
            raise TokenizerSectionDerivationError("section output contains a non-regular file")
        path.chmod(0o600)
    try:
        manifest = SectionDerivationManifest.model_validate(json.loads(manifest_path.read_bytes()))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenizerSectionDerivationError("invalid section derivation manifest") from exc
    if Path(manifest.repository_root) != repo:
        raise TokenizerSectionDerivationError("section repository root differs")
    config = manifest.config
    config_hash = hash_canonical(config.model_dump(mode="json"))
    if config_hash != manifest.config_hash:
        raise TokenizerSectionDerivationError("section config hash differs")
    environment = _verify_environment(repo, config)
    if environment != manifest.environment:
        raise TokenizerSectionDerivationError("section environment binding differs")
    _helper_body, helper_hash = _helper(repo)
    if helper_hash != manifest.helper_sha256:
        raise TokenizerSectionDerivationError("section helper hash differs")
    derivation_binding_sha256 = _derivation_binding(
        config_hash=config_hash,
        theorem_partition_sha256=config.theorem_partition_sha256,
        helper_sha256=helper_hash,
        environment=environment,
    )
    if derivation_binding_sha256 != manifest.derivation_binding_sha256:
        raise TokenizerSectionDerivationError("section derivation binding differs")
    theorems = _theorems(config)
    rows: list[SemanticSectionRecord] = []
    with section_path.open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                rows.append(SemanticSectionRecord.model_validate(json.loads(raw)))
            except (ValueError, json.JSONDecodeError) as exc:
                raise TokenizerSectionDerivationError(
                    f"invalid section output row {line_number}"
                ) from exc
    if len(rows) != len(theorems):
        raise TokenizerSectionDerivationError("section output denominator differs")
    if any(
        row.theorem_id != theorem.theorem_id or row.source != theorem.source
        for theorem, row in zip(theorems, rows, strict=True)
    ):
        raise TokenizerSectionDerivationError("section output order/source differs")
    partition_hash = hash_file(section_path)
    if manifest.output_sha256 != {"sections.jsonl": partition_hash}:
        raise TokenizerSectionDerivationError("section output hash differs")
    per_source = dict(sorted(Counter(row.source for row in rows).items()))
    preflight_ids = tuple(theorem.theorem_id for theorem in _preflight_theorems(theorems, config))
    if (
        manifest.record_count != len(rows)
        or manifest.per_source != per_source
        or manifest.preflight_theorem_ids != preflight_ids
    ):
        raise TokenizerSectionDerivationError("section manifest accounting differs")
    identity = {
        "derivation_binding_sha256": derivation_binding_sha256,
        "record_count": len(rows),
        "per_source": per_source,
        "context_id": config.context_id,
        "sections_sha256": partition_hash,
    }
    if manifest.derivation_id != "tokenizer_sections:" + hash_canonical(identity):
        raise TokenizerSectionDerivationError("section derivation identity differs")
    return manifest


__all__ = [
    "FROZEN_PROFILE_ID",
    "METHOD_VERSION",
    "SectionDerivationConfig",
    "SectionDerivationManifest",
    "SectionEnvironmentBinding",
    "SemanticSectionItem",
    "SemanticSectionRecord",
    "SemanticSectionUnit",
    "TokenizerSectionDerivationError",
    "load_tokenizer_section_config",
    "run_tokenizer_section_derivation",
    "verify_tokenizer_section_derivation",
]
