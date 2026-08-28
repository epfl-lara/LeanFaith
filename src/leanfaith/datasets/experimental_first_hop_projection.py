"""Replay-safe projection of the complete deterministic first-hop inventory.

This module is an intentionally narrow bridge between the frozen deterministic
transformation audit and later *experimental-only* mixed proxy corpora.  It
does not create semantic labels, silver labels, split assignments, or a new
transformation verdict.  Instead it:

* replays the exact provisional-pair audit and certificate-backed E2 seed set;
* retains every exact unique first-hop pair in an inventory;
* marks only seed-bound P14--P18 positives as E2 and clean N11--N18 intentions
  as D0;
* re-runs the active benchmark denylist on both sides of every pair;
* projects one name/proof-free ``normalized_headless_text_v1`` view; and
* publishes the clean E2/D0 subset separately for opt-in proxy experiments.

The current pinned source artifact contains 11,208 exact unique pairs.  Counts
are nevertheless configuration-bound rather than hidden constants so fixture
artifacts and future frozen source revisions can be tested without weakening
the replay boundary.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Self, cast

from pydantic import Field, field_validator, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file, sha256_hex
from leanfaith.config.models import StrictModel
from leanfaith.datasets.denylist import (
    ActiveBenchmarkRegistry,
    load_active_benchmark_registry,
)
from leanfaith.representations.views import normalize_headless, signature_near_dup_hash
from leanfaith.schemas.manifest import CodeState, collect_code_state
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_seed import (
    CompositionSeedManifest,
    CompositionSeedRecord,
)
from leanfaith.transforms.provisional_pair_combine import (
    MaterializationRootBinding,
    ProvisionalPairCombinationManifest,
    ProvisionalPairObservation,
    UniqueProvisionalPair,
    _canonical_line,
    _iter_jsonl_objects,
    _load_canonical_model,
    _parse_json_object,
    _result_model,
)

if TYPE_CHECKING:
    from leanfaith.datasets.experimental_machine_supervision import (
        ExperimentalMachineSupervisionRecord,
    )

_HEX64 = r"^[0-9a-f]{64}$"
_PROJECTION_ID = r"^experimental_first_hop_projection:[0-9a-f]{64}$"
_RECORD_ID = r"^experimental_first_hop_pair:[0-9a-f]{64}$"
_OUTPUT_FILES = frozenset(
    {
        "inventory.jsonl",
        "selectable.jsonl",
        "excluded.jsonl",
        "summary.json",
        "manifest.json",
    }
)
_REPRESENTATION_ID_BYTES = re.compile(rb'"representation_id"\s*:\s*"([^"]+)"')
_PROOF_PLACEHOLDER_TAIL = re.compile(r"\s*:=\s*(?:by\s+sorry|sorry)?\s*$")
_NONREC_DECLARATION_MODIFIER = re.compile(r"\bnonrec\s+(?=(?:theorem|lemma)\b)")

PseudoTarget = Literal["same_claim", "not_same_claim"]
EvidenceTier = Literal["E2", "D0"]
SourceCategory = Literal["mathlib", "sft_classic"]
SelectionStatus = Literal["selectable", "excluded"]
ExclusionReason = Literal[
    "benchmark_overlap_candidate",
    "benchmark_overlap_source",
    "conflicting_intentions",
    "headless_normalization_failed",
    "headless_representation_mismatch",
    "missing_required_representation_view",
    "mixed_or_unsupported_source",
    "multiple_families",
    "non_unary_pair",
    "unsupported_evidence_tier",
]

_DEFAULT_E2_FAMILIES = (
    "p14_independent_binder_permutation",
    "p15_root_iff_reversal",
    "p16_conjunction_reassociation",
    "p17_hypothesis_packing",
    "p18_root_equality_symmetry",
)
_DEFAULT_D0_FAMILIES = (
    "n11_bound_variable_substitution",
    "n12_implication_converse",
    "n13_witness_dependency",
    "n14_negation_scope",
    "n15_conjunct_omission",
    "n16_domain_guard_removal",
    "n17_role_sensitive_arguments",
    "n18_root_equality_polarity",
)


class ExperimentalFirstHopProjectionError(ValueError):
    """A source binding, evidence tier, projection, or replay failed closed."""


class ExperimentalFirstHopInputBinding(StrictModel):
    """Exact path/hash/size binding for one source artifact."""

    path: str = Field(min_length=1)
    sha256: str = Field(pattern=_HEX64)
    byte_count: int = Field(ge=0, strict=True)


class ExperimentalFirstHopProjectionConfig(StrictModel):
    """Frozen policy and source hashes for one complete first-hop projection."""

    schema_version: Literal[1] = 1
    profile_id: str = Field(min_length=1)
    expected_gross_observation_count: int = Field(gt=0, strict=True)
    expected_unique_pair_count: int = Field(gt=0, strict=True)
    expected_counts_by_source: dict[str, int]
    admitted_source_categories: tuple[SourceCategory, ...] = ("mathlib", "sft_classic")
    e2_positive_families: tuple[str, ...] = _DEFAULT_E2_FAMILIES
    d0_negative_families: tuple[str, ...] = _DEFAULT_D0_FAMILIES
    audit_manifest_sha256: str = Field(pattern=_HEX64)
    audit_gross_observations_sha256: str = Field(pattern=_HEX64)
    audit_unique_pairs_sha256: str = Field(pattern=_HEX64)
    positive_seed_manifest_sha256: str = Field(pattern=_HEX64)
    positive_seed_records_sha256: str = Field(pattern=_HEX64)
    positive_seed_theorems_sha256: str = Field(pattern=_HEX64)
    positive_seed_representations_sha256: str = Field(pattern=_HEX64)
    benchmark_manifest_sha256: str = Field(pattern=_HEX64)
    benchmark_active_registry_sha256: str = Field(pattern=_HEX64)
    benchmark_authorization_sha256: str = Field(pattern=_HEX64)

    @field_validator(
        "admitted_source_categories",
        "e2_positive_families",
        "d0_negative_families",
    )
    @classmethod
    def _tuple_is_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or value != tuple(sorted(set(value))):
            raise ValueError("projection policy tuples must be nonempty, sorted, and unique")
        return value

    @field_validator("expected_counts_by_source")
    @classmethod
    def _source_counts_are_canonical(cls, value: dict[str, int]) -> dict[str, int]:
        if not value or list(value) != sorted(value):
            raise ValueError("expected source counts must be nonempty and key-sorted")
        if any(key not in {"mathlib", "sft_classic"} or count <= 0 for key, count in value.items()):
            raise ValueError("expected source counts contain an unsupported source or count")
        return value

    @model_validator(mode="after")
    def _policy_reconciles(self) -> Self:
        if self.expected_unique_pair_count > self.expected_gross_observation_count:
            raise ValueError("unique-pair count exceeds gross observations")
        if sum(self.expected_counts_by_source.values()) != self.expected_unique_pair_count:
            raise ValueError("expected source counts do not sum to unique-pair count")
        if set(self.expected_counts_by_source) != set(self.admitted_source_categories):
            raise ValueError("expected source counts differ from admitted source categories")
        if set(self.e2_positive_families) & set(self.d0_negative_families):
            raise ValueError("E2 and D0 family registries overlap")
        if any(not family.startswith("p") for family in self.e2_positive_families):
            raise ValueError("E2 family IDs must begin with p")
        if any(not family.startswith("n") for family in self.d0_negative_families):
            raise ValueError("D0 family IDs must begin with n")
        return self


class ExperimentalFirstHopStatementView(StrictModel):
    """A compact, proof/name-free statement projection plus audit hashes."""

    normalization_version: Literal["normalized_headless_text_v1"] = "normalized_headless_text_v1"
    theorem_id: str = Field(min_length=1)
    representation_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    statement_content_hash: str = Field(pattern=_HEX64)
    representation_content_hash: str = Field(pattern=_HEX64)
    alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    normalized_headless_text_v1: str = Field(min_length=1)
    normalized_headless_sha256: str = Field(pattern=_HEX64)
    signature_pp_sha256: str | None = Field(default=None, pattern=_HEX64)
    signature_explicit_sha256: str | None = Field(default=None, pattern=_HEX64)

    @model_validator(mode="after")
    def _view_reconciles(self) -> Self:
        text = self.normalized_headless_text_v1
        if not text.strip() or "\x00" in text:
            raise ValueError("normalized headless text must be safe and nonempty")
        if self.normalized_headless_sha256 != sha256_hex(text.encode("utf-8")):
            raise ValueError("normalized headless hash differs from text")
        if _PROOF_PLACEHOLDER_TAIL.search(text):
            raise ValueError("normalized headless text retains a proof placeholder")
        return self


class ExperimentalFirstHopProjectionRecord(StrictModel):
    """One exact first-hop pair; its target is explicitly only a proxy."""

    schema_version: Literal[1] = 1
    projection_record_id: str = Field(pattern=_RECORD_ID)
    unique_pair_id: str = Field(min_length=1)
    exact_pair_key: str = Field(pattern=_HEX64)
    observation_ids: tuple[str, ...] = Field(min_length=1)
    selected_observation_id: str = Field(min_length=1)
    provenance_count: int = Field(ge=1, strict=True)
    root_binding_id: str = Field(min_length=1)
    result_id: str = Field(min_length=1)
    result_line_number: int = Field(ge=1, strict=True)
    pair_id: str = Field(min_length=1)
    family_ids: tuple[str, ...] = Field(min_length=1)
    rule_id: str = Field(min_length=1)
    intended_relations: tuple[str, ...] = Field(min_length=1)
    source_category: SourceCategory
    source_root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    evidence_tier: EvidenceTier | None = None
    pseudo_target: PseudoTarget | None = None
    certificate_kind: str | None = None
    certificate_sha256: str | None = Field(default=None, pattern=_HEX64)
    selection_status: SelectionStatus
    exclusion_reasons: tuple[ExclusionReason, ...] = ()
    source: ExperimentalFirstHopStatementView | None
    candidate: ExperimentalFirstHopStatementView | None
    private_source_content: bool
    redistribution_allowed: bool
    external_transmission_allowed: bool
    release_eligible: bool
    benchmark_screened_source: Literal[True] = True
    benchmark_screened_candidate: Literal[True] = True
    quality_tier: Literal["provisional"] = "provisional"
    pseudo_target_basis: Literal["deterministic_transformation_intention"] = (
        "deterministic_transformation_intention"
    )
    semantic_label_id: None = None
    semantic_label: Literal[False] = False
    human_label: Literal[False] = False
    silver_record: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    machine_proxy_only: Literal[True] = True
    experimental_mixed_input_eligible: bool
    scientific_training_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False
    split_assignment_id: None = None

    @field_validator(
        "observation_ids",
        "family_ids",
        "intended_relations",
        "source_root_ancestry_ids",
        "exclusion_reasons",
    )
    @classmethod
    def _tuples_are_canonical(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if value != tuple(sorted(set(value))):
            raise ValueError("projection tuple fields must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _record_reconciles(self) -> Self:
        if self.provenance_count != len(self.observation_ids):
            raise ValueError("provenance count differs from observation IDs")
        if self.selected_observation_id not in self.observation_ids:
            raise ValueError("selected observation is absent from provenance")
        if (
            self.source is not None
            and self.candidate is not None
            and self.source.context_id != self.candidate.context_id
        ):
            raise ValueError("source and candidate contexts differ")
        if self.source_category == "sft_classic":
            if not self.private_source_content:
                raise ValueError("sft_classic content must remain private")
            if any(
                (
                    self.redistribution_allowed,
                    self.external_transmission_allowed,
                    self.release_eligible,
                )
            ):
                raise ValueError("private source policy permits an unsafe use")
        elif self.private_source_content:
            raise ValueError("mathlib content cannot be marked private")
        if self.evidence_tier == "E2":
            if self.pseudo_target != "same_claim":
                raise ValueError("E2 requires the positive proxy target")
            if self.intended_relations != ("equivalent",):
                raise ValueError("E2 requires one equivalent intention")
            if self.certificate_kind is None or self.certificate_sha256 is None:
                raise ValueError("E2 requires an exact certificate binding")
        elif self.evidence_tier == "D0":
            if self.pseudo_target != "not_same_claim":
                raise ValueError("D0 requires the negative proxy target")
            if self.intended_relations != ("near_miss",):
                raise ValueError("D0 requires one near-miss intention")
            if self.certificate_kind is not None or self.certificate_sha256 is not None:
                raise ValueError("D0 cannot claim a positive certificate")
        elif any(
            value is not None
            for value in (self.pseudo_target, self.certificate_kind, self.certificate_sha256)
        ):
            raise ValueError("unsupported evidence cannot expose a proxy target/certificate")
        eligible = self.selection_status == "selectable"
        if eligible and (self.source is None or self.candidate is None):
            raise ValueError("selectable records require both statement views")
        if (self.source is None or self.candidate is None) and not {
            "headless_normalization_failed",
            "missing_required_representation_view",
        }.intersection(self.exclusion_reasons):
            raise ValueError("missing statement view lacks an explicit projection exclusion")
        if eligible != self.experimental_mixed_input_eligible:
            raise ValueError("selection status differs from mixed-input eligibility")
        if eligible != (self.evidence_tier is not None and not self.exclusion_reasons):
            raise ValueError("selection status differs from evidence/exclusion policy")
        if not eligible and not self.exclusion_reasons:
            raise ValueError("excluded record lacks a machine-readable reason")
        expected = "experimental_first_hop_pair:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "projection_record_id")
        )
        if self.projection_record_id != expected:
            raise ValueError("projection record ID differs from canonical content")
        return self


class ExperimentalFirstHopProjectionSummary(StrictModel):
    schema_version: Literal[1] = 1
    projection_id: str = Field(pattern=_PROJECTION_ID)
    profile_id: str = Field(min_length=1)
    inventory_count: int = Field(gt=0, strict=True)
    selectable_count: int = Field(ge=0, strict=True)
    excluded_count: int = Field(ge=0, strict=True)
    counts_by_source: dict[str, int]
    selectable_counts_by_source: dict[str, int]
    counts_by_family: dict[str, int]
    counts_by_evidence_tier: dict[str, int]
    counts_by_pseudo_target: dict[str, int]
    counts_by_exclusion_reason: dict[str, int]
    semantic_label_count: Literal[0] = 0
    human_label_count: Literal[0] = 0
    silver_record_count: Literal[0] = 0
    split_assignment_count: Literal[0] = 0
    use_note: Literal["experimental first-hop proxy projection only; not semantic ground truth"] = (
        "experimental first-hop proxy projection only; not semantic ground truth"
    )

    @model_validator(mode="after")
    def _counts_reconcile(self) -> Self:
        if self.selectable_count + self.excluded_count != self.inventory_count:
            raise ValueError("selectable and excluded counts do not reconcile")
        if sum(self.counts_by_source.values()) != self.inventory_count:
            raise ValueError("source counts do not reconcile")
        if sum(self.counts_by_family.values()) < self.inventory_count:
            raise ValueError("family counts cannot be smaller than inventory")
        if sum(self.selectable_counts_by_source.values()) != self.selectable_count:
            raise ValueError("selectable source counts do not reconcile")
        if sum(self.counts_by_evidence_tier.values()) != self.selectable_count:
            raise ValueError("selectable evidence counts do not reconcile")
        if sum(self.counts_by_pseudo_target.values()) != self.selectable_count:
            raise ValueError("selectable target counts do not reconcile")
        return self


class ExperimentalFirstHopProjectionManifest(StrictModel):
    """Content-addressed manifest for the complete projection."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["experimental_deterministic_first_hop_projection_v1"] = (
        "experimental_deterministic_first_hop_projection_v1"
    )
    projection_id: str = Field(pattern=_PROJECTION_ID)
    profile_id: str = Field(min_length=1)
    config_hash: str = Field(pattern=_HEX64)
    config: ExperimentalFirstHopProjectionConfig
    code: CodeState
    inputs: dict[str, ExperimentalFirstHopInputBinding]
    inventory_count: int = Field(gt=0, strict=True)
    selectable_count: int = Field(ge=0, strict=True)
    excluded_count: int = Field(ge=0, strict=True)
    output_sha256: dict[str, str]
    model_input_profile: Literal["normalized_headless_text_v1"] = "normalized_headless_text_v1"
    split_assignments_created: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    human_labels_created: Literal[False] = False
    silver_records_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    scientific_training_eligible: Literal[False] = False
    model_selection_eligible: Literal[False] = False
    calibration_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False
    required_opt_in_flag: Literal["--allow-experimental-first-hop-projection"] = (
        "--allow-experimental-first-hop-projection"
    )

    @model_validator(mode="after")
    def _manifest_reconciles(self) -> Self:
        if self.profile_id != self.config.profile_id:
            raise ValueError("manifest profile differs from config")
        if self.config_hash != hash_canonical(self.config.model_dump(mode="json")):
            raise ValueError("manifest config hash differs from embedded config")
        if self.code.git_dirty or self.code.code_tree_hash is None or self.code.untracked_files:
            raise ValueError("projection requires a clean, fully tracked code tree")
        if not self.inputs or list(self.inputs) != sorted(self.inputs):
            raise ValueError("manifest inputs must be nonempty and key-sorted")
        if set(self.output_sha256) != _OUTPUT_FILES - {"manifest.json"}:
            raise ValueError("manifest does not bind the exact non-manifest outputs")
        if self.selectable_count + self.excluded_count != self.inventory_count:
            raise ValueError("manifest counts do not reconcile")
        return self


