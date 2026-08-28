"""Content-addressed tokenizer audit for the preregistered backbone pilot.

This module performs no model training and selects no scientific winner.  It
binds the exact Gate-3 denominator and ``repr_v3`` representation partition to
four immutable tokenizer snapshots, measures the frozen
``[HEADLESS]``/``[SIGNATURE_EXPLICIT]`` bundle, and makes only the data-driven
512-versus-1,024 context decision from ADR-0004.

The conservative section-budget rule never credits a partial explicit
signature as retaining the complete binder/typeclass/hypothesis set.  Under a
budget, the headless conclusion is reserved first and the complete explicit
signature is retained only when it fits in full.  Records that do not fit the
selected budget remain in the ``long_input`` slice; they are never filtered
from the frozen denominator.
"""

from __future__ import annotations

import errno
import hashlib
import importlib.metadata
import json
import math
import os
import re
import shutil
import stat
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.loading import LoadedConfig, load_config, load_yaml_mapping
from leanfaith.config.models import StrictModel
from leanfaith.models.tokenizer_sections import SectionDerivationManifest
from leanfaith.schemas.manifest import CodeState, collect_code_state

_HEX64 = r"^[0-9a-f]{64}$"
_CANDIDATE_KEYS = (
    "modernbert_base",
    "modernbert_large",
    "codet5p_220m_encoder",
    "deberta_v3_large",
)
_OUTPUT_FILES = {
    "fragmentation.json",
    "manifest.json",
    "records.jsonl",
    "summary.json",
}
_NON_MANIFEST_OUTPUT_FILES = _OUTPUT_FILES - {"manifest.json"}
_FROZEN_PROFILE_ID = "gate3_repr_v3_backbone_tokenizer_audit_v1"
_SECTION_METHOD_VERSION = "lean_meta_tokenizer_sections_v1"
_NAMESPACE_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_'])"
    r"(?:[A-Za-z_][A-Za-z0-9_']*\.)+"
    r"[A-Za-z_][A-Za-z0-9_']*"
)
_MARKER_HEADLESS = "[HEADLESS]\n"
_MARKER_EXPLICIT = "\n[SIGNATURE_EXPLICIT]\n"


class TokenizerAuditError(RuntimeError):
    """The tokenizer audit failed a frozen-input or replay invariant."""


class _Tokenizer(Protocol):
    model_max_length: int
    is_fast: bool

    def encode(self, text: str, *, add_special_tokens: bool) -> list[int]: ...

    def convert_ids_to_tokens(self, ids: Sequence[int]) -> list[str]: ...

    def num_special_tokens_to_add(self, pair: bool = False) -> int: ...

    def __len__(self) -> int: ...


class FilePin(StrictModel):
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=1)


class CandidateAuditConfig(StrictModel):
    model_id: str = Field(min_length=1)
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    cache_snapshot: str = Field(min_length=1)
    branch: Literal["full_encoder", "encoder_only"]
    use_fast: bool
    native_max_length: int = Field(ge=1)
    files: dict[str, FilePin] = Field(min_length=2)

    @field_validator("cache_snapshot")
    @classmethod
    def _absolute_snapshot(cls, value: str) -> str:
        if not Path(value).is_absolute():
            raise ValueError("cache_snapshot must be absolute")
        return value

    @field_validator("files")
    @classmethod
    def _safe_files(cls, value: dict[str, FilePin]) -> dict[str, FilePin]:
        for name in value:
            path = Path(name)
            if path.is_absolute() or len(path.parts) != 1 or name.startswith("."):
                raise ValueError("candidate file pins must be safe snapshot basenames")
        if "config.json" not in value or "tokenizer_config.json" not in value:
            raise ValueError("candidate pins require config.json and tokenizer_config.json")
        return dict(sorted(value.items()))


class TokenizerAuditInputs(StrictModel):
    gate3_manifest: str
    gate3_manifest_sha256: str = Field(pattern=_HEX64)
    theorem_partition: str
    theorem_partition_sha256: str = Field(pattern=_HEX64)
    representation_manifest: str
    representation_manifest_sha256: str = Field(pattern=_HEX64)
    representation_partition: str
    representation_partition_sha256: str = Field(pattern=_HEX64)
    expected_records: int = Field(ge=1)
    expected_per_source: dict[str, int] = Field(min_length=1)
    expected_normalization_version: str = Field(min_length=1)
    semantic_sections_status: Literal["pending_derivation", "frozen"]
    semantic_sections_manifest: str | None = None
    semantic_sections_manifest_sha256: str | None = Field(default=None, pattern=_HEX64)
    semantic_sections_partition: str | None = None
    semantic_sections_partition_sha256: str | None = Field(default=None, pattern=_HEX64)
    semantic_sections_method_version: Literal["lean_meta_tokenizer_sections_v1"]

    @model_validator(mode="after")
    def _accounting(self) -> TokenizerAuditInputs:
        if sum(self.expected_per_source.values()) != self.expected_records:
            raise ValueError("expected_per_source does not sum to expected_records")
        for value in (
            self.gate3_manifest,
            self.theorem_partition,
            self.representation_manifest,
            self.representation_partition,
        ):
            if not Path(value).is_absolute():
                raise ValueError("tokenizer audit input paths must be absolute")
        section_values = (
            self.semantic_sections_manifest,
            self.semantic_sections_manifest_sha256,
            self.semantic_sections_partition,
            self.semantic_sections_partition_sha256,
        )
        if self.semantic_sections_status == "frozen":
            if any(value is None for value in section_values):
                raise ValueError("frozen semantic sections require exact manifest/partition pins")
            for section_path in (
                self.semantic_sections_manifest,
                self.semantic_sections_partition,
            ):
                assert section_path is not None
                if not Path(section_path).is_absolute():
                    raise ValueError("semantic-section input paths must be absolute")
        elif any(value is not None for value in section_values):
            raise ValueError("pending semantic sections cannot carry partial frozen pins")
        return self


