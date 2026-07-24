"""Fail-closed one-model local qualification pipeline for LF-021.

This module authorizes only a public, hand-authored smoke fixture.  It binds a
pinned local model, runtime, prompt, parser, provider artifacts, Lean
validation, materialization, and candidate screening without creating a
semantic label or awarding Gate 5G credit.
"""

from __future__ import annotations

import datetime
import json
import os
import platform
import stat
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, Self

from pydantic import Field, model_validator

from leanfaith.config.code_bundle import validate_code_bundle
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.loading import LoadedConfig, load_config
from leanfaith.config.models import StrictModel
from leanfaith.datasets.benchmark_signatures import (
    BenchmarkSignatureArtifact,
    BenchmarkSignatureWorkManifest,
)
from leanfaith.datasets.denylist import (
    DenylistIndex,
    FrozenRegistry,
    RepresentationSignatureManifest,
)
from leanfaith.generation.candidate_screening import (
    CandidateScreeningIndex,
    PriorCandidateIdentity,
    screen_materialized_candidate,
)
from leanfaith.generation.local_hf import (
    ChatTemplatePromptFormatter,
    LocalHFDecodingConfig,
    LocalHFGenerationRequest,
    LocalHFGenerationResult,
    LocalHFModelPin,
    PostTemplateSuffix,
)
from leanfaith.generation.local_output_adapter import (
    FINAL_FENCE_PARSER_ID,
    RAW_OR_FINAL_PARSER_ID,
    TERMINAL_FENCE_OR_RAW_PARSER_ID,
    FinalFenceError,
    LeanExtractedCandidate,
    extract_candidate_signature_with_lean,
    extract_candidate_signature_with_lean_v2,
    extract_candidate_signature_with_lean_v3,
    parser_source_sha256,
)
from leanfaith.generation.local_output_adapter_stepfun import (
    STEPFUN_TERMINAL_PARSER_ID,
    extract_stepfun_candidate_signature_with_lean,
    stepfun_parser_source_sha256,
)
from leanfaith.generation.problem_pool import ProblemPoolDenylistBinding
from leanfaith.generation.providers import (
    ProviderIdentity,
    ProviderLLMLineage,
    ProviderRawResponse,
    ProviderRequest,
    ProviderResult,
    ReplayArtifactError,
    bridge_provider_result_to_llm_lineage,
    create_provider_request_for_problem,
    persist_provider_raw_response,
    persist_provider_request,
    verify_llm_call_artifacts,
)
from leanfaith.generation.real_outputs import (
    CandidateScreeningRecord,
    CandidateScreeningStatus,
    RealOutputCandidateOutcome,
    RealOutputMaterializationResult,
    RealOutputOutcomeCode,
    admit_screened_real_output_candidate,
    candidate_benchmark_hits,
    materialize_real_output_candidate,
)
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.representations.pipeline import (
    RepresentationBatch,
    TheoremForRepresentation,
    build_representation_batch,
)
from leanfaith.schemas.enums import NLTrust, ParseStatus
from leanfaith.schemas.ids import HEX64_PATTERN
from leanfaith.schemas.llm import LLMAttemptRecord, LLMCallRecord
from leanfaith.schemas.nl_lean import ProblemPoolRecord
from leanfaith.schemas.theorem import ContextRecord, RepresentationRecord, TheoremRecord
from leanfaith.schemas.variant import VariantRecord

_HEX40 = r"^[0-9a-f]{40}$"
_REPO_ID = r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$"
_IDENTIFIER = r"^[a-z][a-z0-9_]*(?:_v[0-9]+)?$"
_CHECKPOINT_NAME = r"^[A-Za-z0-9][A-Za-z0-9._-]*$"
_TERMINAL_ID = r"^local_qualification:[0-9a-f]{64}$"
_BUNDLE_ID = r"^local_qualification_bundle:[0-9a-f]{64}$"
_RECEIPT_ID = r"^smoke_admission_receipt:[0-9a-f]{64}$"
_COMMON_PROMPT_TOKENS = ("{{THEOREM_NAME}}", "{{NL_STATEMENT}}", "{{COMMON_SUFFIX}}")
_HEADER_PROMPT_TOKEN = "{{REGISTERED_HEADER}}"
_AUTHORIZED_LOCAL_MODELS: dict[str, tuple[str, str, str, str, str]] = {
    "kimina_autoformalizer_7b": (
        "local_kimina_qualification",
        "AI-MO/Kimina-Autoformalizer-7B",
        "Qwen2.5-Coder-7B-Instruct",
        "Qwen2ForCausalLM",
        "unresolved",
    ),
    "goedel_formalizer_v2_8b": (
        "local_goedel_qualification",
        "Goedel-LM/Goedel-Formalizer-V2-8B",
        "Qwen3-8B",
        "Qwen3ForCausalLM",
        "unresolved",
    ),
    "stepfun_formalizer_7b": (
        "local_stepfun_qualification",
        "stepfun-ai/StepFun-Formalizer-7B",
        "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
        "Qwen2ForCausalLM",
        "dataset_id_declared_revision_and_lineages_unresolved",
    ),
}
_CONFIG_FAMILY: dict[str, str] = {
    "lf021_local_qualification_v1": "kimina_autoformalizer_7b",
    "lf021_local_qualification_kimina_v2": "kimina_autoformalizer_7b",
    "lf021_local_qualification_goedel_v1": "goedel_formalizer_v2_8b",
    "lf021_local_qualification_stepfun_v1": "stepfun_formalizer_7b",
}


class LocalQualificationError(RuntimeError):
    """Base error for configuration, execution, and replay failures."""


class LocalQualificationConfigError(LocalQualificationError):
    """The pinned qualification config or one of its artifacts drifted."""


class LocalQualificationReplayError(LocalQualificationError):
    """A persisted qualification bundle cannot be reproduced."""


def _relative_posix_path(value: str, *, label: str) -> PurePosixPath:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise LocalQualificationConfigError(f"{label} must be repository-relative")
    return path


def _safe_regular_input(repo_root: Path, value: str, *, label: str) -> Path:
    """Resolve one repository input without accepting any symlink component."""

    relative = _relative_posix_path(value, label=label)
    root = Path(os.path.abspath(repo_root))
    candidate = root.joinpath(*relative.parts)
    current = root
    if current.is_symlink():
        raise LocalQualificationConfigError(f"{label} repository root must not be a symlink")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise LocalQualificationConfigError(f"{label} must not traverse a symlink: {value}")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
        mode = os.stat(candidate, follow_symlinks=False).st_mode
    except (OSError, ValueError) as exc:
        raise LocalQualificationConfigError(
            f"{label} is missing, unreadable, or escapes the repository: {value}"
        ) from exc
    if not stat.S_ISREG(mode):
        raise LocalQualificationConfigError(f"{label} must be a regular file: {value}")
    return resolved


def _safe_bundle_path(
    artifact_root: Path,
    path: Path,
    *,
    label: str,
    must_exist: bool,
) -> Path:
    """Keep a bundle path below its root and reject symlinked components."""

    root = Path(os.path.abspath(artifact_root))
    candidate = Path(os.path.abspath(path))
    try:
        relative = candidate.relative_to(root)
    except ValueError as exc:
        raise LocalQualificationReplayError(f"{label} escapes artifact_root") from exc
    current = root
    if current.is_symlink():
        raise LocalQualificationReplayError(f"{label} artifact_root must not be a symlink")
    for index, part in enumerate(relative.parts):
        current = current / part
        if current.is_symlink():
            raise LocalQualificationReplayError(f"{label} traverses a symlink: {current}")
        if current.exists() and index < len(relative.parts) - 1 and not current.is_dir():
            raise LocalQualificationReplayError(f"{label} parent is not a directory: {current}")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root.resolve(strict=False))
    except ValueError as exc:
        raise LocalQualificationReplayError(f"{label} resolves outside artifact_root") from exc
    if must_exist:
        try:
            mode = os.stat(candidate, follow_symlinks=False).st_mode
        except OSError as exc:
            raise LocalQualificationReplayError(f"{label} is missing: {candidate}") from exc
        if not stat.S_ISREG(mode):
            raise LocalQualificationReplayError(f"{label} must be a regular file: {candidate}")
    return candidate


def _ensure_safe_bundle_directory(artifact_root: Path, directory: Path) -> None:
    directory = _safe_bundle_path(
        artifact_root,
        directory,
        label="qualification bundle directory",
        must_exist=False,
    )
    root = Path(os.path.abspath(artifact_root))
    relative = directory.relative_to(root)
    current = root
    for part in relative.parts:
        current = current / part
        with suppress(FileExistsError):
            current.mkdir()
        if current.is_symlink() or not current.is_dir():
            raise LocalQualificationReplayError(
                f"qualification bundle directory is unsafe: {current}"
            )


def _persist_immutable_bytes(
    path: Path,
    payload: bytes,
    *,
    artifact_root: Path,
) -> str:
    """Create an immutable regular file, accepting only byte-identical replay."""

    path = _safe_bundle_path(
        artifact_root,
        path,
        label="qualification bundle artifact",
        must_exist=False,
    )
    _ensure_safe_bundle_directory(artifact_root, path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o644)
    except FileExistsError:
        existing = _safe_bundle_path(
            artifact_root,
            path,
            label="existing qualification bundle artifact",
            must_exist=True,
        )
        try:
            observed = existing.read_bytes()
        except OSError as exc:
            raise LocalQualificationReplayError(
                f"cannot read existing qualification artifact: {existing}"
            ) from exc
        if observed != payload:
            raise LocalQualificationReplayError(
                f"immutable qualification artifact conflict: {existing}"
            ) from None
    else:
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            with suppress(OSError):
                path.unlink()
            raise
    return sha256_hex(payload)


class LocalMetadataHashes(StrictModel):
    readme: str = Field(pattern=HEX64_PATTERN)
    config: str = Field(pattern=HEX64_PATTERN)
    tokenizer_config: str = Field(pattern=HEX64_PATTERN)
    generation_config: str = Field(pattern=HEX64_PATTERN)


class LocalCheckpointFile(StrictModel):
    artifact: str = Field(pattern=_CHECKPOINT_NAME)
    bytes: int = Field(ge=1, strict=True)
    sha256: str = Field(pattern=HEX64_PATTERN)