@dataclass(frozen=True, slots=True)
class ExperimentalFirstHopProjectionArtifacts:
    output_dir: Path
    manifest_path: Path
    inventory_path: Path
    selectable_path: Path
    excluded_path: Path
    summary_path: Path
    projection_id: str
    inventory_count: int
    selectable_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class MathlibV1DifferentialResult:
    compared_count: int
    projection_inventory_count: int
    v1_record_count: int


@dataclass(frozen=True, slots=True)
class _Locator:
    unique: UniqueProvisionalPair
    observation: ProvisionalPairObservation
    root: MaterializationRootBinding
    evidence_tier: EvidenceTier | None
    pseudo_target: PseudoTarget | None
    seed: CompositionSeedRecord | None
    initial_exclusions: tuple[ExclusionReason, ...]


@dataclass(frozen=True, slots=True)
class _SideMaterial:
    theorem: TheoremRecord
    representation: RepresentationRecord


@dataclass(frozen=True, slots=True)
class _PreparedSide:
    theorem_id: str
    representation_id: str
    theorem_source: str
    context_id: str
    root_ancestry_ids: tuple[str, ...]
    view: ExperimentalFirstHopStatementView | None
    projection_reasons: tuple[ExclusionReason, ...]
    benchmark_protected: bool


def _without_id(payload: Mapping[str, object], field: str) -> dict[str, object]:
    output = dict(payload)
    output.pop(field, None)
    return output