class TokenizerAuditConfig(StrictModel):
    schema_version: Literal[2]
    profile_id: str = Field(min_length=1)
    backbone_registry_path: str = Field(min_length=1)
    backbone_registry_sha256: str = Field(pattern=_HEX64)
    inputs: TokenizerAuditInputs
    candidates: dict[str, CandidateAuditConfig]
    budgets: tuple[int, int]
    complete_semantics_fraction_at_512: float = Field(gt=0.0, le=1.0)
    representation_markers: tuple[str, str]
    section_budget_policy: Literal["conclusion_then_ordered_lean_sections_v2"]
    unicode_symbols: tuple[str, ...] = Field(min_length=1)
    maximum_namespace_piece_bins: int = Field(ge=1)

    @model_validator(mode="after")
    def _frozen_profile_contract(self) -> TokenizerAuditConfig:
        if self.profile_id == _FROZEN_PROFILE_ID:
            if self.inputs.expected_records != 10_000:
                raise ValueError("frozen tokenizer profile requires exactly 10,000 records")
            if self.inputs.expected_per_source != {"mathlib": 5_000, "sft_classic": 5_000}:
                raise ValueError(
                    "frozen tokenizer profile requires exactly 5,000 records per source"
                )
            if self.complete_semantics_fraction_at_512 != 0.99:
                raise ValueError("frozen tokenizer profile requires the exact 0.99 threshold")
        return self

    @field_validator("backbone_registry_path")
    @classmethod
    def _registry_is_repository_relative(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("backbone_registry_path must be repository-relative")
        return value

    @field_validator("candidates")
    @classmethod
    def _candidate_registry_is_exact(
        cls, value: dict[str, CandidateAuditConfig]
    ) -> dict[str, CandidateAuditConfig]:
        if tuple(sorted(value)) != tuple(sorted(_CANDIDATE_KEYS)):
            raise ValueError(f"candidates must be exactly {_CANDIDATE_KEYS}")
        return value

    @field_validator("budgets")
    @classmethod
    def _budgets_are_plan_values(cls, value: tuple[int, int]) -> tuple[int, int]:
        if value != (512, 1024):
            raise ValueError("tokenizer audit budgets must be exactly [512, 1024]")
        return value

    @field_validator("representation_markers")
    @classmethod
    def _markers_are_frozen(cls, value: tuple[str, str]) -> tuple[str, str]:
        if value != ("[HEADLESS]", "[SIGNATURE_EXPLICIT]"):
            raise ValueError("representation markers differ from ADR-0004")
        return value

    @field_validator("unicode_symbols")
    @classmethod
    def _symbols_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if tuple(sorted(set(value))) != tuple(sorted(value)):
            raise ValueError("unicode_symbols must be sorted and unique")
        if any(not symbol or symbol.isascii() for symbol in value):
            raise ValueError("unicode_symbols must contain nonempty non-ASCII strings")
        return value


class InputBinding(StrictModel):
    path: str
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=1)


class SnapshotBinding(StrictModel):
    model_id: str
    revision: str
    path: str
    use_fast: bool
    native_max_length: int
    files: dict[str, FilePin]
    snapshot_content_hash: str = Field(pattern=_HEX64)


class RuntimeVersions(StrictModel):
    python: str
    transformers: str
    tokenizers: str
    sentencepiece: str | None
    protobuf: str | None


class BudgetRetention(StrictModel):
    budget: int
    conclusion_retained: bool
    complete_binder_set_retained: bool
    complete_typeclass_binder_set_retained: bool
    complete_hypothesis_set_retained: bool
    complete_semantic_sections_retained: bool
    full_bundle_retained: bool


class TokenizerAuditRecord(StrictModel):
    candidate: str
    theorem_id: str
    source: str
    full_bundle_tokens: int
    headless_tokens: int
    signature_explicit_tokens: int
    conclusion_tokens: int
    ordinary_binder_tokens: int
    typeclass_binder_tokens: int
    prop_hypothesis_tokens: int
    semantic_unit_count: int
    semantic_minimum_tokens: int
    retention: tuple[BudgetRetention, BudgetRetention]


class LengthStats(StrictModel):
    count: int
    minimum: int
    mean: float
    p50: int
    p90: int
    p95: int
    p99: int
    maximum: int


class BudgetSummary(StrictModel):
    budget: int
    conclusion_retained: int
    complete_binder_set_retained: int
    complete_typeclass_binder_set_retained: int
    complete_hypothesis_set_retained: int
    complete_semantic_sections_retained: int
    full_bundle_retained: int
    long_input: int


class CandidateSummary(StrictModel):
    candidate: str
    model_id: str
    revision: str
    tokenizer_class: str
    tokenizer_fast: bool
    tokenizer_vocab_size: int
    tokenizer_reported_max_length: int
    native_max_length: int
    lengths: LengthStats
    headless_lengths: LengthStats
    signature_explicit_lengths: LengthStats
    by_source: dict[str, LengthStats]
    budgets: tuple[BudgetSummary, BudgetSummary]
    eligible_at_selected_length: bool
    ineligibility_reason: str | None


class UnicodeFragmentation(StrictModel):
    symbol: str
    occurrence_count: int
    isolated_piece_count: int
    isolated_pieces: tuple[str, ...]


class NamespaceExample(StrictModel):
    piece_count: int
    unique_count: int
    occurrence_count: int


class CandidateFragmentation(StrictModel):
    candidate: str
    unicode: tuple[UnicodeFragmentation, ...]
    unicode_occurrences: int
    unicode_weighted_mean_pieces: float | None
    unicode_single_piece_occurrence_fraction: float | None
    namespace_occurrences: int
    namespace_unique: int
    namespace_weighted_mean_pieces: float | None
    namespace_single_piece_occurrence_fraction: float | None
    namespace_p95_pieces: int | None
    namespace_max_pieces: int | None
    namespace_piece_histogram: tuple[NamespaceExample, ...]
    contains_private_source: Literal[True]
    aggregate_only: Literal[True]
    redistribution: Literal[False]
    external_transmission: Literal[False]
    release_eligible: Literal[False]


class _FragmentationArtifact(StrictModel):
    schema_version: Literal[2]
    privacy: dict[str, bool]
    candidates: tuple[CandidateFragmentation, ...]

    @model_validator(mode="after")
    def _privacy_contract(self) -> _FragmentationArtifact:
        expected = {
            "contains_private_source": True,
            "aggregate_only": True,
            "redistribution": False,
            "external_transmission": False,
            "release_eligible": False,
        }
        if self.privacy != expected:
            raise ValueError("fragmentation privacy contract differs")
        if tuple(item.candidate for item in self.candidates) != tuple(sorted(_CANDIDATE_KEYS)):
            raise ValueError("fragmentation candidate set/order differs")
        return self


class TokenizerAuditSummary(StrictModel):
    audit_id: str = Field(pattern=r"^tokenizer_audit:[0-9a-f]{64}$")
    profile_id: str
    scientific_winner_selected: Literal[False]
    record_count: int
    per_source: dict[str, int]
    selected_length: Literal[512, 1024]
    selection_reason: str
    candidate_summaries: tuple[CandidateSummary, ...]
    eligible_candidates: tuple[str, ...]
    long_input_counts: dict[str, int]
    contains_private_source: Literal[True]
    redistribution: Literal[False]
    external_transmission: Literal[False]
    release_eligible: Literal[False]


