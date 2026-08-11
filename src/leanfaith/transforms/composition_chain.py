"""Immutable two-hop deterministic composition audit and combination.

The first hop is always one clean certificate-backed P14--P18 E2 seed from
``composition_seed``.  A completed second-hop materialization may apply either
another P14--P18 E2 rule or one N11--N18 D0 rule.  This module only records the
mechanical chain.  It creates no semantic label, promotion, training/evaluation
eligibility, or gate credit.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.representations import NORMALIZATION_VERSION
from leanfaith.schemas.enums import IntendedRelation, Polarity, QualityTier
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_seed import (
    _E2_RULE_CERTIFICATES,
    CertificateKind,
    CompositionSeedManifest,
    CompositionSeedRecord,
)
from leanfaith.transforms.protocol import (
    verify_transformation_attempt_id,
    verify_transformation_audit_id,
    verify_variant_draft_id,
)
from leanfaith.transforms.provisional_pair_combine import (
    FileBinding,
    MaterializationRootBinding,
    ProvisionalPairCombineError,
    ProvisionalPairObservation,
    _iter_jsonl_objects,
    _load_root,
    _load_run_models,
    _load_source_inventory,
)
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_d0_materializer import V2D0MaterializationResult
from leanfaith.transforms.v2_e2_materializer import V2E2MaterializationResult

_HEX64 = r"^[0-9a-f]{64}$"
_CHAIN_ID = r"^detcomp_chain:[0-9a-f]{64}$"
_CHAIN_SET_ID = r"^detcomp_chain_set:[0-9a-f]{64}$"

_SEED_FILES = frozenset({"seeds.jsonl", "theorems.jsonl", "representations.jsonl", "manifest.json"})
_OUTPUT_FILES = frozenset({"chains.jsonl", "manifest.json"})
_E2_RULES = frozenset(
    {
        "p14_independent_binder_permutation",
        "p15_root_iff_reversal",
        "p16_conjunction_reassociation",
        "p17_hypothesis_packing",
        "p18_root_equality_symmetry",
    }
)
_D0_RULES = frozenset(
    {
        "n11_bound_variable_substitution",
        "n12_implication_converse",
        "n13_witness_dependency",
        "n14_negation_scope",
        "n15_conjunct_omission",
        "n16_domain_guard_removal",
        "n17_role_sensitive_arguments",
        "n18_root_equality_polarity",
    }
)
# Re-export the one authoritative first/second-hop positive certificate policy.
E2_RULE_CERTIFICATES: Mapping[str, CertificateKind] = _E2_RULE_CERTIFICATES

type SecondHopKind = Literal["P_to_P", "P_to_N"]
type SecondHopEvidenceClass = Literal["E2", "D0"]
type SecondHopResult = V2E2MaterializationResult | V2D0MaterializationResult


class CompositionChainError(ValueError):
    """A seed set, second-hop root, or immutable replay failed closed."""


def _without_id(payload: Mapping[str, object], field_name: str) -> dict[str, object]:
    output = dict(payload)
    output.pop(field_name, None)
    return output


class CompositionSecondHopRootBinding(StrictModel):
    """Relocation-independent complete binding for one second-hop root."""

    schema_version: Literal[1] = 1
    root_binding_id: str = Field(pattern=r"^detprov_root:[0-9a-f]{64}$")
    run_kind: Literal["e2", "d0"]
    profile_id: str = Field(min_length=1)
    rule_ids: tuple[str, ...] = Field(min_length=1)
    context_id: str = Field(min_length=1)
    execution_settings_provenance: Literal["recorded"]
    workers: int = Field(ge=1)
    memory_hard_limit_mb: int | None = Field(default=None, ge=1)
    run_spec: FileBinding
    materialization_manifest: FileBinding
    results: FileBinding
    journal_files: tuple[FileBinding, ...] = Field(min_length=1)
    root_file_count: int = Field(ge=4)
    root_tree_hash: str = Field(pattern=_HEX64)
    theorem_partition_sha256: str = Field(pattern=_HEX64)
    representation_partition_sha256: str = Field(pattern=_HEX64)
    source_count: int = Field(ge=1)
    result_count: int = Field(ge=1)
    provisional_count: int = Field(ge=0)

    @model_validator(mode="after")
    def _allowed(self) -> CompositionSecondHopRootBinding:
        if self.rule_ids != tuple(sorted(set(self.rule_ids))):
            raise ValueError("second-hop rule IDs must be sorted and unique")
        allowed = _E2_RULES if self.run_kind == "e2" else _D0_RULES
        if not set(self.rule_ids).issubset(allowed):
            raise ValueError("second-hop root contains a rule outside its admitted portfolio")
        return self


class DeterministicCompositionChainRecord(StrictModel):
    """One exact provisional two-hop mechanical lineage."""

    schema_version: Literal[1] = 1
    chain_id: str = Field(pattern=_CHAIN_ID)
    seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    seed_id: str = Field(pattern=r"^detcomp_seed:[0-9a-f]{64}$")
    chain_kind: SecondHopKind
    chain_depth: Literal[2] = 2
    context_id: str = Field(min_length=1)
    root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    original_source_theorem_id: str = Field(min_length=1)
    original_source_representation_id: str = Field(min_length=1)
    intermediate_theorem_id: str = Field(min_length=1)
    intermediate_representation_id: str = Field(min_length=1)
    final_theorem_id: str = Field(min_length=1)
    final_representation_id: str = Field(min_length=1)
    first_hop_root_binding_id: str = Field(pattern=r"^detprov_root:[0-9a-f]{64}$")
    first_hop_result_id: str = Field(min_length=1)
    first_hop_rule_id: str = Field(min_length=1)
    first_hop_attempt_id: str = Field(min_length=1)
    first_hop_draft_id: str = Field(min_length=1)
    first_hop_audit_id: str = Field(min_length=1)
    first_hop_variant_id: str = Field(min_length=1)
    first_hop_certificate_kind: str = Field(min_length=1)
    first_hop_certificate_sha256: str = Field(pattern=_HEX64)
    first_hop_evidence_class: Literal["E2"] = "E2"
    second_hop_root_binding_id: str = Field(pattern=r"^detprov_root:[0-9a-f]{64}$")
    second_hop_result_id: str = Field(min_length=1)
    second_hop_result_line_number: int = Field(ge=1)
    second_hop_profile_id: str = Field(min_length=1)
    second_hop_rule_id: str = Field(min_length=1)
    second_hop_family_id: str = Field(min_length=1)
    second_hop_attempt_id: str = Field(min_length=1)
    second_hop_draft_id: str = Field(min_length=1)
    second_hop_audit_id: str = Field(min_length=1)
    second_hop_variant_id: str = Field(min_length=1)
    second_hop_evidence_class: SecondHopEvidenceClass
    second_hop_intended_relation: IntendedRelation
    second_hop_polarity_metadata: Polarity
    second_hop_certificate_kind: CertificateKind | None = None
    second_hop_certificate_sha256: str | None = Field(default=None, pattern=_HEX64)
    final_candidate_code_hash: str = Field(pattern=_HEX64)
    final_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    quality_tier: Literal["provisional"] = "provisional"
    intention_only: Literal[True] = True
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> DeterministicCompositionChainRecord:
        expected = "detcomp_chain:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "chain_id")
        )
        if self.chain_id != expected:
            raise ValueError("chain_id does not match its immutable payload")
        if self.root_ancestry_ids != tuple(sorted(set(self.root_ancestry_ids))):
            raise ValueError("chain root ancestry must be sorted and unique")
        if self.first_hop_rule_id not in _E2_RULES:
            raise ValueError("first hop is outside P14-P18 E2")
        if self.second_hop_family_id != self.second_hop_rule_id:
            raise ValueError("second-hop family and rule differ")
        if self.chain_kind == "P_to_P":
            if self.second_hop_evidence_class != "E2" or self.second_hop_rule_id not in _E2_RULES:
                raise ValueError("P-to-P chain has a non-E2 second hop")
            if (
                self.second_hop_intended_relation != IntendedRelation.EQUIVALENT
                or self.second_hop_polarity_metadata != Polarity.POSITIVE
            ):
                raise ValueError("P-to-P chain has non-positive intention metadata")
            expected_certificate = E2_RULE_CERTIFICATES[self.second_hop_rule_id]
            if (
                self.second_hop_certificate_kind != expected_certificate
                or self.second_hop_certificate_sha256 is None
            ):
                raise ValueError("P-to-P chain lacks its exact family certificate")
        else:
            if self.second_hop_evidence_class != "D0" or self.second_hop_rule_id not in _D0_RULES:
                raise ValueError("P-to-N chain has a non-D0 second hop")
            if (
                self.second_hop_intended_relation != IntendedRelation.NEAR_MISS
                or self.second_hop_polarity_metadata != Polarity.NEGATIVE
            ):
                raise ValueError("P-to-N chain has non-negative intention metadata")
            if (
                self.second_hop_certificate_kind is not None
                or self.second_hop_certificate_sha256 is not None
            ):
                raise ValueError("P-to-N chain cannot claim an E2 family certificate")
        if self.intermediate_theorem_id in {
            self.original_source_theorem_id,
            self.final_theorem_id,
        }:
            raise ValueError("two-hop chain collapses a theorem lineage edge")
        return self


class DeterministicCompositionChainManifest(StrictModel):
    """Self-authenticating manifest for all audited second-hop chains."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_v2_composition_chain_set"] = (
        "deterministic_v2_composition_chain_set"
    )
    chain_set_id: str = Field(pattern=_CHAIN_SET_ID)
    input_seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    input_seed_manifest_sha256: str = Field(pattern=_HEX64)
    input_seed_records_sha256: str = Field(pattern=_HEX64)
    input_seed_theorems_sha256: str = Field(pattern=_HEX64)
    input_seed_representations_sha256: str = Field(pattern=_HEX64)
    input_seed_count: int = Field(ge=1)
    second_hop_roots: tuple[CompositionSecondHopRootBinding, ...] = Field(min_length=1)
    second_hop_root_count: int = Field(ge=1)
    second_hop_result_count: int = Field(ge=1)
    second_hop_terminal_status_counts: dict[str, int]
    chain_count: int = Field(ge=0)
    chain_kind_counts: dict[str, int]
    second_hop_rule_counts: dict[str, int]
    chain_output: Literal["chains.jsonl"] = "chains.jsonl"
    chain_output_sha256: str = Field(pattern=_HEX64)
    first_hop_policy: Literal["clean_certificate_backed_e2_p14_p18_only_v1"] = (
        "clean_certificate_backed_e2_p14_p18_only_v1"
    )
    second_hop_policy: Literal["e2_p14_p18_or_d0_n11_n18_only_v1"] = (
        "e2_p14_p18_or_d0_n11_n18_only_v1"
    )
    chain_depth_limit: Literal[2] = 2
    negative_source_admitted: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _reconciles(self) -> DeterministicCompositionChainManifest:
        expected = "detcomp_chain_set:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "chain_set_id")
        )
        if self.chain_set_id != expected:
            raise ValueError("chain_set_id does not match its immutable payload")
        ids = tuple(item.root_binding_id for item in self.second_hop_roots)
        if ids != tuple(sorted(set(ids))):
            raise ValueError("second-hop root bindings must be sorted and unique")
        if self.second_hop_root_count != len(self.second_hop_roots):
            raise ValueError("second-hop root count does not reconcile")
        if self.second_hop_result_count != sum(self.second_hop_terminal_status_counts.values()):
            raise ValueError("second-hop result status counts do not reconcile")
        if self.chain_count != sum(self.chain_kind_counts.values()):
            raise ValueError("chain kind counts do not reconcile")
        if self.chain_count != sum(self.second_hop_rule_counts.values()):
            raise ValueError("second-hop chain rule counts do not reconcile")
        return self