class LocalCheckpointArtifacts(StrictModel):
    index: LocalCheckpointFile
    shards: tuple[LocalCheckpointFile, ...] = Field(min_length=1)
    auxiliary_files: tuple[LocalCheckpointFile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_files(self) -> Self:
        names = (
            self.index.artifact,
            *(shard.artifact for shard in self.shards),
            *(item.artifact for item in self.auxiliary_files),
        )
        if len(names) != len(set(names)):
            raise ValueError("checkpoint artifact names must be unique")
        if self.index.artifact != "model.safetensors.index.json":
            raise ValueError("checkpoint index must be model.safetensors.index.json")
        if any(not shard.artifact.endswith(".safetensors") for shard in self.shards):
            raise ValueError("checkpoint shards must be safetensors artifacts")
        return self


class LocalQualificationModel(StrictModel):
    family_id: str = Field(pattern=_IDENTIFIER)
    provider_slot: str = Field(pattern=_IDENTIFIER)
    repo_id: str = Field(pattern=_REPO_ID)
    revision: str = Field(pattern=_HEX40)
    tokenizer_revision: str = Field(pattern=_HEX40)
    license: Literal["Apache-2.0"]
    base_family: str = Field(min_length=1)
    architecture: Literal["Qwen2ForCausalLM", "Qwen3ForCausalLM"]
    model_positions: int = Field(ge=1, strict=True)
    tokenizer_positions: int | None = Field(
        default=None,
        ge=1,
        strict=True,
        exclude_if=lambda value: value is None,
    )
    checkpoint_bytes: int = Field(ge=1, strict=True)
    metadata_hashes: LocalMetadataHashes
    checkpoint_artifacts: LocalCheckpointArtifacts | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    training_lineage_status: str = Field(min_length=1)
    activation_scope: Literal["one_public_fixture"]
    supervision_eligible: Literal[False] = False
    heldout_generator: Literal[False] = False

    @model_validator(mode="after")
    def _authorized_exact_model(self) -> Self:
        expected = _AUTHORIZED_LOCAL_MODELS.get(self.family_id)
        if expected is None:
            raise ValueError("local qualification model family is not authorized")
        observed = (
            self.provider_slot,
            self.repo_id,
            self.base_family,
            self.architecture,
            self.training_lineage_status,
        )
        if observed != expected:
            raise ValueError("local qualification model metadata differs from its authorized pin")
        if self.tokenizer_revision != self.revision:
            raise ValueError("local model and tokenizer revisions must be identical")
        if self.checkpoint_artifacts is None:
            raise ValueError("local qualification models require exact checkpoint hashes")
        total = sum(shard.bytes for shard in self.checkpoint_artifacts.shards)
        if total != self.checkpoint_bytes:
            raise ValueError("checkpoint shard bytes do not match checkpoint_bytes")
        return self


class LocalCheckpointVerification(StrictModel):
    """Exact local-cache bytes verified before a qualification model is loaded."""

    schema_version: Literal[1] = 1
    model_repo_id: str = Field(pattern=_REPO_ID)
    model_revision: str = Field(pattern=_HEX40)
    snapshot_reference: str = Field(min_length=1)
    files: tuple[LocalCheckpointFile, ...] = Field(min_length=5)
    checkpoint_bytes: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def _unique_files(self) -> Self:
        names = [item.artifact for item in self.files]
        if len(names) != len(set(names)):
            raise ValueError("checkpoint verification file names must be unique")
        shard_bytes = sum(
            item.bytes for item in self.files if item.artifact.endswith(".safetensors")
        )
        if shard_bytes != self.checkpoint_bytes:
            raise ValueError("verified checkpoint bytes do not reconcile")
        return self

    @property
    def verification_hash(self) -> str:
        return hash_canonical(
            {
                "schema": "lf021_local_checkpoint_verification_v1",
                **self.model_dump(mode="json"),
            }
        )

    def matches_model(self, model: LocalQualificationModel) -> bool:
        checkpoint = model.checkpoint_artifacts
        if (
            checkpoint is None
            or self.model_repo_id != model.repo_id
            or self.model_revision != model.revision
            or self.checkpoint_bytes != model.checkpoint_bytes
        ):
            return False
        expected_hashes = {
            "README.md": model.metadata_hashes.readme,
            "config.json": model.metadata_hashes.config,
            "tokenizer_config.json": model.metadata_hashes.tokenizer_config,
            "generation_config.json": model.metadata_hashes.generation_config,
            checkpoint.index.artifact: checkpoint.index.sha256,
            **{shard.artifact: shard.sha256 for shard in checkpoint.shards},
            **{item.artifact: item.sha256 for item in checkpoint.auxiliary_files},
        }
        observed_hashes = {item.artifact: item.sha256 for item in self.files}
        if observed_hashes != expected_hashes:
            return False
        expected_sizes = {
            checkpoint.index.artifact: checkpoint.index.bytes,
            **{shard.artifact: shard.bytes for shard in checkpoint.shards},
            **{item.artifact: item.bytes for item in checkpoint.auxiliary_files},
        }
        observed_sizes = {item.artifact: item.bytes for item in self.files}
        return all(
            observed_sizes.get(artifact) == size for artifact, size in expected_sizes.items()
        )


def verify_local_checkpoint_artifacts(
    model: LocalQualificationModel,
    *,
    snapshot_directory: Path,
) -> LocalCheckpointVerification:
    """Hash every configured metadata/index/shard byte before model loading."""

    checkpoint = model.checkpoint_artifacts
    if checkpoint is None:
        raise LocalQualificationConfigError(
            "model does not declare a checkpoint manifest for exact verification"
        )
    snapshot = Path(os.path.abspath(snapshot_directory))
    if snapshot.name != model.revision or not snapshot.is_dir():
        raise LocalQualificationConfigError(
            "local checkpoint snapshot directory does not match the pinned revision"
        )
    expected = (
        LocalCheckpointFile(
            artifact="README.md",
            bytes=(snapshot / "README.md").stat().st_size
            if (snapshot / "README.md").is_file()
            else 1,
            sha256=model.metadata_hashes.readme,
        ),
        LocalCheckpointFile(
            artifact="config.json",
            bytes=(snapshot / "config.json").stat().st_size
            if (snapshot / "config.json").is_file()
            else 1,
            sha256=model.metadata_hashes.config,
        ),
        LocalCheckpointFile(
            artifact="tokenizer_config.json",
            bytes=(snapshot / "tokenizer_config.json").stat().st_size
            if (snapshot / "tokenizer_config.json").is_file()
            else 1,
            sha256=model.metadata_hashes.tokenizer_config,
        ),
        LocalCheckpointFile(
            artifact="generation_config.json",
            bytes=(snapshot / "generation_config.json").stat().st_size
            if (snapshot / "generation_config.json").is_file()
            else 1,
            sha256=model.metadata_hashes.generation_config,
        ),
        checkpoint.index,
        *checkpoint.shards,
        *checkpoint.auxiliary_files,
    )
    verified: list[LocalCheckpointFile] = []
    for item in expected:
        path = snapshot / item.artifact
        try:
            is_file = path.is_file()
            byte_count = path.stat().st_size if is_file else -1
            digest = hash_file(path) if is_file else ""
        except OSError as exc:
            raise LocalQualificationConfigError(
                f"cannot verify local checkpoint artifact: {item.artifact}"
            ) from exc
        if not is_file or byte_count != item.bytes or digest != item.sha256:
            raise LocalQualificationConfigError(
                f"local checkpoint artifact differs from its exact pin: {item.artifact}"
            )
        verified.append(item)
    return LocalCheckpointVerification(
        model_repo_id=model.repo_id,
        model_revision=model.revision,
        snapshot_reference=f"hf-cache://{model.repo_id}@{model.revision}",
        files=tuple(verified),
        checkpoint_bytes=model.checkpoint_bytes,
    )


class LocalPromptSuffix(StrictModel):
    suffix_id: str = Field(pattern=_IDENTIFIER)
    text: str = Field(min_length=1)
    content_sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _content_matches(self) -> Self:
        if sha256_hex(self.text.encode("utf-8")) != self.content_sha256:
            raise ValueError("post-template suffix hash differs from text")
        return self

    def runtime_suffix(self) -> PostTemplateSuffix:
        return PostTemplateSuffix(suffix_id=self.suffix_id, text=self.text)


class LocalQualificationPrompt(StrictModel):
    formatter_id: Literal[
        "kimina_card_chat_v1",
        "goedel_card_chat_v1",
        "stepfun_card_think_v1",
    ]
    formatter_hash: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
        exclude_if=lambda value: value is None,
    )
    system_prompt: str | None
    add_generation_prompt: bool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    post_template_suffix: LocalPromptSuffix | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    chat_template_sha256: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
        exclude_if=lambda value: value is None,
    )
    template_artifact: str = Field(min_length=1)
    template_sha256: str = Field(pattern=HEX64_PATTERN)
    common_suffix_artifact: str = Field(min_length=1)
    common_suffix_sha256: str = Field(pattern=HEX64_PATTERN)
    parser_id: Literal[
        "lean_final_fence_signature_v1",
        "lean_final_fence_or_raw_signature_v2",
        "lean_terminal_fence_or_raw_signature_v3",
        "lean_stepfun_think_terminal_fence_v1",
    ]
    parser_source_artifact: str = Field(min_length=1)
    parser_source_sha256: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _relative_artifacts(self) -> Self:
        for field_name in (
            "template_artifact",
            "common_suffix_artifact",
            "parser_source_artifact",
        ):
            value = getattr(self, field_name)
            path = PurePosixPath(value)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field_name} must be repository-relative")
        expert = "You are an expert in mathematics and Lean 4."
        if self.formatter_id == "kimina_card_chat_v1":
            if (
                self.system_prompt != expert
                or self.formatter_hash is not None
                or self.add_generation_prompt is not None
                or self.post_template_suffix is not None
                or self.chat_template_sha256 is not None
            ):
                raise ValueError("legacy Kimina formatter fields must remain byte-compatible")
            return self
        if self.add_generation_prompt is not True or self.chat_template_sha256 is None:
            raise ValueError("new qualification formatters require exact chat-template binding")
        if self.formatter_hash is None:
            raise ValueError("new qualification formatters require formatter_hash")
        if self.formatter_id == "goedel_card_chat_v1":
            if (
                self.parser_id != TERMINAL_FENCE_OR_RAW_PARSER_ID
                or self.system_prompt is not None
                or self.post_template_suffix is not None
            ):
                raise ValueError("Goedel requires user-only chat and no post-template suffix")
        elif (
            self.parser_id != STEPFUN_TERMINAL_PARSER_ID
            or self.system_prompt != expert
            or self.post_template_suffix is None
            or self.post_template_suffix.suffix_id != "stepfun_think_v1"
            or self.post_template_suffix.text != "<think>"
        ):
            raise ValueError("StepFun requires the exact system prompt and <think> suffix")
        formatter = ChatTemplatePromptFormatter(
            formatter_id=self.formatter_id,
            system_prompt=self.system_prompt,
            add_generation_prompt=self.add_generation_prompt,
            post_template_suffix=(
                None
                if self.post_template_suffix is None
                else self.post_template_suffix.runtime_suffix()
            ),
        )
        if formatter.formatter_hash != self.formatter_hash:
            raise ValueError("configured formatter hash differs from executable formatter")
        return self


class LocalObservedSpecialTokens(StrictModel):
    observation_mode: Literal["exact_local_tokenizer_and_generation_config"]
    transformers_version: str = Field(min_length=1)
    tokenizer_class: str = Field(min_length=1)
    tokenizer_bos_token_id: int = Field(ge=0, strict=True)
    tokenizer_eos_token_id: int = Field(ge=0, strict=True)
    tokenizer_pad_token_id: int = Field(ge=0, strict=True)
    model_config_bos_token_id: int = Field(ge=0, strict=True)
    model_config_eos_token_id: int = Field(ge=0, strict=True)
    generation_config_bos_token_id: int = Field(ge=0, strict=True)
    generation_config_eos_token_id: int = Field(ge=0, strict=True)
    generation_config_pad_token_id: int | None = Field(default=None, ge=0, strict=True)
    rendered_prompt_first_token_id: int = Field(ge=0, strict=True)
    request_eos_token_id: int = Field(ge=0, strict=True)
    request_pad_token_id: int = Field(ge=0, strict=True)


class LocalQualificationFixtureConfig(StrictModel):
    fixture_artifact: str = Field(min_length=1)
    fixture_sha256: str = Field(pattern=HEX64_PATTERN)
    import_header_artifact: str = Field(min_length=1)
    import_header_sha256: str = Field(pattern=HEX64_PATTERN)
    project_registry_key: str = Field(min_length=1)
    public_only: Literal[True] = True
    private_source_content: Literal[False] = False

    @model_validator(mode="after")
    def _relative_artifacts(self) -> Self:
        for field_name in ("fixture_artifact", "import_header_artifact"):
            path = PurePosixPath(getattr(self, field_name))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field_name} must be repository-relative")
        return self


class LocalCandidateRegistryEntry(StrictModel):
    family_id: str = Field(min_length=1)
    repo_id: str = Field(pattern=_REPO_ID)
    revision: str = Field(pattern=_HEX40)
    role: Literal["supervision_candidate", "heldout_candidate"]
    status: Literal["fixture_qualification_only", "disabled"]


class LocalQualificationConfig(StrictModel):
    schema_version: Literal[1] = 1
    config_id: str = Field(pattern=_IDENTIFIER)
    implementation_status: (
        Literal[
            "pending_generic_qualification_schema",
            "ready_for_preflight",
        ]
        | None
    ) = Field(default=None, exclude_if=lambda value: value is None)
    status: Literal["fixture_qualification_only"]
    artifact_class: Literal["smoke"]
    qualifies_for_gate5g: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    external_endpoints_allowed: Literal[False] = False
    private_source_content_allowed: Literal[False] = False
    active_model: LocalQualificationModel
    prompt: LocalQualificationPrompt
    observed_special_tokens: LocalObservedSpecialTokens | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    decoding: LocalHFDecodingConfig
    qualification_fixture: LocalQualificationFixtureConfig | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    candidate_registry: tuple[LocalCandidateRegistryEntry, ...] = Field(min_length=4)

    @model_validator(mode="after")
    def _registry_and_decoding(self) -> Self:
        family_ids = [entry.family_id for entry in self.candidate_registry]
        if len(family_ids) != len(set(family_ids)):
            raise ValueError("candidate_registry family IDs must be unique")
        active = [entry for entry in self.candidate_registry if entry.status != "disabled"]
        if len(active) != 1:
            raise ValueError("exactly one fixture-qualification candidate must be active")
        if (
            active[0].family_id != self.active_model.family_id
            or active[0].repo_id != self.active_model.repo_id
            or active[0].revision != self.active_model.revision
        ):
            raise ValueError("active candidate registry entry differs from active_model")
        if not self.decoding.do_sample:
            raise ValueError("local qualification requires frozen sampled decoding")
        expected_family = _CONFIG_FAMILY.get(self.config_id)
        if expected_family is None or expected_family != self.active_model.family_id:
            raise ValueError("qualification config ID differs from the active model family")
        formatter_family = {
            "kimina_card_chat_v1": "kimina_autoformalizer_7b",
            "goedel_card_chat_v1": "goedel_formalizer_v2_8b",
            "stepfun_card_think_v1": "stepfun_formalizer_7b",
        }[self.prompt.formatter_id]
        if formatter_family != self.active_model.family_id:
            raise ValueError("prompt formatter differs from the active model family")
        if self.active_model.family_id == "goedel_formalizer_v2_8b":
            expected_decoding = LocalHFDecodingConfig(
                max_new_tokens=16_384,
                do_sample=True,
                temperature=0.9,
                top_p=0.95,
                top_k=20,
                seed=30,
                repetition_penalty=1.0,
                eos_token_id=(151_645, 151_643),
                pad_token_id=151_643,
            )
            if self.decoding != expected_decoding:
                raise ValueError("Goedel decoding differs from the pinned model card")
        if self.active_model.family_id == "stepfun_formalizer_7b":
            expected_decoding = LocalHFDecodingConfig(
                max_new_tokens=16_384,
                do_sample=True,
                temperature=0.6,
                top_p=0.95,
                top_k=None,
                seed=0,
                repetition_penalty=1.0,
                eos_token_id=151_643,
                pad_token_id=151_643,
            )
            if self.decoding != expected_decoding:
                raise ValueError("StepFun decoding differs from the pinned model card")
            observed = self.observed_special_tokens
            if (
                observed is None
                or observed.request_eos_token_id != self.decoding.eos_token_id
                or observed.request_pad_token_id != self.decoding.pad_token_id
                or observed.rendered_prompt_first_token_id != 151_646
            ):
                raise ValueError("StepFun effective token bindings are absent or inconsistent")
        return self


def _repo_artifact(repo_root: Path, value: str) -> Path:
    path = (repo_root / value).resolve()
    try:
        path.relative_to(repo_root.resolve())
    except ValueError as exc:
        raise LocalQualificationConfigError(f"artifact escapes repository: {value}") from exc
    return path


def load_local_qualification_config(
    path: Path,
    *,
    repo_root: Path,
) -> LoadedConfig[LocalQualificationConfig]:
    """Load the exact fixture-only config and verify all executable artifacts."""

    loaded = load_config(path, LocalQualificationConfig)
    prompt = loaded.config.prompt
    checks = (
        ("template", prompt.template_artifact, prompt.template_sha256),
        ("common suffix", prompt.common_suffix_artifact, prompt.common_suffix_sha256),
        ("parser source", prompt.parser_source_artifact, prompt.parser_source_sha256),
    )
    for label, artifact, expected in checks:
        artifact_path = _repo_artifact(repo_root, artifact)
        if not artifact_path.is_file():
            raise LocalQualificationConfigError(f"{label} artifact is missing: {artifact}")
        observed = hash_file(artifact_path)
        if observed != expected:
            raise LocalQualificationConfigError(
                f"{label} hash mismatch: expected {expected}, observed {observed}"
            )
    executable_parser_hash = (
        stepfun_parser_source_sha256()
        if prompt.parser_id == STEPFUN_TERMINAL_PARSER_ID
        else parser_source_sha256()
    )
    if (
        prompt.parser_id
        not in {
            FINAL_FENCE_PARSER_ID,
            RAW_OR_FINAL_PARSER_ID,
            TERMINAL_FENCE_OR_RAW_PARSER_ID,
            STEPFUN_TERMINAL_PARSER_ID,
        }
        or prompt.parser_source_sha256 != executable_parser_hash
    ):
        raise LocalQualificationConfigError("configured parser identity differs from executable")
    fixture = loaded.config.qualification_fixture
    if fixture is not None:
        for label, artifact, expected in (
            ("qualification fixture", fixture.fixture_artifact, fixture.fixture_sha256),
            (
                "qualification import header",
                fixture.import_header_artifact,
                fixture.import_header_sha256,
            ),
        ):
            artifact_path = _repo_artifact(repo_root, artifact)
            if not artifact_path.is_file() or hash_file(artifact_path) != expected:
                raise LocalQualificationConfigError(f"{label} artifact/hash binding is invalid")
    return loaded


class RuntimeEnvironmentBinding(StrictModel):
    """Exact runtime/environment facts bound into one qualification attempt."""

    schema_version: Literal[1] = 1
    environment_lock_artifact: str = Field(min_length=1)
    environment_lock_sha256: str = Field(pattern=HEX64_PATTERN)
    python_version: str = Field(min_length=1)
    torch_version: str = Field(min_length=1)
    transformers_version: str = Field(min_length=1)
    driver_version: str = Field(min_length=1)
    device_name: str = Field(min_length=1)
    dtype: Literal["auto", "float16", "bfloat16"]
    runtime_adapter_artifact: str = Field(min_length=1)
    runtime_adapter_sha256: str = Field(pattern=HEX64_PATTERN)

    @property
    def runtime_hash(self) -> str:
        return hash_canonical(
            {
                "schema": "lf021_runtime_environment_v1",
                **self.model_dump(mode="json"),
            }
        )

    @model_validator(mode="after")
    def _paths_are_relative(self) -> Self:
        for field_name in ("environment_lock_artifact", "runtime_adapter_artifact"):
            path = PurePosixPath(getattr(self, field_name))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field_name} must be repository-relative")
        return self