class TokenizerAuditManifest(StrictModel):
    schema_version: Literal[2]
    audit_id: str
    profile_id: str
    config_hash: str = Field(pattern=_HEX64)
    config: TokenizerAuditConfig
    code: CodeState
    repository_root: str
    runtime: RuntimeVersions
    inputs: dict[str, InputBinding]
    snapshots: dict[str, SnapshotBinding]
    selected_length: Literal[512, 1024]
    scientific_winner_selected: Literal[False]
    contains_private_source: Literal[True]
    redistribution: Literal[False]
    external_transmission: Literal[False]
    release_eligible: Literal[False]
    output_sha256: dict[str, str]

    @field_validator("output_sha256")
    @classmethod
    def _exact_output_hash_keys(cls, value: dict[str, str]) -> dict[str, str]:
        if set(value) != _NON_MANIFEST_OUTPUT_FILES:
            raise ValueError("output_sha256 keys must be exactly the non-manifest outputs")
        if any(re.fullmatch(_HEX64, digest) is None for digest in value.values()):
            raise ValueError("output_sha256 values must be lowercase SHA-256 digests")
        return dict(sorted(value.items()))


class TokenizerAuditArtifacts(StrictModel):
    output_dir: Path
    manifest_path: Path
    audit_id: str
    selected_length: Literal[512, 1024]
    eligible_candidates: tuple[str, ...]
    replayed: bool


class _TheoremInput(StrictModel):
    theorem_id: str
    source: str
    conclusion: str


class _RepresentationInput(StrictModel):
    theorem_id: str
    normalization_version: str
    headless: str
    signature_explicit: str


class _SemanticSectionUnit(StrictModel):
    ordinal: int = Field(ge=0)
    kind: Literal["ordinary_binder", "instance_binder", "prop_hypothesis"]
    binder_info: Literal["default", "implicit", "strictImplicit", "instImplicit"]
    domain_is_prop: bool
    text: str = Field(min_length=1)


class _SemanticSectionsInput(StrictModel):
    theorem_id: str
    source: str
    method_version: Literal["lean_meta_tokenizer_sections_v1"]
    units: tuple[_SemanticSectionUnit, ...]
    conclusion: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered_exact_classification(self) -> _SemanticSectionsInput:
        if [unit.ordinal for unit in self.units] != list(range(len(self.units))):
            raise ValueError("semantic-section ordinals must be contiguous and ordered")
        for unit in self.units:
            expected = (
                "instance_binder"
                if unit.binder_info == "instImplicit"
                else "prop_hypothesis"
                if unit.domain_is_prop
                else "ordinary_binder"
            )
            if unit.kind != expected:
                raise ValueError("semantic-section kind differs from Lean meta classification")
        return self


def load_tokenizer_audit_config(path: Path) -> LoadedConfig[TokenizerAuditConfig]:
    """Load and strictly validate a tokenizer-audit configuration."""

    return load_config(path, TokenizerAuditConfig)


def _regular_file(path: Path) -> Path:
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        raise TokenizerAuditError(f"required file is unavailable: {path}") from exc
    if not stat.S_ISREG(mode):
        raise TokenizerAuditError(f"required path is not a regular file: {path}")
    return path