@dataclass(frozen=True, slots=True)
class CompositionChainArtifacts:
    output_dir: Path
    manifest_path: Path
    chains_path: Path
    chain_set_id: str
    chain_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _SeedInventory:
    root: Path
    manifest: CompositionSeedManifest
    manifest_sha256: str
    seeds: tuple[CompositionSeedRecord, ...]
    theorems: tuple[TheoremRecord, ...]
    representations: tuple[RepresentationRecord, ...]
    by_theorem_id: Mapping[str, tuple[CompositionSeedRecord, TheoremRecord, RepresentationRecord]]
    file_snapshot: tuple[tuple[str, str, int], ...]


def _canonical_line(record: StrictModel) -> bytes:
    return canonical_json_bytes(record.model_dump(mode="json")) + b"\n"


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_line(record) for record in records)


def _load_canonical_jsonl[ModelT: StrictModel](
    path: Path, model: type[ModelT]
) -> tuple[ModelT, ...]:
    output: list[ModelT] = []
    for line_number, raw, raw_line in _iter_jsonl_objects(path):
        try:
            item = model.model_validate(raw)
        except ValueError as exc:
            raise CompositionChainError(
                f"invalid {model.__name__} at {path}:{line_number}: {exc}"
            ) from exc
        if raw_line != _canonical_line(item):
            raise CompositionChainError(f"non-canonical {model.__name__} at {path}:{line_number}")
        output.append(item)
    return tuple(output)