def verify_runtime_binding(binding: RuntimeEnvironmentBinding, *, repo_root: Path) -> None:
    """Fail when a runtime lock or adapter changed after binding."""

    for label, artifact, expected in (
        (
            "environment lock",
            binding.environment_lock_artifact,
            binding.environment_lock_sha256,
        ),
        (
            "runtime adapter",
            binding.runtime_adapter_artifact,
            binding.runtime_adapter_sha256,
        ),
    ):
        path = _safe_regular_input(repo_root, artifact, label=label)
        if not path.is_file() or hash_file(path) != expected:
            raise LocalQualificationConfigError(f"{label} artifact/hash binding is invalid")


def make_runtime_binding(
    *,
    repo_root: Path,
    environment_lock_artifact: str,
    torch_version: str,
    transformers_version: str,
    driver_version: str,
    device_name: str,
    dtype: Literal["auto", "float16", "bfloat16"],
    runtime_adapter_artifact: str = "src/leanfaith/generation/local_hf.py",
) -> RuntimeEnvironmentBinding:
    """Build a runtime binding from existing, repository-owned artifacts."""

    lock_path = _safe_regular_input(
        repo_root,
        environment_lock_artifact,
        label="environment lock",
    )
    adapter_path = _safe_regular_input(
        repo_root,
        runtime_adapter_artifact,
        label="runtime adapter",
    )
    if not lock_path.is_file() or not adapter_path.is_file():
        raise LocalQualificationConfigError("runtime binding artifacts must already exist")
    return RuntimeEnvironmentBinding(
        environment_lock_artifact=environment_lock_artifact,
        environment_lock_sha256=hash_file(lock_path),
        python_version=platform.python_version(),
        torch_version=torch_version,
        transformers_version=transformers_version,
        driver_version=driver_version,
        device_name=device_name,
        dtype=dtype,
        runtime_adapter_artifact=runtime_adapter_artifact,
        runtime_adapter_sha256=hash_file(adapter_path),
    )


@dataclass(frozen=True, slots=True)
class RenderedKiminaPrompt:
    user_prompt: str
    template_bundle_hash: str
    render_hash: str


def _read_exact_text(path: Path) -> str:
    try:
        return path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LocalQualificationConfigError(f"cannot read UTF-8 prompt artifact {path}") from exc


def build_local_qualification_formatter(
    config: LocalQualificationConfig,
) -> ChatTemplatePromptFormatter:
    """Build the exact chat formatter bound by one qualification config."""

    suffix = config.prompt.post_template_suffix
    formatter = ChatTemplatePromptFormatter(
        formatter_id=config.prompt.formatter_id,
        system_prompt=config.prompt.system_prompt,
        add_generation_prompt=(
            True
            if config.prompt.add_generation_prompt is None
            else config.prompt.add_generation_prompt
        ),
        post_template_suffix=None if suffix is None else suffix.runtime_suffix(),
    )
    expected = config.prompt.formatter_hash
    if expected is not None and formatter.formatter_hash != expected:
        raise LocalQualificationConfigError(
            "configured prompt formatter hash differs from executable formatter"
        )
    return formatter


def render_local_qualification_prompt(
    *,
    config: LocalQualificationConfig,
    repo_root: Path,
    problem: ProblemPoolRecord,
    expected_declaration_name: str,
    registered_header: str,
) -> RenderedKiminaPrompt:
    """Render exact versioned user text for one public qualification fixture."""

    if (
        problem.eligibility != "eligible"
        or problem.nl_trust is not NLTrust.TRUSTED
        or problem.private_source_content
        or not problem.denylist_checked
        or problem.denylist_hits
    ):
        raise LocalQualificationConfigError(
            "local qualification requires an eligible public trusted denylist-cleared fixture"
        )
    if not expected_declaration_name.strip() or "\x00" in expected_declaration_name:
        raise LocalQualificationConfigError("expected declaration name is invalid")
    template = _read_exact_text(_repo_artifact(repo_root, config.prompt.template_artifact))
    suffix = _read_exact_text(
        _repo_artifact(repo_root, config.prompt.common_suffix_artifact)
    ).strip()
    required_tokens = list(_COMMON_PROMPT_TOKENS)
    requires_header = config.prompt.formatter_id != "goedel_card_chat_v1"
    if requires_header:
        required_tokens.append(_HEADER_PROMPT_TOKEN)
    if any(template.count(token) != 1 for token in required_tokens) or (
        not requires_header and _HEADER_PROMPT_TOKEN in template
    ):
        raise LocalQualificationConfigError(
            "qualification template placeholder set differs from its formatter contract"
        )
    rendered = (
        template.replace("{{THEOREM_NAME}}", expected_declaration_name)
        .replace("{{NL_STATEMENT}}", problem.nl_statement)
        .replace("{{REGISTERED_HEADER}}", registered_header.rstrip())
        .replace("{{COMMON_SUFFIX}}", suffix)
    )
    if "{{" in rendered or "}}" in rendered:
        raise LocalQualificationConfigError("rendered qualification prompt contains placeholders")
    if config.prompt.formatter_id == "kimina_card_chat_v1":
        # Keep existing Kimina request/provider identities byte-for-byte stable.
        bundle_payload: dict[str, object] = {
            "schema": "kimina_prompt_bundle_v1",
            "formatter_id": config.prompt.formatter_id,
            "system_prompt": config.prompt.system_prompt,
            "template_sha256": config.prompt.template_sha256,
            "common_suffix_sha256": config.prompt.common_suffix_sha256,
        }
    else:
        bundle_payload = {
            "schema": "local_qualification_prompt_bundle_v2",
            "prompt": config.prompt.model_dump(mode="json"),
        }
    bundle_hash = hash_canonical(bundle_payload)
    return RenderedKiminaPrompt(
        user_prompt=rendered,
        template_bundle_hash=bundle_hash,
        render_hash=sha256_hex(rendered.encode("utf-8")),
    )


def render_kimina_qualification_prompt(
    *,
    config: LocalQualificationConfig,
    repo_root: Path,
    problem: ProblemPoolRecord,
    expected_declaration_name: str,
    registered_header: str,
) -> RenderedKiminaPrompt:
    """Backward-compatible name for the generic qualification renderer."""

    return render_local_qualification_prompt(
        config=config,
        repo_root=repo_root,
        problem=problem,
        expected_declaration_name=expected_declaration_name,
        registered_header=registered_header,
    )


class LocalRuntime(Protocol):
    def generate(self, request: LocalHFGenerationRequest) -> LocalHFGenerationResult: ...


class LocalQualificationFixturePreflight(StrictModel):
    """Model-free proof that one public fixture is safe to send to Kimina."""

    schema_version: Literal[1] = 1
    fixture_id: str = Field(min_length=1)
    fixture_sha256: str = Field(pattern=HEX64_PATTERN)
    problem_record_id: str = Field(min_length=1)
    reference_theorem_id: str = Field(min_length=1)
    reference_representation_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    project_registry_key: str = Field(min_length=1)
    project_revision: str = Field(min_length=1)
    import_header_artifact: str = Field(min_length=1)
    import_header_sha256: str = Field(pattern=HEX64_PATTERN)
    active_registry_hash: str = Field(pattern=HEX64_PATTERN)
    candidate_benchmark_hits: tuple[str, ...] = ()
    model_execution_performed: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    qualifies_for_gate5g: Literal[False] = False

    @model_validator(mode="after")
    def _clean_preflight(self) -> Self:
        if self.candidate_benchmark_hits:
            raise ValueError("fixture preflight cannot retain benchmark-overlap hits")
        header = PurePosixPath(self.import_header_artifact)
        if header.is_absolute() or ".." in header.parts:
            raise ValueError("import_header_artifact must be repository-relative")
        return self


def preflight_local_qualification_fixture(
    *,
    fixture_id: str,
    fixture_sha256: str,
    import_header_artifact: str,
    problem: ProblemPoolRecord,
    reference: TheoremRecord,
    context: ContextRecord,
    registered_header: str,
    backend: LeanInteractBackend,
    screening_index: CandidateScreeningIndex,
    created_at: datetime.datetime,
) -> LocalQualificationFixturePreflight:
    """Validate context, reference views, and active-registry non-overlap.

    This runs before the local model is imported or loaded.  It elaborates the
    hand-authored reference through LeanInteract, derives the same model-visible
    views used for candidate screening, and rejects any active benchmark hit.
    No provider record, semantic label, or Gate-5 artifact is created.
    """

    if (
        problem.eligibility != "eligible"
        or problem.nl_trust is not NLTrust.TRUSTED
        or problem.private_source_content
        or not problem.denylist_checked
        or problem.denylist_hits
    ):
        raise LocalQualificationConfigError(
            "fixture preflight requires an eligible public trusted problem "
            "with a clean active-registry problem check"
        )
    if problem.context_id != context.context_id or reference.context_id != context.context_id:
        raise LocalQualificationConfigError("fixture problem/reference/context identities differ")
    if reference.theorem_id not in problem.reference_theorem_ids:
        raise LocalQualificationConfigError(
            "fixture problem does not bind the preflight reference theorem"
        )
    reference_full_name = reference.declaration_full_name
    if not reference_full_name:
        raise LocalQualificationConfigError(
            "fixture reference lacks a fully qualified declaration name"
        )
    if context.header_text != registered_header:
        raise LocalQualificationConfigError("fixture registered header differs from ContextRecord")
    if problem.denylist_registry_content_hash != screening_index.denylist.registry_content_hash:
        raise LocalQualificationConfigError(
            "fixture problem and candidate screening use different active registries"
        )

    result = build_representation_batch(
        backend,
        RepresentationBatch(
            context_id=context.context_id,
            import_header=registered_header,
            ordered_theorem_inputs=(
                TheoremForRepresentation(
                    theorem_id=reference.theorem_id,
                    full_name=reference_full_name,
                    proof_stripped=reference.proof_stripped_declaration,
                    context_id=context.context_id,
                    inline_declaration=True,
                    inline_source=reference.proof_stripped_declaration,
                ),
            ),
        ),
        created_at=created_at,
    )
    if len(result.ordered_representation_records) != 1 or result.per_theorem_failures:
        details = "; ".join(
            f"{failure.view}:{failure.status}:{failure.detail}"
            for failure in result.per_theorem_failures
        )
        raise LocalQualificationConfigError(
            "fixture reference representation preflight failed"
            + (f": {details}" if details else "")
        )
    representation: RepresentationRecord = result.ordered_representation_records[0]
    if (
        representation.theorem_id != reference.theorem_id
        or representation.signature_pp is None
        or representation.signature_explicit is None
        or representation.semantic_atoms is None
        or representation.operator_tree is None
        or representation.alpha_identity_fingerprint is None
    ):
        raise LocalQualificationConfigError(
            "fixture reference preflight lacks required candidate-screening views"
        )
    hits = candidate_benchmark_hits(
        denylist_index=screening_index.denylist.index,
        theorem=reference,
        representation=representation,
    )
    if hits:
        raise LocalQualificationConfigError(
            "fixture reference overlaps the active benchmark registry: " + ", ".join(hits)
        )
    return LocalQualificationFixturePreflight(
        fixture_id=fixture_id,
        fixture_sha256=fixture_sha256,
        problem_record_id=problem.problem_record_id,
        reference_theorem_id=reference.theorem_id,
        reference_representation_id=representation.representation_id,
        context_id=context.context_id,
        project_registry_key=context.project_registry_key,
        project_revision=context.project_revision,
        import_header_artifact=import_header_artifact,
        import_header_sha256=problem.import_header_hash,
        active_registry_hash=screening_index.denylist.registry_content_hash,
    )


class QualificationStatus(StrEnum):
    RUNTIME_ERROR = "runtime_error"
    PARSE_FAILED = "parse_failed"
    MATERIALIZATION_FAILED = "materialization_failed"
    SCREENING_UNAVAILABLE = "screening_unavailable"
    SCREENING_REJECTED = "screening_rejected"
    QUALIFIED_SMOKE = "qualified_smoke"


def _terminal_id(payload: Mapping[str, object]) -> str:
    return "local_qualification:" + hash_canonical(
        {"schema": "lf021_local_qualification_terminal_v1", **dict(payload)}
    )


class LocalQualificationTerminal(StrictModel):
    """One terminal accounting record; every attempt gets exactly one."""

    schema_version: Literal[1] = 1
    terminal_id: str = Field(pattern=_TERMINAL_ID)
    status: QualificationStatus
    artifact_class: Literal["smoke"] = "smoke"
    qualifies_for_gate5g: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    problem_record_id: str
    model_family: str
    model: str
    model_revision: str = Field(pattern=_HEX40)
    qualification_config_hash: str = Field(pattern=HEX64_PATTERN)
    runtime_hash: str = Field(pattern=HEX64_PATTERN)
    generation_config_hash: str = Field(pattern=HEX64_PATTERN)
    prompt_template_hash: str = Field(pattern=HEX64_PATTERN)
    prompt_render_hash: str = Field(pattern=HEX64_PATTERN)
    formatted_prompt_hash: str | None = Field(default=None, pattern=HEX64_PATTERN)
    parser_id: Literal[
        "lean_final_fence_signature_v1",
        "lean_final_fence_or_raw_signature_v2",
        "lean_terminal_fence_or_raw_signature_v3",
        "lean_stepfun_think_terminal_fence_v1",
    ]
    parser_source_sha256: str = Field(pattern=HEX64_PATTERN)
    provider_request_hash: str = Field(pattern=HEX64_PATTERN)
    llm_call_id: str
    llm_attempt_id: str
    provider_request_artifact: str
    provider_request_artifact_sha256: str = Field(pattern=HEX64_PATTERN)
    raw_response_artifact: str
    raw_response_sha256: str = Field(pattern=HEX64_PATTERN)
    parsed_statement_sha256: str | None = Field(default=None, pattern=HEX64_PATTERN)
    materialization_outcome_id: str | None = None
    candidate_theorem_id: str | None = None
    representation_id: str | None = None
    screening_id: str | None = None
    admission_receipt_id: str | None = Field(default=None, pattern=_RECEIPT_ID)
    admitted_pair_ids: tuple[str, ...] = ()
    admitted_nl_lean_id: str | None = None
    error_code: str | None = None
    error_detail: str | None = None
    created_at: datetime.datetime
    screening_at: datetime.datetime | None = None
    admission_at: datetime.datetime | None = None

    def id_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status.value,
            "problem_record_id": self.problem_record_id,
            "model_family": self.model_family,
            "model": self.model,
            "model_revision": self.model_revision,
            "qualification_config_hash": self.qualification_config_hash,
            "runtime_hash": self.runtime_hash,
            "generation_config_hash": self.generation_config_hash,
            "provider_request_hash": self.provider_request_hash,
            "llm_call_id": self.llm_call_id,
            "llm_attempt_id": self.llm_attempt_id,
            "raw_response_sha256": self.raw_response_sha256,
            "parsed_statement_sha256": self.parsed_statement_sha256,
            "materialization_outcome_id": self.materialization_outcome_id,
            "screening_id": self.screening_id,
            "error_code": self.error_code,
        }
        # Omit the new field for legacy records so their persisted terminal IDs
        # remain replayable. New qualification records always bind a receipt.
        if self.admission_receipt_id is not None:
            payload["admission_receipt_id"] = self.admission_receipt_id
        return payload

    @model_validator(mode="after")
    def _terminal_shape(self) -> Self:
        if self.terminal_id != _terminal_id(self.id_payload()):
            raise ValueError("terminal_id does not match immutable qualification outcome")
        if self.status is QualificationStatus.QUALIFIED_SMOKE:
            required = (
                self.parsed_statement_sha256,
                self.materialization_outcome_id,
                self.candidate_theorem_id,
                self.representation_id,
                self.screening_id,
                self.screening_at,
                self.admission_at,
            )
            if any(value is None for value in required):
                raise ValueError("qualified smoke terminal lacks materialization lineage")
            if self.admission_receipt_id is None:
                # Backward-compatible reader for the already archived diagnostic
                # Kimina run. New writers never take this branch.
                if self.admitted_nl_lean_id is None or not self.admitted_pair_ids:
                    raise ValueError("legacy qualified smoke terminal lacks admitted lineage")
            elif self.admitted_pair_ids or self.admitted_nl_lean_id is not None:
                raise ValueError(
                    "receipt-bound smoke terminal must not expose semantic-pool record IDs"
                )
            if self.error_code is not None or self.error_detail is not None:
                raise ValueError("qualified smoke terminal cannot carry an error")
        else:
            if not self.error_code:
                raise ValueError("failed qualification terminal requires error_code")
        return self