def _lexical_absolute(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink_components(path: Path, *, allow_missing: bool) -> Path:
    absolute = _lexical_absolute(path)
    current = Path(absolute.anchor)
    for index, part in enumerate(absolute.parts[1:], start=1):
        current /= part
        try:
            metadata = os.lstat(current)
        except FileNotFoundError:
            if allow_missing:
                break
            raise ExperimentalFirstHopProjectionError(
                f"required path is absent: {current}"
            ) from None
        except OSError as exc:
            raise ExperimentalFirstHopProjectionError(
                f"cannot inspect path component {current}: {exc}"
            ) from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise ExperimentalFirstHopProjectionError(f"path contains a symlink: {current}")
        if index < len(absolute.parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise ExperimentalFirstHopProjectionError(
                f"path parent component is not a directory: {current}"
            )
    return absolute


def _regular_file(path: Path) -> Path:
    safe = _reject_symlink_components(path, allow_missing=False)
    if not safe.is_file():
        raise ExperimentalFirstHopProjectionError(f"input is not a regular file: {safe}")
    return safe


def _real_directory(path: Path) -> Path:
    safe = _reject_symlink_components(path, allow_missing=False)
    if not safe.is_dir():
        raise ExperimentalFirstHopProjectionError(f"input is not a directory: {safe}")
    return safe


def _require_hash(path: Path, expected: str, *, field: str) -> Path:
    safe = _regular_file(path)
    observed = hash_file(safe)
    if observed != expected:
        raise ExperimentalFirstHopProjectionError(
            f"{field} hash differs: expected {expected}, observed {observed}"
        )
    return safe


def _binding(path: Path) -> ExperimentalFirstHopInputBinding:
    safe = _regular_file(path)
    return ExperimentalFirstHopInputBinding(
        path=str(safe),
        sha256=hash_file(safe),
        byte_count=safe.stat().st_size,
    )


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_line(record) for record in records)


def _load_canonical_jsonl[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
) -> tuple[ModelT, ...]:
    records: list[ModelT] = []
    try:
        for line_number, raw, raw_line in _iter_jsonl_objects(path):
            try:
                record = model.model_validate(raw)
            except ValueError as exc:
                raise ExperimentalFirstHopProjectionError(
                    f"invalid {model.__name__} at {path}:{line_number}: {exc}"
                ) from exc
            if raw_line != _canonical_line(record):
                raise ExperimentalFirstHopProjectionError(
                    f"non-canonical {model.__name__} at {path}:{line_number}"
                )
            records.append(record)
    except ExperimentalFirstHopProjectionError:
        raise
    except Exception as exc:
        raise ExperimentalFirstHopProjectionError(f"cannot load JSONL {path}: {exc}") from exc
    return tuple(records)


def _load_audit(
    audit_dir: Path,
    config: ExperimentalFirstHopProjectionConfig,
) -> tuple[
    ProvisionalPairCombinationManifest,
    tuple[ProvisionalPairObservation, ...],
    tuple[UniqueProvisionalPair, ...],
]:
    root = _real_directory(audit_dir)
    manifest_path = _require_hash(
        root / "manifest.json", config.audit_manifest_sha256, field="audit manifest"
    )
    gross_path = _require_hash(
        root / "gross_observations.jsonl",
        config.audit_gross_observations_sha256,
        field="audit gross observations",
    )
    unique_path = _require_hash(
        root / "unique_pairs.jsonl",
        config.audit_unique_pairs_sha256,
        field="audit unique pairs",
    )
    try:
        manifest = _load_canonical_model(manifest_path, ProvisionalPairCombinationManifest)
    except Exception as exc:
        raise ExperimentalFirstHopProjectionError(f"invalid audit manifest: {exc}") from exc
    if (
        manifest.gross_output_sha256 != config.audit_gross_observations_sha256
        or manifest.unique_output_sha256 != config.audit_unique_pairs_sha256
    ):
        raise ExperimentalFirstHopProjectionError("audit manifest does not bind its outputs")
    gross = _load_canonical_jsonl(gross_path, ProvisionalPairObservation)
    unique = _load_canonical_jsonl(unique_path, UniqueProvisionalPair)
    if (
        len(gross) != manifest.gross_observation_count
        or len(gross) != config.expected_gross_observation_count
    ):
        raise ExperimentalFirstHopProjectionError("gross observation count differs")
    if (
        len(unique) != manifest.unique_pair_count
        or len(unique) != config.expected_unique_pair_count
    ):
        raise ExperimentalFirstHopProjectionError("unique pair count differs")
    if manifest.unique_counts_by_source != config.expected_counts_by_source:
        raise ExperimentalFirstHopProjectionError("audit source counts differ from config")
    return manifest, gross, unique


def _validate_seed_sidecar[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
    *,
    id_field: str,
) -> set[str]:
    identities: set[str] = set()
    for record in _load_canonical_jsonl(path, model):
        identity = getattr(record, id_field)
        if identity in identities:
            raise ExperimentalFirstHopProjectionError(
                f"duplicate {id_field} in positive-seed sidecar: {identity}"
            )
        identities.add(identity)
    return identities


def _load_seeds(
    seed_dir: Path,
    config: ExperimentalFirstHopProjectionConfig,
) -> tuple[CompositionSeedManifest, dict[str, CompositionSeedRecord]]:
    root = _real_directory(seed_dir)
    manifest_path = _require_hash(
        root / "manifest.json",
        config.positive_seed_manifest_sha256,
        field="positive-seed manifest",
    )
    records_path = _require_hash(
        root / "seeds.jsonl",
        config.positive_seed_records_sha256,
        field="positive-seed records",
    )
    theorem_path = _require_hash(
        root / "theorems.jsonl",
        config.positive_seed_theorems_sha256,
        field="positive-seed theorem sidecar",
    )
    representation_path = _require_hash(
        root / "representations.jsonl",
        config.positive_seed_representations_sha256,
        field="positive-seed representation sidecar",
    )
    try:
        manifest = _load_canonical_model(manifest_path, CompositionSeedManifest)
    except Exception as exc:
        raise ExperimentalFirstHopProjectionError(f"invalid seed manifest: {exc}") from exc
    if (
        manifest.seed_output_sha256 != config.positive_seed_records_sha256
        or manifest.theorem_output_sha256 != config.positive_seed_theorems_sha256
        or manifest.representation_output_sha256 != config.positive_seed_representations_sha256
    ):
        raise ExperimentalFirstHopProjectionError("seed manifest does not bind its outputs")
    seeds = _load_canonical_jsonl(records_path, CompositionSeedRecord)
    if len(seeds) != manifest.seed_count:
        raise ExperimentalFirstHopProjectionError("seed count differs from manifest")
    by_pair: dict[str, CompositionSeedRecord] = {}
    for seed in seeds:
        if seed.unique_pair_id in by_pair:
            raise ExperimentalFirstHopProjectionError(
                f"duplicate seed unique_pair_id: {seed.unique_pair_id}"
            )
        by_pair[seed.unique_pair_id] = seed
    theorem_ids = _validate_seed_sidecar(theorem_path, TheoremRecord, id_field="theorem_id")
    representation_theorem_ids = _validate_seed_sidecar(
        representation_path, RepresentationRecord, id_field="theorem_id"
    )
    expected_theorem_ids = {seed.intermediate_theorem_id for seed in seeds}
    if theorem_ids != expected_theorem_ids or representation_theorem_ids != expected_theorem_ids:
        raise ExperimentalFirstHopProjectionError(
            "positive-seed theorem/representation sidecars differ from seed records"
        )
    return manifest, by_pair


def _load_benchmark_registry(
    repo_root: Path,
    config: ExperimentalFirstHopProjectionConfig,
) -> ActiveBenchmarkRegistry:
    authorization_path = _require_hash(
        repo_root / "reports/gates/lf_016_authorization.json",
        config.benchmark_authorization_sha256,
        field="benchmark authorization",
    )
    manifest_path = repo_root / "data/benchmarks/manifests/representation_signatures_v1.json"
    registry = load_active_benchmark_registry(
        manifest_path,
        repo_root=repo_root,
        expected_manifest_sha256=config.benchmark_manifest_sha256,
        authorization_path=authorization_path,
    )
    if registry.manifest.active_registry.sha256 != config.benchmark_active_registry_sha256:
        raise ExperimentalFirstHopProjectionError("active benchmark registry hash differs")
    return registry


def _seed_matches_observation(
    seed: CompositionSeedRecord,
    observation: ProvisionalPairObservation,
    unique: UniqueProvisionalPair,
    manifest: ProvisionalPairCombinationManifest,
) -> bool:
    return (
        seed.input_combination_hash == manifest.combination_hash
        and seed.unique_pair_id == unique.unique_pair_id
        and seed.exact_pair_key == unique.exact_pair_key
        and seed.selected_observation_id == observation.observation_id
        and seed.first_hop_observation_ids == unique.observation_ids
        and seed.first_hop_root_binding_id == observation.root_binding_id
        and seed.first_hop_result_id == observation.result_id
        and seed.first_hop_result_line_number == observation.result_line_number
        and seed.first_hop_profile_id == observation.profile_id
        and seed.first_hop_rule_id == observation.rule_id
        and seed.first_hop_family_id == observation.family_id
        and seed.first_hop_attempt_id == observation.attempt_id
        and seed.first_hop_draft_id == observation.draft_id
        and seed.first_hop_audit_id == observation.audit_id
        and seed.first_hop_variant_id == observation.variant_id
        and seed.source_theorem_id == observation.source_theorem_ids[0]
        and seed.source_representation_id == observation.source_representation_ids[0]
        and seed.intermediate_theorem_id == observation.candidate_theorem_id
        and seed.intermediate_representation_id == observation.candidate_representation_id
        and seed.context_id == observation.context_id
        and seed.root_ancestry_ids == observation.source_root_ancestry_ids
        and seed.intermediate_candidate_code_hash == observation.candidate_code_hash
        and seed.intermediate_alpha_identity_fingerprint
        == observation.candidate_alpha_identity_fingerprint
    )


def _build_locators(
    manifest: ProvisionalPairCombinationManifest,
    gross: Sequence[ProvisionalPairObservation],
    unique: Sequence[UniqueProvisionalPair],
    seeds: Mapping[str, CompositionSeedRecord],
    config: ExperimentalFirstHopProjectionConfig,
) -> tuple[_Locator, ...]:
    observations = {record.observation_id: record for record in gross}
    if len(observations) != len(gross):
        raise ExperimentalFirstHopProjectionError("gross audit repeats an observation ID")
    represented_observations = {
        observation_id for pair in unique for observation_id in pair.observation_ids
    }
    if represented_observations != set(observations):
        raise ExperimentalFirstHopProjectionError(
            "unique-pair provenance does not cover the gross audit exactly"
        )
    roots = {binding.root_binding_id: binding for binding in manifest.root_bindings}
    if len(roots) != len(manifest.root_bindings):
        raise ExperimentalFirstHopProjectionError("audit manifest repeats a root binding")
    e2_families = set(config.e2_positive_families)
    d0_families = set(config.d0_negative_families)
    expected_seed_pairs: set[str] = set()
    locators: list[_Locator] = []
    for pair in unique:
        reasons: set[ExclusionReason] = set()
        if pair.conflicting_intentions:
            reasons.add("conflicting_intentions")
        if len(pair.family_ids) != 1:
            reasons.add("multiple_families")
        if len(pair.source_theorem_ids) != 1:
            reasons.add("non_unary_pair")
        if (
            len(pair.source_categories) != 1
            or pair.source_categories[0] not in config.admitted_source_categories
        ):
            reasons.add("mixed_or_unsupported_source")
        family = pair.family_ids[0] if len(pair.family_ids) == 1 else None
        evidence_tier: EvidenceTier | None = None
        pseudo_target: PseudoTarget | None = None
        seed = seeds.get(pair.unique_pair_id)
        if (
            not reasons
            and family in e2_families
            and pair.intended_relations == ("equivalent",)
            and pair.polarity_metadata == ("positive",)
        ):
            expected_seed_pairs.add(pair.unique_pair_id)
            if seed is None:
                raise ExperimentalFirstHopProjectionError(
                    f"certificate-backed E2 family lacks a seed: {pair.unique_pair_id}"
                )
            selected_id = seed.selected_observation_id
            evidence_tier = "E2"
            pseudo_target = "same_claim"
        elif (
            not reasons
            and family in d0_families
            and pair.intended_relations == ("near_miss",)
            and pair.polarity_metadata == ("negative",)
        ):
            if seed is not None:
                raise ExperimentalFirstHopProjectionError(
                    f"D0 intention unexpectedly has an E2 seed: {pair.unique_pair_id}"
                )
            selected_id = min(pair.observation_ids)
            evidence_tier = "D0"
            pseudo_target = "not_same_claim"
        else:
            selected_id = min(pair.observation_ids)
            reasons.add("unsupported_evidence_tier")
        if selected_id not in pair.observation_ids:
            raise ExperimentalFirstHopProjectionError(
                f"selected observation is absent from pair: {pair.unique_pair_id}"
            )
        observation = observations[selected_id]
        if (
            observation.exact_pair_key != pair.exact_pair_key
            or observation.context_id != pair.context_id
            or observation.source_theorem_ids != pair.source_theorem_ids
            or observation.source_categories != pair.source_categories
            or observation.family_id not in pair.family_ids
            or observation.intended_relation not in pair.intended_relations
            or observation.candidate_code_hash != pair.candidate_code_hash
            or observation.candidate_alpha_identity_fingerprint is None
            or observation.alpha_candidate_key is None
        ):
            raise ExperimentalFirstHopProjectionError(
                f"selected observation differs from unique pair: {pair.unique_pair_id}"
            )
        root = roots.get(observation.root_binding_id)
        if root is None:
            raise ExperimentalFirstHopProjectionError(
                f"observation references unknown root: {observation.root_binding_id}"
            )
        if root.context_id != observation.context_id:
            raise ExperimentalFirstHopProjectionError("root and observation contexts differ")
        if evidence_tier == "E2":
            assert seed is not None
            if root.run_kind != "e2" or not _seed_matches_observation(
                seed, observation, pair, manifest
            ):
                raise ExperimentalFirstHopProjectionError(
                    f"E2 seed/root lineage differs: {pair.unique_pair_id}"
                )
        if evidence_tier == "D0" and root.run_kind != "d0":
            raise ExperimentalFirstHopProjectionError(
                f"D0 pair is not bound to a D0 root: {pair.unique_pair_id}"
            )
        locators.append(
            _Locator(
                unique=pair,
                observation=observation,
                root=root,
                evidence_tier=evidence_tier,
                pseudo_target=pseudo_target,
                seed=seed if evidence_tier == "E2" else None,
                initial_exclusions=tuple(sorted(reasons)),
            )
        )
    if set(seeds) != expected_seed_pairs:
        extra = sorted(set(seeds) - expected_seed_pairs)
        missing = sorted(expected_seed_pairs - set(seeds))
        raise ExperimentalFirstHopProjectionError(
            f"seed set differs from clean E2 pairs: extra={extra[:3]}, missing={missing[:3]}"
        )
    return tuple(locators)


def _source_targets(
    locators: Sequence[_Locator],
) -> tuple[
    dict[tuple[str, str], set[str]],
    dict[tuple[str, str], set[str]],
]:
    theorem_targets: dict[tuple[str, str], set[str]] = defaultdict(set)
    representation_targets: dict[tuple[str, str], set[str]] = defaultdict(set)
    for locator in locators:
        observation = locator.observation
        if (
            len(observation.source_theorem_ids) != 1
            or len(observation.source_representation_ids) != 1
        ):
            continue
        theorem_targets[
            (
                locator.root.theorem_partition_path,
                locator.root.theorem_partition_sha256,
            )
        ].add(observation.source_theorem_ids[0])
        representation_targets[
            (
                locator.root.representation_partition_path,
                locator.root.representation_partition_sha256,
            )
        ].add(observation.source_representation_ids[0])
    return theorem_targets, representation_targets


def _load_target_theorems(
    targets: Mapping[tuple[str, str], set[str]],
) -> dict[str, TheoremRecord]:
    output: dict[str, TheoremRecord] = {}
    for path_value, expected_hash in sorted(targets):
        path = _require_hash(Path(path_value), expected_hash, field="source theorem partition")
        wanted = targets[(path_value, expected_hash)]
        found: set[str] = set()
        for line_number, raw, _ in _iter_jsonl_objects(path):
            payload = raw.get("theorem", raw)
            if not isinstance(payload, dict) or payload.get("theorem_id") not in wanted:
                continue
            try:
                theorem = TheoremRecord.model_validate(payload)
            except ValueError as exc:
                raise ExperimentalFirstHopProjectionError(
                    f"invalid source theorem at {path}:{line_number}: {exc}"
                ) from exc
            prior = output.get(theorem.theorem_id)
            if prior is not None and prior != theorem:
                raise ExperimentalFirstHopProjectionError(
                    f"source theorem differs across partitions: {theorem.theorem_id}"
                )
            output[theorem.theorem_id] = theorem
            found.add(theorem.theorem_id)
        if found != wanted:
            raise ExperimentalFirstHopProjectionError(
                f"source theorem partition misses {len(wanted - found)} records"
            )
    return output


def _load_target_representations(
    targets: Mapping[tuple[str, str], set[str]],
    theorems: Mapping[str, TheoremRecord],
    registry: ActiveBenchmarkRegistry,
) -> dict[str, _PreparedSide]:
    """Stream source representations and retain only compact projected views.

    The mathlib representation partition is roughly 1.7 GB because explicit
    elaborated signatures are large.  Holding selected ``RepresentationRecord``
    instances would therefore defeat the projection's memory boundary.
    """

    output: dict[str, _PreparedSide] = {}
    for path_value, expected_hash in sorted(targets):
        path = _require_hash(
            Path(path_value), expected_hash, field="source representation partition"
        )
        wanted = targets[(path_value, expected_hash)]
        found: set[str] = set()
        with path.open("rb") as handle:
            raw_rows = enumerate(handle, start=1)
            for line_number, raw_line in raw_rows:
                if not raw_line.endswith(b"\n") or not raw_line.strip():
                    raise ExperimentalFirstHopProjectionError(
                        f"invalid representation JSONL framing at {path}:{line_number}"
                    )
                raw_ids = {
                    match.decode("utf-8") for match in _REPRESENTATION_ID_BYTES.findall(raw_line)
                }
                if not raw_ids & wanted:
                    continue
                raw = _parse_json_object(raw_line, path=path)
                if raw.get("representation_id") not in wanted:
                    continue
                try:
                    representation = RepresentationRecord.model_validate(raw)
                except ValueError as exc:
                    raise ExperimentalFirstHopProjectionError(
                        f"invalid source representation at {path}:{line_number}: {exc}"
                    ) from exc
                theorem = theorems.get(representation.theorem_id)
                if theorem is None:
                    raise ExperimentalFirstHopProjectionError(
                        "source representation references an unselected theorem: "
                        f"{representation.representation_id}"
                    )
                prepared = _prepare_side(_SideMaterial(theorem, representation), registry)
                prior = output.get(representation.representation_id)
                if prior is not None and prior != prepared:
                    raise ExperimentalFirstHopProjectionError(
                        "source representation differs across partitions: "
                        f"{representation.representation_id}"
                    )
                output[representation.representation_id] = prepared
                found.add(representation.representation_id)
        if found != wanted:
            raise ExperimentalFirstHopProjectionError(
                f"source representation partition misses {len(wanted - found)} records"
            )
    return output


def _candidate_materials(
    locators: Sequence[_Locator],
    registry: ActiveBenchmarkRegistry,
) -> dict[str, _PreparedSide]:
    by_root: dict[str, dict[int, _Locator]] = defaultdict(dict)
    roots: dict[str, MaterializationRootBinding] = {}
    for locator in locators:
        line = locator.observation.result_line_number
        root_id = locator.root.root_binding_id
        prior = by_root[root_id].get(line)
        if prior is not None and prior.observation.result_id != locator.observation.result_id:
            raise ExperimentalFirstHopProjectionError("two result IDs claim one root line")
        by_root[root_id][line] = locator
        roots[root_id] = locator.root
    output: dict[str, _PreparedSide] = {}
    for root_id in sorted(by_root):
        binding = roots[root_id]
        root = _real_directory(Path(binding.root_path))
        for file_binding in (binding.run_spec, binding.manifest, binding.results):
            path = _require_hash(
                root / file_binding.relative_path,
                file_binding.sha256,
                field=f"{root_id}:{file_binding.relative_path}",
            )
            if path.stat().st_size != file_binding.byte_count:
                raise ExperimentalFirstHopProjectionError(
                    f"bound input size differs: {root_id}:{file_binding.relative_path}"
                )
        required = by_root[root_id]
        found: set[int] = set()
        result_path = root / binding.results.relative_path
        result_type = _result_model(binding.run_kind)
        with result_path.open("rb") as handle:
            for line_number, raw_line in enumerate(handle, start=1):
                selected_locator = required.get(line_number)
                if selected_locator is None:
                    continue
                raw = _parse_json_object(raw_line, path=result_path)
                try:
                    result = result_type.model_validate(raw)
                except ValueError as exc:
                    raise ExperimentalFirstHopProjectionError(
                        f"invalid bound result at {result_path}:{line_number}: {exc}"
                    ) from exc
                if raw_line != _canonical_line(result):
                    raise ExperimentalFirstHopProjectionError(
                        f"non-canonical bound result at {result_path}:{line_number}"
                    )
                payload = cast(dict[str, object], result.model_dump(mode="json"))
                if payload.get("result_id") != selected_locator.observation.result_id:
                    raise ExperimentalFirstHopProjectionError(
                        f"result ID differs at {result_path}:{line_number}"
                    )
                theorem_raw = payload.get("candidate_theorem")
                representation_raw = payload.get("candidate_representation")
                if not isinstance(theorem_raw, dict) or not isinstance(representation_raw, dict):
                    raise ExperimentalFirstHopProjectionError(
                        "bound result lacks candidate records"
                    )
                theorem = TheoremRecord.model_validate(theorem_raw)
                representation = RepresentationRecord.model_validate(representation_raw)
                observation = selected_locator.observation
                if (
                    theorem.theorem_id != observation.candidate_theorem_id
                    or representation.representation_id != observation.candidate_representation_id
                    or theorem.theorem_id != representation.theorem_id
                    or theorem.context_id != observation.context_id
                    or representation.context_id != observation.context_id
                    or theorem.statement_content_hash != observation.candidate_code_hash
                    or representation.alpha_identity_fingerprint
                    != observation.candidate_alpha_identity_fingerprint
                    or theorem.root_ancestry_ids != observation.source_root_ancestry_ids
                ):
                    raise ExperimentalFirstHopProjectionError(
                        f"candidate lineage differs: {observation.observation_id}"
                    )
                output[observation.observation_id] = _prepare_side(
                    _SideMaterial(theorem, representation), registry
                )
                found.add(line_number)
        if found != set(required):
            raise ExperimentalFirstHopProjectionError(
                f"bound result file misses required lines: {sorted(set(required) - found)[:5]}"
            )
    if len(output) != len(locators):
        raise ExperimentalFirstHopProjectionError("candidate material count does not reconcile")
    return output


def _representation_is_protected(
    registry: ActiveBenchmarkRegistry,
    theorem: TheoremRecord,
    representation: RepresentationRecord,
) -> bool:
    row_ids = (
        theorem.theorem_id,
        theorem.source_record,
        theorem.source_record_id,
        theorem.upstream_uuid,
    )
    if any(value is not None and registry.index.contains_row_id(value) for value in row_ids):
        return True
    if registry.index.contains_lean(theorem.proof_stripped_declaration):
        return True
    for view_name in ("headless", "signature_pp", "signature_explicit"):
        value = getattr(representation, view_name)
        if value is None:
            continue
        if registry.index.contains_lean(value):
            return True
        if registry.index.contains_representation(signature_near_dup_hash(value)):
            return True
    alpha = representation.alpha_identity_fingerprint
    return alpha is not None and registry.index.contains_representation(alpha)


def _normalized_whitespace(value: str) -> str:
    return " ".join(value.split())


def _project_view(
    material: _SideMaterial,
) -> tuple[ExperimentalFirstHopStatementView | None, set[ExclusionReason]]:
    theorem = material.theorem
    representation = material.representation
    reasons: set[ExclusionReason] = set()
    if (
        representation.alpha_identity_fingerprint is None
        or theorem.theorem_id != representation.theorem_id
        or theorem.context_id != representation.context_id
    ):
        reasons.add("missing_required_representation_view")
        return None, reasons
    # ``RepresentationRecord.headless`` is the parser-derived primary view;
    # the string normalizer is only a cross-check.  This matters for valid
    # declarations with modifiers such as ``nonrec`` that the conservative
    # fallback intentionally does not parse.  Canonical whitespace is applied
    # identically to both sources and candidates.
    fallback_source = _NONREC_DECLARATION_MODIFIER.sub(
        "", theorem.proof_stripped_declaration, count=1
    )
    fallback = normalize_headless(fallback_source)
    if representation.headless is None:
        reasons.add("missing_required_representation_view")
        if fallback is None:
            reasons.add("headless_normalization_failed")
            return None, reasons
        normalized = fallback
    else:
        normalized = _normalized_whitespace(representation.headless)
    if fallback is not None and fallback != normalized:
        reasons.add("headless_representation_mismatch")
    view = ExperimentalFirstHopStatementView(
        theorem_id=theorem.theorem_id,
        representation_id=representation.representation_id,
        context_id=theorem.context_id,
        statement_content_hash=theorem.statement_content_hash,
        representation_content_hash=representation.content_hash,
        alpha_identity_fingerprint=representation.alpha_identity_fingerprint,
        normalized_headless_text_v1=normalized,
        normalized_headless_sha256=sha256_hex(normalized.encode("utf-8")),
        signature_pp_sha256=(
            None
            if representation.signature_pp is None
            else sha256_hex(representation.signature_pp.encode("utf-8"))
        ),
        signature_explicit_sha256=(
            None
            if representation.signature_explicit is None
            else sha256_hex(representation.signature_explicit.encode("utf-8"))
        ),
    )
    return view, reasons


def _prepare_side(
    material: _SideMaterial,
    registry: ActiveBenchmarkRegistry,
) -> _PreparedSide:
    view, reasons = _project_view(material)
    return _PreparedSide(
        theorem_id=material.theorem.theorem_id,
        representation_id=material.representation.representation_id,
        theorem_source=material.theorem.source,
        context_id=material.theorem.context_id,
        root_ancestry_ids=material.theorem.root_ancestry_ids,
        view=view,
        projection_reasons=tuple(sorted(reasons)),
        benchmark_protected=_representation_is_protected(
            registry, material.theorem, material.representation
        ),
    )


def _source_policy(category: SourceCategory) -> tuple[bool, bool, bool, bool]:
    if category == "sft_classic":
        return True, False, False, False
    return False, True, True, True


def _make_record(
    locator: _Locator,
    source_material: _PreparedSide,
    candidate_material: _PreparedSide,
) -> ExperimentalFirstHopProjectionRecord:
    observation = locator.observation
    source_category = cast(SourceCategory, observation.source_categories[0])
    if source_material.theorem_source != source_category:
        raise ExperimentalFirstHopProjectionError(
            f"source theorem category differs: {observation.observation_id}"
        )
    if (
        source_material.theorem_id != observation.source_theorem_ids[0]
        or source_material.representation_id != observation.source_representation_ids[0]
        or source_material.context_id != observation.context_id
        or source_material.root_ancestry_ids != observation.source_root_ancestry_ids
        or candidate_material.theorem_id != observation.candidate_theorem_id
        or candidate_material.representation_id != observation.candidate_representation_id
        or candidate_material.context_id != observation.context_id
        or candidate_material.root_ancestry_ids != observation.source_root_ancestry_ids
    ):
        raise ExperimentalFirstHopProjectionError(
            f"source/candidate lineage differs: {observation.observation_id}"
        )
    reasons: set[ExclusionReason] = (
        set(locator.initial_exclusions)
        | set(source_material.projection_reasons)
        | set(candidate_material.projection_reasons)
    )
    if source_material.benchmark_protected:
        reasons.add("benchmark_overlap_source")
    if candidate_material.benchmark_protected:
        reasons.add("benchmark_overlap_candidate")
    selectable = locator.evidence_tier is not None and not reasons
    private, redistribution, transmission, release = _source_policy(source_category)
    payload: dict[str, object] = {
        "projection_record_id": f"experimental_first_hop_pair:{'0' * 64}",
        "unique_pair_id": locator.unique.unique_pair_id,
        "exact_pair_key": locator.unique.exact_pair_key,
        "observation_ids": locator.unique.observation_ids,
        "selected_observation_id": observation.observation_id,
        "provenance_count": locator.unique.provenance_count,
        "root_binding_id": observation.root_binding_id,
        "result_id": observation.result_id,
        "result_line_number": observation.result_line_number,
        "pair_id": observation.pair_id,
        "family_ids": locator.unique.family_ids,
        "rule_id": observation.rule_id,
        "intended_relations": tuple(str(value) for value in locator.unique.intended_relations),
        "source_category": source_category,
        "source_root_ancestry_ids": observation.source_root_ancestry_ids,
        "evidence_tier": locator.evidence_tier,
        "pseudo_target": locator.pseudo_target,
        "certificate_kind": None if locator.seed is None else locator.seed.certificate_kind,
        "certificate_sha256": None if locator.seed is None else locator.seed.certificate_sha256,
        "selection_status": "selectable" if selectable else "excluded",
        "exclusion_reasons": tuple(sorted(reasons)),
        "source": source_material.view,
        "candidate": candidate_material.view,
        "private_source_content": private,
        "redistribution_allowed": redistribution,
        "external_transmission_allowed": transmission,
        "release_eligible": release,
        "experimental_mixed_input_eligible": selectable,
    }
    provisional = ExperimentalFirstHopProjectionRecord.model_construct(_fields_set=None, **payload)
    payload["projection_record_id"] = "experimental_first_hop_pair:" + hash_canonical(
        _without_id(provisional.model_dump(mode="json"), "projection_record_id")
    )
    return ExperimentalFirstHopProjectionRecord.model_validate(payload)


def _input_bindings(
    audit_dir: Path,
    seed_dir: Path,
    audit_manifest: ProvisionalPairCombinationManifest,
    benchmark: ActiveBenchmarkRegistry,
    repo_root: Path,
) -> dict[str, ExperimentalFirstHopInputBinding]:
    bindings: dict[str, ExperimentalFirstHopInputBinding] = {
        "audit_gross_observations": _binding(audit_dir / "gross_observations.jsonl"),
        "audit_manifest": _binding(audit_dir / "manifest.json"),
        "audit_unique_pairs": _binding(audit_dir / "unique_pairs.jsonl"),
        "benchmark_active_registry": _binding(benchmark.active_registry_path),
        "benchmark_authorization": _binding(repo_root / "reports/gates/lf_016_authorization.json"),
        "benchmark_manifest": _binding(benchmark.manifest_path),
        "positive_seed_manifest": _binding(seed_dir / "manifest.json"),
        "positive_seed_records": _binding(seed_dir / "seeds.jsonl"),
        "positive_seed_representations": _binding(seed_dir / "representations.jsonl"),
        "positive_seed_theorems": _binding(seed_dir / "theorems.jsonl"),
    }
    for binding in audit_manifest.root_bindings:
        root = Path(binding.root_path)
        prefix = binding.root_binding_id
        bindings[f"{prefix}:manifest"] = _binding(root / binding.manifest.relative_path)
        bindings[f"{prefix}:results"] = _binding(root / binding.results.relative_path)
        bindings[f"{prefix}:run_spec"] = _binding(root / binding.run_spec.relative_path)
        theorem_key = f"source_theorems:{binding.theorem_partition_sha256}"
        representation_key = f"source_representations:{binding.representation_partition_sha256}"
        if theorem_key not in bindings:
            bindings[theorem_key] = _binding(Path(binding.theorem_partition_path))
        if representation_key not in bindings:
            bindings[representation_key] = _binding(Path(binding.representation_partition_path))
    return dict(sorted(bindings.items()))


def _summary(
    records: Sequence[ExperimentalFirstHopProjectionRecord],
    *,
    projection_id: str,
    profile_id: str,
) -> ExperimentalFirstHopProjectionSummary:
    selectable = [record for record in records if record.selection_status == "selectable"]
    return ExperimentalFirstHopProjectionSummary(
        projection_id=projection_id,
        profile_id=profile_id,
        inventory_count=len(records),
        selectable_count=len(selectable),
        excluded_count=len(records) - len(selectable),
        counts_by_source=dict(
            sorted(Counter(record.source_category for record in records).items())
        ),
        selectable_counts_by_source=dict(
            sorted(Counter(record.source_category for record in selectable).items())
        ),
        counts_by_family=dict(
            sorted(Counter(family for record in records for family in record.family_ids).items())
        ),
        counts_by_evidence_tier=dict(
            sorted(Counter(cast(str, record.evidence_tier) for record in selectable).items())
        ),
        counts_by_pseudo_target=dict(
            sorted(Counter(cast(str, record.pseudo_target) for record in selectable).items())
        ),
        counts_by_exclusion_reason=dict(
            sorted(
                Counter(reason for record in records for reason in record.exclusion_reasons).items()
            )
        ),
    )


def _projection_id(
    *,
    config_hash: str,
    code_tree_hash: str,
    inputs: Mapping[str, ExperimentalFirstHopInputBinding],
    records: Sequence[ExperimentalFirstHopProjectionRecord],
) -> str:
    return "experimental_first_hop_projection:" + hash_canonical(
        {
            "schema": "experimental_deterministic_first_hop_projection_v1",
            "config_hash": config_hash,
            "code_tree_hash": code_tree_hash,
            "inputs": {
                name: binding.model_dump(mode="json") for name, binding in sorted(inputs.items())
            },
            "record_ids": [record.projection_record_id for record in records],
        }
    )


def _verify_existing_output(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    root = _real_directory(output_dir)
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalFirstHopProjectionError("existing projection file set is not exact")
    for name, expected in payloads.items():
        if _regular_file(root / name).read_bytes() != expected:
            raise ExperimentalFirstHopProjectionError(
                f"existing projection output differs: {root / name}"
            )
    return True


def _write_or_replay(output_dir: Path, payloads: Mapping[str, bytes]) -> bool:
    if set(payloads) != _OUTPUT_FILES:
        raise ExperimentalFirstHopProjectionError("projection output payload set is not exact")
    output = _reject_symlink_components(output_dir, allow_missing=True)
    if output.exists():
        return _verify_existing_output(output, payloads)
    output.parent.mkdir(parents=True, exist_ok=True)
    _real_directory(output.parent)
    output = _reject_symlink_components(output, allow_missing=True)
    if output.exists():
        return _verify_existing_output(output, payloads)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.", dir=output.parent))
    try:
        for name, payload in sorted(payloads.items()):
            path = temporary / name
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
        os.rename(temporary, output)
    except FileExistsError:
        if temporary.exists():
            shutil.rmtree(temporary)
        return _verify_existing_output(output, payloads)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    return False


def freeze_experimental_first_hop_projection(
    *,
    repo_root: Path,
    audit_dir: Path,
    positive_seed_dir: Path,
    output_dir: Path,
    config: ExperimentalFirstHopProjectionConfig,
    config_hash: str | None = None,
) -> ExperimentalFirstHopProjectionArtifacts:
    """Build or exactly replay the complete first-hop projection."""

    repo = _real_directory(repo_root)
    audit = _real_directory(audit_dir)
    seed_root = _real_directory(positive_seed_dir)
    output = _reject_symlink_components(output_dir, allow_missing=True)
    for input_root in (repo, audit, seed_root):
        if output == input_root or output in input_root.parents or input_root in output.parents:
            raise ExperimentalFirstHopProjectionError(
                "output directory must be disjoint from every input root"
            )
    effective_config_hash = config_hash or hash_canonical(config.model_dump(mode="json"))
    if effective_config_hash != hash_canonical(config.model_dump(mode="json")):
        raise ExperimentalFirstHopProjectionError("config hash differs from effective config")
    code = collect_code_state(repo)
    if code.git_dirty or code.code_tree_hash is None or code.untracked_files:
        raise ExperimentalFirstHopProjectionError(
            "projection freeze requires a clean, fully tracked code tree"
        )

    audit_manifest, gross, unique = _load_audit(audit, config)
    seed_manifest, seeds = _load_seeds(seed_root, config)
    if (
        seed_manifest.input_combination_hash != audit_manifest.combination_hash
        or seed_manifest.input_combination_manifest_sha256 != config.audit_manifest_sha256
        or seed_manifest.input_gross_observations_sha256 != config.audit_gross_observations_sha256
        or seed_manifest.input_unique_pairs_sha256 != config.audit_unique_pairs_sha256
    ):
        raise ExperimentalFirstHopProjectionError(
            "positive-seed set does not bind the selected first-hop audit"
        )
    registry = _load_benchmark_registry(repo, config)
    locators = _build_locators(audit_manifest, gross, unique, seeds, config)
    theorem_targets, representation_targets = _source_targets(locators)
    source_theorems = _load_target_theorems(theorem_targets)
    source_representations = _load_target_representations(
        representation_targets, source_theorems, registry
    )
    candidate_materials = _candidate_materials(locators, registry)
    records: list[ExperimentalFirstHopProjectionRecord] = []
    for locator in locators:
        observation = locator.observation
        if (
            len(observation.source_theorem_ids) != 1
            or len(observation.source_representation_ids) != 1
        ):
            raise ExperimentalFirstHopProjectionError(
                "current complete first-hop projection requires unary source records"
            )
        source_material = source_representations[observation.source_representation_ids[0]]
        records.append(
            _make_record(
                locator,
                source_material,
                candidate_materials[observation.observation_id],
            )
        )
    ordered = tuple(sorted(records, key=lambda record: record.projection_record_id))
    if len(ordered) != config.expected_unique_pair_count:
        raise ExperimentalFirstHopProjectionError("projected inventory count differs from config")
    if len({record.unique_pair_id for record in ordered}) != len(ordered):
        raise ExperimentalFirstHopProjectionError("projected inventory repeats a unique pair")
    if Counter(record.source_category for record in ordered) != Counter(
        config.expected_counts_by_source
    ):
        raise ExperimentalFirstHopProjectionError("projected source counts differ from config")
    inputs = _input_bindings(audit, seed_root, audit_manifest, registry, repo)
    code_tree_hash = code.code_tree_hash
    assert code_tree_hash is not None
    projection_id = _projection_id(
        config_hash=effective_config_hash,
        code_tree_hash=code_tree_hash,
        inputs=inputs,
        records=ordered,
    )
    summary = _summary(ordered, projection_id=projection_id, profile_id=config.profile_id)
    selectable = tuple(record for record in ordered if record.selection_status == "selectable")
    excluded = tuple(record for record in ordered if record.selection_status == "excluded")
    non_manifest_payloads = {
        "inventory.jsonl": _canonical_jsonl(ordered),
        "selectable.jsonl": _canonical_jsonl(selectable),
        "excluded.jsonl": _canonical_jsonl(excluded),
        "summary.json": canonical_json_bytes(summary.model_dump(mode="json")) + b"\n",
    }
    manifest = ExperimentalFirstHopProjectionManifest(
        projection_id=projection_id,
        profile_id=config.profile_id,
        config_hash=effective_config_hash,
        config=config,
        code=code,
        inputs=inputs,
        inventory_count=len(ordered),
        selectable_count=len(selectable),
        excluded_count=len(excluded),
        output_sha256={
            name: hashlib.sha256(payload).hexdigest()
            for name, payload in non_manifest_payloads.items()
        },
    )
    payloads = {
        **non_manifest_payloads,
        "manifest.json": canonical_json_bytes(manifest.model_dump(mode="json")) + b"\n",
    }
    for name, binding in inputs.items():
        path = _regular_file(Path(binding.path))
        if hash_file(path) != binding.sha256 or path.stat().st_size != binding.byte_count:
            raise ExperimentalFirstHopProjectionError(f"input changed during build: {name}")
    replayed = _write_or_replay(output, payloads)
    verify_experimental_first_hop_projection(output)
    return ExperimentalFirstHopProjectionArtifacts(
        output_dir=output,
        manifest_path=output / "manifest.json",
        inventory_path=output / "inventory.jsonl",
        selectable_path=output / "selectable.jsonl",
        excluded_path=output / "excluded.jsonl",
        summary_path=output / "summary.json",
        projection_id=projection_id,
        inventory_count=len(ordered),
        selectable_count=len(selectable),
        replayed=replayed,
    )


def verify_experimental_first_hop_projection(
    output_dir: Path,
    *,
    verify_external_inputs: bool = True,
) -> ExperimentalFirstHopProjectionManifest:
    """Verify a projection from immutable bytes without Lean/model calls."""

    root = _real_directory(output_dir)
    if {path.name for path in root.iterdir()} != _OUTPUT_FILES:
        raise ExperimentalFirstHopProjectionError("projection file set is not exact")
    try:
        manifest = _load_canonical_model(
            root / "manifest.json", ExperimentalFirstHopProjectionManifest
        )
        summary = _load_canonical_model(
            root / "summary.json", ExperimentalFirstHopProjectionSummary
        )
    except Exception as exc:
        raise ExperimentalFirstHopProjectionError(f"invalid projection metadata: {exc}") from exc
    for name, expected in manifest.output_sha256.items():
        if hash_file(_regular_file(root / name)) != expected:
            raise ExperimentalFirstHopProjectionError(f"projection output hash differs: {name}")
    inventory = _load_canonical_jsonl(
        root / "inventory.jsonl", ExperimentalFirstHopProjectionRecord
    )
    selectable = _load_canonical_jsonl(
        root / "selectable.jsonl", ExperimentalFirstHopProjectionRecord
    )
    excluded = _load_canonical_jsonl(root / "excluded.jsonl", ExperimentalFirstHopProjectionRecord)
    if tuple(sorted(inventory, key=lambda record: record.projection_record_id)) != inventory:
        raise ExperimentalFirstHopProjectionError("inventory is not in canonical ID order")
    if len({record.projection_record_id for record in inventory}) != len(inventory):
        raise ExperimentalFirstHopProjectionError("inventory repeats a projection record ID")
    expected_selectable = tuple(
        record for record in inventory if record.selection_status == "selectable"
    )
    expected_excluded = tuple(
        record for record in inventory if record.selection_status == "excluded"
    )
    if selectable != expected_selectable or excluded != expected_excluded:
        raise ExperimentalFirstHopProjectionError("projection partitions differ from inventory")
    if (
        len(inventory) != manifest.inventory_count
        or len(selectable) != manifest.selectable_count
        or len(excluded) != manifest.excluded_count
    ):
        raise ExperimentalFirstHopProjectionError("projection counts differ from manifest")
    if len(inventory) != manifest.config.expected_unique_pair_count:
        raise ExperimentalFirstHopProjectionError("inventory count differs from frozen config")
    if Counter(record.source_category for record in inventory) != Counter(
        manifest.config.expected_counts_by_source
    ):
        raise ExperimentalFirstHopProjectionError("inventory source counts differ from config")
    code_tree_hash = manifest.code.code_tree_hash
    if code_tree_hash is None:
        raise ExperimentalFirstHopProjectionError("manifest lacks code-tree hash")
    if (
        _projection_id(
            config_hash=manifest.config_hash,
            code_tree_hash=code_tree_hash,
            inputs=manifest.inputs,
            records=inventory,
        )
        != manifest.projection_id
    ):
        raise ExperimentalFirstHopProjectionError("projection ID differs from content")
    expected_summary = _summary(
        inventory,
        projection_id=manifest.projection_id,
        profile_id=manifest.profile_id,
    )
    if summary != expected_summary:
        raise ExperimentalFirstHopProjectionError("projection summary differs from inventory")
    if verify_external_inputs:
        for name, binding in manifest.inputs.items():
            path = _regular_file(Path(binding.path))
            if hash_file(path) != binding.sha256 or path.stat().st_size != binding.byte_count:
                raise ExperimentalFirstHopProjectionError(f"external input differs: {name}")
    return manifest


def load_selectable_experimental_first_hop_projection(
    output_dir: Path,
    *,
    allow_experimental_first_hop_projection: bool,
    purpose: str,
) -> tuple[ExperimentalFirstHopProjectionRecord, ...]:
    """Load clean proxy rows only after explicit experimental opt-in."""

    if not allow_experimental_first_hop_projection:
        raise ExperimentalFirstHopProjectionError(
            "loading requires --allow-experimental-first-hop-projection"
        )
    if purpose not in {"mixed_proxy_construction", "learning_curve", "smoke_training"}:
        raise ExperimentalFirstHopProjectionError(
            "first-hop projection is forbidden for scientific training/selection/evaluation"
        )
    verify_experimental_first_hop_projection(output_dir)
    return _load_canonical_jsonl(
        output_dir / "selectable.jsonl", ExperimentalFirstHopProjectionRecord
    )


def differential_check_mathlib_v1_records(
    projection_records: Sequence[ExperimentalFirstHopProjectionRecord],
    v1_records: Sequence[ExperimentalMachineSupervisionRecord],
) -> MathlibV1DifferentialResult:
    """Fail if overlapping mathlib v1 rows changed their semantic proxy fields.

    ``v1_records`` is typed structurally to avoid coupling this new projection
    to the old freezer's serialized schema.  It is expected to contain parsed
    ``ExperimentalMachineSupervisionRecord`` instances.
    """

    projected = {
        record.unique_pair_id: record
        for record in projection_records
        if record.source_category == "mathlib"
    }
    compared = 0
    for old in v1_records:
        unique_pair_id = old.unique_pair_id
        if unique_pair_id not in projected:
            raise ExperimentalFirstHopProjectionError(
                f"v1 mathlib pair is absent from full projection: {unique_pair_id}"
            )
        new = projected[unique_pair_id]
        if new.source is None or new.candidate is None:
            raise ExperimentalFirstHopProjectionError(
                f"v1 mathlib pair lacks a projected statement view: {unique_pair_id}"
            )
        old_source = old.source
        old_candidate = old.candidate
        comparisons = {
            "family": (new.family_ids, (old.family_id,)),
            "evidence": (new.evidence_tier, old.evidence_class),
            "target": (new.pseudo_target, old.pseudo_target),
            "relation": (new.intended_relations, (old.intended_relation,)),
            "ancestry": (new.source_root_ancestry_ids, old.split_group_ids),
            "source_context": (new.source.context_id, old_source.context_id),
            "candidate_context": (
                new.candidate.context_id,
                old_candidate.context_id,
            ),
            "source_theorem": (new.source.theorem_id, old_source.theorem_id),
            "candidate_theorem": (
                new.candidate.theorem_id,
                old_candidate.theorem_id,
            ),
            "source_representation": (
                new.source.representation_id,
                old_source.representation_id,
            ),
            "candidate_representation": (
                new.candidate.representation_id,
                old_candidate.representation_id,
            ),
            "source_alpha": (
                new.source.alpha_identity_fingerprint,
                old_source.alpha_identity_fingerprint,
            ),
            "candidate_alpha": (
                new.candidate.alpha_identity_fingerprint,
                old_candidate.alpha_identity_fingerprint,
            ),
            "source_headless": (
                new.source.normalized_headless_text_v1,
                _normalized_whitespace(old_source.headless),
            ),
            "candidate_headless": (
                new.candidate.normalized_headless_text_v1,
                _normalized_whitespace(old_candidate.headless),
            ),
            "certificate_kind": (new.certificate_kind, old.certificate_kind),
            "certificate_sha256": (
                new.certificate_sha256,
                old.certificate_sha256,
            ),
        }
        mismatches = [name for name, values in comparisons.items() if values[0] != values[1]]
        if mismatches:
            raise ExperimentalFirstHopProjectionError(
                f"v1 differential mismatch for {unique_pair_id}: {mismatches}"
            )
        compared += 1
    return MathlibV1DifferentialResult(
        compared_count=compared,
        projection_inventory_count=len(projection_records),
        v1_record_count=len(v1_records),
    )


__all__ = [
    "ExperimentalFirstHopProjectionArtifacts",
    "ExperimentalFirstHopProjectionConfig",
    "ExperimentalFirstHopProjectionError",
    "ExperimentalFirstHopProjectionManifest",
    "ExperimentalFirstHopProjectionRecord",
    "ExperimentalFirstHopProjectionSummary",
    "ExperimentalFirstHopStatementView",
    "MathlibV1DifferentialResult",
    "differential_check_mathlib_v1_records",
    "freeze_experimental_first_hop_projection",
    "load_selectable_experimental_first_hop_projection",
    "verify_experimental_first_hop_projection",
]