def _load_canonical_manifest(path: Path) -> CompositionSeedManifest:
    if not path.is_file() or path.is_symlink():
        raise CompositionChainError("composition seed manifest is not a regular file")
    try:
        raw = path.read_bytes()
        manifest = CompositionSeedManifest.model_validate_json(raw)
    except (OSError, ValueError) as exc:
        raise CompositionChainError(f"invalid composition seed manifest: {exc}") from exc
    if raw != _canonical_line(manifest):
        raise CompositionChainError("composition seed manifest is not canonical")
    return manifest


def _seed_snapshot(seed_dir: Path) -> tuple[tuple[str, str, int], ...]:
    snapshot: list[tuple[str, str, int]] = []
    for name in sorted(_SEED_FILES):
        path = seed_dir / name
        if not path.is_file() or path.is_symlink():
            raise CompositionChainError(f"composition seed input is not a regular file: {name}")
        snapshot.append((name, hash_file(path), path.stat().st_size))
    return tuple(snapshot)


def _verify_seed_snapshot(inventory: _SeedInventory) -> None:
    if _seed_snapshot(inventory.root) != inventory.file_snapshot:
        raise CompositionChainError("composition seed files changed during chain audit")


def _load_seed_inventory(seed_dir: Path) -> _SeedInventory:
    try:
        seed_dir = seed_dir.resolve(strict=True)
    except OSError as exc:
        raise CompositionChainError(f"composition seed directory is unavailable: {exc}") from exc
    if not seed_dir.is_dir() or seed_dir.is_symlink():
        raise CompositionChainError("composition seed input is not a regular directory")
    actual = {path.name for path in seed_dir.iterdir()}
    if actual != _SEED_FILES:
        raise CompositionChainError("composition seed directory is not exact")
    file_snapshot = _seed_snapshot(seed_dir)
    manifest_path = seed_dir / "manifest.json"
    manifest = _load_canonical_manifest(manifest_path)
    paths = {
        "seeds": seed_dir / manifest.seed_output,
        "theorems": seed_dir / manifest.theorem_output,
        "representations": seed_dir / manifest.representation_output,
    }
    expected_hashes = {
        "seeds": manifest.seed_output_sha256,
        "theorems": manifest.theorem_output_sha256,
        "representations": manifest.representation_output_sha256,
    }
    for name, path in paths.items():
        if not path.is_file() or path.is_symlink() or hash_file(path) != expected_hashes[name]:
            raise CompositionChainError(f"composition seed {name} partition differs from manifest")
    seeds = _load_canonical_jsonl(paths["seeds"], CompositionSeedRecord)
    theorems = _load_canonical_jsonl(paths["theorems"], TheoremRecord)
    representations = _load_canonical_jsonl(paths["representations"], RepresentationRecord)
    if not (len(seeds) == len(theorems) == len(representations) == manifest.seed_count):
        raise CompositionChainError("composition seed partition counts do not reconcile")
    if len({item.seed_id for item in seeds}) != len(seeds):
        raise CompositionChainError("composition seed IDs are duplicated")
    by_theorem: dict[str, tuple[CompositionSeedRecord, TheoremRecord, RepresentationRecord]] = {}
    for seed, theorem, representation in zip(seeds, theorems, representations, strict=True):
        if seed.chain_depth != 1 or seed.seed_evidence_class != "E2":
            raise CompositionChainError("N-derived or depth>1 composition source is forbidden")
        if seed.first_hop_rule_id not in _E2_RULES:
            raise CompositionChainError("first hop is outside E2 P14-P18")
        if theorem.metadata.get("rule_id") in _D0_RULES:
            raise CompositionChainError("N-derived composition source is forbidden")
        if (
            theorem.theorem_id != seed.intermediate_theorem_id
            or representation.representation_id != seed.intermediate_representation_id
            or representation.theorem_id != theorem.theorem_id
        ):
            raise CompositionChainError("seed theorem/representation ordering differs")
        if theorem.parent_theorem_ids != (seed.source_theorem_id,):
            raise CompositionChainError("seed intermediate is not exactly depth one")
        if theorem.source != "deterministic_transform":
            raise CompositionChainError("seed intermediate is not a deterministic transform")
        if (
            theorem.metadata.get("rule_id") != seed.first_hop_rule_id
            or theorem.metadata.get("family_id") != seed.first_hop_family_id
        ):
            raise CompositionChainError("seed theorem first-hop provenance differs")
        if (
            theorem.context_id != seed.context_id
            or representation.context_id != seed.context_id
            or theorem.root_ancestry_ids != seed.root_ancestry_ids
        ):
            raise CompositionChainError("seed context or root ancestry differs")
        if representation.normalization_version != NORMALIZATION_VERSION:
            raise CompositionChainError("seed representation normalization differs")
        if representation.content_hash != _representation_payload_hash(representation):
            raise CompositionChainError("seed representation content hash is invalid")
        if theorem.theorem_id in by_theorem:
            raise CompositionChainError("more than one seed maps to an intermediate theorem")
        by_theorem[theorem.theorem_id] = (seed, theorem, representation)
    inventory = _SeedInventory(
        root=seed_dir,
        manifest=manifest,
        manifest_sha256=hash_file(manifest_path),
        seeds=seeds,
        theorems=theorems,
        representations=representations,
        by_theorem_id=by_theorem,
        file_snapshot=file_snapshot,
    )
    _verify_seed_snapshot(inventory)
    return inventory