def _smoke_admission_receipt_id(payload: Mapping[str, object]) -> str:
    return "smoke_admission_receipt:" + hash_canonical(
        {"schema": "lf021_smoke_admission_dry_run_receipt_v1", **dict(payload)}
    )


def _canonical_utc_timestamp(value: datetime.datetime) -> str:
    """Match Pydantic's canonical JSON representation for an exact UTC instant."""

    if value.tzinfo is None or value.utcoffset() != datetime.timedelta(0):
        raise ValueError("receipt timestamps must be timezone-aware UTC")
    return value.isoformat().replace("+00:00", "Z")


class SmokeAdmissionDryRunReceipt(StrictModel):
    """Non-admitting proof that semantic-pool construction was exercised.

    The receipt records deterministic IDs produced by the admission code path,
    while every eligibility flag is hard-false and the underlying Pair/NLLean
    records are deliberately not persisted.
    """

    schema_version: Literal[1] = 1
    receipt_id: str = Field(pattern=_RECEIPT_ID)
    artifact_class: Literal["smoke"] = "smoke"
    qualifies_for_gate5g: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    semantic_pool_eligible: Literal[False] = False
    persistence_allowed: Literal[False] = False
    problem_record_id: str
    call_id: str
    pending_outcome_id: str
    dry_run_admitted_outcome_id: str
    candidate_theorem_id: str
    representation_id: str
    screening_id: str
    active_registry_hash: str = Field(pattern=HEX64_PATTERN)
    dry_run_pair_ids: tuple[str, ...] = Field(min_length=1)
    dry_run_nl_lean_id: str
    created_at: datetime.datetime

    def id_payload(self) -> dict[str, object]:
        return {
            "problem_record_id": self.problem_record_id,
            "call_id": self.call_id,
            "pending_outcome_id": self.pending_outcome_id,
            "dry_run_admitted_outcome_id": self.dry_run_admitted_outcome_id,
            "candidate_theorem_id": self.candidate_theorem_id,
            "representation_id": self.representation_id,
            "screening_id": self.screening_id,
            "active_registry_hash": self.active_registry_hash,
            "dry_run_pair_ids": self.dry_run_pair_ids,
            "dry_run_nl_lean_id": self.dry_run_nl_lean_id,
            "created_at": _canonical_utc_timestamp(self.created_at),
        }

    @model_validator(mode="after")
    def _receipt_shape(self) -> Self:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() != datetime.timedelta(0):
            raise ValueError("receipt created_at must be timezone-aware UTC")
        if list(self.dry_run_pair_ids) != sorted(set(self.dry_run_pair_ids)):
            raise ValueError("dry_run_pair_ids must be sorted and unique")
        if self.receipt_id != _smoke_admission_receipt_id(self.id_payload()):
            raise ValueError("receipt_id does not match dry-run admission lineage")
        return self

    @classmethod
    def create(
        cls,
        *,
        pending: RealOutputCandidateOutcome,
        admitted: RealOutputCandidateOutcome,
        screening: CandidateScreeningRecord,
        active_registry_hash: str,
        created_at: datetime.datetime,
    ) -> SmokeAdmissionDryRunReceipt:
        if (
            pending.outcome is not RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING
            or pending.semantic_pool_eligible
            or pending.pair_ids
            or pending.nl_lean_id is not None
            or pending.screening_id is not None
        ):
            raise ValueError("smoke receipt requires a pristine pending-screening outcome")
        if (
            admitted.outcome is not RealOutputOutcomeCode.MATERIALIZED
            or not admitted.semantic_pool_eligible
            or not admitted.pair_ids
            or admitted.nl_lean_id is None
            or admitted.screening_id != screening.screening_id
        ):
            raise ValueError("smoke receipt requires a complete in-memory dry-run admission")
        if (
            pending.problem_record_id != admitted.problem_record_id
            or pending.call_id != admitted.call_id
            or pending.candidate_theorem_id != admitted.candidate_theorem_id
            or pending.representation_id != admitted.representation_id
            or screening.candidate_theorem_id != pending.candidate_theorem_id
            or screening.representation_id != pending.representation_id
            or screening.frozen_registry_hash != active_registry_hash
        ):
            raise ValueError("smoke receipt inputs do not share one immutable lineage")
        payload: dict[str, object] = {
            "problem_record_id": pending.problem_record_id,
            "call_id": pending.call_id,
            "pending_outcome_id": pending.outcome_id,
            "dry_run_admitted_outcome_id": admitted.outcome_id,
            "candidate_theorem_id": str(pending.candidate_theorem_id),
            "representation_id": str(pending.representation_id),
            "screening_id": screening.screening_id,
            "active_registry_hash": active_registry_hash,
            "dry_run_pair_ids": tuple(sorted(admitted.pair_ids)),
            "dry_run_nl_lean_id": admitted.nl_lean_id,
            "created_at": _canonical_utc_timestamp(created_at),
        }
        return cls(
            receipt_id=_smoke_admission_receipt_id(payload),
            problem_record_id=pending.problem_record_id,
            call_id=pending.call_id,
            pending_outcome_id=pending.outcome_id,
            dry_run_admitted_outcome_id=admitted.outcome_id,
            candidate_theorem_id=str(pending.candidate_theorem_id),
            representation_id=str(pending.representation_id),
            screening_id=screening.screening_id,
            active_registry_hash=active_registry_hash,
            dry_run_pair_ids=tuple(sorted(admitted.pair_ids)),
            dry_run_nl_lean_id=str(admitted.nl_lean_id),
            created_at=created_at,
        )


type QualificationInputRole = Literal[
    "qualification_config",
    "prompt_template",
    "common_suffix",
    "parser_source",
    "runtime_adapter",
    "environment_lock",
    "fixture_source",
    "fixture_preflight",
    "import_header",
    "execution_input",
    "checkpoint_verification",
    "code_bundle",
    "benchmark_registry_manifest",
    "benchmark_active_registry",
    "benchmark_detailed_index",
    "benchmark_input_manifest",
    "prior_candidate_index",
]
type QualificationInputSourceKind = Literal["repo_file", "local_file", "canonical_record"]


class ArchivedQualificationInput(StrictModel):
    """One exact consumed input copied into a content-addressed run archive."""

    schema_version: Literal[1] = 1
    role: QualificationInputRole
    source_kind: QualificationInputSourceKind
    source_artifact: str = Field(min_length=1)
    archive_artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)
    byte_count: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _content_addressed_path(self) -> Self:
        archive = PurePosixPath(self.archive_artifact)
        if archive.is_absolute() or ".." in archive.parts:
            raise ValueError("archive_artifact must be artifact-root-relative")
        if (
            len(archive.parts) < 3
            or archive.parts[-3] != "qualification_inputs"
            or archive.parts[-2] != "sha256"
            or archive.parts[-1] != self.sha256
        ):
            raise ValueError("archive_artifact is not content-addressed by sha256")
        if self.source_kind == "repo_file":
            source = PurePosixPath(self.source_artifact)
            if source.is_absolute() or ".." in source.parts:
                raise ValueError("repo-file source_artifact must be repository-relative")
        elif self.source_kind == "local_file":
            if not self.source_artifact.startswith("file://"):
                raise ValueError("local-file source_artifact must use the file:// namespace")
        elif not self.source_artifact.startswith("inline:"):
            raise ValueError("canonical source_artifact must use the inline: namespace")
        return self


class QualificationCodeBundleBinding(StrictModel):
    """Exact executable source snapshot frozen before model execution."""

    schema_version: Literal[1] = 1
    source_artifact: str = Field(min_length=1)
    sha256: str = Field(pattern=HEX64_PATTERN)
    code_tree_hash: str = Field(pattern=HEX64_PATTERN)

    @model_validator(mode="after")
    def _relative_source(self) -> Self:
        path = PurePosixPath(self.source_artifact)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("code-bundle source_artifact must be repository-relative")
        return self


@dataclass(frozen=True, slots=True)
class QualificationScreeningInputFiles:
    """Validated benchmark bytes consumed by candidate screening."""

    registry_manifest: Path
    active_registry: Path
    detailed_index: Path
    input_manifest: Path


class QualificationInputBinding(StrictModel):
    """Replay-verifiable config, runtime, prompt, parser, and model inputs."""

    schema_version: Literal[1, 2] = 2
    qualification_config_artifact: str
    qualification_config_file_sha256: str = Field(pattern=HEX64_PATTERN)
    qualification_config_hash: str = Field(pattern=HEX64_PATTERN)
    runtime: RuntimeEnvironmentBinding
    prompt_template_artifact: str
    prompt_template_sha256: str = Field(pattern=HEX64_PATTERN)
    common_suffix_artifact: str
    common_suffix_sha256: str = Field(pattern=HEX64_PATTERN)
    parser_id: str = Field(default="lean_final_fence_signature_v1", min_length=1)
    parser_source_artifact: str
    parser_source_sha256: str = Field(pattern=HEX64_PATTERN)
    model_repo_id: str
    model_revision: str = Field(pattern=_HEX40)
    tokenizer_revision: str = Field(pattern=_HEX40)
    model_metadata_hashes: LocalMetadataHashes
    prompt_formatter_hash: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
        exclude_if=lambda value: value is None,
    )
    checkpoint_verification: LocalCheckpointVerification | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    code_bundle: QualificationCodeBundleBinding | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    screening_registry_hash: str | None = Field(
        default=None,
        pattern=HEX64_PATTERN,
        exclude_if=lambda value: value is None,
    )
    archived_inputs: tuple[ArchivedQualificationInput, ...] = ()

    @model_validator(mode="after")
    def _paths_are_relative(self) -> Self:
        for field_name in (
            "qualification_config_artifact",
            "prompt_template_artifact",
            "common_suffix_artifact",
            "parser_source_artifact",
        ):
            path = PurePosixPath(getattr(self, field_name))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError(f"{field_name} must be repository-relative")
        if self.schema_version == 1:
            if self.archived_inputs:
                raise ValueError("v1 qualification binding cannot contain archived inputs")
            return self
        by_role = {item.role: item for item in self.archived_inputs}
        if len(by_role) != len(self.archived_inputs):
            raise ValueError("qualification archive roles must be unique")
        required_roles = {
            "qualification_config",
            "prompt_template",
            "common_suffix",
            "parser_source",
            "runtime_adapter",
            "environment_lock",
            "import_header",
            "execution_input",
        }
        missing = required_roles - set(by_role)
        if missing:
            raise ValueError(
                "qualification archive lacks required roles: " + ", ".join(sorted(missing))
            )
        expected: dict[QualificationInputRole, tuple[str, str]] = {
            "qualification_config": (
                self.qualification_config_artifact,
                self.qualification_config_file_sha256,
            ),
            "prompt_template": (
                self.prompt_template_artifact,
                self.prompt_template_sha256,
            ),
            "common_suffix": (
                self.common_suffix_artifact,
                self.common_suffix_sha256,
            ),
            "parser_source": (
                self.parser_source_artifact,
                self.parser_source_sha256,
            ),
            "runtime_adapter": (
                self.runtime.runtime_adapter_artifact,
                self.runtime.runtime_adapter_sha256,
            ),
            "environment_lock": (
                self.runtime.environment_lock_artifact,
                self.runtime.environment_lock_sha256,
            ),
        }
        for role, (source_artifact, digest) in expected.items():
            item = by_role[role]
            if (
                item.source_kind != "repo_file"
                or item.source_artifact != source_artifact
                or item.sha256 != digest
            ):
                raise ValueError(f"archived {role} differs from its input binding")
        if by_role["import_header"].source_kind != "repo_file":
            raise ValueError("archived import_header must be a repository file")
        if by_role["execution_input"].source_kind != "canonical_record":
            raise ValueError("archived execution_input must be a canonical record")
        checkpoint = self.checkpoint_verification
        if checkpoint is not None:
            checkpoint_item = by_role.get("checkpoint_verification")
            if (
                checkpoint_item is None
                or checkpoint_item.source_kind != "canonical_record"
                or checkpoint_item.sha256
                != sha256_hex(canonical_json_bytes(checkpoint.model_dump(mode="json")) + b"\n")
            ):
                raise ValueError("archived checkpoint verification differs from its binding")
        elif "checkpoint_verification" in by_role:
            raise ValueError("unexpected archived checkpoint verification")
        code_bundle = self.code_bundle
        if code_bundle is not None:
            code_bundle_item = by_role.get("code_bundle")
            if (
                code_bundle_item is None
                or code_bundle_item.source_kind != "repo_file"
                or code_bundle_item.source_artifact != code_bundle.source_artifact
                or code_bundle_item.sha256 != code_bundle.sha256
            ):
                raise ValueError("archived code bundle differs from its binding")
        elif "code_bundle" in by_role:
            raise ValueError("unexpected archived code bundle")
        screening_roles = {
            "benchmark_registry_manifest",
            "benchmark_active_registry",
            "benchmark_detailed_index",
            "benchmark_input_manifest",
            "prior_candidate_index",
        }
        present_screening = screening_roles & set(by_role)
        if self.screening_registry_hash is not None:
            missing_screening = screening_roles - present_screening
            if missing_screening:
                raise ValueError(
                    "qualification archive lacks screening roles: "
                    + ", ".join(sorted(missing_screening))
                )
        elif present_screening:
            raise ValueError("unexpected archived screening inputs")
        return self

    @property
    def binding_hash(self) -> str:
        schema = (
            "lf021_qualification_input_binding_v1"
            if self.schema_version == 1
            else "lf021_qualification_input_binding_v2"
        )
        return hash_canonical(
            {
                "schema": schema,
                **self.model_dump(mode="json"),
            }
        )