def _real_directory(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise TokenizerAuditError(f"required directory is unavailable: {path}") from exc
    if not resolved.is_dir():
        raise TokenizerAuditError(f"required path is not a directory: {path}")
    return resolved


def _reject_symlinks(path: Path, *, allow_missing: bool) -> Path:
    absolute = path.absolute()
    cursor = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        cursor /= part
        if not cursor.exists():
            if allow_missing:
                continue
            raise TokenizerAuditError(f"path component is unavailable: {cursor}")
        if cursor.is_symlink():
            raise TokenizerAuditError(f"output path contains a symlink: {cursor}")
    return absolute


def _strict_json(path: Path) -> object:
    try:
        return json.loads(_regular_file(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TokenizerAuditError(f"invalid JSON: {path}") from exc


def _canonical_jsonl(models: Sequence[StrictModel]) -> bytes:
    return b"".join(canonical_json_bytes(model.model_dump(mode="json")) + b"\n" for model in models)


def _fragmentation_payload(items: Sequence[CandidateFragmentation]) -> bytes:
    return (
        canonical_json_bytes(
            {
                "schema_version": 2,
                "privacy": {
                    "contains_private_source": True,
                    "aggregate_only": True,
                    "redistribution": False,
                    "external_transmission": False,
                    "release_eligible": False,
                },
                "candidates": [item.model_dump(mode="json") for item in items],
            }
        )
        + b"\n"
    )


def _audit_id(
    *,
    config_hash: str,
    code_tree_hash: str,
    input_bindings: Mapping[str, InputBinding],
    snapshot_bindings: Mapping[str, SnapshotBinding],
    runtime: RuntimeVersions,
    records_payload: bytes,
    fragmentation_payload: bytes,
    summary_core: Mapping[str, object],
) -> str:
    return "tokenizer_audit:" + hash_canonical(
        {
            "config_hash": config_hash,
            "code_tree_hash": code_tree_hash,
            "inputs": {
                key: value.model_dump(mode="json") for key, value in sorted(input_bindings.items())
            },
            "snapshots": {
                key: value.model_dump(mode="json")
                for key, value in sorted(snapshot_bindings.items())
            },
            "runtime": runtime.model_dump(mode="json"),
            "records_sha256": hashlib.sha256(records_payload).hexdigest(),
            "fragmentation_sha256": hashlib.sha256(fragmentation_payload).hexdigest(),
            "summary_core": summary_core,
        }
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with _regular_file(path).open("rb") as handle:
        for line_number, raw in enumerate(handle, start=1):
            try:
                value = json.loads(raw)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise TokenizerAuditError(f"invalid JSONL at {path}:{line_number}") from exc
            if not isinstance(value, dict):
                raise TokenizerAuditError(f"non-object JSONL row at {path}:{line_number}")
            rows.append(value)
    return rows


def _verify_file(path: str, expected: str) -> InputBinding:
    file_path = _regular_file(Path(path))
    actual = hash_file(file_path)
    if actual != expected:
        raise TokenizerAuditError(f"input hash differs: {file_path}")
    return InputBinding(path=str(file_path), sha256=actual, byte_count=file_path.stat().st_size)


def _snapshot_binding(candidate: CandidateAuditConfig) -> SnapshotBinding:
    snapshot = _real_directory(Path(candidate.cache_snapshot))
    if snapshot.name != candidate.revision:
        raise TokenizerAuditError(
            f"snapshot directory does not equal pinned revision for {candidate.model_id}"
        )
    observed: dict[str, FilePin] = {}
    for name, pin in candidate.files.items():
        path = _regular_file(snapshot / name)
        actual = FilePin(sha256=hash_file(path), byte_count=path.stat().st_size)
        if actual != pin:
            raise TokenizerAuditError(f"tokenizer file differs: {path}")
        observed[name] = actual
    raw_model_config = _strict_json(snapshot / "config.json")
    if not isinstance(raw_model_config, dict):
        raise TokenizerAuditError(f"model config is not an object: {snapshot / 'config.json'}")
    model_config = cast(dict[str, Any], raw_model_config)
    native = model_config.get("max_position_embeddings", model_config.get("n_positions"))
    if native != candidate.native_max_length:
        raise TokenizerAuditError(f"native context differs for {candidate.model_id}: {native!r}")
    content_hash = hash_canonical(
        {name: item.model_dump(mode="json") for name, item in sorted(observed.items())}
    )
    return SnapshotBinding(
        model_id=candidate.model_id,
        revision=candidate.revision,
        path=str(snapshot),
        use_fast=candidate.use_fast,
        native_max_length=candidate.native_max_length,
        files=observed,
        snapshot_content_hash=content_hash,
    )


def _runtime_versions() -> RuntimeVersions:
    import platform

    def version(name: str) -> str | None:
        try:
            return importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            return None

    transformers = version("transformers")
    tokenizers = version("tokenizers")
    if transformers is None or tokenizers is None:
        raise TokenizerAuditError(
            "tokenizer audit requires the optional local-inference transformers runtime"
        )
    return RuntimeVersions(
        python=platform.python_version(),
        transformers=transformers,
        tokenizers=tokenizers,
        sentencepiece=version("sentencepiece"),
        protobuf=version("protobuf"),
    )


def _load_tokenizer(binding: SnapshotBinding) -> _Tokenizer:
    try:
        from transformers import AutoTokenizer
    except ImportError as exc:  # pragma: no cover - exercised in minimal installs
        raise TokenizerAuditError(
            "tokenizer audit requires the optional local-inference transformers runtime"
        ) from exc
    try:
        tokenizer = AutoTokenizer.from_pretrained(  # type: ignore[no-untyped-call]
            binding.path,
            local_files_only=True,
            trust_remote_code=False,
            use_fast=binding.use_fast,
        )
    except Exception as exc:
        raise TokenizerAuditError(f"cannot load pinned tokenizer {binding.model_id}") from exc
    return cast(_Tokenizer, tokenizer)


def _load_inputs(
    config: TokenizerAuditConfig,
) -> tuple[
    list[_TheoremInput],
    list[_RepresentationInput],
    list[_SemanticSectionsInput],
    dict[str, InputBinding],
]:
    inputs = config.inputs
    if inputs.semantic_sections_status != "frozen":
        raise TokenizerAuditError(
            "exact Lean-meta semantic-section derivation is not frozen; tokenizer audit refuses "
            "the former whole-signature proxy"
        )
    assert inputs.semantic_sections_manifest is not None
    assert inputs.semantic_sections_manifest_sha256 is not None
    assert inputs.semantic_sections_partition is not None
    assert inputs.semantic_sections_partition_sha256 is not None
    bindings = {
        "gate3_manifest": _verify_file(inputs.gate3_manifest, inputs.gate3_manifest_sha256),
        "theorem_partition": _verify_file(
            inputs.theorem_partition, inputs.theorem_partition_sha256
        ),
        "representation_manifest": _verify_file(
            inputs.representation_manifest, inputs.representation_manifest_sha256
        ),
        "representation_partition": _verify_file(
            inputs.representation_partition, inputs.representation_partition_sha256
        ),
        "semantic_sections_manifest": _verify_file(
            inputs.semantic_sections_manifest,
            inputs.semantic_sections_manifest_sha256,
        ),
        "semantic_sections_partition": _verify_file(
            inputs.semantic_sections_partition,
            inputs.semantic_sections_partition_sha256,
        ),
    }
    gate_manifest = cast(dict[str, Any], _strict_json(Path(inputs.gate3_manifest)))
    if gate_manifest.get("record_count") != inputs.expected_records:
        raise TokenizerAuditError("Gate-3 manifest record count differs")

    theorem_rows = _read_jsonl(Path(inputs.theorem_partition))
    representation_rows = _read_jsonl(Path(inputs.representation_partition))
    if len(theorem_rows) != inputs.expected_records or len(representation_rows) != len(
        theorem_rows
    ):
        raise TokenizerAuditError("frozen tokenizer-audit denominator differs")

    theorems: list[_TheoremInput] = []
    source_counts: Counter[str] = Counter()
    for row in theorem_rows:
        theorem = row.get("theorem")
        if not isinstance(theorem, dict):
            raise TokenizerAuditError("theorem partition row lacks theorem object")
        metadata = theorem.get("metadata")
        if not isinstance(metadata, dict):
            raise TokenizerAuditError("theorem partition row lacks metadata")
        theorem_id = theorem.get("theorem_id")
        source = theorem.get("source")
        conclusion = metadata.get("conclusion_pp")
        if not isinstance(theorem_id, str) or not theorem_id:
            raise TokenizerAuditError("theorem audit theorem_id is missing")
        if not isinstance(source, str) or not source:
            raise TokenizerAuditError("theorem audit source is missing")
        if not isinstance(conclusion, str) or not conclusion:
            raise TokenizerAuditError("theorem audit fields are missing")
        source_counts[source] += 1
        theorems.append(_TheoremInput(theorem_id=theorem_id, source=source, conclusion=conclusion))
    if dict(sorted(source_counts.items())) != dict(sorted(inputs.expected_per_source.items())):
        raise TokenizerAuditError("frozen per-source denominator differs")

    representations: list[_RepresentationInput] = []
    for expected, row in zip(theorems, representation_rows, strict=True):
        if row.get("theorem_id") != expected.theorem_id:
            raise TokenizerAuditError("representation order differs from Gate-3 denominator")
        if row.get("normalization_version") != inputs.expected_normalization_version:
            raise TokenizerAuditError("representation normalization version differs")
        statuses = row.get("view_status")
        if not isinstance(statuses, dict) or any(
            statuses.get(view) != "ok" for view in ("headless", "signature_explicit")
        ):
            raise TokenizerAuditError("required model view is not successful")
        headless = row.get("headless")
        explicit = row.get("signature_explicit")
        if not isinstance(headless, str) or not isinstance(explicit, str):
            raise TokenizerAuditError("required model view is absent")
        representations.append(
            _RepresentationInput(
                theorem_id=expected.theorem_id,
                normalization_version=inputs.expected_normalization_version,
                headless=headless,
                signature_explicit=explicit,
            )
        )
    try:
        sections_manifest = SectionDerivationManifest.model_validate(
            _strict_json(Path(inputs.semantic_sections_manifest))
        )
    except ValueError as exc:
        raise TokenizerAuditError("invalid semantic-section manifest") from exc
    if (
        sections_manifest.method_version != inputs.semantic_sections_method_version
        or sections_manifest.record_count != inputs.expected_records
        or sections_manifest.per_source != inputs.expected_per_source
        or sections_manifest.output_sha256["sections.jsonl"]
        != inputs.semantic_sections_partition_sha256
    ):
        raise TokenizerAuditError("semantic-section manifest differs from frozen audit profile")
    section_rows = _read_jsonl(Path(inputs.semantic_sections_partition))
    if len(section_rows) != inputs.expected_records:
        raise TokenizerAuditError("semantic-section denominator differs")
    sections: list[_SemanticSectionsInput] = []
    section_counts: Counter[str] = Counter()
    for theorem, row in zip(theorems, section_rows, strict=True):
        try:
            section = _SemanticSectionsInput.model_validate(row)
        except ValueError as exc:
            raise TokenizerAuditError(
                f"invalid semantic sections for theorem {theorem.theorem_id}"
            ) from exc
        if section.theorem_id != theorem.theorem_id or section.source != theorem.source:
            raise TokenizerAuditError("semantic-section order/source differs from denominator")
        section_counts[section.source] += 1
        sections.append(section)
    if dict(sorted(section_counts.items())) != dict(sorted(inputs.expected_per_source.items())):
        raise TokenizerAuditError("semantic-section per-source denominator differs")
    return theorems, representations, sections, bindings


def _verify_backbone_registry(
    repo_root: Path,
    config: TokenizerAuditConfig,
) -> InputBinding:
    path = _regular_file(repo_root / config.backbone_registry_path)
    if hash_file(path) != config.backbone_registry_sha256:
        raise TokenizerAuditError("backbone registry hash differs")
    registry = load_yaml_mapping(path)
    raw_candidates = registry.get("candidates")
    if not isinstance(raw_candidates, dict):
        raise TokenizerAuditError("backbone registry lacks candidate mapping")
    for key, candidate in config.candidates.items():
        raw = raw_candidates.get(key)
        if not isinstance(raw, dict):
            raise TokenizerAuditError(f"backbone registry lacks candidate {key}")
        expected = {
            "model_id": candidate.model_id,
            "revision": candidate.revision,
            "branch": candidate.branch,
        }
        if any(raw.get(field) != value for field, value in expected.items()):
            raise TokenizerAuditError(f"tokenizer audit candidate differs from registry: {key}")
        if candidate.branch == "encoder_only" and raw.get("decoder_loaded") is not False:
            raise TokenizerAuditError("encoder-only candidate registry does not disable decoder")
    return InputBinding(path=str(path), sha256=hash_file(path), byte_count=path.stat().st_size)


def _tokens(tokenizer: _Tokenizer, text: str, *, special: bool = False) -> list[int]:
    return list(tokenizer.encode(text, add_special_tokens=special))


def _retention(
    *,
    budget: int,
    full_length: int,
    tokenizer: _Tokenizer,
    sections: _SemanticSectionsInput,
) -> BudgetRetention:
    prefix = _MARKER_HEADLESS + sections.conclusion + _MARKER_EXPLICIT
    conclusion = len(_tokens(tokenizer, prefix, special=True)) <= budget
    retained_ordinals: set[int] = set()
    if conclusion:
        current = prefix
        for unit in sections.units:
            proposed = current + unit.text + "\n"
            if len(_tokens(tokenizer, proposed, special=True)) > budget:
                break
            current = proposed
            retained_ordinals.add(unit.ordinal)

    def complete(kind: str) -> bool:
        return all(
            unit.ordinal in retained_ordinals for unit in sections.units if unit.kind == kind
        )

    ordinary = conclusion and complete("ordinary_binder")
    typeclasses = conclusion and complete("instance_binder")
    hypotheses = conclusion and complete("prop_hypothesis")
    return BudgetRetention(
        budget=budget,
        conclusion_retained=conclusion,
        complete_binder_set_retained=ordinary,
        complete_typeclass_binder_set_retained=typeclasses,
        complete_hypothesis_set_retained=hypotheses,
        complete_semantic_sections_retained=(ordinary and typeclasses and hypotheses),
        full_bundle_retained=full_length <= budget,
    )


def _percentile(sorted_values: Sequence[int], fraction: float) -> int:
    if not sorted_values:
        raise TokenizerAuditError("cannot summarize empty lengths")
    index = max(0, math.ceil(fraction * len(sorted_values)) - 1)
    return sorted_values[index]


def _length_stats(values: Sequence[int]) -> LengthStats:
    ordered = sorted(values)
    if not ordered:
        raise TokenizerAuditError("cannot summarize empty lengths")
    return LengthStats(
        count=len(ordered),
        minimum=ordered[0],
        mean=sum(ordered) / len(ordered),
        p50=_percentile(ordered, 0.50),
        p90=_percentile(ordered, 0.90),
        p95=_percentile(ordered, 0.95),
        p99=_percentile(ordered, 0.99),
        maximum=ordered[-1],
    )


def _budget_summary(records: Sequence[TokenizerAuditRecord], budget: int) -> BudgetSummary:
    positions = {item.budget: item for item in records[0].retention}
    if budget not in positions:
        raise TokenizerAuditError("record lacks configured budget")

    def count(field: str) -> int:
        return sum(
            bool(getattr({item.budget: item for item in record.retention}[budget], field))
            for record in records
        )

    complete = count("complete_semantic_sections_retained")
    return BudgetSummary(
        budget=budget,
        conclusion_retained=count("conclusion_retained"),
        complete_binder_set_retained=count("complete_binder_set_retained"),
        complete_typeclass_binder_set_retained=count("complete_typeclass_binder_set_retained"),
        complete_hypothesis_set_retained=count("complete_hypothesis_set_retained"),
        complete_semantic_sections_retained=complete,
        full_bundle_retained=count("full_bundle_retained"),
        long_input=len(records) - complete,
    )


def _pieces(tokenizer: _Tokenizer, text: str) -> tuple[str, ...]:
    ids = _tokens(tokenizer, text)
    return tuple(tokenizer.convert_ids_to_tokens(ids))


def _fragmentation(
    *,
    candidate: str,
    tokenizer: _Tokenizer,
    texts: Sequence[str],
    symbols: Sequence[str],
    maximum_namespace_piece_bins: int,
) -> CandidateFragmentation:
    symbol_occurrences = {symbol: sum(text.count(symbol) for text in texts) for symbol in symbols}
    unicode_rows: list[UnicodeFragmentation] = []
    weighted_unicode_pieces = 0
    weighted_unicode_single = 0
    for symbol in symbols:
        pieces = _pieces(tokenizer, symbol)
        occurrences = symbol_occurrences[symbol]
        unicode_rows.append(
            UnicodeFragmentation(
                symbol=symbol,
                occurrence_count=occurrences,
                isolated_piece_count=len(pieces),
                isolated_pieces=pieces,
            )
        )
        weighted_unicode_pieces += occurrences * len(pieces)
        if len(pieces) == 1:
            weighted_unicode_single += occurrences
    unicode_total = sum(symbol_occurrences.values())

    namespace_counter: Counter[str] = Counter()
    for text in texts:
        namespace_counter.update(_NAMESPACE_PATTERN.findall(text))
    namespace_piece_histogram: Counter[int] = Counter()
    namespace_occurrence_histogram: Counter[int] = Counter()
    weighted_namespace_pieces = 0
    weighted_namespace_single = 0
    piece_counts: list[int] = []
    for name, occurrences in namespace_counter.items():
        pieces = _pieces(tokenizer, name)
        count = len(pieces)
        piece_counts.append(count)
        weighted_namespace_pieces += occurrences * count
        if count == 1:
            weighted_namespace_single += occurrences
        namespace_piece_histogram[count] += 1
        namespace_occurrence_histogram[count] += occurrences
    if len(namespace_piece_histogram) > maximum_namespace_piece_bins:
        raise TokenizerAuditError("namespace piece histogram exceeds configured public bin cap")
    namespace_total = sum(namespace_counter.values())
    ordered_piece_counts = sorted(piece_counts)
    return CandidateFragmentation(
        candidate=candidate,
        unicode=tuple(unicode_rows),
        unicode_occurrences=unicode_total,
        unicode_weighted_mean_pieces=(
            weighted_unicode_pieces / unicode_total if unicode_total else None
        ),
        unicode_single_piece_occurrence_fraction=(
            weighted_unicode_single / unicode_total if unicode_total else None
        ),
        namespace_occurrences=namespace_total,
        namespace_unique=len(namespace_counter),
        namespace_weighted_mean_pieces=(
            weighted_namespace_pieces / namespace_total if namespace_total else None
        ),
        namespace_single_piece_occurrence_fraction=(
            weighted_namespace_single / namespace_total if namespace_total else None
        ),
        namespace_p95_pieces=(
            _percentile(ordered_piece_counts, 0.95) if ordered_piece_counts else None
        ),
        namespace_max_pieces=(ordered_piece_counts[-1] if ordered_piece_counts else None),
        namespace_piece_histogram=tuple(
            NamespaceExample(
                piece_count=piece_count,
                unique_count=namespace_piece_histogram[piece_count],
                occurrence_count=namespace_occurrence_histogram[piece_count],
            )
            for piece_count in sorted(namespace_piece_histogram)
        ),
        contains_private_source=True,
        aggregate_only=True,
        redistribution=False,
        external_transmission=False,
        release_eligible=False,
    )


def _candidate_audit(
    *,
    key: str,
    candidate: CandidateAuditConfig,
    binding: SnapshotBinding,
    tokenizer: _Tokenizer,
    theorems: Sequence[_TheoremInput],
    representations: Sequence[_RepresentationInput],
    semantic_sections: Sequence[_SemanticSectionsInput],
    budgets: tuple[int, int],
    symbols: Sequence[str],
    maximum_namespace_piece_bins: int,
) -> tuple[list[TokenizerAuditRecord], CandidateFragmentation, str, bool, int, int]:
    if (
        binding.model_id != candidate.model_id
        or binding.revision != candidate.revision
        or binding.native_max_length != candidate.native_max_length
    ):
        raise TokenizerAuditError(f"candidate and snapshot binding differ for {key}")
    records: list[TokenizerAuditRecord] = []
    texts: list[str] = []
    for theorem, representation, sections in zip(
        theorems, representations, semantic_sections, strict=True
    ):
        if (
            theorem.theorem_id != representation.theorem_id
            or theorem.theorem_id != sections.theorem_id
        ):
            raise TokenizerAuditError("theorem and representation order differs")
        full_text = (
            _MARKER_HEADLESS
            + representation.headless
            + _MARKER_EXPLICIT
            + representation.signature_explicit
        )
        texts.append(representation.headless + "\n" + representation.signature_explicit)
        full_length = len(_tokens(tokenizer, full_text, special=True))
        headless_length = len(_tokens(tokenizer, representation.headless))
        explicit_length = len(_tokens(tokenizer, representation.signature_explicit))
        conclusion_length = len(_tokens(tokenizer, sections.conclusion))
        by_kind = {
            kind: [unit.text for unit in sections.units if unit.kind == kind]
            for kind in ("ordinary_binder", "instance_binder", "prop_hypothesis")
        }
        ordinary_tokens = len(_tokens(tokenizer, "\n".join(by_kind["ordinary_binder"])))
        typeclass_tokens = len(_tokens(tokenizer, "\n".join(by_kind["instance_binder"])))
        hypothesis_tokens = len(_tokens(tokenizer, "\n".join(by_kind["prop_hypothesis"])))
        semantic_minimum = len(
            _tokens(
                tokenizer,
                _MARKER_HEADLESS
                + sections.conclusion
                + _MARKER_EXPLICIT
                + "\n".join(unit.text for unit in sections.units),
                special=True,
            )
        )
        records.append(
            TokenizerAuditRecord(
                candidate=key,
                theorem_id=theorem.theorem_id,
                source=theorem.source,
                full_bundle_tokens=full_length,
                headless_tokens=headless_length,
                signature_explicit_tokens=explicit_length,
                conclusion_tokens=conclusion_length,
                ordinary_binder_tokens=ordinary_tokens,
                typeclass_binder_tokens=typeclass_tokens,
                prop_hypothesis_tokens=hypothesis_tokens,
                semantic_unit_count=len(sections.units),
                semantic_minimum_tokens=semantic_minimum,
                retention=(
                    _retention(
                        budget=budgets[0],
                        full_length=full_length,
                        tokenizer=tokenizer,
                        sections=sections,
                    ),
                    _retention(
                        budget=budgets[1],
                        full_length=full_length,
                        tokenizer=tokenizer,
                        sections=sections,
                    ),
                ),
            )
        )
    fragmentation = _fragmentation(
        candidate=key,
        tokenizer=tokenizer,
        texts=texts,
        symbols=symbols,
        maximum_namespace_piece_bins=maximum_namespace_piece_bins,
    )
    return (
        records,
        fragmentation,
        type(tokenizer).__name__,
        bool(tokenizer.is_fast),
        len(tokenizer),
        int(tokenizer.model_max_length),
    )


def _summary(
    *,
    config: TokenizerAuditConfig,
    config_hash: str,
    code_tree_hash: str,
    snapshot_bindings: Mapping[str, SnapshotBinding],
    tokenizers: Mapping[str, _Tokenizer],
    theorems: Sequence[_TheoremInput],
    records: Sequence[TokenizerAuditRecord],
    fragmentation_payload: bytes,
    runtime: RuntimeVersions,
    input_bindings: Mapping[str, InputBinding],
    tokenizer_metadata: Mapping[str, tuple[str, bool, int, int]],
) -> TokenizerAuditSummary:
    grouped: dict[str, list[TokenizerAuditRecord]] = {key: [] for key in sorted(config.candidates)}
    for record in records:
        grouped[record.candidate].append(record)

    threshold = config.complete_semantics_fraction_at_512
    at_512_passes = True
    for candidate_records in grouped.values():
        if len(candidate_records) != config.inputs.expected_records:
            raise TokenizerAuditError("candidate record denominator differs from frozen profile")
        budget = _budget_summary(candidate_records, 512)
        if budget.conclusion_retained != len(candidate_records):
            at_512_passes = False
        for retained in (
            budget.complete_binder_set_retained,
            budget.complete_typeclass_binder_set_retained,
            budget.complete_hypothesis_set_retained,
        ):
            if retained / len(candidate_records) < threshold:
                at_512_passes = False
    selected: Literal[512, 1024] = 512 if at_512_passes else 1024
    reason = (
        "all candidates retain every conclusion and at least 99% of complete semantic "
        "unit sets at 512"
        if selected == 512
        else "at least one candidate fails the frozen 512-token complete-semantic-retention rule"
    )

    candidate_summaries: list[CandidateSummary] = []
    eligible: list[str] = []
    long_counts: dict[str, int] = {}
    for key in sorted(grouped):
        candidate_records = grouped[key]
        budget_summaries = (
            _budget_summary(candidate_records, config.budgets[0]),
            _budget_summary(candidate_records, config.budgets[1]),
        )
        selected_summary = {item.budget: item for item in budget_summaries}[selected]
        native = config.candidates[key].native_max_length
        is_eligible = native >= selected
        if is_eligible:
            eligible.append(key)
        long_counts[key] = selected_summary.long_input
        by_source: dict[str, LengthStats] = {}
        for source in sorted({record.source for record in candidate_records}):
            by_source[source] = _length_stats(
                [
                    record.full_bundle_tokens
                    for record in candidate_records
                    if record.source == source
                ]
            )
        tokenizer_class, is_fast, vocab_size, reported_max = tokenizer_metadata[key]
        candidate_summaries.append(
            CandidateSummary(
                candidate=key,
                model_id=snapshot_bindings[key].model_id,
                revision=snapshot_bindings[key].revision,
                tokenizer_class=tokenizer_class,
                tokenizer_fast=is_fast,
                tokenizer_vocab_size=vocab_size,
                tokenizer_reported_max_length=reported_max,
                native_max_length=native,
                lengths=_length_stats([record.full_bundle_tokens for record in candidate_records]),
                headless_lengths=_length_stats(
                    [record.headless_tokens for record in candidate_records]
                ),
                signature_explicit_lengths=_length_stats(
                    [record.signature_explicit_tokens for record in candidate_records]
                ),
                by_source=by_source,
                budgets=budget_summaries,
                eligible_at_selected_length=is_eligible,
                ineligibility_reason=(
                    None
                    if is_eligible
                    else f"native context {native} is below selected length {selected}"
                ),
            )
        )
    if not eligible:
        raise TokenizerAuditError("data-only length decision leaves no eligible candidates")
    per_source = dict(sorted(Counter(item.source for item in theorems).items()))
    # Keep the tokenizer mapping in the function contract so a caller cannot
    # accidentally summarize a different candidate set than it loaded.
    if set(tokenizers) != set(grouped):
        raise TokenizerAuditError("loaded tokenizer set differs from candidate registry")
    summary_core = {
        "profile_id": config.profile_id,
        "scientific_winner_selected": False,
        "record_count": len(theorems),
        "per_source": per_source,
        "selected_length": selected,
        "selection_reason": reason,
        "candidate_summaries": [item.model_dump(mode="json") for item in candidate_summaries],
        "eligible_candidates": eligible,
        "long_input_counts": long_counts,
        "contains_private_source": True,
        "redistribution": False,
        "external_transmission": False,
        "release_eligible": False,
    }
    audit_id = _audit_id(
        config_hash=config_hash,
        code_tree_hash=code_tree_hash,
        input_bindings=input_bindings,
        snapshot_bindings=snapshot_bindings,
        runtime=runtime,
        records_payload=_canonical_jsonl(records),
        fragmentation_payload=fragmentation_payload,
        summary_core=summary_core,
    )
    return TokenizerAuditSummary.model_validate({"audit_id": audit_id, **summary_core})


def _validated_repository_root(path: Path) -> Path:
    root = _real_directory(path)
    _regular_file(root / "PLAN.md")
    _regular_file(root / "pyproject.toml")
    expected = root / "src/leanfaith/models/tokenizer_audit.py"
    if Path(__file__).resolve() != expected:
        raise TokenizerAuditError("repository root does not contain executing audit module")
    return root


def _verify_clean_code(code: CodeState) -> str:
    if code.git_dirty or code.code_tree_hash is None or code.untracked_files:
        raise TokenizerAuditError("tokenizer-audit freeze requires a clean tracked code tree")
    return code.code_tree_hash


def _paths_overlap(left: Path, right: Path) -> bool:
    try:
        left.relative_to(right)
        return True
    except ValueError:
        pass
    try:
        right.relative_to(left)
        return True
    except ValueError:
        return False


def _build_payloads(
    *,
    repo_root: Path,
    config: TokenizerAuditConfig,
    config_hash: str,
    code: CodeState,
) -> tuple[dict[str, bytes], TokenizerAuditSummary]:
    code_tree_hash = _verify_clean_code(code)
    theorems, representations, semantic_sections, input_bindings = _load_inputs(config)
    input_bindings["backbone_registry"] = _verify_backbone_registry(repo_root, config)
    snapshots = {
        key: _snapshot_binding(candidate) for key, candidate in sorted(config.candidates.items())
    }
    tokenizers = {key: _load_tokenizer(binding) for key, binding in snapshots.items()}
    all_records: list[TokenizerAuditRecord] = []
    fragmentation: list[CandidateFragmentation] = []
    metadata: dict[str, tuple[str, bool, int, int]] = {}
    for key in sorted(config.candidates):
        records, fragment, tokenizer_class, is_fast, vocab_size, reported_max = _candidate_audit(
            key=key,
            candidate=config.candidates[key],
            binding=snapshots[key],
            tokenizer=tokenizers[key],
            theorems=theorems,
            representations=representations,
            semantic_sections=semantic_sections,
            budgets=config.budgets,
            symbols=config.unicode_symbols,
            maximum_namespace_piece_bins=config.maximum_namespace_piece_bins,
        )
        all_records.extend(records)
        fragmentation.append(fragment)
        metadata[key] = (tokenizer_class, is_fast, vocab_size, reported_max)
    fragmentation_bytes = _fragmentation_payload(fragmentation)
    runtime = _runtime_versions()
    summary = _summary(
        config=config,
        config_hash=config_hash,
        code_tree_hash=code_tree_hash,
        snapshot_bindings=snapshots,
        tokenizers=tokenizers,
        theorems=theorems,
        records=all_records,
        fragmentation_payload=fragmentation_bytes,
        runtime=runtime,
        input_bindings=input_bindings,
        tokenizer_metadata=metadata,
    )
    non_manifest = {
        "fragmentation.json": fragmentation_bytes,
        "records.jsonl": _canonical_jsonl(all_records),
        "summary.json": canonical_json_bytes(summary.model_dump(mode="json")) + b"\n",
    }
    manifest = TokenizerAuditManifest(
        schema_version=2,
        audit_id=summary.audit_id,
        profile_id=config.profile_id,
        config_hash=config_hash,
        config=config,
        code=code,
        repository_root=str(repo_root),
        runtime=runtime,
        inputs=input_bindings,
        snapshots=snapshots,
        selected_length=summary.selected_length,
        scientific_winner_selected=False,
        contains_private_source=True,
        redistribution=False,
        external_transmission=False,
        release_eligible=False,
        output_sha256={
            name: hashlib.sha256(payload).hexdigest() for name, payload in non_manifest.items()
        },
    )
    return {
        **non_manifest,
        "manifest.json": canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    }, summary


def _verify_payloads(output: Path, payloads: Mapping[str, bytes]) -> bool:
    root = _reject_symlinks(output, allow_missing=False)
    if not root.is_dir():
        raise TokenizerAuditError("tokenizer-audit output is not a real directory")
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise TokenizerAuditError("existing tokenizer-audit output file set is not exact")
    for name, payload in sorted(payloads.items()):
        path = root / name
        if path.is_symlink() or _regular_file(path).read_bytes() != payload:
            raise TokenizerAuditError(f"existing tokenizer-audit output differs: {name}")
    return True


def _write_or_replay(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    if set(payloads) != _OUTPUT_FILES:
        raise TokenizerAuditError("tokenizer-audit output payload set is not exact")
    output = _reject_symlinks(output_dir, allow_missing=True)
    if output.exists():
        return _verify_payloads(output, payloads)
    output.parent.mkdir(parents=True, exist_ok=True)
    _real_directory(output.parent)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for name, payload in sorted(payloads.items()):
            path = temporary / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        try:
            os.rename(temporary, output)
        except OSError as exc:
            if exc.errno not in {errno.EEXIST, errno.ENOTEMPTY}:
                raise
            if temporary.exists():
                shutil.rmtree(temporary)
            return _verify_payloads(output, payloads)
        descriptor = os.open(output.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return False


def run_tokenizer_audit(
    *,
    repo_root: Path,
    output_dir: Path,
    config: TokenizerAuditConfig,
    config_hash: str | None = None,
) -> TokenizerAuditArtifacts:
    """Run or exact-replay the frozen data-only tokenizer audit."""

    repo = _validated_repository_root(repo_root)
    output = _reject_symlinks(output_dir, allow_missing=True)
    for path in (
        repo,
        Path(config.inputs.theorem_partition).parent,
        Path(config.inputs.representation_partition).parent,
    ):
        if _paths_overlap(output, path.resolve()):
            raise TokenizerAuditError("output must be disjoint from repository and audit inputs")
    expected_hash = hash_canonical(config.model_dump(mode="json"))
    effective_hash = config_hash or expected_hash
    if effective_hash != expected_hash:
        raise TokenizerAuditError("config hash differs from effective tokenizer-audit config")
    code = collect_code_state(repo)
    payloads, summary = _build_payloads(
        repo_root=repo,
        config=config,
        config_hash=effective_hash,
        code=code,
    )
    replayed = _write_or_replay(output, payloads)
    verify_tokenizer_audit(output, replay=True)
    return TokenizerAuditArtifacts(
        output_dir=output,
        manifest_path=output / "manifest.json",
        audit_id=summary.audit_id,
        selected_length=summary.selected_length,
        eligible_candidates=summary.eligible_candidates,
        replayed=replayed,
    )


def verify_tokenizer_audit(
    output_dir: Path,
    *,
    replay: bool,
) -> TokenizerAuditManifest:
    """Verify artifact hashes and, when requested, reproduce every output byte."""

    root = _reject_symlinks(output_dir, allow_missing=False)
    if not root.is_dir():
        raise TokenizerAuditError("tokenizer-audit output is not a real directory")
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise TokenizerAuditError("tokenizer-audit output file set is not exact")
    if any(path.is_symlink() for path in root.iterdir()):
        raise TokenizerAuditError("tokenizer-audit output contains a symlink")
    manifest = TokenizerAuditManifest.model_validate(_strict_json(root / "manifest.json"))
    for name, expected in manifest.output_sha256.items():
        if hash_file(_regular_file(root / name)) != expected:
            raise TokenizerAuditError(f"tokenizer-audit output hash differs: {name}")
    summary = TokenizerAuditSummary.model_validate(_strict_json(root / "summary.json"))
    _FragmentationArtifact.model_validate(_strict_json(root / "fragmentation.json"))
    record_rows = _read_jsonl(root / "records.jsonl")
    try:
        records = [TokenizerAuditRecord.model_validate(row) for row in record_rows]
    except ValueError as exc:
        raise TokenizerAuditError("invalid tokenizer-audit record partition") from exc
    expected_record_count = manifest.config.inputs.expected_records * len(_CANDIDATE_KEYS)
    if len(records) != expected_record_count:
        raise TokenizerAuditError("tokenizer-audit record denominator differs")
    candidate_counts = Counter(record.candidate for record in records)
    if candidate_counts != Counter(
        dict.fromkeys(_CANDIDATE_KEYS, manifest.config.inputs.expected_records)
    ):
        raise TokenizerAuditError("tokenizer-audit per-candidate denominator differs")
    if manifest.code.code_tree_hash is None:
        raise TokenizerAuditError("tokenizer-audit manifest lacks code-tree identity")
    summary_core = summary.model_dump(mode="json", exclude={"audit_id"})
    expected_audit_id = _audit_id(
        config_hash=manifest.config_hash,
        code_tree_hash=manifest.code.code_tree_hash,
        input_bindings=manifest.inputs,
        snapshot_bindings=manifest.snapshots,
        runtime=manifest.runtime,
        records_payload=(root / "records.jsonl").read_bytes(),
        fragmentation_payload=(root / "fragmentation.json").read_bytes(),
        summary_core=summary_core,
    )
    if (
        summary.audit_id != manifest.audit_id
        or summary.audit_id != expected_audit_id
        or summary.selected_length != manifest.selected_length
        or summary.scientific_winner_selected
    ):
        raise TokenizerAuditError("tokenizer-audit summary differs from manifest")
    if replay:
        repo = _validated_repository_root(Path(manifest.repository_root))
        code = collect_code_state(repo)
        if code != manifest.code:
            raise TokenizerAuditError("repository code state differs from frozen audit")
        if _runtime_versions() != manifest.runtime:
            raise TokenizerAuditError("tokenizer runtime differs from frozen audit")
        payloads, replay_summary = _build_payloads(
            repo_root=repo,
            config=manifest.config,
            config_hash=manifest.config_hash,
            code=code,
        )
        if replay_summary != summary:
            raise TokenizerAuditError("tokenizer-audit summary differs from exact replay")
        _verify_payloads(root, payloads)
    return manifest


__all__ = [
    "BudgetRetention",
    "CandidateAuditConfig",
    "CandidateSummary",
    "TokenizerAuditArtifacts",
    "TokenizerAuditConfig",
    "TokenizerAuditError",
    "TokenizerAuditManifest",
    "TokenizerAuditRecord",
    "TokenizerAuditSummary",
    "load_tokenizer_audit_config",
    "run_tokenizer_audit",
    "verify_tokenizer_audit",
]