def _second_hop_binding(binding: MaterializationRootBinding) -> CompositionSecondHopRootBinding:
    if binding.run_kind not in {"e2", "d0"}:
        raise CompositionChainError("second-hop root must be E2 or D0")
    if binding.execution_settings_provenance != "recorded" or binding.workers is None:
        raise CompositionChainError("legacy second-hop execution settings are not admitted")
    return CompositionSecondHopRootBinding(
        root_binding_id=binding.root_binding_id,
        run_kind=binding.run_kind,
        profile_id=binding.profile_id,
        rule_ids=binding.rule_ids,
        context_id=binding.context_id,
        execution_settings_provenance="recorded",
        workers=binding.workers,
        memory_hard_limit_mb=binding.memory_hard_limit_mb,
        run_spec=binding.run_spec,
        materialization_manifest=binding.manifest,
        results=binding.results,
        journal_files=binding.journal_files,
        root_file_count=binding.root_file_count,
        root_tree_hash=binding.root_tree_hash,
        theorem_partition_sha256=binding.theorem_partition_sha256,
        representation_partition_sha256=binding.representation_partition_sha256,
        source_count=binding.source_count,
        result_count=binding.result_count,
        provisional_count=binding.provisional_count,
    )


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CompositionChainError(message)


def _build_chain(
    *,
    seed_set_id: str,
    seed: CompositionSeedRecord,
    source_theorem: TheoremRecord,
    source_representation: RepresentationRecord,
    root_binding: MaterializationRootBinding,
    line_number: int,
    observation: ProvisionalPairObservation,
    result: SecondHopResult,
) -> DeterministicCompositionChainRecord:
    _require(result.terminal_status == "provisional_variant", "chain result is not provisional")
    _require(result.resolved_label_count == 0, "second hop carries resolved labels")
    _require(result.promoted_item_count == 0, "second hop carries promoted items")
    _require(result.training_eligible is False, "second hop is training eligible")
    _require(
        all(
            item is not None
            for item in (
                result.draft,
                result.candidate_theorem,
                result.candidate_representation,
                result.audit,
                result.variant,
            )
        ),
        "second hop lacks complete mechanical lineage",
    )
    assert result.draft is not None
    assert result.candidate_theorem is not None
    assert result.candidate_representation is not None
    assert result.audit is not None
    assert result.variant is not None
    verify_transformation_attempt_id(result.attempt)
    verify_variant_draft_id(result.draft)
    verify_transformation_audit_id(result.audit)
    _require(result.draft.family_id == result.rule_id, "second-hop family/rule mismatch")
    _require(not result.audit.violation_codes, "second-hop audit contains violations")
    _require(
        result.audit.recommended_quality_tier == QualityTier.PROVISIONAL,
        "second-hop audit is not provisional",
    )
    _require(
        result.variant.quality_tier == QualityTier.PROVISIONAL,
        "second-hop variant is not provisional",
    )
    _require(result.variant.validation_evidence_id is None, "second hop embeds evidence credit")
    _require(
        result.attempt.source_theorem_ids == (source_theorem.theorem_id,)
        and result.attempt.source_representation_ids == (source_representation.representation_id,),
        "second-hop attempt does not use exactly one admitted seed",
    )
    _require(
        result.draft.source_theorem_ids == (source_theorem.theorem_id,)
        and result.draft.source_representation_ids == (source_representation.representation_id,),
        "second-hop draft does not use exactly one admitted seed",
    )
    _require(
        result.candidate_theorem.parent_theorem_ids == (source_theorem.theorem_id,),
        "second-hop candidate does not preserve the depth-two parent",
    )
    _require(
        result.candidate_theorem.root_ancestry_ids == seed.root_ancestry_ids,
        "second-hop candidate loses root ancestry",
    )
    _require(
        result.candidate_theorem.context_id
        == result.candidate_representation.context_id
        == seed.context_id,
        "second-hop candidate context differs",
    )
    _require(
        result.candidate_representation.theorem_id == result.candidate_theorem.theorem_id,
        "second-hop theorem/representation differ",
    )
    _require(
        result.candidate_representation.normalization_version == NORMALIZATION_VERSION,
        "second-hop representation normalization differs",
    )
    _require(
        result.candidate_representation.content_hash
        == _representation_payload_hash(result.candidate_representation),
        "second-hop representation content hash is invalid",
    )
    _require(
        result.candidate_representation.alpha_identity_fingerprint is not None,
        "second-hop representation lacks an alpha identity",
    )
    _require(observation.result_id == result.result_id, "second-hop observation result differs")
    _require(
        observation.source_theorem_ids == (source_theorem.theorem_id,)
        and observation.source_representation_ids == (source_representation.representation_id,),
        "second-hop observation source differs from seed",
    )
    _require(
        observation.source_root_ancestry_ids == seed.root_ancestry_ids,
        "second-hop observation loses root ancestry",
    )
    _require(
        observation.source_categories == ("deterministic_transform",),
        "second-hop source category is not the admitted positive intermediate",
    )
    if isinstance(result, V2E2MaterializationResult):
        _require(result.evidence_class == "E2", "P-to-P result lacks E2 evidence class")
        _require(result.rule_id in _E2_RULES, "P-to-P second hop is outside P14-P18")
        _require(result.audit.structural_diff_ok is True, "P-to-P structural certificate failed")
        _require(result.audit.atom_mapping_ok is True, "P-to-P atom mapping certificate failed")
        _require(
            result.audit.inverse_or_roundtrip_ok is True,
            "P-to-P inverse/roundtrip certificate failed",
        )
        _require(
            result.audit.metadata.get("resolved_semantic_label") is False,
            "P-to-P audit claims a resolved semantic label",
        )
        _require(
            result.audit.metadata.get("training_eligible") is False,
            "P-to-P audit claims training eligibility",
        )
        certificate_kind = E2_RULE_CERTIFICATES[result.rule_id]
        certificate = result.audit.metadata.get(certificate_kind)
        _require(
            isinstance(certificate, str) and len(certificate) == 64,
            "P-to-P audit lacks its exact family certificate",
        )
        assert isinstance(certificate, str)
        try:
            int(certificate, 16)
        except ValueError as exc:
            raise CompositionChainError("P-to-P family certificate is not hexadecimal") from exc
        _require(
            result.draft.intended_relation == IntendedRelation.EQUIVALENT
            and result.variant.polarity_metadata == Polarity.POSITIVE,
            "P-to-P intention metadata differs",
        )
        chain_kind: SecondHopKind = "P_to_P"
        evidence_class: SecondHopEvidenceClass = "E2"
        second_hop_certificate_kind: CertificateKind | None = certificate_kind
        second_hop_certificate_sha256: str | None = certificate
    else:
        _require(result.rule_id in _D0_RULES, "P-to-N second hop is outside N11-N18")
        _require(
            result.draft.intended_relation == IntendedRelation.NEAR_MISS
            and result.variant.polarity_metadata == Polarity.NEGATIVE,
            "P-to-N intention metadata differs",
        )
        chain_kind = "P_to_N"
        evidence_class = "D0"
        second_hop_certificate_kind = None
        second_hop_certificate_sha256 = None
    data: dict[str, object] = {
        "seed_set_id": seed_set_id,
        "seed_id": seed.seed_id,
        "chain_kind": chain_kind,
        "context_id": seed.context_id,
        "root_ancestry_ids": seed.root_ancestry_ids,
        "original_source_theorem_id": seed.source_theorem_id,
        "original_source_representation_id": seed.source_representation_id,
        "intermediate_theorem_id": source_theorem.theorem_id,
        "intermediate_representation_id": source_representation.representation_id,
        "final_theorem_id": result.candidate_theorem.theorem_id,
        "final_representation_id": result.candidate_representation.representation_id,
        "first_hop_root_binding_id": seed.first_hop_root_binding_id,
        "first_hop_result_id": seed.first_hop_result_id,
        "first_hop_rule_id": seed.first_hop_rule_id,
        "first_hop_attempt_id": seed.first_hop_attempt_id,
        "first_hop_draft_id": seed.first_hop_draft_id,
        "first_hop_audit_id": seed.first_hop_audit_id,
        "first_hop_variant_id": seed.first_hop_variant_id,
        "first_hop_certificate_kind": seed.certificate_kind,
        "first_hop_certificate_sha256": seed.certificate_sha256,
        "second_hop_root_binding_id": root_binding.root_binding_id,
        "second_hop_result_id": result.result_id,
        "second_hop_result_line_number": line_number,
        "second_hop_profile_id": result.profile_id,
        "second_hop_rule_id": result.rule_id,
        "second_hop_family_id": result.draft.family_id,
        "second_hop_attempt_id": result.attempt.attempt_id,
        "second_hop_draft_id": result.draft.draft_id,
        "second_hop_audit_id": result.audit.audit_id,
        "second_hop_variant_id": result.variant.variant_id,
        "second_hop_evidence_class": evidence_class,
        "second_hop_intended_relation": result.draft.intended_relation,
        "second_hop_polarity_metadata": result.variant.polarity_metadata,
        "second_hop_certificate_kind": second_hop_certificate_kind,
        "second_hop_certificate_sha256": second_hop_certificate_sha256,
        "final_candidate_code_hash": result.draft.candidate_code_hash,
        "final_alpha_identity_fingerprint": (
            result.candidate_representation.alpha_identity_fingerprint
        ),
    }
    placeholder = DeterministicCompositionChainRecord.model_construct(
        _fields_set=None,
        chain_id=f"detcomp_chain:{'0' * 64}",
        **data,
    )
    chain_id = "detcomp_chain:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "chain_id")
    )
    return DeterministicCompositionChainRecord.model_validate({"chain_id": chain_id, **data})