def _archive_qualification_inputs(
    *,
    repo_root: Path,
    artifact_root: Path,
    run_directory: Path,
    config_artifact: str,
    config: LocalQualificationConfig,
    runtime: RuntimeEnvironmentBinding,
    problem: ProblemPoolRecord,
    expected_declaration_name: str,
    context: ContextRecord,
    references: tuple[TheoremRecord, ...],
    registered_header: str,
    fixture_artifact: str | None,
    fixture_preflight: LocalQualificationFixturePreflight | None,
    checkpoint_verification: LocalCheckpointVerification | None,
    code_bundle: QualificationCodeBundleBinding | None,
    screening_index: CandidateScreeningIndex | None,
    screening_inputs: QualificationScreeningInputFiles | None,
) -> tuple[ArchivedQualificationInput, ...]:
    """Snapshot every consumed repository/input byte before model execution."""

    if problem.private_source_content:
        raise LocalQualificationConfigError(
            "private source content is forbidden in local qualification"
        )
    _safe_bundle_path(
        artifact_root,
        run_directory,
        label="qualification run_directory",
        must_exist=False,
    )
    repo_specs: dict[QualificationInputRole, str] = {
        "qualification_config": config_artifact,
        "prompt_template": config.prompt.template_artifact,
        "common_suffix": config.prompt.common_suffix_artifact,
        "parser_source": config.prompt.parser_source_artifact,
        "runtime_adapter": runtime.runtime_adapter_artifact,
        "environment_lock": runtime.environment_lock_artifact,
        "import_header": problem.import_header_artifact,
    }
    if fixture_artifact is not None:
        repo_specs["fixture_source"] = fixture_artifact
    if code_bundle is not None:
        repo_specs["code_bundle"] = code_bundle.source_artifact
    if (fixture_artifact is None) != (fixture_preflight is None):
        raise LocalQualificationConfigError(
            "fixture source and fixture preflight must be archived together"
        )

    inputs: list[tuple[QualificationInputRole, QualificationInputSourceKind, str, bytes]] = []
    for role, source_artifact in sorted(repo_specs.items()):
        source_path = _safe_regular_input(
            repo_root,
            source_artifact,
            label=f"qualification {role}",
        )
        try:
            payload = source_path.read_bytes()
        except OSError as exc:
            raise LocalQualificationConfigError(
                f"cannot read qualification {role}: {source_artifact}"
            ) from exc
        inputs.append((role, "repo_file", source_artifact, payload))

    if (screening_index is None) != (screening_inputs is None):
        raise LocalQualificationConfigError(
            "screening index and archived screening input files must be supplied together"
        )
    if screening_index is not None and screening_inputs is not None:
        if (
            screening_index.denylist.registry_content_hash
            != screening_index.denylist.index.registry_content_hash
        ):
            raise LocalQualificationConfigError(
                "candidate screening registry content hash is inconsistent"
            )
        screening_paths: dict[QualificationInputRole, Path] = {
            "benchmark_registry_manifest": screening_inputs.registry_manifest,
            "benchmark_active_registry": screening_inputs.active_registry,
            "benchmark_detailed_index": screening_inputs.detailed_index,
            "benchmark_input_manifest": screening_inputs.input_manifest,
        }
        for role, source_path_value in sorted(screening_paths.items()):
            source_path = Path(os.path.abspath(source_path_value))
            if source_path.is_symlink():
                raise LocalQualificationConfigError(f"qualification {role} must not be a symlink")
            try:
                mode = os.stat(source_path, follow_symlinks=False).st_mode
                payload = source_path.read_bytes()
            except OSError as exc:
                raise LocalQualificationConfigError(
                    f"cannot read qualification {role}: {source_path}"
                ) from exc
            if not stat.S_ISREG(mode):
                raise LocalQualificationConfigError(f"qualification {role} must be a regular file")
            try:
                relative_source = str(source_path.relative_to(repo_root.resolve()))
                source_kind: QualificationInputSourceKind = "repo_file"
                source_artifact = relative_source
            except ValueError:
                source_kind = "local_file"
                source_artifact = source_path.as_uri()
            inputs.append((role, source_kind, source_artifact, payload))
        prior_payload = (
            canonical_json_bytes(
                {
                    "schema": "lf021_prior_candidate_index_v1",
                    "active_registry_hash": screening_index.denylist.registry_content_hash,
                    "prior_candidates": [
                        item.model_dump(mode="json") for item in screening_index.prior_candidates
                    ],
                }
            )
            + b"\n"
        )
        inputs.append(
            (
                "prior_candidate_index",
                "canonical_record",
                "inline:lf021_prior_candidate_index_v1",
                prior_payload,
            )
        )

    by_role = {role: payload for role, _, _, payload in inputs}
    header_bytes = registered_header.encode("utf-8")
    if (
        by_role["import_header"] != header_bytes
        or sha256_hex(header_bytes) != problem.import_header_hash
        or context.header_text != registered_header
        or context.header_hash != problem.import_header_hash
    ):
        raise LocalQualificationConfigError(
            "registered header differs from its archived file/problem/context binding"
        )
    if fixture_preflight is not None:
        fixture_payload = by_role["fixture_source"]
        if (
            sha256_hex(fixture_payload) != fixture_preflight.fixture_sha256
            or fixture_preflight.problem_record_id != problem.problem_record_id
            or fixture_preflight.context_id != context.context_id
            or fixture_preflight.reference_theorem_id
            not in {reference.theorem_id for reference in references}
            or fixture_preflight.import_header_artifact != problem.import_header_artifact
            or fixture_preflight.import_header_sha256 != problem.import_header_hash
        ):
            raise LocalQualificationConfigError(
                "fixture preflight differs from its archived execution inputs"
            )
    execution_payload = (
        canonical_json_bytes(
            {
                "schema": "lf021_qualification_execution_input_v1",
                "problem": problem.model_dump(mode="json"),
                "expected_declaration_name": expected_declaration_name,
                "context": context.model_dump(mode="json"),
                "references": [reference.model_dump(mode="json") for reference in references],
                "registered_header": registered_header,
            }
        )
        + b"\n"
    )
    inputs.append(
        (
            "execution_input",
            "canonical_record",
            "inline:lf021_qualification_execution_input_v1",
            execution_payload,
        )
    )
    if fixture_preflight is not None:
        inputs.append(
            (
                "fixture_preflight",
                "canonical_record",
                "inline:lf021_local_qualification_fixture_preflight_v1",
                canonical_json_bytes(fixture_preflight.model_dump(mode="json")) + b"\n",
            )
        )
    if checkpoint_verification is not None:
        inputs.append(
            (
                "checkpoint_verification",
                "canonical_record",
                "inline:lf021_local_checkpoint_verification_v1",
                canonical_json_bytes(checkpoint_verification.model_dump(mode="json")) + b"\n",
            )
        )

    archived: list[ArchivedQualificationInput] = []
    for role, source_kind, source_artifact, payload in sorted(inputs):
        digest = sha256_hex(payload)
        destination = run_directory / "qualification_inputs" / "sha256" / digest
        observed = _persist_immutable_bytes(
            destination,
            payload,
            artifact_root=artifact_root,
        )
        if observed != digest:
            raise LocalQualificationReplayError(
                f"qualification input archive hash mismatch for {role}"
            )
        relative = str(
            _safe_bundle_path(
                artifact_root,
                destination,
                label=f"archived qualification {role}",
                must_exist=True,
            ).relative_to(Path(os.path.abspath(artifact_root)))
        )
        archived.append(
            ArchivedQualificationInput(
                role=role,
                source_kind=source_kind,
                source_artifact=source_artifact,
                archive_artifact=relative,
                sha256=digest,
                byte_count=len(payload),
            )
        )
    return tuple(archived)


@dataclass(frozen=True, slots=True)
class LocalQualificationRunResult:
    terminal: LocalQualificationTerminal
    lineage: ProviderLLMLineage
    provider_result: ProviderResult
    input_binding: QualificationInputBinding
    extracted: LeanExtractedCandidate | None = None
    materialized: RealOutputMaterializationResult | None = None
    screening: CandidateScreeningRecord | None = None
    admitted: RealOutputMaterializationResult | None = None
    admission_receipt: SmokeAdmissionDryRunReceipt | None = None


def _generation_config_hash(
    *,
    qualification_config_hash: str,
    runtime_hash: str,
    context: ContextRecord,
    expected_declaration_name: str,
) -> str:
    return hash_canonical(
        {
            "schema": "lf021_local_generation_config_v1",
            "qualification_config_hash": qualification_config_hash,
            "runtime_hash": runtime_hash,
            "context_id": context.context_id,
            "context_header_hash": context.header_hash,
            "expected_declaration_name": expected_declaration_name,
        }
    )


def _provider_result_for_runtime(
    *,
    request: ProviderRequest,
    local_result: LocalHFGenerationResult,
    raw_response_root: Path,
) -> ProviderResult:
    if (
        local_result.output_hash != sha256_hex(local_result.raw_text.encode("utf-8"))
        or local_result.compatibility.model != request.model
        or local_result.compatibility.revision != request.revision
        or local_result.decoding_hash != request.decoding_hash
        or local_result.compatibility.execution_mode != "local"
        or local_result.compatibility.transport != "in_process"
        or not local_result.compatibility.local_files_only
        or local_result.compatibility.private_content_transmitted
    ):
        raise LocalQualificationError("local runtime result differs from provider request")
    response = ProviderRawResponse.success(request, local_result.raw_text)
    return persist_provider_raw_response(raw_response_root, response)


def _failure_terminal(
    *,
    status: QualificationStatus,
    error_code: str,
    error_detail: str,
    config: LocalQualificationConfig,
    qualification_config_hash: str,
    runtime: RuntimeEnvironmentBinding,
    generation_config_hash: str,
    prompt: RenderedKiminaPrompt,
    request: ProviderRequest,
    lineage: ProviderLLMLineage,
    provider_result: ProviderResult,
    formatted_prompt_hash: str | None,
    created_at: datetime.datetime,
    extracted: LeanExtractedCandidate | None = None,
    materialized: RealOutputMaterializationResult | None = None,
    screening: CandidateScreeningRecord | None = None,
) -> LocalQualificationTerminal:
    payload: dict[str, object] = {
        "status": status.value,
        "problem_record_id": lineage.call.problem_record_id,
        "model_family": config.active_model.family_id,
        "model": config.active_model.repo_id,
        "model_revision": config.active_model.revision,
        "qualification_config_hash": qualification_config_hash,
        "runtime_hash": runtime.runtime_hash,
        "generation_config_hash": generation_config_hash,
        "provider_request_hash": request.request_hash,
        "llm_call_id": lineage.call.call_id,
        "llm_attempt_id": lineage.attempt.attempt_id,
        "raw_response_sha256": provider_result.raw_response_sha256,
        "parsed_statement_sha256": (
            extracted.parsed.statement_sha256 if extracted is not None else None
        ),
        "materialization_outcome_id": (
            materialized.outcome.outcome_id if materialized is not None else None
        ),
        "screening_id": screening.screening_id if screening is not None else None,
        "error_code": error_code,
    }
    return LocalQualificationTerminal(
        terminal_id=_terminal_id(payload),
        status=status,
        problem_record_id=str(lineage.call.problem_record_id),
        model_family=config.active_model.family_id,
        model=config.active_model.repo_id,
        model_revision=config.active_model.revision,
        qualification_config_hash=qualification_config_hash,
        runtime_hash=runtime.runtime_hash,
        generation_config_hash=generation_config_hash,
        prompt_template_hash=prompt.template_bundle_hash,
        prompt_render_hash=prompt.render_hash,
        formatted_prompt_hash=formatted_prompt_hash,
        parser_id=config.prompt.parser_id,
        parser_source_sha256=config.prompt.parser_source_sha256,
        provider_request_hash=request.request_hash,
        llm_call_id=lineage.call.call_id,
        llm_attempt_id=lineage.attempt.attempt_id,
        provider_request_artifact=str(lineage.call.request_artifact),
        provider_request_artifact_sha256=str(lineage.call.request_artifact_sha256),
        raw_response_artifact=str(lineage.call.raw_output_artifact),
        raw_response_sha256=provider_result.raw_response_sha256,
        parsed_statement_sha256=(
            extracted.parsed.statement_sha256 if extracted is not None else None
        ),
        materialization_outcome_id=(
            materialized.outcome.outcome_id if materialized is not None else None
        ),
        candidate_theorem_id=(
            materialized.outcome.candidate_theorem_id if materialized is not None else None
        ),
        representation_id=(
            materialized.outcome.representation_id if materialized is not None else None
        ),
        screening_id=screening.screening_id if screening is not None else None,
        error_code=error_code,
        error_detail=error_detail,
        created_at=created_at,
    )