def _audit_root(
    path: Path,
    inventory: _SeedInventory,
) -> tuple[
    CompositionSecondHopRootBinding,
    tuple[DeterministicCompositionChainRecord, ...],
    Counter[str],
]:
    try:
        resolved = path.resolve(strict=True)
        loaded = _load_root(resolved)
    except (OSError, ProvisionalPairCombineError) as exc:
        raise CompositionChainError(f"second-hop root failed audit: {exc}") from exc
    binding = loaded.binding
    root_binding = _second_hop_binding(binding)
    if (
        binding.theorem_partition_sha256 != inventory.manifest.theorem_output_sha256
        or binding.representation_partition_sha256
        != inventory.manifest.representation_output_sha256
    ):
        raise CompositionChainError("second-hop root source partitions differ from seed set")
    try:
        run_kind, spec, _ = _load_run_models(resolved)
        source_inventory = _load_source_inventory(spec)
    except ProvisionalPairCombineError as exc:
        raise CompositionChainError(str(exc)) from exc
    if run_kind not in {"e2", "d0"} or run_kind != binding.run_kind:
        raise CompositionChainError("second-hop root run kind differs")
    if len(source_inventory.ordered) != inventory.manifest.seed_count:
        raise CompositionChainError("second-hop root does not contain every admitted seed")
    for theorem, representation in source_inventory.ordered:
        rule = theorem.metadata.get("rule_id")
        if rule in _D0_RULES:
            raise CompositionChainError("N-derived second-hop source is forbidden")
        admitted = inventory.by_theorem_id.get(theorem.theorem_id)
        if admitted is None:
            if theorem.source == "deterministic_transform" and theorem.parent_theorem_ids:
                raise CompositionChainError("depth>2 or foreign derived second-hop source")
            raise CompositionChainError("foreign second-hop source theorem")
        _, expected_theorem, expected_representation = admitted
        if theorem != expected_theorem or representation != expected_representation:
            raise CompositionChainError("second-hop source payload differs from admitted seed")
    if {item[0].theorem_id for item in source_inventory.ordered} != set(inventory.by_theorem_id):
        raise CompositionChainError("second-hop root seed coverage differs")

    observation_by_result: dict[str, ProvisionalPairObservation] = {}
    for observation in loaded.observations:
        if observation.result_id in observation_by_result:
            raise CompositionChainError("second-hop root duplicates a provisional result")
        observation_by_result[observation.result_id] = observation

    result_model: type[V2E2MaterializationResult] | type[V2D0MaterializationResult]
    result_model = V2E2MaterializationResult if run_kind == "e2" else V2D0MaterializationResult
    statuses: Counter[str] = Counter()
    chains: list[DeterministicCompositionChainRecord] = []
    for line_number, raw, raw_line in _iter_jsonl_objects(resolved / "results.jsonl"):
        try:
            result = result_model.model_validate(raw)
        except ValueError as exc:
            raise CompositionChainError(
                f"invalid second-hop result at {resolved}/results.jsonl:{line_number}: {exc}"
            ) from exc
        if raw_line != _canonical_line(result):
            raise CompositionChainError("second-hop result is not canonical")
        statuses[result.terminal_status] += 1
        if (
            len(result.attempt.source_theorem_ids) != 1
            or len(result.attempt.source_representation_ids) != 1
        ):
            raise CompositionChainError("second-hop result is not unary")
        admitted = inventory.by_theorem_id.get(result.attempt.source_theorem_ids[0])
        if admitted is None:
            raise CompositionChainError("second-hop result uses a foreign seed")
        seed, theorem, representation = admitted
        if result.attempt.source_representation_ids != (representation.representation_id,):
            raise CompositionChainError("second-hop result theorem/representation seed differs")
        if result.terminal_status != "provisional_variant":
            continue
        matched_observation = observation_by_result.get(result.result_id)
        if matched_observation is None:
            raise CompositionChainError("provisional second-hop result lacks an observation")
        chains.append(
            _build_chain(
                seed_set_id=inventory.manifest.seed_set_id,
                seed=seed,
                source_theorem=theorem,
                source_representation=representation,
                root_binding=binding,
                line_number=line_number,
                observation=matched_observation,
                result=result,
            )
        )
    if sum(statuses.values()) != binding.result_count:
        raise CompositionChainError("second-hop root result count differs")
    if len(chains) != binding.provisional_count or len(observation_by_result) != len(chains):
        raise CompositionChainError("second-hop provisional chain count differs")
    try:
        final_loaded = _load_root(resolved)
    except (OSError, ProvisionalPairCombineError) as exc:
        raise CompositionChainError(f"second-hop root changed after validation: {exc}") from exc
    if final_loaded.binding != binding or final_loaded.observations != loaded.observations:
        raise CompositionChainError("second-hop root changed after validation")
    return root_binding, tuple(chains), statuses