def run_local_kimina_qualification(
    *,
    loaded_config: LoadedConfig[LocalQualificationConfig],
    runtime_binding: RuntimeEnvironmentBinding,
    runtime: LocalRuntime,
    problem: ProblemPoolRecord,
    expected_declaration_name: str,
    context: ContextRecord,
    references: tuple[TheoremRecord, ...],
    registered_header: str,
    backend: LeanInteractBackend,
    screening_index: CandidateScreeningIndex | None,
    artifact_root: Path,
    run_directory: Path,
    created_at: datetime.datetime,
    fixture_artifact: str | None = None,
    fixture_preflight: LocalQualificationFixturePreflight | None = None,
    checkpoint_verification: LocalCheckpointVerification | None = None,
    code_bundle: QualificationCodeBundleBinding | None = None,
    screening_inputs: QualificationScreeningInputFiles | None = None,
) -> LocalQualificationRunResult:
    """Execute one local smoke attempt with complete terminal accounting.

    The historical name remains the backward-compatible public entry point;
    the implementation is model-generic for the explicitly authorized local
    qualification families.
    """

    config = loaded_config.config
    config_path = Path(os.path.abspath(loaded_config.path))
    if config_path.is_symlink():
        raise LocalQualificationConfigError("qualification config must not be a symlink")
    repo_root = config_path.parents[2]
    verify_runtime_binding(runtime_binding, repo_root=repo_root)
    try:
        config_artifact = str(config_path.relative_to(repo_root))
    except ValueError as exc:
        raise LocalQualificationConfigError(
            "qualification config must reside under the repository root"
        ) from exc
    if problem.private_source_content:
        raise LocalQualificationConfigError(
            "private source content is forbidden in local qualification"
        )
    checkpoint_manifest = config.active_model.checkpoint_artifacts
    if checkpoint_manifest is not None:
        if checkpoint_verification is None or not checkpoint_verification.matches_model(
            config.active_model
        ):
            raise LocalQualificationConfigError(
                "checkpoint verification is absent or differs from the active model"
            )
    elif checkpoint_verification is not None:
        raise LocalQualificationConfigError(
            "legacy model without a checkpoint manifest cannot attach checkpoint verification"
        )
    if code_bundle is None:
        raise LocalQualificationConfigError(
            "local qualification requires an immutable code-bundle binding"
        )
    bundle_path = _safe_regular_input(
        repo_root,
        code_bundle.source_artifact,
        label="qualification code bundle",
    )
    try:
        observed_bundle_hash = validate_code_bundle(
            bundle_path,
            code_bundle.code_tree_hash,
        )
    except Exception as exc:
        raise LocalQualificationConfigError(
            f"qualification code bundle validation failed: {exc}"
        ) from exc
    if observed_bundle_hash != code_bundle.sha256:
        raise LocalQualificationConfigError(
            "qualification code bundle hash differs from its binding"
        )
    archived_inputs = _archive_qualification_inputs(
        repo_root=repo_root,
        artifact_root=artifact_root,
        run_directory=run_directory,
        config_artifact=config_artifact,
        config=config,
        runtime=runtime_binding,
        problem=problem,
        expected_declaration_name=expected_declaration_name,
        context=context,
        references=references,
        registered_header=registered_header,
        fixture_artifact=fixture_artifact,
        fixture_preflight=fixture_preflight,
        checkpoint_verification=checkpoint_verification,
        code_bundle=code_bundle,
        screening_index=screening_index,
        screening_inputs=screening_inputs,
    )
    input_binding = QualificationInputBinding(
        qualification_config_artifact=config_artifact,
        qualification_config_file_sha256=hash_file(config_path),
        qualification_config_hash=loaded_config.config_hash,
        runtime=runtime_binding,
        prompt_template_artifact=config.prompt.template_artifact,
        prompt_template_sha256=config.prompt.template_sha256,
        common_suffix_artifact=config.prompt.common_suffix_artifact,
        common_suffix_sha256=config.prompt.common_suffix_sha256,
        parser_id=config.prompt.parser_id,
        parser_source_artifact=config.prompt.parser_source_artifact,
        parser_source_sha256=config.prompt.parser_source_sha256,
        model_repo_id=config.active_model.repo_id,
        model_revision=config.active_model.revision,
        tokenizer_revision=config.active_model.tokenizer_revision,
        model_metadata_hashes=config.active_model.metadata_hashes,
        prompt_formatter_hash=config.prompt.formatter_hash,
        checkpoint_verification=checkpoint_verification,
        code_bundle=code_bundle,
        screening_registry_hash=(
            screening_index.denylist.registry_content_hash
            if screening_inputs is not None and screening_index is not None
            else None
        ),
        archived_inputs=archived_inputs,
    )
    prompt = render_local_qualification_prompt(
        config=config,
        repo_root=repo_root,
        problem=problem,
        expected_declaration_name=expected_declaration_name,
        registered_header=registered_header,
    )
    identity = ProviderIdentity(
        provider="local_hf",
        model=config.active_model.repo_id,
        revision=config.active_model.revision,
        transport="local",
    )
    request = create_provider_request_for_problem(
        identity=identity,
        problem=problem,
        prompt_template_hash=prompt.template_bundle_hash,
        rendered_prompt=prompt.user_prompt,
        decoding=config.decoding.model_dump(mode="python"),
    )
    request_path = run_directory / "provider_request.json"
    persist_provider_request(request, request_path)
    local_request = LocalHFGenerationRequest(
        pin=LocalHFModelPin(
            repo_id=config.active_model.repo_id,
            revision=config.active_model.revision,
            dtype=runtime_binding.dtype,
        ),
        prompt=prompt.user_prompt,
        prompt_formatter_id=config.prompt.formatter_id,
        prompt_formatter_hash=config.prompt.formatter_hash,
        decoding=config.decoding,
        input_ids=(problem.problem_record_id,),
        private_source_content=False,
        execution_purpose="qualification_fixture",
    )
    generation_hash = _generation_config_hash(
        qualification_config_hash=loaded_config.config_hash,
        runtime_hash=runtime_binding.runtime_hash,
        context=context,
        expected_declaration_name=expected_declaration_name,
    )
    raw_root = run_directory / "raw"
    runtime_result: LocalHFGenerationResult | None = None
    try:
        runtime_result = runtime.generate(local_request)
        if runtime_result.request_hash != local_request.request_hash:
            raise LocalQualificationError("runtime result request_hash mismatch")
        provider_result = _provider_result_for_runtime(
            request=request,
            local_result=runtime_result,
            raw_response_root=raw_root,
        )
    except Exception as exc:
        response = ProviderRawResponse.error(
            request,
            error_type=type(exc).__name__,
            error_detail=str(exc),
        )
        provider_result = persist_provider_raw_response(raw_root, response)
        completed_at = created_at + datetime.timedelta(milliseconds=1)
        lineage = bridge_provider_result_to_llm_lineage(
            request=request,
            result=provider_result,
            request_artifact_path=request_path,
            artifact_root=artifact_root,
            problem=problem,
            provider_slot=config.active_model.provider_slot,
            model_family=config.active_model.family_id,
            prompt_template_id=config.prompt.formatter_id,
            prompt_template_version="v1",
            execution_mode="local",
            parse_status=ParseStatus.EMPTY,
            parsed_statement=None,
            started_at=created_at,
            completed_at=completed_at,
            supervision_eligible=False,
            heldout_generator=False,
            metadata={
                "artifact_class": "smoke",
                "qualifies_for_gate5g": False,
                "local_runtime_request_hash": local_request.request_hash,
                "runtime_hash": runtime_binding.runtime_hash,
                "generation_config_hash": generation_hash,
                "parser_id": config.prompt.parser_id,
                "parser_source_sha256": config.prompt.parser_source_sha256,
            },
        )
        terminal = _failure_terminal(
            status=QualificationStatus.RUNTIME_ERROR,
            error_code=type(exc).__name__,
            error_detail=str(exc),
            config=config,
            qualification_config_hash=loaded_config.config_hash,
            runtime=runtime_binding,
            generation_config_hash=generation_hash,
            prompt=prompt,
            request=request,
            lineage=lineage,
            provider_result=provider_result,
            formatted_prompt_hash=None,
            created_at=created_at,
        )
        return LocalQualificationRunResult(
            terminal,
            lineage,
            provider_result,
            input_binding,
        )

    assert runtime_result is not None
    completed_at = created_at + datetime.timedelta(
        milliseconds=max(1, runtime_result.total_latency_ms)
    )
    extracted: LeanExtractedCandidate | None = None
    parse_error: FinalFenceError | None = None
    try:
        parser = {
            FINAL_FENCE_PARSER_ID: extract_candidate_signature_with_lean,
            RAW_OR_FINAL_PARSER_ID: extract_candidate_signature_with_lean_v2,
            TERMINAL_FENCE_OR_RAW_PARSER_ID: extract_candidate_signature_with_lean_v3,
            STEPFUN_TERMINAL_PARSER_ID: extract_stepfun_candidate_signature_with_lean,
        }[config.prompt.parser_id]
        extracted = parser(
            raw_output=runtime_result.raw_text,
            expected_declaration_name=expected_declaration_name,
            registered_header=registered_header,
            problem=problem,
            context=context,
            backend=backend,
            created_at=created_at,
        )
    except FinalFenceError as exc:
        parse_error = exc

    lineage = bridge_provider_result_to_llm_lineage(
        request=request,
        result=provider_result,
        request_artifact_path=request_path,
        artifact_root=artifact_root,
        problem=problem,
        provider_slot=config.active_model.provider_slot,
        model_family=config.active_model.family_id,
        prompt_template_id=config.prompt.formatter_id,
        prompt_template_version="v1",
        execution_mode="local",
        parse_status=(ParseStatus.PARSED if extracted is not None else ParseStatus.PARSE_FAILED),
        parsed_statement=(extracted.parsed.statement if extracted is not None else None),
        started_at=created_at,
        completed_at=completed_at,
        supervision_eligible=False,
        heldout_generator=False,
        metadata={
            "artifact_class": "smoke",
            "qualifies_for_gate5g": False,
            "formatted_prompt_hash": runtime_result.formatted_prompt_hash,
            "local_runtime_request_hash": local_request.request_hash,
            "runtime_hash": runtime_binding.runtime_hash,
            "generation_config_hash": generation_hash,
            "parser_id": config.prompt.parser_id,
            "parser_source_sha256": config.prompt.parser_source_sha256,
            "prompt_formatter_id": config.prompt.formatter_id,
            "prompt_tokens": runtime_result.prompt_tokens,
            "output_tokens": runtime_result.output_tokens,
        },
    )
    if parse_error is not None:
        terminal = _failure_terminal(
            status=QualificationStatus.PARSE_FAILED,
            error_code=parse_error.code.value,
            error_detail=str(parse_error),
            config=config,
            qualification_config_hash=loaded_config.config_hash,
            runtime=runtime_binding,
            generation_config_hash=generation_hash,
            prompt=prompt,
            request=request,
            lineage=lineage,
            provider_result=provider_result,
            formatted_prompt_hash=runtime_result.formatted_prompt_hash,
            created_at=created_at,
        )
        return LocalQualificationRunResult(
            terminal,
            lineage,
            provider_result,
            input_binding,
        )

    assert extracted is not None
    verified_response = verify_llm_call_artifacts(
        call=lineage.call,
        problem=problem,
        artifact_root=artifact_root,
    )
    if verified_response.output_text != runtime_result.raw_text:
        raise ReplayArtifactError("verified raw response differs from local runtime output")
    materialized = materialize_real_output_candidate(
        problem=problem,
        parsed=extracted.parsed,
        call=lineage.call,
        raw_output_artifact=str(lineage.call.raw_output_artifact),
        context=context,
        references=references,
        imports=registered_header,
        backend=backend,
        generation_config_hash=generation_hash,
        created_at=created_at,
    )
    if materialized.outcome.outcome is not RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING:
        terminal = _failure_terminal(
            status=QualificationStatus.MATERIALIZATION_FAILED,
            error_code=materialized.outcome.failure_code.value
            if materialized.outcome.failure_code is not None
            else materialized.outcome.outcome.value,
            error_detail=materialized.outcome.failure_detail or materialized.outcome.outcome.value,
            config=config,
            qualification_config_hash=loaded_config.config_hash,
            runtime=runtime_binding,
            generation_config_hash=generation_hash,
            prompt=prompt,
            request=request,
            lineage=lineage,
            provider_result=provider_result,
            formatted_prompt_hash=runtime_result.formatted_prompt_hash,
            created_at=created_at,
            extracted=extracted,
            materialized=materialized,
        )
        return LocalQualificationRunResult(
            terminal,
            lineage,
            provider_result,
            input_binding,
            extracted,
            materialized,
        )
    assert materialized.theorem is not None
    assert materialized.representation is not None
    if screening_index is None:
        terminal = _failure_terminal(
            status=QualificationStatus.SCREENING_UNAVAILABLE,
            error_code="active_candidate_screening_unavailable",
            error_detail=(
                "qualification cannot admit a candidate without the preflighted "
                "active benchmark and duplicate index"
            ),
            config=config,
            qualification_config_hash=loaded_config.config_hash,
            runtime=runtime_binding,
            generation_config_hash=generation_hash,
            prompt=prompt,
            request=request,
            lineage=lineage,
            provider_result=provider_result,
            formatted_prompt_hash=runtime_result.formatted_prompt_hash,
            created_at=created_at,
            extracted=extracted,
            materialized=materialized,
        )
        return LocalQualificationRunResult(
            terminal,
            lineage,
            provider_result,
            input_binding,
            extracted,
            materialized,
        )
    screening_at = created_at + datetime.timedelta(seconds=1)
    screening = screen_materialized_candidate(
        index=screening_index,
        problem_record_id=problem.problem_record_id,
        call_id=lineage.call.call_id,
        theorem=materialized.theorem,
        representation=materialized.representation,
        created_at=screening_at,
    )
    if screening.status is not CandidateScreeningStatus.CLEAN:
        terminal = _failure_terminal(
            status=QualificationStatus.SCREENING_REJECTED,
            error_code="candidate_screening_rejected",
            error_detail="candidate overlaps a frozen benchmark or duplicate registry entry",
            config=config,
            qualification_config_hash=loaded_config.config_hash,
            runtime=runtime_binding,
            generation_config_hash=generation_hash,
            prompt=prompt,
            request=request,
            lineage=lineage,
            provider_result=provider_result,
            formatted_prompt_hash=runtime_result.formatted_prompt_hash,
            created_at=created_at,
            extracted=extracted,
            materialized=materialized,
            screening=screening,
        )
        return LocalQualificationRunResult(
            terminal,
            lineage,
            provider_result,
            input_binding,
            extracted,
            materialized,
            screening,
        )

    admission_at = created_at + datetime.timedelta(seconds=2)
    admitted = admit_screened_real_output_candidate(
        materialized=materialized,
        screening=screening,
        problem=problem,
        references=references,
        expected_frozen_registry_hash=screening_index.denylist.registry_content_hash,
        created_at=admission_at,
    )
    assert admitted.nl_lean is not None
    receipt = SmokeAdmissionDryRunReceipt.create(
        pending=materialized.outcome,
        admitted=admitted.outcome,
        screening=screening,
        active_registry_hash=screening_index.denylist.registry_content_hash,
        created_at=admission_at,
    )
    payload: dict[str, object] = {
        "status": QualificationStatus.QUALIFIED_SMOKE.value,
        "problem_record_id": problem.problem_record_id,
        "model_family": config.active_model.family_id,
        "model": config.active_model.repo_id,
        "model_revision": config.active_model.revision,
        "qualification_config_hash": loaded_config.config_hash,
        "runtime_hash": runtime_binding.runtime_hash,
        "generation_config_hash": generation_hash,
        "provider_request_hash": request.request_hash,
        "llm_call_id": lineage.call.call_id,
        "llm_attempt_id": lineage.attempt.attempt_id,
        "raw_response_sha256": provider_result.raw_response_sha256,
        "parsed_statement_sha256": extracted.parsed.statement_sha256,
        "materialization_outcome_id": materialized.outcome.outcome_id,
        "screening_id": screening.screening_id,
        "admission_receipt_id": receipt.receipt_id,
        "error_code": None,
    }
    terminal = LocalQualificationTerminal(
        terminal_id=_terminal_id(payload),
        status=QualificationStatus.QUALIFIED_SMOKE,
        problem_record_id=problem.problem_record_id,
        model_family=config.active_model.family_id,
        model=config.active_model.repo_id,
        model_revision=config.active_model.revision,
        qualification_config_hash=loaded_config.config_hash,
        runtime_hash=runtime_binding.runtime_hash,
        generation_config_hash=generation_hash,
        prompt_template_hash=prompt.template_bundle_hash,
        prompt_render_hash=prompt.render_hash,
        formatted_prompt_hash=runtime_result.formatted_prompt_hash,
        parser_id=config.prompt.parser_id,
        parser_source_sha256=config.prompt.parser_source_sha256,
        provider_request_hash=request.request_hash,
        llm_call_id=lineage.call.call_id,
        llm_attempt_id=lineage.attempt.attempt_id,
        provider_request_artifact=str(lineage.call.request_artifact),
        provider_request_artifact_sha256=str(lineage.call.request_artifact_sha256),
        raw_response_artifact=str(lineage.call.raw_output_artifact),
        raw_response_sha256=provider_result.raw_response_sha256,
        parsed_statement_sha256=extracted.parsed.statement_sha256,
        materialization_outcome_id=materialized.outcome.outcome_id,
        candidate_theorem_id=materialized.outcome.candidate_theorem_id,
        representation_id=materialized.outcome.representation_id,
        screening_id=screening.screening_id,
        admission_receipt_id=receipt.receipt_id,
        created_at=created_at,
        screening_at=screening_at,
        admission_at=admission_at,
    )
    return LocalQualificationRunResult(
        terminal,
        lineage,
        provider_result,
        input_binding,
        extracted,
        materialized,
        screening,
        admitted,
        receipt,
    )


def _persist_record(
    path: Path,
    model: StrictModel,
    *,
    artifact_root: Path,
) -> str:
    payload = canonical_json_bytes(model.model_dump(mode="json")) + b"\n"
    return _persist_immutable_bytes(path, payload, artifact_root=artifact_root)


class LocalQualificationBundleManifest(StrictModel):
    schema_version: Literal[1, 2] = 2
    bundle_id: str = Field(pattern=_BUNDLE_ID)
    terminal_id: str = Field(pattern=_TERMINAL_ID)
    artifact_class: Literal["smoke"] = "smoke"
    qualifies_for_gate5g: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    supervision_eligible: Literal[False] = False
    training_eligible: Literal[False] = False
    release_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    scientific_evaluation_eligible: Literal[False] = False
    artifacts: dict[str, str]
    artifact_sha256: dict[str, str]

    @model_validator(mode="after")
    def _manifest(self) -> Self:
        if set(self.artifacts) != set(self.artifact_sha256):
            raise ValueError("bundle artifact path/hash key sets differ")
        payload: dict[str, object] = {
            "schema": (
                "lf021_local_qualification_bundle_v1"
                if self.schema_version == 1
                else "lf021_local_qualification_bundle_v2"
            ),
            "terminal_id": self.terminal_id,
            "artifacts": self.artifacts,
            "artifact_sha256": self.artifact_sha256,
        }
        if self.schema_version == 2:
            payload.update(
                {
                    "artifact_class": self.artifact_class,
                    "qualifies_for_gate5g": self.qualifies_for_gate5g,
                    "semantic_labels_created": self.semantic_labels_created,
                    "supervision_eligible": self.supervision_eligible,
                    "training_eligible": self.training_eligible,
                    "release_eligible": self.release_eligible,
                    "calibration_eligible": self.calibration_eligible,
                    "model_selection_eligible": self.model_selection_eligible,
                    "scientific_evaluation_eligible": self.scientific_evaluation_eligible,
                }
            )
        expected = "local_qualification_bundle:" + hash_canonical(payload)
        if self.bundle_id != expected:
            raise ValueError("bundle_id does not match artifact manifest")
        return self


def persist_local_qualification_bundle(
    result: LocalQualificationRunResult,
    *,
    run_directory: Path,
    artifact_root: Path,
) -> LocalQualificationBundleManifest:
    """Persist lineage and terminal records as one immutable replay bundle."""

    records: dict[str, StrictModel] = {
        "attempt": result.lineage.attempt,
        "call": result.lineage.call,
        "qualification_inputs": result.input_binding,
        "terminal": result.terminal,
    }
    if result.materialized is not None:
        # Persist the pre-screen materialization only. The admitted semantic-pool
        # outcome is exercised in memory and summarized by a hard-false receipt.
        records["materialization_outcome"] = result.materialized.outcome
        records["variant"] = result.materialized.variant.model_copy(
            update={
                "metadata": {
                    **result.materialized.variant.metadata,
                    "artifact_class": "smoke",
                    "training_eligible": False,
                    "gate5g_eligible": False,
                }
            }
        )
        if result.materialized.theorem is not None:
            records["theorem"] = result.materialized.theorem.model_copy(
                update={
                    "metadata": {
                        **result.materialized.theorem.metadata,
                        "artifact_class": "smoke",
                        "transform_source_eligible": False,
                        "training_eligible": False,
                        "gate5g_eligible": False,
                    }
                }
            )
        if result.materialized.representation is not None:
            records["representation"] = result.materialized.representation
    if result.screening is not None:
        records["screening"] = result.screening
    if result.admission_receipt is not None:
        records["admission_receipt"] = result.admission_receipt
    # Admission is exercised only as an in-memory dry run.  Smoke PairRecord
    # and NLPLeanRecord values are intentionally not persisted as reusable
    # semantic-pool artifacts; their deterministic IDs live only in the
    # explicitly non-admitting receipt.

    artifacts: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for archived in result.input_binding.archived_inputs:
        name = f"input_{archived.role}"
        path = _safe_bundle_path(
            artifact_root,
            artifact_root / archived.archive_artifact,
            label=f"archived qualification input {archived.role}",
            must_exist=True,
        )
        if hash_file(path) != archived.sha256 or path.stat().st_size != archived.byte_count:
            raise LocalQualificationReplayError(
                f"archived qualification input drift: {archived.role}"
            )
        artifacts[name] = archived.archive_artifact
        hashes[name] = archived.sha256
    for name, record in sorted(records.items()):
        path = run_directory / f"{name}.json"
        safe_path = _safe_bundle_path(
            artifact_root,
            path,
            label=f"qualification record {name}",
            must_exist=False,
        )
        relative = str(safe_path.relative_to(Path(os.path.abspath(artifact_root))))
        artifacts[name] = relative
        hashes[name] = _persist_record(path, record, artifact_root=artifact_root)
    payload = {
        "schema": "lf021_local_qualification_bundle_v2",
        "terminal_id": result.terminal.terminal_id,
        "artifact_class": "smoke",
        "qualifies_for_gate5g": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "training_eligible": False,
        "release_eligible": False,
        "calibration_eligible": False,
        "model_selection_eligible": False,
        "scientific_evaluation_eligible": False,
        "artifacts": artifacts,
        "artifact_sha256": hashes,
    }
    manifest = LocalQualificationBundleManifest(
        bundle_id="local_qualification_bundle:" + hash_canonical(payload),
        terminal_id=result.terminal.terminal_id,
        artifacts=artifacts,
        artifact_sha256=hashes,
    )
    _persist_record(
        run_directory / "bundle_manifest.json",
        manifest,
        artifact_root=artifact_root,
    )
    return manifest


def _load_record(path: Path, model_type: type[StrictModel]) -> StrictModel:
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
        record = model_type.model_validate(document)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise LocalQualificationReplayError(f"invalid replay record {path}: {exc}") from exc
    expected = canonical_json_bytes(record.model_dump(mode="json")) + b"\n"
    if raw != expected:
        raise LocalQualificationReplayError(f"noncanonical replay record: {path}")
    return record