def _write_new_file(path: Path, payload: bytes) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _verify_existing(output_dir: Path, payloads: Mapping[str, bytes]) -> None:
    if not output_dir.is_dir() or output_dir.is_symlink():
        raise CompositionChainError("existing chain output is not a regular directory")
    if {path.name for path in output_dir.iterdir()} != _OUTPUT_FILES:
        raise CompositionChainError("existing chain output is not an exact replay")
    for name, payload in payloads.items():
        path = output_dir / name
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise CompositionChainError(f"existing chain output differs: {path}")


def audit_deterministic_v2_composition_chains(
    *,
    seed_dir: Path,
    second_hop_roots: Sequence[Path],
    output_dir: Path,
) -> CompositionChainArtifacts:
    """Audit completed second hops and immutably combine exact depth-two chains."""

    inventory = _load_seed_inventory(seed_dir)
    if not second_hop_roots:
        raise CompositionChainError("at least one completed second-hop root is required")
    resolved_roots: list[Path] = []
    for root in second_hop_roots:
        try:
            resolved_roots.append(root.resolve(strict=True))
        except OSError as exc:
            raise CompositionChainError(f"second-hop root is unavailable: {exc}") from exc
    if len(set(resolved_roots)) != len(resolved_roots):
        raise CompositionChainError("second-hop roots must be unique")

    bindings: list[CompositionSecondHopRootBinding] = []
    chains: list[DeterministicCompositionChainRecord] = []
    statuses: Counter[str] = Counter()
    for root in sorted(resolved_roots):
        binding, root_chains, root_statuses = _audit_root(root, inventory)
        bindings.append(binding)
        chains.extend(root_chains)
        statuses.update(root_statuses)
    if len({item.root_binding_id for item in bindings}) != len(bindings):
        raise CompositionChainError("second-hop root bindings are duplicated")
    bindings.sort(key=lambda item: item.root_binding_id)
    chains.sort(key=lambda item: item.chain_id)
    if len({item.chain_id for item in chains}) != len(chains):
        raise CompositionChainError("two second-hop results produce the same chain identity")

    chain_payload = _canonical_jsonl(chains)
    kind_counts = Counter(item.chain_kind for item in chains)
    rule_counts = Counter(item.second_hop_rule_id for item in chains)
    manifest_data: dict[str, object] = {
        "input_seed_set_id": inventory.manifest.seed_set_id,
        "input_seed_manifest_sha256": inventory.manifest_sha256,
        "input_seed_records_sha256": inventory.manifest.seed_output_sha256,
        "input_seed_theorems_sha256": inventory.manifest.theorem_output_sha256,
        "input_seed_representations_sha256": inventory.manifest.representation_output_sha256,
        "input_seed_count": inventory.manifest.seed_count,
        "second_hop_roots": tuple(bindings),
        "second_hop_root_count": len(bindings),
        "second_hop_result_count": sum(statuses.values()),
        "second_hop_terminal_status_counts": dict(sorted(statuses.items())),
        "chain_count": len(chains),
        "chain_kind_counts": dict(sorted(kind_counts.items())),
        "second_hop_rule_counts": dict(sorted(rule_counts.items())),
        "chain_output_sha256": hashlib.sha256(chain_payload).hexdigest(),
    }
    placeholder = DeterministicCompositionChainManifest.model_construct(
        _fields_set=None,
        chain_set_id=f"detcomp_chain_set:{'0' * 64}",
        **manifest_data,
    )
    chain_set_id = "detcomp_chain_set:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "chain_set_id")
    )
    manifest = DeterministicCompositionChainManifest.model_validate(
        {"chain_set_id": chain_set_id, **manifest_data}
    )
    payloads = {
        "chains.jsonl": chain_payload,
        "manifest.json": _canonical_line(manifest),
    }
    _verify_seed_snapshot(inventory)

    output_dir = output_dir.resolve(strict=False)
    if output_dir == inventory.root or output_dir.is_relative_to(inventory.root):
        raise CompositionChainError("chain output cannot be inside the seed input")
    if any(output_dir == root or output_dir.is_relative_to(root) for root in resolved_roots):
        raise CompositionChainError("chain output cannot be inside a second-hop root")
    if output_dir.exists():
        _verify_existing(output_dir, payloads)
        replayed = True
    else:
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=output_dir.parent))
        try:
            for name, payload in payloads.items():
                _write_new_file(temporary / name, payload)
            try:
                os.rename(temporary, output_dir)
            except FileExistsError:
                _verify_existing(output_dir, payloads)
                replayed = True
            else:
                replayed = False
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return CompositionChainArtifacts(
        output_dir=output_dir,
        manifest_path=output_dir / "manifest.json",
        chains_path=output_dir / "chains.jsonl",
        chain_set_id=chain_set_id,
        chain_count=len(chains),
        replayed=replayed,
    )


__all__ = [
    "CompositionChainArtifacts",
    "CompositionChainError",
    "CompositionSecondHopRootBinding",
    "DeterministicCompositionChainManifest",
    "DeterministicCompositionChainRecord",
    "audit_deterministic_v2_composition_chains",
]