def verify_local_qualification_bundle(
    manifest: LocalQualificationBundleManifest,
    *,
    artifact_root: Path,
    repo_root: Path,
    problem: ProblemPoolRecord,
) -> tuple[LocalQualificationTerminal, LLMCallRecord, LLMAttemptRecord]:
    """Verify every bundle byte and the provider artifacts behind its call."""

    if manifest.schema_version != 2:
        raise LocalQualificationReplayError(
            "legacy qualification bundle manifest is diagnostic-only, not canonical"
        )
    forbidden_record_keys = {"pair", "nl_lean", "nllean", "label", "resolved_label"}
    for key in manifest.artifacts:
        normalized = key.lower()
        if (
            normalized in forbidden_record_keys
            or normalized.startswith("pair_")
            or normalized.startswith("label_")
            or normalized.startswith("resolved_label_")
            or normalized.startswith("nl_lean_")
            or normalized.startswith("nllean_")
        ):
            raise LocalQualificationReplayError(
                f"smoke qualification bundle contains forbidden semantic-pool artifact: {key}"
            )
    for name, artifact in manifest.artifacts.items():
        path = _safe_bundle_path(
            artifact_root,
            artifact_root / artifact,
            label=f"bundle artifact {name}",
            must_exist=True,
        )
        if hash_file(path) != manifest.artifact_sha256[name]:
            raise LocalQualificationReplayError(f"bundle artifact drift: {name}")
    terminal = _load_record(
        _safe_bundle_path(
            artifact_root,
            artifact_root / manifest.artifacts["terminal"],
            label="terminal replay record",
            must_exist=True,
        ),
        LocalQualificationTerminal,
    )
    call = _load_record(
        _safe_bundle_path(
            artifact_root,
            artifact_root / manifest.artifacts["call"],
            label="call replay record",
            must_exist=True,
        ),
        LLMCallRecord,
    )
    attempt = _load_record(
        _safe_bundle_path(
            artifact_root,
            artifact_root / manifest.artifacts["attempt"],
            label="attempt replay record",
            must_exist=True,
        ),
        LLMAttemptRecord,
    )
    inputs = _load_record(
        _safe_bundle_path(
            artifact_root,
            artifact_root / manifest.artifacts["qualification_inputs"],
            label="qualification input replay record",
            must_exist=True,
        ),
        QualificationInputBinding,
    )
    assert isinstance(terminal, LocalQualificationTerminal)
    assert isinstance(call, LLMCallRecord)
    assert isinstance(attempt, LLMAttemptRecord)
    assert isinstance(inputs, QualificationInputBinding)
    if (
        inputs.schema_version != 2
        or inputs.checkpoint_verification is None
        or inputs.code_bundle is None
        or inputs.screening_registry_hash is None
    ):
        raise LocalQualificationReplayError(
            "canonical qualification bundle lacks checkpoint/code/screening input bindings"
        )
    if (
        terminal.terminal_id != manifest.terminal_id
        or terminal.llm_call_id != call.call_id
        or terminal.llm_attempt_id != attempt.attempt_id
        or attempt.call_id != call.call_id
    ):
        raise LocalQualificationReplayError("bundle record IDs are inconsistent")
    outcome: RealOutputCandidateOutcome | None = None
    if "materialization_outcome" in manifest.artifacts:
        loaded_outcome = _load_record(
            _safe_bundle_path(
                artifact_root,
                artifact_root / manifest.artifacts["materialization_outcome"],
                label="materialization outcome replay record",
                must_exist=True,
            ),
            RealOutputCandidateOutcome,
        )
        assert isinstance(loaded_outcome, RealOutputCandidateOutcome)
        outcome = loaded_outcome
    variant: VariantRecord | None = None
    if "variant" in manifest.artifacts:
        loaded_variant = _load_record(
            _safe_bundle_path(
                artifact_root,
                artifact_root / manifest.artifacts["variant"],
                label="variant replay record",
                must_exist=True,
            ),
            VariantRecord,
        )
        assert isinstance(loaded_variant, VariantRecord)
        variant = loaded_variant
    theorem: TheoremRecord | None = None
    if "theorem" in manifest.artifacts:
        loaded_theorem = _load_record(
            _safe_bundle_path(
                artifact_root,
                artifact_root / manifest.artifacts["theorem"],
                label="theorem replay record",
                must_exist=True,
            ),
            TheoremRecord,
        )
        assert isinstance(loaded_theorem, TheoremRecord)
        theorem = loaded_theorem
    representation: RepresentationRecord | None = None
    if "representation" in manifest.artifacts:
        loaded_representation = _load_record(
            _safe_bundle_path(
                artifact_root,
                artifact_root / manifest.artifacts["representation"],
                label="representation replay record",
                must_exist=True,
            ),
            RepresentationRecord,
        )
        assert isinstance(loaded_representation, RepresentationRecord)
        representation = loaded_representation
    screening: CandidateScreeningRecord | None = None
    if "screening" in manifest.artifacts:
        loaded_screening = _load_record(
            _safe_bundle_path(
                artifact_root,
                artifact_root / manifest.artifacts["screening"],
                label="candidate screening replay record",
                must_exist=True,
            ),
            CandidateScreeningRecord,
        )
        assert isinstance(loaded_screening, CandidateScreeningRecord)
        screening = loaded_screening
    receipt: SmokeAdmissionDryRunReceipt | None = None
    if "admission_receipt" in manifest.artifacts:
        loaded_receipt = _load_record(
            _safe_bundle_path(
                artifact_root,
                artifact_root / manifest.artifacts["admission_receipt"],
                label="smoke admission receipt replay record",
                must_exist=True,
            ),
            SmokeAdmissionDryRunReceipt,
        )
        assert isinstance(loaded_receipt, SmokeAdmissionDryRunReceipt)
        receipt = loaded_receipt
    if terminal.materialization_outcome_id is None:
        if any(item is not None for item in (outcome, variant, theorem, representation)):
            raise LocalQualificationReplayError(
                "bundle persists materialization records absent from the terminal"
            )
    elif (
        outcome is None
        or outcome.outcome_id != terminal.materialization_outcome_id
        or outcome.candidate_theorem_id != terminal.candidate_theorem_id
        or outcome.representation_id != terminal.representation_id
        or outcome.created_at != terminal.created_at
    ):
        raise LocalQualificationReplayError(
            "terminal materialization identity differs from the persisted outcome"
        )
    if outcome is not None:
        if variant is None or variant.variant_id != outcome.variant_id:
            raise LocalQualificationReplayError(
                "persisted variant differs from the materialization outcome"
            )
        if outcome.candidate_theorem_id is None:
            if theorem is not None or representation is not None:
                raise LocalQualificationReplayError(
                    "failed materialization persists unexpected theorem views"
                )
        elif (
            theorem is None
            or theorem.theorem_id != outcome.candidate_theorem_id
            or representation is None
            or representation.representation_id != outcome.representation_id
            or representation.theorem_id != theorem.theorem_id
            or variant.derived_theorem_id != theorem.theorem_id
            or variant.derived_representation_id != representation.representation_id
        ):
            raise LocalQualificationReplayError(
                "persisted theorem/variant/representation lineage is inconsistent"
            )
    if terminal.screening_id is None:
        if screening is not None:
            raise LocalQualificationReplayError(
                "bundle persists screening absent from the terminal"
            )
    elif (
        screening is None
        or screening.screening_id != terminal.screening_id
        or outcome is None
        or screening.candidate_theorem_id != outcome.candidate_theorem_id
        or screening.representation_id != outcome.representation_id
        or screening.created_at != terminal.screening_at
    ):
        raise LocalQualificationReplayError(
            "terminal screening identity differs from the persisted screening"
        )
    if terminal.admission_receipt_id is None:
        if receipt is not None:
            raise LocalQualificationReplayError(
                "bundle persists an admission receipt absent from the terminal"
            )
        if terminal.status is QualificationStatus.QUALIFIED_SMOKE:
            raise LocalQualificationReplayError(
                "legacy qualified smoke bundle lacks the required non-admitting receipt"
            )
    elif (
        receipt is None
        or receipt.receipt_id != terminal.admission_receipt_id
        or outcome is None
        or screening is None
        or receipt.pending_outcome_id != outcome.outcome_id
        or receipt.problem_record_id != outcome.problem_record_id
        or receipt.call_id != outcome.call_id
        or receipt.candidate_theorem_id != outcome.candidate_theorem_id
        or receipt.representation_id != outcome.representation_id
        or receipt.screening_id != screening.screening_id
        or receipt.active_registry_hash != screening.frozen_registry_hash
        or receipt.created_at != terminal.admission_at
        or outcome.outcome is not RealOutputOutcomeCode.MATERIALIZED_PENDING_SCREENING
        or outcome.semantic_pool_eligible
        or outcome.pair_ids
        or outcome.nl_lean_id is not None
        or outcome.screening_id is not None
        or terminal.admitted_pair_ids
        or terminal.admitted_nl_lean_id is not None
    ):
        raise LocalQualificationReplayError(
            "smoke admission receipt differs from pending materialization lineage"
        )
    if not inputs.archived_inputs:
        raise LocalQualificationReplayError(
            "canonical qualification bundle lacks archived execution inputs"
        )
    if inputs.archived_inputs:
        by_role = {item.role: item for item in inputs.archived_inputs}
        expected_keys = {f"input_{role}" for role in by_role}
        if not expected_keys.issubset(manifest.artifacts):
            raise LocalQualificationReplayError(
                "bundle manifest omits archived qualification inputs"
            )
        archived_paths: dict[str, Path] = {}
        for role, item in by_role.items():
            key = f"input_{role}"
            if (
                manifest.artifacts[key] != item.archive_artifact
                or manifest.artifact_sha256[key] != item.sha256
            ):
                raise LocalQualificationReplayError(
                    f"bundle manifest differs from archived qualification input: {role}"
                )
            archived_path = _safe_bundle_path(
                artifact_root,
                artifact_root / item.archive_artifact,
                label=f"archived qualification input {role}",
                must_exist=True,
            )
            if (
                hash_file(archived_path) != item.sha256
                or archived_path.stat().st_size != item.byte_count
            ):
                raise LocalQualificationReplayError(f"archived qualification input drift: {role}")
            archived_paths[role] = archived_path
        config_path = archived_paths["qualification_config"]
        try:
            loaded = load_config(config_path, LocalQualificationConfig)
        except ValueError as exc:
            raise LocalQualificationReplayError(
                f"invalid archived qualification config: {exc}"
            ) from exc
        prompt = loaded.config.prompt
        expected_archive_bindings: dict[QualificationInputRole, tuple[str, str]] = {
            "qualification_config": (
                inputs.qualification_config_artifact,
                inputs.qualification_config_file_sha256,
            ),
            "prompt_template": (
                prompt.template_artifact,
                prompt.template_sha256,
            ),
            "common_suffix": (
                prompt.common_suffix_artifact,
                prompt.common_suffix_sha256,
            ),
            "parser_source": (
                prompt.parser_source_artifact,
                prompt.parser_source_sha256,
            ),
            "runtime_adapter": (
                inputs.runtime.runtime_adapter_artifact,
                inputs.runtime.runtime_adapter_sha256,
            ),
            "environment_lock": (
                inputs.runtime.environment_lock_artifact,
                inputs.runtime.environment_lock_sha256,
            ),
            "import_header": (
                problem.import_header_artifact,
                problem.import_header_hash,
            ),
        }
        for role, (source_artifact, digest) in expected_archive_bindings.items():
            item = by_role[role]
            if item.source_artifact != source_artifact or item.sha256 != digest:
                raise LocalQualificationReplayError(
                    f"archived qualification binding differs for {role}"
                )
        has_fixture = "fixture_source" in by_role
        has_preflight = "fixture_preflight" in by_role
        if has_fixture != has_preflight:
            raise LocalQualificationReplayError(
                "archived fixture source/preflight pairing is incomplete"
            )
        if has_fixture:
            try:
                preflight_raw = archived_paths["fixture_preflight"].read_bytes()
                preflight_document = json.loads(preflight_raw)
                preflight = LocalQualificationFixturePreflight.model_validate(preflight_document)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise LocalQualificationReplayError(
                    f"invalid archived fixture preflight: {exc}"
                ) from exc
            if (
                preflight_raw != canonical_json_bytes(preflight.model_dump(mode="json")) + b"\n"
                or preflight.fixture_sha256 != by_role["fixture_source"].sha256
                or preflight.problem_record_id != problem.problem_record_id
                or preflight.import_header_artifact != problem.import_header_artifact
                or preflight.import_header_sha256 != problem.import_header_hash
            ):
                raise LocalQualificationReplayError(
                    "archived fixture preflight differs from replay inputs"
                )
        try:
            execution_raw = archived_paths["execution_input"].read_bytes()
            execution_document = json.loads(execution_raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise LocalQualificationReplayError(f"invalid archived execution input: {exc}") from exc
        if (
            execution_raw != canonical_json_bytes(execution_document) + b"\n"
            or execution_document.get("schema") != "lf021_qualification_execution_input_v1"
            or execution_document.get("problem") != problem.model_dump(mode="json")
        ):
            raise LocalQualificationReplayError(
                "archived execution input differs from replay problem"
            )
        reference_values = execution_document.get("references")
        if not isinstance(reference_values, list):
            raise LocalQualificationReplayError(
                "archived execution input lacks reference theorem records"
            )
        try:
            archived_references = tuple(
                TheoremRecord.model_validate(reference) for reference in reference_values
            )
        except ValueError as exc:
            raise LocalQualificationReplayError(
                f"invalid archived reference theorem records: {exc}"
            ) from exc
        reference_ids = tuple(reference.theorem_id for reference in archived_references)
        if len(set(reference_ids)) != len(reference_ids) or tuple(sorted(reference_ids)) != tuple(
            sorted(problem.reference_theorem_ids)
        ):
            raise LocalQualificationReplayError(
                "archived execution references differ from the replay problem"
            )
        try:
            execution_context = ContextRecord.model_validate(execution_document.get("context"))
        except ValueError as exc:
            raise LocalQualificationReplayError(
                f"invalid archived execution context: {exc}"
            ) from exc
        registered_header = execution_document.get("registered_header")
        expected_declaration_name = execution_document.get("expected_declaration_name")
        if (
            not isinstance(registered_header, str)
            or not isinstance(expected_declaration_name, str)
            or not expected_declaration_name
        ):
            raise LocalQualificationReplayError(
                "archived execution input lacks its header or declaration name"
            )
        try:
            archived_header = archived_paths["import_header"].read_bytes()
        except OSError as exc:
            raise LocalQualificationReplayError(
                f"cannot read archived execution import header: {exc}"
            ) from exc
        registered_header_bytes = registered_header.encode("utf-8")
        if (
            archived_header != registered_header_bytes
            or sha256_hex(registered_header_bytes) != problem.import_header_hash
            or execution_context.context_id != problem.context_id
            or execution_context.header_text != registered_header
            or execution_context.header_hash != problem.import_header_hash
            or any(
                reference.context_id != execution_context.context_id
                for reference in archived_references
            )
            or (outcome is not None and outcome.declaration_name != expected_declaration_name)
        ):
            raise LocalQualificationReplayError(
                "archived execution context/header/declaration binding is inconsistent"
            )
        if inputs.checkpoint_verification is not None:
            checkpoint_record = _load_record(
                archived_paths["checkpoint_verification"],
                LocalCheckpointVerification,
            )
            if (
                checkpoint_record != inputs.checkpoint_verification
                or not checkpoint_record.matches_model(loaded.config.active_model)
            ):
                raise LocalQualificationReplayError(
                    "archived checkpoint verification differs from its binding or active model"
                )
        if inputs.code_bundle is not None:
            try:
                bundle_hash = validate_code_bundle(
                    archived_paths["code_bundle"],
                    inputs.code_bundle.code_tree_hash,
                )
            except Exception as exc:
                raise LocalQualificationReplayError(
                    f"archived qualification code bundle is invalid: {exc}"
                ) from exc
            if bundle_hash != inputs.code_bundle.sha256:
                raise LocalQualificationReplayError(
                    "archived qualification code bundle differs from its binding"
                )
        if inputs.screening_registry_hash is not None:
            try:
                registry_manifest_raw = archived_paths["benchmark_registry_manifest"].read_bytes()
                registry_manifest = RepresentationSignatureManifest.model_validate(
                    json.loads(registry_manifest_raw)
                )
                active_raw = archived_paths["benchmark_active_registry"].read_bytes()
                active_registry = FrozenRegistry.model_validate(json.loads(active_raw))
                detailed_raw = archived_paths["benchmark_detailed_index"].read_bytes()
                detailed = BenchmarkSignatureArtifact.model_validate(json.loads(detailed_raw))
                work_raw = archived_paths["benchmark_input_manifest"].read_bytes()
                work = BenchmarkSignatureWorkManifest.model_validate(json.loads(work_raw))
                prior_raw = archived_paths["prior_candidate_index"].read_bytes()
                prior_document = json.loads(prior_raw)
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise LocalQualificationReplayError(
                    f"invalid archived candidate-screening inputs: {exc}"
                ) from exc
            if (
                registry_manifest.active_registry.sha256
                != by_role["benchmark_active_registry"].sha256
                or registry_manifest.detailed_index.sha256
                != by_role["benchmark_detailed_index"].sha256
                or registry_manifest.input_manifest.sha256
                != by_role["benchmark_input_manifest"].sha256
                or registry_manifest.input_manifest.statement_count != len(work.ordered_inputs)
                or work.selection_version != registry_manifest.selection_version
                or work.normalization_version != registry_manifest.normalization_version
                or work.context_id != registry_manifest.context_id
                or detailed.selection_version != registry_manifest.selection_version
                or detailed.normalization_version != registry_manifest.normalization_version
                or detailed.context_id != registry_manifest.context_id
                or detailed.identity_registry_sha256 != work.identity_registry_sha256
                or tuple(
                    (record.statement_id, record.input_content_hash) for record in detailed.records
                )
                != work.ordered_inputs
                or active_raw
                != canonical_json_bytes(active_registry.model_dump(mode="json")) + b"\n"
                or detailed_raw != canonical_json_bytes(detailed.model_dump(mode="json")) + b"\n"
                or work_raw != canonical_json_bytes(work.model_dump(mode="json")) + b"\n"
                or DenylistIndex(active_registry).registry_content_hash
                != inputs.screening_registry_hash
                or prior_raw != canonical_json_bytes(prior_document) + b"\n"
                or prior_document.get("schema") != "lf021_prior_candidate_index_v1"
                or prior_document.get("active_registry_hash") != inputs.screening_registry_hash
            ):
                raise LocalQualificationReplayError(
                    "archived candidate-screening registry/prior index is inconsistent"
                )
            prior_values = prior_document.get("prior_candidates")
            if not isinstance(prior_values, list):
                raise LocalQualificationReplayError(
                    "archived prior candidate index lacks prior_candidates"
                )
            try:
                prior_candidates = tuple(
                    PriorCandidateIdentity.model_validate(item) for item in prior_values
                )
                reconstructed_index = CandidateScreeningIndex(
                    denylist=ProblemPoolDenylistBinding(
                        index=DenylistIndex(active_registry),
                        manifest_path="qualification_inputs/benchmark_registry_manifest.json",
                        manifest_sha256=by_role["benchmark_registry_manifest"].sha256,
                        active_registry_sha256=by_role["benchmark_active_registry"].sha256,
                        registry_content_hash=inputs.screening_registry_hash,
                    ),
                    prior_candidates=prior_candidates,
                )
            except ValueError as exc:
                raise LocalQualificationReplayError(
                    f"archived prior candidate index is invalid: {exc}"
                ) from exc
            if (
                screening is not None
                and screening.frozen_registry_hash != inputs.screening_registry_hash
            ):
                raise LocalQualificationReplayError(
                    "candidate screening record differs from archived active registry"
                )
            if screening is not None:
                if theorem is None or representation is None:
                    raise LocalQualificationReplayError(
                        "candidate screening cannot be recomputed without theorem views"
                    )
                recomputed = screen_materialized_candidate(
                    index=reconstructed_index,
                    problem_record_id=screening.problem_record_id,
                    call_id=screening.call_id,
                    theorem=theorem,
                    representation=representation,
                    created_at=screening.created_at,
                )
                if recomputed != screening:
                    raise LocalQualificationReplayError(
                        "persisted candidate screening differs from fail-closed recomputation"
                    )
            if receipt is not None:
                if (
                    outcome is None
                    or variant is None
                    or theorem is None
                    or representation is None
                    or screening is None
                ):
                    raise LocalQualificationReplayError(
                        "smoke admission cannot be replayed without complete pending lineage"
                    )
                pending_materialization = RealOutputMaterializationResult(
                    outcome=outcome,
                    variant=variant,
                    theorem=theorem,
                    representation=representation,
                )
                try:
                    recomputed_admission = admit_screened_real_output_candidate(
                        materialized=pending_materialization,
                        screening=screening,
                        problem=problem,
                        references=archived_references,
                        expected_frozen_registry_hash=inputs.screening_registry_hash,
                        created_at=receipt.created_at,
                    )
                    recomputed_receipt = SmokeAdmissionDryRunReceipt.create(
                        pending=outcome,
                        admitted=recomputed_admission.outcome,
                        screening=screening,
                        active_registry_hash=inputs.screening_registry_hash,
                        created_at=receipt.created_at,
                    )
                except ValueError as exc:
                    raise LocalQualificationReplayError(
                        f"smoke admission dry run could not be replayed: {exc}"
                    ) from exc
                if recomputed_receipt != receipt:
                    raise LocalQualificationReplayError(
                        "smoke admission receipt differs from fail-closed recomputation"
                    )
    if (
        loaded.config_hash != inputs.qualification_config_hash
        or terminal.qualification_config_hash != inputs.qualification_config_hash
    ):
        raise LocalQualificationReplayError("qualification config canonical hash changed")
    if terminal.runtime_hash != inputs.runtime.runtime_hash:
        raise LocalQualificationReplayError("terminal runtime hash differs from input binding")
    prompt = loaded.config.prompt
    if (
        inputs.prompt_template_artifact != prompt.template_artifact
        or inputs.prompt_template_sha256 != prompt.template_sha256
        or inputs.common_suffix_artifact != prompt.common_suffix_artifact
        or inputs.common_suffix_sha256 != prompt.common_suffix_sha256
        or inputs.parser_id != prompt.parser_id
        or inputs.parser_source_artifact != prompt.parser_source_artifact
        or inputs.parser_source_sha256 != prompt.parser_source_sha256
        or inputs.model_repo_id != loaded.config.active_model.repo_id
        or inputs.model_revision != loaded.config.active_model.revision
        or inputs.tokenizer_revision != loaded.config.active_model.tokenizer_revision
        or inputs.model_metadata_hashes != loaded.config.active_model.metadata_hashes
    ):
        raise LocalQualificationReplayError("qualification input binding differs from config")
    verify_llm_call_artifacts(call=call, problem=problem, artifact_root=artifact_root)
    return terminal, call, attempt


# Generic public name; retain the historical Kimina name for archived callers.
run_local_qualification = run_local_kimina_qualification


__all__ = [
    "ArchivedQualificationInput",
    "LocalCandidateRegistryEntry",
    "LocalCheckpointArtifacts",
    "LocalCheckpointFile",
    "LocalCheckpointVerification",
    "LocalMetadataHashes",
    "LocalQualificationBundleManifest",
    "LocalQualificationConfig",
    "LocalQualificationConfigError",
    "LocalQualificationError",
    "LocalQualificationFixturePreflight",
    "LocalQualificationModel",
    "LocalQualificationPrompt",
    "LocalQualificationReplayError",
    "LocalQualificationRunResult",
    "LocalQualificationTerminal",
    "QualificationCodeBundleBinding",
    "QualificationInputBinding",
    "QualificationScreeningInputFiles",
    "QualificationStatus",
    "RenderedKiminaPrompt",
    "RuntimeEnvironmentBinding",
    "SmokeAdmissionDryRunReceipt",
    "build_local_qualification_formatter",
    "load_local_qualification_config",
    "make_runtime_binding",
    "persist_local_qualification_bundle",
    "preflight_local_qualification_fixture",
    "render_kimina_qualification_prompt",
    "render_local_qualification_prompt",
    "run_local_kimina_qualification",
    "run_local_qualification",
    "verify_local_checkpoint_artifacts",
    "verify_local_qualification_bundle",
    "verify_runtime_binding",
]
