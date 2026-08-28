"""Audit and deduplicate the positive-only third deterministic composition hop.

The input frontier has already proved that every admitted source is either a
``P -> P`` equivalent *candidate* or a ``P -> N`` near-miss *candidate*.
Exactly one further P14--P18 E2 hop is permitted.  This module binds the exact
frontier bytes to five complete materialization roots, reconstructs the full
depth-three mechanical lineage, and groups outputs by
``(original source theorem, final alpha identity)``.

This is deliberately an admission-free artifact.  Certificates establish only
that the third hop is a mechanically valid positive transform.  They do not
turn the preserved intention into a semantic label, promotion, or training or
evaluation eligibility.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical
from leanfaith.config.models import StrictModel
from leanfaith.representations import NORMALIZATION_VERSION
from leanfaith.schemas.enums import IntendedRelation, Polarity, QualityTier
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_chain import (
    E2_RULE_CERTIFICATES,
    CompositionSecondHopRootBinding,
    DeterministicCompositionChainManifest,
    DeterministicCompositionChainRecord,
)
from leanfaith.transforms.composition_polarity_frontier import (
    DeterministicCompositionPolarityFrontierManifest,
    DeterministicCompositionPolarityFrontierRecord,
)
from leanfaith.transforms.composition_seed import (
    CompositionSeedManifest,
    CompositionSeedRecord,
)
from leanfaith.transforms.composition_unique_pairs import (
    CompositionUniquePairError,
    DeterministicCompositionUniquePairManifest,
    DeterministicCompositionUniquePairRecord,
    _absolute_path,
    _child_directory_metadata,
    _cleanup_private_directory,
    _HeldDirectory,
    _load_chain_inventory,
    _load_seed_inventory,
    _open_child_directory,
    _open_held_directory,
    _rename_noreplace_at,
    _snapshot_exact_directory,
    _verify_chain_seed_binding,
    _verify_child_identity,
    _verify_directory_path_identity,
    _write_new_file_at,
)
from leanfaith.transforms.protocol import (
    verify_transformation_attempt_id,
    verify_transformation_audit_id,
    verify_variant_draft_id,
)
from leanfaith.transforms.provisional_pair_combine import (
    MaterializationRootBinding,
    ProvisionalPairCombineError,
    ProvisionalPairObservation,
    _iter_jsonl_objects,
    _load_root,
    _load_run_models,
    _load_source_inventory,
)
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_e2_materializer import (
    E2ProfileId,
    E2RuleId,
    V2E2MaterializationResult,
)

_HEX64 = r"^[0-9a-f]{64}$"
_CHAIN_ID = r"^detcomp_depth3_chain:[0-9a-f]{64}$"
_PAIR_ID = r"^detcomp_depth3_pair:[0-9a-f]{64}$"
_QUARANTINE_ID = r"^detcomp_depth3_quarantine:[0-9a-f]{64}$"
_SET_ID = r"^detcomp_depth3_set:[0-9a-f]{64}$"

_FRONTIER_FILES = frozenset(
    {"frontier.jsonl", "theorems.jsonl", "representations.jsonl", "manifest.json"}
)
_UNIQUE_PAIR_FILES = frozenset({"unique_pairs.jsonl", "manifest.json"})
_SEED_FILES = frozenset({"seeds.jsonl", "theorems.jsonl", "representations.jsonl", "manifest.json"})
_CHAIN_FILES = frozenset({"chains.jsonl", "manifest.json"})
_OUTPUT_FILES = frozenset(
    {
        "chains.jsonl",
        "unique_pairs.jsonl",
        "quarantine.jsonl",
        "theorems.jsonl",
        "representations.jsonl",
        "manifest.json",
    }
)
_RULES: frozenset[str] = frozenset(E2_RULE_CERTIFICATES)
_PROFILE_RULE: Mapping[str, str] = {
    "deterministic_v2_e2_p14_experimental": "p14_independent_binder_permutation",
    "deterministic_v2_e2_p15_experimental": "p15_root_iff_reversal",
    "deterministic_v2_e2_p16_experimental": "p16_conjunction_reassociation",
    "deterministic_v2_e2_p17_experimental": "p17_hypothesis_packing",
    "deterministic_v2_e2_p18_experimental": "p18_root_equality_symmetry",
}

type PreservedIntention = Literal["equivalent_candidate", "near_miss_candidate"]
type QuarantineReason = Literal[
    "lineage_cycle",
    "mixed_lineage_conflict",
    "mixed_preserved_intention",
    "original_source_alpha_return",
    "third_hop_source_alpha_return",
]


class CompositionThirdHopError(ValueError):
    """The frontier, a completed root, or immutable output failed closed."""


def _without_id(payload: Mapping[str, object], field: str) -> dict[str, object]:
    output = dict(payload)
    output.pop(field, None)
    return output


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_line(record) for record in records)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CompositionThirdHopError(message)


class DeterministicCompositionThirdHopChainRecord(StrictModel):
    """One certificate-backed depth-three mechanical lineage."""

    schema_version: Literal[2] = 2
    chain_id: str = Field(pattern=_CHAIN_ID)
    input_frontier_set_id: str = Field(pattern=r"^detcomp_frontier_set:[0-9a-f]{64}$")
    input_frontier_id: str = Field(pattern=r"^detcomp_frontier:[0-9a-f]{64}$")
    input_seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    input_chain_set_id: str = Field(pattern=r"^detcomp_chain_set:[0-9a-f]{64}$")
    seed_id: str = Field(pattern=r"^detcomp_seed:[0-9a-f]{64}$")
    context_id: str = Field(min_length=1)
    root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    original_source_theorem_id: str = Field(min_length=1)
    original_source_representation_id: str = Field(min_length=1)
    original_source_statement_content_hash: str = Field(pattern=_HEX64)
    original_source_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    depth_one_theorem_id: str = Field(min_length=1)
    depth_one_representation_id: str = Field(min_length=1)
    depth_one_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    depth_two_theorem_id: str = Field(min_length=1)
    depth_two_representation_id: str = Field(min_length=1)
    depth_two_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    third_hop_source_theorem_id: str = Field(min_length=1)
    third_hop_source_representation_id: str = Field(min_length=1)
    parent_chain_ids: tuple[str, ...] = Field(min_length=1)
    parent_chain_sequences: tuple[str, ...] = Field(min_length=1)
    parent_chain_kind: Literal["P_to_P", "P_to_N"]
    preserved_intention: PreservedIntention
    semantic_negative_hop_count: Literal[0, 1]
    third_hop_root_binding_id: str = Field(pattern=r"^detprov_root:[0-9a-f]{64}$")
    third_hop_result_id: str = Field(min_length=1)
    third_hop_result_line_number: int = Field(ge=1)
    third_hop_profile_id: E2ProfileId
    third_hop_rule_id: E2RuleId
    third_hop_family_id: E2RuleId
    third_hop_attempt_id: str = Field(min_length=1)
    third_hop_draft_id: str = Field(min_length=1)
    third_hop_audit_id: str = Field(min_length=1)
    third_hop_variant_id: str = Field(min_length=1)
    third_hop_certificate_kind: str = Field(min_length=1)
    third_hop_certificate_sha256: str = Field(pattern=_HEX64)
    depth_three_sequences: tuple[str, ...] = Field(min_length=1)
    final_candidate_theorem_id: str = Field(min_length=1)
    final_candidate_representation_id: str = Field(min_length=1)
    final_candidate_code_hash: str = Field(pattern=_HEX64)
    final_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    prior_alpha_identity_fingerprints: tuple[str, str, str]
    original_source_alpha_return: bool
    depth_one_alpha_return: bool
    third_hop_source_alpha_return: bool
    lineage_cycle: bool
    chain_depth: Literal[3] = 3
    third_hop_evidence_class: Literal["E2"] = "E2"
    third_hop_intended_relation: Literal["equivalent"] = "equivalent"
    third_hop_polarity: Literal["positive"] = "positive"
    quality_tier: Literal["provisional"] = "provisional"
    intention_only: Literal[True] = True
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> DeterministicCompositionThirdHopChainRecord:
        expected = "detcomp_depth3_chain:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "chain_id")
        )
        if self.chain_id != expected:
            raise ValueError("depth-three chain_id does not match immutable payload")
        for name in ("root_ancestry_ids",):
            values = getattr(self, name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be sorted and unique")
        if not (
            len(self.parent_chain_ids)
            == len(self.parent_chain_sequences)
            == len(self.depth_three_sequences)
            == 1
        ):
            raise ValueError("depth-three record must bind one exact parent chain")
        expected_kind = "P_to_P" if self.preserved_intention == "equivalent_candidate" else "P_to_N"
        if self.parent_chain_kind != expected_kind:
            raise ValueError("parent chain kind and preserved intention differ")
        if self.semantic_negative_hop_count != (0 if expected_kind == "P_to_P" else 1):
            raise ValueError("semantic negative-hop count differs from parent polarity")
        if self.third_hop_family_id != self.third_hop_rule_id:
            raise ValueError("third-hop family and rule differ")
        if self.third_hop_certificate_kind != E2_RULE_CERTIFICATES[self.third_hop_rule_id]:
            raise ValueError("third-hop certificate kind differs from rule")
        expected_sequences = tuple(
            sorted(
                f"{sequence}->{self.third_hop_rule_id}" for sequence in self.parent_chain_sequences
            )
        )
        if self.depth_three_sequences != expected_sequences:
            raise ValueError("depth-three sequences do not extend exact parents")
        if self.original_source_alpha_return != (
            self.original_source_alpha_identity_fingerprint == self.final_alpha_identity_fingerprint
        ):
            raise ValueError("original-source alpha return does not reconcile")
        if self.depth_one_alpha_return != (
            self.depth_one_alpha_identity_fingerprint == self.final_alpha_identity_fingerprint
        ):
            raise ValueError("depth-one alpha return does not reconcile")
        if self.third_hop_source_alpha_return != (
            self.depth_two_alpha_identity_fingerprint == self.final_alpha_identity_fingerprint
        ):
            raise ValueError("depth-two alpha return does not reconcile")
        expected_history = (
            self.original_source_alpha_identity_fingerprint,
            self.depth_one_alpha_identity_fingerprint,
            self.depth_two_alpha_identity_fingerprint,
        )
        if self.prior_alpha_identity_fingerprints != expected_history:
            raise ValueError("prior alpha history differs from the exact lineage")
        if self.lineage_cycle != (self.final_alpha_identity_fingerprint in expected_history):
            raise ValueError("lineage cycle does not reconcile with prior alpha history")
        return self


class DeterministicCompositionThirdHopPairRecord(StrictModel):
    """One deduplicated original-source/final-alpha provisional pair."""

    schema_version: Literal[2] = 2
    pair_id: str = Field(pattern=_PAIR_ID)
    canonical_unique_key: str = Field(pattern=_HEX64)
    input_frontier_set_id: str = Field(pattern=r"^detcomp_frontier_set:[0-9a-f]{64}$")
    context_id: str = Field(min_length=1)
    root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    original_source_theorem_id: str = Field(min_length=1)
    original_source_representation_id: str = Field(min_length=1)
    original_source_statement_content_hash: str = Field(pattern=_HEX64)
    original_source_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    frontier_ids: tuple[str, ...] = Field(min_length=1)
    depth_two_theorem_ids: tuple[str, ...] = Field(min_length=1)
    depth_two_representation_ids: tuple[str, ...] = Field(min_length=1)
    chain_ids: tuple[str, ...] = Field(min_length=1)
    depth_three_sequences: tuple[str, ...] = Field(min_length=1)
    third_hop_rule_ids: tuple[E2RuleId, ...] = Field(min_length=1)
    third_hop_root_binding_ids: tuple[str, ...] = Field(min_length=1)
    third_hop_result_ids: tuple[str, ...] = Field(min_length=1)
    final_candidate_theorem_ids: tuple[str, ...] = Field(min_length=1)
    final_candidate_representation_ids: tuple[str, ...] = Field(min_length=1)
    final_candidate_code_hashes: tuple[str, ...] = Field(min_length=1)
    final_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    selected_final_theorem_id: str = Field(min_length=1)
    selected_final_representation_id: str = Field(min_length=1)
    preserved_intention: PreservedIntention
    semantic_negative_hop_count: Literal[0, 1]
    gross_chain_count: int = Field(ge=1)
    duplicate_excess_count: int = Field(ge=0)
    chain_depth: Literal[3] = 3
    quality_tier: Literal["provisional"] = "provisional"
    intention_only: Literal[True] = True
    semantic_labels_created: Literal[False] = False
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> DeterministicCompositionThirdHopPairRecord:
        expected_key = hash_canonical(
            {
                "schema": "deterministic_v2_depth3_unique_pair_v2",
                "original_source_theorem_id": self.original_source_theorem_id,
                "final_alpha_identity_fingerprint": self.final_alpha_identity_fingerprint,
            }
        )
        if (
            self.canonical_unique_key != expected_key
            or self.pair_id != f"detcomp_depth3_pair:{expected_key}"
        ):
            raise ValueError("depth-three pair identity differs from source/final alpha")
        for name in (
            "root_ancestry_ids",
            "frontier_ids",
            "depth_two_theorem_ids",
            "depth_two_representation_ids",
            "chain_ids",
            "depth_three_sequences",
            "third_hop_rule_ids",
            "third_hop_root_binding_ids",
            "third_hop_result_ids",
            "final_candidate_theorem_ids",
            "final_candidate_representation_ids",
            "final_candidate_code_hashes",
        ):
            values = getattr(self, name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be sorted and unique")
        if self.gross_chain_count != len(self.chain_ids):
            raise ValueError("gross chain count differs from chain IDs")
        if self.duplicate_excess_count != self.gross_chain_count - 1:
            raise ValueError("duplicate excess count does not reconcile")
        if self.selected_final_theorem_id not in self.final_candidate_theorem_ids:
            raise ValueError("selected final theorem is outside pair provenance")
        if self.selected_final_representation_id not in self.final_candidate_representation_ids:
            raise ValueError("selected final representation is outside pair provenance")
        expected_negative = 0 if self.preserved_intention == "equivalent_candidate" else 1
        if self.semantic_negative_hop_count != expected_negative:
            raise ValueError("pair negative-hop count differs from preserved intention")
        if self.original_source_alpha_identity_fingerprint == self.final_alpha_identity_fingerprint:
            raise ValueError("original-source alpha return cannot enter a pair")
        return self


class DeterministicCompositionThirdHopQuarantineRecord(StrictModel):
    """A mechanical depth-three lineage intentionally excluded from dedup output."""

    schema_version: Literal[2] = 2
    quarantine_id: str = Field(pattern=_QUARANTINE_ID)
    canonical_unique_key: str = Field(pattern=_HEX64)
    chain_ids: tuple[str, ...] = Field(min_length=1)
    input_frontier_ids: tuple[str, ...] = Field(min_length=1)
    original_source_theorem_id: str = Field(min_length=1)
    original_source_statement_content_hash: str = Field(pattern=_HEX64)
    original_source_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    final_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    reason_codes: tuple[QuarantineReason, ...] = Field(min_length=1)
    semantic_labels_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> DeterministicCompositionThirdHopQuarantineRecord:
        for name in ("chain_ids", "input_frontier_ids", "reason_codes"):
            values = getattr(self, name)
            if values != tuple(sorted(set(values))):
                raise ValueError(f"{name} must be sorted and unique")
        expected = "detcomp_depth3_quarantine:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "quarantine_id")
        )
        if self.quarantine_id != expected:
            raise ValueError("quarantine_id does not match immutable payload")
        return self


class DeterministicCompositionThirdHopManifest(StrictModel):
    """Self-authenticating receipt for exactly five completed E2 roots."""

    schema_version: Literal[2] = 2
    artifact_kind: Literal["deterministic_v2_composition_third_hop_set"] = (
        "deterministic_v2_composition_third_hop_set"
    )
    method_version: Literal["deterministic_v2_composition_third_hop_v2"] = (
        "deterministic_v2_composition_third_hop_v2"
    )
    third_hop_set_id: str = Field(pattern=_SET_ID)
    input_frontier_set_id: str = Field(pattern=r"^detcomp_frontier_set:[0-9a-f]{64}$")
    input_frontier_manifest_sha256: str = Field(pattern=_HEX64)
    input_frontier_records_sha256: str = Field(pattern=_HEX64)
    input_frontier_theorems_sha256: str = Field(pattern=_HEX64)
    input_frontier_representations_sha256: str = Field(pattern=_HEX64)
    input_frontier_count: int = Field(ge=1)
    input_unique_pair_set_id: str = Field(pattern=r"^detcomp_unique_pair_set:[0-9a-f]{64}$")
    input_unique_pair_manifest_sha256: str = Field(pattern=_HEX64)
    input_unique_pair_records_sha256: str = Field(pattern=_HEX64)
    input_unique_pair_count: int = Field(ge=1)
    input_seed_set_id: str = Field(pattern=r"^detcomp_seed_set:[0-9a-f]{64}$")
    input_seed_manifest_sha256: str = Field(pattern=_HEX64)
    input_seed_records_sha256: str = Field(pattern=_HEX64)
    input_seed_theorems_sha256: str = Field(pattern=_HEX64)
    input_seed_representations_sha256: str = Field(pattern=_HEX64)
    input_seed_count: int = Field(ge=1)
    input_chain_set_id: str = Field(pattern=r"^detcomp_chain_set:[0-9a-f]{64}$")
    input_chain_manifest_sha256: str = Field(pattern=_HEX64)
    input_chain_records_sha256: str = Field(pattern=_HEX64)
    input_chain_count: int = Field(ge=1)
    third_hop_roots: tuple[CompositionSecondHopRootBinding, ...] = Field(min_length=5, max_length=5)
    third_hop_result_count: int = Field(ge=5)
    third_hop_provisional_result_count: int = Field(ge=0)
    terminal_status_counts: dict[str, int]
    third_hop_rule_counts: dict[str, int]
    gross_chain_rule_counts: dict[str, int]
    gross_chain_count: int = Field(ge=0)
    expanded_parent_lineage_excess_count: int = Field(ge=0)
    admitted_chain_count: int = Field(ge=0)
    quarantined_chain_count: int = Field(ge=0)
    unique_pair_count: int = Field(ge=0)
    theorem_count: int = Field(ge=0)
    representation_count: int = Field(ge=0)
    duplicate_excess_count: int = Field(ge=0)
    quarantine_reason_counts: dict[str, int]
    chain_output: Literal["chains.jsonl"] = "chains.jsonl"
    unique_output: Literal["unique_pairs.jsonl"] = "unique_pairs.jsonl"
    quarantine_output: Literal["quarantine.jsonl"] = "quarantine.jsonl"
    theorem_output: Literal["theorems.jsonl"] = "theorems.jsonl"
    representation_output: Literal["representations.jsonl"] = "representations.jsonl"
    chain_output_sha256: str = Field(pattern=_HEX64)
    unique_output_sha256: str = Field(pattern=_HEX64)
    quarantine_output_sha256: str = Field(pattern=_HEX64)
    theorem_output_sha256: str = Field(pattern=_HEX64)
    representation_output_sha256: str = Field(pattern=_HEX64)
    chain_depth: Literal[3] = 3
    third_hop_policy: Literal["complete_e2_p14_p18_positive_only_v2"] = (
        "complete_e2_p14_p18_positive_only_v2"
    )
    lineage_cycle_policy: Literal["full_prior_alpha_history_per_exact_parent_chain_v2"] = (
        "full_prior_alpha_history_per_exact_parent_chain_v2"
    )
    deduplication_policy: Literal["original_source_and_final_alpha_after_cycle_quarantine_v2"] = (
        "original_source_and_final_alpha_after_cycle_quarantine_v2"
    )
    original_source_identity_policy: Literal["exact_unique_pair_alpha_and_content_v2"] = (
        "exact_unique_pair_alpha_and_content_v2"
    )
    original_source_payloads_included: Literal[False] = False
    semantic_labels_created: Literal[False] = False
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False
    gate_credit: Literal[False] = False

    @model_validator(mode="after")
    def _reconciles(self) -> DeterministicCompositionThirdHopManifest:
        expected = "detcomp_depth3_set:" + hash_canonical(
            _without_id(self.model_dump(mode="json"), "third_hop_set_id")
        )
        if self.third_hop_set_id != expected:
            raise ValueError("third_hop_set_id does not match immutable payload")
        root_ids = tuple(item.root_binding_id for item in self.third_hop_roots)
        if root_ids != tuple(sorted(set(root_ids))):
            raise ValueError("third-hop root bindings must be sorted and unique")
        rules = tuple(sorted(rule for root in self.third_hop_roots for rule in root.rule_ids))
        if rules != tuple(sorted(_RULES)):
            raise ValueError("third-hop roots must cover P14-P18 exactly once")
        if self.third_hop_result_count != sum(self.terminal_status_counts.values()):
            raise ValueError("third-hop terminal status counts do not reconcile")
        if self.third_hop_result_count != sum(self.third_hop_rule_counts.values()):
            raise ValueError("third-hop rule counts do not reconcile")
        if self.third_hop_result_count != self.input_frontier_count * 5:
            raise ValueError("third-hop results do not cover each frontier under five rules")
        if self.input_unique_pair_count < self.input_frontier_count:
            raise ValueError("frontier exceeds its exact unique-pair input")
        if set(self.third_hop_rule_counts) != _RULES or any(
            count != self.input_frontier_count for count in self.third_hop_rule_counts.values()
        ):
            raise ValueError("each P14-P18 root must cover the exact frontier once")
        if self.third_hop_provisional_result_count != self.terminal_status_counts.get(
            "provisional_variant", 0
        ):
            raise ValueError("third-hop provisional results do not reconcile")
        if self.gross_chain_count != sum(self.gross_chain_rule_counts.values()):
            raise ValueError("expanded chain rule counts do not reconcile")
        if self.expanded_parent_lineage_excess_count != (
            self.gross_chain_count - self.third_hop_provisional_result_count
        ):
            raise ValueError("expanded parent-lineage excess does not reconcile")
        if self.gross_chain_count != self.admitted_chain_count + self.quarantined_chain_count:
            raise ValueError("chain admission/quarantine counts do not reconcile")
        if self.duplicate_excess_count != self.admitted_chain_count - self.unique_pair_count:
            raise ValueError("depth-three duplicate excess does not reconcile")
        if not (self.unique_pair_count == self.theorem_count == self.representation_count):
            raise ValueError("pair/theorem/representation output counts do not reconcile")
        if any(value < 0 for value in self.quarantine_reason_counts.values()):
            raise ValueError("quarantine reason counts cannot be negative")
        return self


@dataclass(frozen=True, slots=True)
class CompositionThirdHopArtifacts:
    output_dir: Path
    manifest_path: Path
    chains_path: Path
    unique_pairs_path: Path
    quarantine_path: Path
    theorem_path: Path
    representation_path: Path
    third_hop_set_id: str
    gross_chain_count: int
    unique_pair_count: int
    quarantine_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _FrontierInventory:
    root: _HeldDirectory
    manifest: DeterministicCompositionPolarityFrontierManifest
    manifest_sha256: str
    records: tuple[DeterministicCompositionPolarityFrontierRecord, ...]
    theorems: tuple[TheoremRecord, ...]
    representations: tuple[RepresentationRecord, ...]
    by_theorem_id: Mapping[
        str,
        tuple[
            DeterministicCompositionPolarityFrontierRecord,
            TheoremRecord,
            RepresentationRecord,
        ],
    ]


@dataclass(frozen=True, slots=True)
class _UniquePairInventory:
    root: _HeldDirectory
    manifest: DeterministicCompositionUniquePairManifest
    manifest_sha256: str
    records: tuple[DeterministicCompositionUniquePairRecord, ...]
    by_id: Mapping[str, DeterministicCompositionUniquePairRecord]


@dataclass(frozen=True, slots=True)
class _LineageInventory:
    seed_manifest: CompositionSeedManifest
    seed_manifest_sha256: str
    seeds: tuple[CompositionSeedRecord, ...]
    seed_by_id: Mapping[str, CompositionSeedRecord]
    chain_manifest: DeterministicCompositionChainManifest
    chain_manifest_sha256: str
    chains: tuple[DeterministicCompositionChainRecord, ...]
    chain_by_id: Mapping[str, DeterministicCompositionChainRecord]


def _parse_record_bytes[ModelT: StrictModel](
    payload: bytes,
    model: type[ModelT],
    *,
    label: str,
) -> tuple[ModelT, ...]:
    output: list[ModelT] = []
    for line_number, line in enumerate(payload.splitlines(keepends=True), start=1):
        if not line.endswith(b"\n") or not line.strip():
            raise CompositionThirdHopError(f"invalid JSONL framing at {label}:{line_number}")
        try:
            record = model.model_validate_json(line)
        except ValueError as exc:
            raise CompositionThirdHopError(
                f"invalid {model.__name__} at {label}:{line_number}: {exc}"
            ) from exc
        if line != _canonical_line(record):
            raise CompositionThirdHopError(
                f"non-canonical {model.__name__} at {label}:{line_number}"
            )
        output.append(record)
    return tuple(output)


def _load_frontier(root: _HeldDirectory) -> _FrontierInventory:
    try:
        with _snapshot_exact_directory(root, expected_names=_FRONTIER_FILES) as snapshot:
            raw_manifest = snapshot.files["manifest.json"].payload
            try:
                manifest = DeterministicCompositionPolarityFrontierManifest.model_validate_json(
                    raw_manifest
                )
            except ValueError as exc:
                raise CompositionThirdHopError(f"invalid frontier manifest: {exc}") from exc
            if raw_manifest != _canonical_line(manifest):
                raise CompositionThirdHopError("frontier manifest is not canonical")
            records = _parse_record_bytes(
                snapshot.files[manifest.frontier_output].payload,
                DeterministicCompositionPolarityFrontierRecord,
                label=manifest.frontier_output,
            )
            theorems = _parse_record_bytes(
                snapshot.files[manifest.theorem_output].payload,
                TheoremRecord,
                label=manifest.theorem_output,
            )
            representations = _parse_record_bytes(
                snapshot.files[manifest.representation_output].payload,
                RepresentationRecord,
                label=manifest.representation_output,
            )
            _require(
                snapshot.files[manifest.frontier_output].sha256 == manifest.frontier_output_sha256,
                "frontier records differ from manifest",
            )
            _require(
                snapshot.files[manifest.theorem_output].sha256 == manifest.theorem_output_sha256,
                "frontier theorem partition differs from manifest",
            )
            _require(
                snapshot.files[manifest.representation_output].sha256
                == manifest.representation_output_sha256,
                "frontier representation partition differs from manifest",
            )
            _require(
                len(records) == len(theorems) == len(representations) == manifest.frontier_count,
                "frontier partition counts do not reconcile",
            )
            by_theorem: dict[
                str,
                tuple[
                    DeterministicCompositionPolarityFrontierRecord,
                    TheoremRecord,
                    RepresentationRecord,
                ],
            ] = {}
            for record, theorem, representation in zip(
                records, theorems, representations, strict=True
            ):
                _require(
                    record.selected_frontier_theorem_id == theorem.theorem_id
                    and record.selected_frontier_representation_id
                    == representation.representation_id
                    and representation.theorem_id == theorem.theorem_id,
                    "frontier theorem/representation ordering differs",
                )
                _require(
                    record.input_chain_set_id == manifest.input_chain_set_id,
                    "frontier chain binding differs",
                )
                _require(
                    record.permitted_next_hop == "E2_positive_only",
                    "frontier permits a non-positive hop",
                )
                _require(
                    not record.second_negative_hop_authorized,
                    "frontier authorizes a second negative hop",
                )
                _require(
                    theorem.context_id == representation.context_id == record.context_id,
                    "frontier context differs",
                )
                _require(
                    theorem.root_ancestry_ids == record.root_ancestry_ids,
                    "frontier ancestry differs",
                )
                _require(
                    hashlib.sha256(theorem.proof_stripped_declaration.encode("utf-8")).hexdigest()
                    == record.final_candidate_code_hash,
                    "frontier theorem code hash differs",
                )
                _require(
                    representation.alpha_identity_fingerprint
                    == record.final_alpha_identity_fingerprint,
                    "frontier alpha identity differs",
                )
                _require(
                    representation.normalization_version == NORMALIZATION_VERSION
                    and representation.content_hash == _representation_payload_hash(representation),
                    "frontier representation is invalid",
                )
                _require(
                    theorem.theorem_id not in by_theorem, "frontier theorem IDs are duplicated"
                )
                by_theorem[theorem.theorem_id] = (record, theorem, representation)
            return _FrontierInventory(
                root=root,
                manifest=manifest,
                manifest_sha256=snapshot.files["manifest.json"].sha256,
                records=records,
                theorems=theorems,
                representations=representations,
                by_theorem_id=by_theorem,
            )
    except CompositionUniquePairError as exc:
        raise CompositionThirdHopError(str(exc)) from exc


def _load_unique_pairs(
    root: _HeldDirectory,
    *,
    frontier: _FrontierInventory,
) -> _UniquePairInventory:
    """Load the exact unique-pair artifact cryptographically named by the frontier."""

    try:
        with _snapshot_exact_directory(root, expected_names=_UNIQUE_PAIR_FILES) as snapshot:
            raw_manifest = snapshot.files["manifest.json"].payload
            try:
                manifest = DeterministicCompositionUniquePairManifest.model_validate_json(
                    raw_manifest
                )
            except ValueError as exc:
                raise CompositionThirdHopError(f"invalid unique-pair manifest: {exc}") from exc
            if raw_manifest != _canonical_line(manifest):
                raise CompositionThirdHopError("unique-pair manifest is not canonical")
            records = _parse_record_bytes(
                snapshot.files[manifest.unique_output].payload,
                DeterministicCompositionUniquePairRecord,
                label=manifest.unique_output,
            )
            expected = frontier.manifest
            _require(
                snapshot.files["manifest.json"].sha256 == expected.input_unique_manifest_sha256
                and manifest.unique_pair_set_id == expected.input_unique_pair_set_id
                and manifest.input_chain_set_id == expected.input_chain_set_id
                and manifest.input_chain_manifest_sha256 == expected.input_chain_manifest_sha256
                and manifest.input_chain_records_sha256 == expected.input_chain_records_sha256,
                "unique-pair artifact differs from exact frontier binding",
            )
            _require(
                snapshot.files[manifest.unique_output].sha256
                == manifest.unique_output_sha256
                == expected.input_unique_records_sha256,
                "unique-pair records differ from exact frontier binding",
            )
            _require(
                len(records) == manifest.unique_pair_count == expected.input_unique_pair_count,
                "unique-pair counts differ from exact frontier binding",
            )
            by_id = {record.unique_pair_id: record for record in records}
            _require(len(by_id) == len(records), "unique-pair IDs are duplicated")
            for frontier_record in frontier.records:
                unique_pair = by_id.get(frontier_record.input_unique_pair_id)
                _require(unique_pair is not None, "frontier references a missing unique pair")
                assert unique_pair is not None
                _require(
                    unique_pair.input_chain_set_id == frontier_record.input_chain_set_id
                    and unique_pair.context_id == frontier_record.context_id
                    and unique_pair.root_ancestry_ids == frontier_record.root_ancestry_ids
                    and unique_pair.original_source_theorem_id
                    == frontier_record.original_source_theorem_id
                    and unique_pair.original_source_representation_id
                    == frontier_record.original_source_representation_id
                    and frontier_record.selected_frontier_theorem_id
                    in unique_pair.final_theorem_ids
                    and frontier_record.selected_frontier_representation_id
                    in unique_pair.final_representation_ids
                    and unique_pair.final_candidate_code_hash
                    == frontier_record.final_candidate_code_hash
                    and unique_pair.final_alpha_identity_fingerprint
                    == frontier_record.final_alpha_identity_fingerprint,
                    "frontier provenance differs from its exact unique pair",
                )
            return _UniquePairInventory(
                root=root,
                manifest=manifest,
                manifest_sha256=snapshot.files["manifest.json"].sha256,
                records=records,
                by_id=by_id,
            )
    except CompositionUniquePairError as exc:
        raise CompositionThirdHopError(str(exc)) from exc


def _load_lineage_inventory(
    seed_root: _HeldDirectory,
    chain_root: _HeldDirectory,
    *,
    frontier: _FrontierInventory,
    unique_pairs: _UniquePairInventory,
) -> _LineageInventory:
    """Load and cross-check the exact depth-one and depth-two lineage receipts."""

    try:
        with (
            _snapshot_exact_directory(seed_root, expected_names=_SEED_FILES) as seed_snapshot,
            _snapshot_exact_directory(chain_root, expected_names=_CHAIN_FILES) as chain_snapshot,
        ):
            seeds = _load_seed_inventory(seed_snapshot)
            chains = _load_chain_inventory(
                chain_snapshot,
                seed_manifest_sha256=seeds.manifest_sha256,
                seed_manifest=seeds.manifest,
            )
            expected = unique_pairs.manifest
            _require(
                seeds.manifest.seed_set_id == expected.input_seed_set_id
                and seeds.manifest_sha256 == expected.input_seed_manifest_sha256
                and seeds.manifest.seed_output_sha256 == expected.input_seed_records_sha256
                and seeds.manifest.theorem_output_sha256 == expected.input_seed_theorems_sha256
                and seeds.manifest.representation_output_sha256
                == expected.input_seed_representations_sha256,
                "seed inventory differs from exact unique-pair binding",
            )
            _require(
                chains.manifest.chain_set_id
                == expected.input_chain_set_id
                == frontier.manifest.input_chain_set_id
                and chains.manifest_sha256
                == expected.input_chain_manifest_sha256
                == frontier.manifest.input_chain_manifest_sha256
                and chains.manifest.chain_output_sha256
                == expected.input_chain_records_sha256
                == frontier.manifest.input_chain_records_sha256,
                "chain receipt differs from exact frontier/unique-pair binding",
            )
            seed_by_id = {item.seed_id: item for item in seeds.seeds}
            chain_by_id = {item.chain_id: item for item in chains.chains}
            _require(len(seed_by_id) == len(seeds.seeds), "seed IDs are duplicated")
            _require(len(chain_by_id) == len(chains.chains), "depth-two chain IDs are duplicated")
            for chain in chains.chains:
                _require(
                    chain.seed_set_id == seeds.manifest.seed_set_id,
                    "depth-two chain seed-set binding differs",
                )
                seed = seed_by_id.get(chain.seed_id)
                _require(seed is not None, "depth-two chain references a foreign seed")
                assert seed is not None
                _verify_chain_seed_binding(chain, seed)
            for frontier_record in frontier.records:
                unique_pair = unique_pairs.by_id[frontier_record.input_unique_pair_id]
                _require(
                    frontier_record.parent_chain_ids == unique_pair.chain_ids
                    and frontier_record.parent_chain_sequences == unique_pair.chain_sequences,
                    "frontier parent paths differ from exact unique pair",
                )
                for parent_chain_id in frontier_record.parent_chain_ids:
                    matched_chain = chain_by_id.get(parent_chain_id)
                    _require(
                        matched_chain is not None,
                        "frontier references a missing depth-two chain",
                    )
                    assert matched_chain is not None
                    sequence = (
                        f"{matched_chain.first_hop_rule_id}->{matched_chain.second_hop_rule_id}"
                    )
                    _require(
                        matched_chain.chain_kind == frontier_record.parent_chain_kind
                        and matched_chain.original_source_theorem_id
                        == frontier_record.original_source_theorem_id
                        and matched_chain.original_source_representation_id
                        == frontier_record.original_source_representation_id
                        and matched_chain.context_id == frontier_record.context_id
                        and matched_chain.root_ancestry_ids == frontier_record.root_ancestry_ids
                        and matched_chain.final_theorem_id in frontier_record.depth_two_theorem_ids
                        and matched_chain.final_representation_id
                        in frontier_record.depth_two_representation_ids
                        and matched_chain.final_candidate_code_hash
                        == frontier_record.final_candidate_code_hash
                        and matched_chain.final_alpha_identity_fingerprint
                        == frontier_record.final_alpha_identity_fingerprint
                        and sequence in frontier_record.parent_chain_sequences,
                        "frontier path differs from exact depth-two chain",
                    )
            return _LineageInventory(
                seed_manifest=seeds.manifest,
                seed_manifest_sha256=seeds.manifest_sha256,
                seeds=seeds.seeds,
                seed_by_id=seed_by_id,
                chain_manifest=chains.manifest,
                chain_manifest_sha256=chains.manifest_sha256,
                chains=chains.chains,
                chain_by_id=chain_by_id,
            )
    except CompositionUniquePairError as exc:
        raise CompositionThirdHopError(str(exc)) from exc


def _third_hop_binding(binding: MaterializationRootBinding) -> CompositionSecondHopRootBinding:
    _require(
        binding.execution_settings_provenance == "recorded",
        "third-hop execution settings are not recorded",
    )
    assert binding.workers is not None
    return CompositionSecondHopRootBinding(
        root_binding_id=binding.root_binding_id,
        run_kind="e2",
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


def _build_chain(
    *,
    frontier_set_id: str,
    frontier: DeterministicCompositionPolarityFrontierRecord,
    unique_pair: DeterministicCompositionUniquePairRecord,
    seed: CompositionSeedRecord,
    parent_chain: DeterministicCompositionChainRecord,
    source_theorem: TheoremRecord,
    source_representation: RepresentationRecord,
    root_binding: MaterializationRootBinding,
    line_number: int,
    observation: ProvisionalPairObservation,
    result: V2E2MaterializationResult,
) -> tuple[DeterministicCompositionThirdHopChainRecord, TheoremRecord, RepresentationRecord]:
    _require(result.terminal_status == "provisional_variant", "third-hop result is not provisional")
    _require(
        unique_pair.unique_pair_id == frontier.input_unique_pair_id
        and unique_pair.original_source_theorem_id == frontier.original_source_theorem_id
        and unique_pair.original_source_representation_id
        == frontier.original_source_representation_id,
        "third-hop original source differs from exact unique pair",
    )
    parent_sequence = f"{parent_chain.first_hop_rule_id}->{parent_chain.second_hop_rule_id}"
    _require(
        parent_chain.chain_id in frontier.parent_chain_ids
        and parent_sequence in frontier.parent_chain_sequences
        and parent_chain.seed_id == seed.seed_id
        and parent_chain.chain_kind == frontier.parent_chain_kind,
        "third-hop exact parent path differs from frontier",
    )
    _require(
        seed.source_theorem_id == unique_pair.original_source_theorem_id
        and seed.source_representation_id == unique_pair.original_source_representation_id
        and seed.source_alpha_identity_fingerprint == unique_pair.source_alpha_identity_fingerprint
        and parent_chain.original_source_theorem_id == unique_pair.original_source_theorem_id
        and parent_chain.original_source_representation_id
        == unique_pair.original_source_representation_id,
        "third-hop prior lineage differs from exact source",
    )
    _require(
        parent_chain.final_theorem_id in frontier.depth_two_theorem_ids
        and parent_chain.final_representation_id in frontier.depth_two_representation_ids
        and parent_chain.final_candidate_code_hash == frontier.final_candidate_code_hash
        and parent_chain.final_alpha_identity_fingerprint
        == source_representation.alpha_identity_fingerprint
        == frontier.final_alpha_identity_fingerprint,
        "third-hop depth-two path differs from materialized source",
    )
    _require(result.evidence_class == "E2", "third-hop result does not declare E2 evidence")
    _require(result.rule_id in _RULES, "third-hop rule is outside P14-P18")
    _require(
        result.resolved_label_count == 0 and result.promoted_item_count == 0,
        "third hop carries semantic credit",
    )
    _require(result.training_eligible is False, "third hop is training eligible")
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
        "third hop lacks complete mechanical lineage",
    )
    assert result.draft is not None
    assert result.candidate_theorem is not None
    assert result.candidate_representation is not None
    assert result.audit is not None
    assert result.variant is not None
    verify_transformation_attempt_id(result.attempt)
    verify_variant_draft_id(result.draft)
    verify_transformation_audit_id(result.audit)
    _require(result.draft.family_id == result.rule_id, "third-hop family/rule mismatch")
    _require(
        result.attempt.source_theorem_ids
        == result.draft.source_theorem_ids
        == (source_theorem.theorem_id,),
        "third-hop theorem source differs from exact frontier",
    )
    _require(
        result.attempt.source_representation_ids
        == result.draft.source_representation_ids
        == (source_representation.representation_id,),
        "third-hop representation source differs from exact frontier",
    )
    _require(observation.result_id == result.result_id, "third-hop observation result differs")
    _require(
        observation.source_theorem_ids == (source_theorem.theorem_id,)
        and observation.source_representation_ids == (source_representation.representation_id,),
        "third-hop observation source differs from frontier",
    )
    _require(
        result.draft.intended_relation == IntendedRelation.EQUIVALENT
        and result.variant.polarity_metadata == Polarity.POSITIVE,
        "third-hop polarity is not positive equivalent-intention",
    )
    _require(
        result.variant.quality_tier == QualityTier.PROVISIONAL,
        "third-hop variant is not provisional",
    )
    _require(result.variant.validation_evidence_id is None, "third hop embeds validation credit")
    _require(not result.audit.violation_codes, "third-hop audit has violations")
    _require(
        result.audit.recommended_quality_tier == QualityTier.PROVISIONAL,
        "third-hop audit tier differs",
    )
    _require(result.audit.structural_diff_ok is True, "third-hop structural certificate failed")
    _require(result.audit.atom_mapping_ok is True, "third-hop atom mapping certificate failed")
    _require(result.audit.inverse_or_roundtrip_ok is True, "third-hop inverse certificate failed")
    _require(result.audit.metadata.get("evidence_class") == "E2", "third-hop audit class differs")
    _require(
        result.audit.metadata.get("resolved_semantic_label") is False,
        "third-hop audit claims a semantic label",
    )
    _require(
        result.audit.metadata.get("training_eligible") is False,
        "third-hop audit claims training eligibility",
    )
    certificate_kind = E2_RULE_CERTIFICATES[result.rule_id]
    certificate = result.audit.metadata.get(certificate_kind)
    _require(
        isinstance(certificate, str) and len(certificate) == 64,
        "third-hop audit lacks its exact family certificate",
    )
    assert isinstance(certificate, str)
    try:
        int(certificate, 16)
    except ValueError as exc:
        raise CompositionThirdHopError("third-hop certificate is not hexadecimal") from exc

    candidate = result.candidate_theorem
    representation = result.candidate_representation
    _require(
        candidate.parent_theorem_ids == (source_theorem.theorem_id,),
        "third-hop candidate parent differs",
    )
    _require(
        candidate.root_ancestry_ids == frontier.root_ancestry_ids,
        "third-hop candidate loses ancestry",
    )
    _require(
        candidate.context_id == representation.context_id == frontier.context_id,
        "third-hop candidate context differs",
    )
    _require(
        representation.theorem_id == candidate.theorem_id, "third-hop theorem/representation differ"
    )
    _require(
        representation.normalization_version == NORMALIZATION_VERSION
        and representation.content_hash == _representation_payload_hash(representation),
        "third-hop candidate representation is invalid",
    )
    _require(
        representation.alpha_identity_fingerprint is not None,
        "third-hop candidate lacks alpha identity",
    )
    _require(
        hashlib.sha256(candidate.proof_stripped_declaration.encode("utf-8")).hexdigest()
        == result.draft.candidate_code_hash,
        "third-hop candidate code differs from draft",
    )
    _require(
        root_binding.root_binding_id == observation.root_binding_id,
        "third-hop observation root differs",
    )

    final_alpha = representation.alpha_identity_fingerprint
    assert final_alpha is not None
    original_source_alpha_return = final_alpha == unique_pair.source_alpha_identity_fingerprint
    depth_one_alpha_return = final_alpha == seed.intermediate_alpha_identity_fingerprint
    source_alpha_return = final_alpha == source_representation.alpha_identity_fingerprint
    prior_alpha_history = (
        unique_pair.source_alpha_identity_fingerprint,
        seed.intermediate_alpha_identity_fingerprint,
        parent_chain.final_alpha_identity_fingerprint,
    )
    lineage_cycle = final_alpha in prior_alpha_history
    data: dict[str, object] = {
        "input_frontier_set_id": frontier_set_id,
        "input_frontier_id": frontier.frontier_id,
        "input_seed_set_id": parent_chain.seed_set_id,
        "input_chain_set_id": unique_pair.input_chain_set_id,
        "seed_id": seed.seed_id,
        "context_id": frontier.context_id,
        "root_ancestry_ids": frontier.root_ancestry_ids,
        "original_source_theorem_id": frontier.original_source_theorem_id,
        "original_source_representation_id": frontier.original_source_representation_id,
        "original_source_statement_content_hash": unique_pair.source_statement_content_hash,
        "original_source_alpha_identity_fingerprint": (
            unique_pair.source_alpha_identity_fingerprint
        ),
        "depth_one_theorem_id": seed.intermediate_theorem_id,
        "depth_one_representation_id": seed.intermediate_representation_id,
        "depth_one_alpha_identity_fingerprint": seed.intermediate_alpha_identity_fingerprint,
        "depth_two_theorem_id": parent_chain.final_theorem_id,
        "depth_two_representation_id": parent_chain.final_representation_id,
        "depth_two_alpha_identity_fingerprint": parent_chain.final_alpha_identity_fingerprint,
        "third_hop_source_theorem_id": source_theorem.theorem_id,
        "third_hop_source_representation_id": source_representation.representation_id,
        "parent_chain_ids": (parent_chain.chain_id,),
        "parent_chain_sequences": (parent_sequence,),
        "parent_chain_kind": frontier.parent_chain_kind,
        "preserved_intention": frontier.preserved_intention,
        "semantic_negative_hop_count": frontier.semantic_negative_hop_count,
        "third_hop_root_binding_id": root_binding.root_binding_id,
        "third_hop_result_id": result.result_id,
        "third_hop_result_line_number": line_number,
        "third_hop_profile_id": result.profile_id,
        "third_hop_rule_id": result.rule_id,
        "third_hop_family_id": result.draft.family_id,
        "third_hop_attempt_id": result.attempt.attempt_id,
        "third_hop_draft_id": result.draft.draft_id,
        "third_hop_audit_id": result.audit.audit_id,
        "third_hop_variant_id": result.variant.variant_id,
        "third_hop_certificate_kind": certificate_kind,
        "third_hop_certificate_sha256": certificate,
        "depth_three_sequences": (f"{parent_sequence}->{result.rule_id}",),
        "final_candidate_theorem_id": candidate.theorem_id,
        "final_candidate_representation_id": representation.representation_id,
        "final_candidate_code_hash": result.draft.candidate_code_hash,
        "final_alpha_identity_fingerprint": final_alpha,
        "prior_alpha_identity_fingerprints": prior_alpha_history,
        "original_source_alpha_return": original_source_alpha_return,
        "depth_one_alpha_return": depth_one_alpha_return,
        "third_hop_source_alpha_return": source_alpha_return,
        "lineage_cycle": lineage_cycle,
    }
    placeholder = DeterministicCompositionThirdHopChainRecord.model_construct(
        _fields_set=None,
        chain_id=f"detcomp_depth3_chain:{'0' * 64}",
        **data,
    )
    chain_id = "detcomp_depth3_chain:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "chain_id")
    )
    return (
        DeterministicCompositionThirdHopChainRecord.model_validate({"chain_id": chain_id, **data}),
        candidate,
        representation,
    )


def _audit_root(
    *,
    held_root: _HeldDirectory,
    inventory: _FrontierInventory,
    unique_pairs: _UniquePairInventory,
    lineage: _LineageInventory,
) -> tuple[
    CompositionSecondHopRootBinding,
    tuple[
        tuple[DeterministicCompositionThirdHopChainRecord, TheoremRecord, RepresentationRecord], ...
    ],
    Counter[str],
]:
    try:
        loaded = _load_root(held_root.path)
        _verify_directory_path_identity(held_root)
    except (CompositionUniquePairError, ProvisionalPairCombineError, OSError) as exc:
        raise CompositionThirdHopError(f"third-hop root failed exact audit: {exc}") from exc
    binding = loaded.binding
    _require(binding.run_kind == "e2", "third-hop root is not E2")
    _require(
        len(binding.rule_ids) == 1 and binding.rule_ids[0] in _RULES,
        "third-hop root is outside one P14-P18 family",
    )
    rule = binding.rule_ids[0]
    _require(
        _PROFILE_RULE.get(binding.profile_id) == rule, "third-hop profile/rule binding differs"
    )
    _require(
        binding.context_id == inventory.records[0].context_id,
        "third-hop root context differs from frontier",
    )
    _require(
        binding.theorem_partition_sha256 == inventory.manifest.theorem_output_sha256
        and binding.representation_partition_sha256
        == inventory.manifest.representation_output_sha256,
        "third-hop root source partitions differ from exact frontier",
    )
    _require(
        binding.source_count == inventory.manifest.frontier_count,
        "third-hop root source count differs",
    )
    _require(
        binding.result_count == inventory.manifest.frontier_count,
        "third-hop root result count differs",
    )

    try:
        run_kind, spec, _ = _load_run_models(held_root.path)
        source_inventory = _load_source_inventory(spec)
    except ProvisionalPairCombineError as exc:
        raise CompositionThirdHopError(str(exc)) from exc
    _require(run_kind == "e2", "third-hop run model is not E2")
    _require(
        source_inventory.ordered
        == tuple(zip(inventory.theorems, inventory.representations, strict=True)),
        "third-hop source payloads differ from exact frontier",
    )

    observations: dict[str, ProvisionalPairObservation] = {}
    for observation in loaded.observations:
        _require(
            observation.result_id not in observations, "third-hop observations duplicate a result"
        )
        observations[observation.result_id] = observation
    statuses: Counter[str] = Counter()
    saw_sources: set[str] = set()
    chains: list[
        tuple[DeterministicCompositionThirdHopChainRecord, TheoremRecord, RepresentationRecord]
    ] = []
    expected_expanded_chain_count = 0
    for line_number, raw, raw_line in _iter_jsonl_objects(held_root.path / "results.jsonl"):
        try:
            result = V2E2MaterializationResult.model_validate(raw)
        except ValueError as exc:
            raise CompositionThirdHopError(
                f"invalid third-hop result at {held_root.path}/results.jsonl:{line_number}: {exc}"
            ) from exc
        if raw_line != _canonical_line(result):
            raise CompositionThirdHopError("third-hop result is not canonical")
        statuses[result.terminal_status] += 1
        _require(result.rule_id == rule, "third-hop result rule differs from root")
        _require(
            len(result.attempt.source_theorem_ids) == 1
            and len(result.attempt.source_representation_ids) == 1,
            "third-hop result is not unary",
        )
        source_id = result.attempt.source_theorem_ids[0]
        _require(source_id not in saw_sources, "third-hop root repeats a frontier source")
        saw_sources.add(source_id)
        admitted = inventory.by_theorem_id.get(source_id)
        _require(admitted is not None, "third-hop result uses a foreign frontier source")
        assert admitted is not None
        frontier, theorem, representation = admitted
        unique_pair = unique_pairs.by_id[frontier.input_unique_pair_id]
        _require(
            result.attempt.source_representation_ids == (representation.representation_id,),
            "third-hop result source representation differs",
        )
        if result.terminal_status != "provisional_variant":
            continue
        matched_observation = observations.get(result.result_id)
        _require(
            matched_observation is not None,
            "provisional third-hop result lacks exact observation",
        )
        assert matched_observation is not None
        expected_expanded_chain_count += len(frontier.parent_chain_ids)
        for parent_chain_id in frontier.parent_chain_ids:
            parent_chain = lineage.chain_by_id[parent_chain_id]
            seed = lineage.seed_by_id[parent_chain.seed_id]
            chains.append(
                _build_chain(
                    frontier_set_id=inventory.manifest.frontier_set_id,
                    frontier=frontier,
                    unique_pair=unique_pair,
                    seed=seed,
                    parent_chain=parent_chain,
                    source_theorem=theorem,
                    source_representation=representation,
                    root_binding=binding,
                    line_number=line_number,
                    observation=matched_observation,
                    result=result,
                )
            )
    _require(
        saw_sources == set(inventory.by_theorem_id),
        "third-hop root does not cover exact frontier once",
    )
    _require(sum(statuses.values()) == binding.result_count, "third-hop result count differs")
    _require(
        binding.provisional_count == len(observations),
        "third-hop provisional result count differs",
    )
    _require(
        len(chains) == expected_expanded_chain_count,
        "third-hop expanded parent-path count differs",
    )
    try:
        final = _load_root(held_root.path)
        _verify_directory_path_identity(held_root)
    except (CompositionUniquePairError, ProvisionalPairCombineError, OSError) as exc:
        raise CompositionThirdHopError(f"third-hop root changed after validation: {exc}") from exc
    _require(
        final.binding == binding and final.observations == loaded.observations,
        "third-hop root changed after validation",
    )
    return _third_hop_binding(binding), tuple(chains), statuses


def _quarantine(
    chains: Sequence[DeterministicCompositionThirdHopChainRecord],
    reasons: Sequence[QuarantineReason],
) -> DeterministicCompositionThirdHopQuarantineRecord:
    first = chains[0]
    unique_key = hash_canonical(
        {
            "schema": "deterministic_v2_depth3_unique_pair_v2",
            "original_source_theorem_id": first.original_source_theorem_id,
            "final_alpha_identity_fingerprint": first.final_alpha_identity_fingerprint,
        }
    )
    data: dict[str, object] = {
        "canonical_unique_key": unique_key,
        "chain_ids": tuple(sorted(item.chain_id for item in chains)),
        "input_frontier_ids": tuple(sorted({item.input_frontier_id for item in chains})),
        "original_source_theorem_id": first.original_source_theorem_id,
        "original_source_statement_content_hash": first.original_source_statement_content_hash,
        "original_source_alpha_identity_fingerprint": (
            first.original_source_alpha_identity_fingerprint
        ),
        "final_alpha_identity_fingerprint": first.final_alpha_identity_fingerprint,
        "reason_codes": tuple(sorted(set(reasons))),
    }
    placeholder = DeterministicCompositionThirdHopQuarantineRecord.model_construct(
        _fields_set=None,
        quarantine_id=f"detcomp_depth3_quarantine:{'0' * 64}",
        **data,
    )
    quarantine_id = "detcomp_depth3_quarantine:" + hash_canonical(
        _without_id(placeholder.model_dump(mode="json"), "quarantine_id")
    )
    return DeterministicCompositionThirdHopQuarantineRecord.model_validate(
        {"quarantine_id": quarantine_id, **data}
    )


def _deduplicate(
    built: Sequence[
        tuple[DeterministicCompositionThirdHopChainRecord, TheoremRecord, RepresentationRecord]
    ],
) -> tuple[
    tuple[DeterministicCompositionThirdHopChainRecord, ...],
    tuple[DeterministicCompositionThirdHopPairRecord, ...],
    tuple[DeterministicCompositionThirdHopQuarantineRecord, ...],
    tuple[TheoremRecord, ...],
    tuple[RepresentationRecord, ...],
]:
    all_chains = tuple(sorted((item[0] for item in built), key=lambda item: item.chain_id))
    payload_by_chain = {item[0].chain_id: (item[1], item[2]) for item in built}
    _require(len(payload_by_chain) == len(built), "third-hop chain IDs are duplicated")
    quarantines: list[DeterministicCompositionThirdHopQuarantineRecord] = []
    candidates: list[DeterministicCompositionThirdHopChainRecord] = []
    for chain in all_chains:
        reasons: list[QuarantineReason] = []
        if chain.lineage_cycle:
            reasons.append("lineage_cycle")
        if chain.original_source_alpha_return:
            reasons.append("original_source_alpha_return")
        if chain.third_hop_source_alpha_return:
            reasons.append("third_hop_source_alpha_return")
        if reasons:
            quarantines.append(_quarantine((chain,), reasons))
        else:
            candidates.append(chain)

    grouped: dict[tuple[str, str], list[DeterministicCompositionThirdHopChainRecord]] = defaultdict(
        list
    )
    for chain in candidates:
        grouped[(chain.original_source_theorem_id, chain.final_alpha_identity_fingerprint)].append(
            chain
        )

    pairs: list[DeterministicCompositionThirdHopPairRecord] = []
    selected_theorems: list[TheoremRecord] = []
    selected_representations: list[RepresentationRecord] = []
    for key in sorted(grouped):
        group = tuple(sorted(grouped[key], key=lambda item: item.chain_id))
        first = group[0]
        lineage_values = {
            (
                item.context_id,
                item.root_ancestry_ids,
                item.original_source_representation_id,
                item.original_source_statement_content_hash,
                item.original_source_alpha_identity_fingerprint,
            )
            for item in group
        }
        intention_values = {
            (
                item.preserved_intention,
                item.semantic_negative_hop_count,
            )
            for item in group
        }
        conflict_reasons: list[QuarantineReason] = []
        if len(lineage_values) != 1:
            conflict_reasons.append("mixed_lineage_conflict")
        if len(intention_values) != 1:
            conflict_reasons.append("mixed_preserved_intention")
        if conflict_reasons:
            quarantines.append(_quarantine(group, conflict_reasons))
            continue
        selected = min(group, key=lambda item: item.final_candidate_theorem_id)
        theorem, representation = payload_by_chain[selected.chain_id]
        _require(
            theorem.theorem_id == selected.final_candidate_theorem_id,
            "selected theorem payload differs",
        )
        _require(
            representation.representation_id == selected.final_candidate_representation_id,
            "selected representation payload differs",
        )
        unique_key = hash_canonical(
            {
                "schema": "deterministic_v2_depth3_unique_pair_v2",
                "original_source_theorem_id": first.original_source_theorem_id,
                "final_alpha_identity_fingerprint": first.final_alpha_identity_fingerprint,
            }
        )
        data: dict[str, object] = {
            "canonical_unique_key": unique_key,
            "input_frontier_set_id": first.input_frontier_set_id,
            "context_id": first.context_id,
            "root_ancestry_ids": first.root_ancestry_ids,
            "original_source_theorem_id": first.original_source_theorem_id,
            "original_source_representation_id": first.original_source_representation_id,
            "original_source_statement_content_hash": (
                first.original_source_statement_content_hash
            ),
            "original_source_alpha_identity_fingerprint": (
                first.original_source_alpha_identity_fingerprint
            ),
            "frontier_ids": tuple(sorted({item.input_frontier_id for item in group})),
            "depth_two_theorem_ids": tuple(sorted({item.depth_two_theorem_id for item in group})),
            "depth_two_representation_ids": tuple(
                sorted({item.depth_two_representation_id for item in group})
            ),
            "chain_ids": tuple(item.chain_id for item in group),
            "depth_three_sequences": tuple(
                sorted({sequence for item in group for sequence in item.depth_three_sequences})
            ),
            "third_hop_rule_ids": tuple(sorted({item.third_hop_rule_id for item in group})),
            "third_hop_root_binding_ids": tuple(
                sorted({item.third_hop_root_binding_id for item in group})
            ),
            "third_hop_result_ids": tuple(sorted({item.third_hop_result_id for item in group})),
            "final_candidate_theorem_ids": tuple(
                sorted({item.final_candidate_theorem_id for item in group})
            ),
            "final_candidate_representation_ids": tuple(
                sorted({item.final_candidate_representation_id for item in group})
            ),
            "final_candidate_code_hashes": tuple(
                sorted({item.final_candidate_code_hash for item in group})
            ),
            "final_alpha_identity_fingerprint": first.final_alpha_identity_fingerprint,
            "selected_final_theorem_id": theorem.theorem_id,
            "selected_final_representation_id": representation.representation_id,
            "preserved_intention": first.preserved_intention,
            "semantic_negative_hop_count": first.semantic_negative_hop_count,
            "gross_chain_count": len(group),
            "duplicate_excess_count": len(group) - 1,
        }
        pairs.append(
            DeterministicCompositionThirdHopPairRecord.model_validate(
                {"pair_id": f"detcomp_depth3_pair:{unique_key}", **data}
            )
        )
        selected_theorems.append(theorem)
        selected_representations.append(representation)

    ordering = sorted(range(len(pairs)), key=lambda index: pairs[index].pair_id)
    pairs = [pairs[index] for index in ordering]
    selected_theorems = [selected_theorems[index] for index in ordering]
    selected_representations = [selected_representations[index] for index in ordering]
    quarantines.sort(key=lambda item: item.quarantine_id)
    quarantined_chain_ids = {chain_id for item in quarantines for chain_id in item.chain_ids}
    _require(
        not quarantined_chain_ids.intersection(
            chain_id for pair in pairs for chain_id in pair.chain_ids
        ),
        "a depth-three chain is both admitted and quarantined",
    )
    _require(
        len(quarantined_chain_ids) + sum(pair.gross_chain_count for pair in pairs)
        == len(all_chains),
        "depth-three chain classification does not reconcile",
    )
    return (
        all_chains,
        tuple(pairs),
        tuple(quarantines),
        tuple(selected_theorems),
        tuple(selected_representations),
    )


def _verify_existing_output(output: _HeldDirectory, payloads: Mapping[str, bytes]) -> None:
    try:
        with _snapshot_exact_directory(output, expected_names=_OUTPUT_FILES) as snapshot:
            for name, expected in payloads.items():
                if snapshot.files[name].payload != expected:
                    raise CompositionThirdHopError(
                        f"existing depth-three output differs: {output.path / name}"
                    )
            _verify_directory_path_identity(output)
    except CompositionUniquePairError as exc:
        raise CompositionThirdHopError(str(exc)) from exc


def _publish_or_verify(
    *,
    output_dir: Path,
    payloads: Mapping[str, bytes],
    forbidden_input_identities: frozenset[tuple[int, int]],
) -> bool:
    output_dir = _absolute_path(output_dir)
    if output_dir == Path(output_dir.anchor) or not output_dir.name:
        raise CompositionThirdHopError("depth-three output must name a child directory")
    try:
        with _open_held_directory(
            output_dir.parent,
            label="depth-three output parent",
            create=True,
            forbidden_identities=forbidden_input_identities,
        ) as parent:
            existing = _child_directory_metadata(parent, output_dir.name)
            if existing is not None:
                with _open_child_directory(parent, output_dir.name) as output:
                    _verify_child_identity(parent, output_dir.name, output.identity)
                    _verify_directory_path_identity(parent)
                    _verify_directory_path_identity(output)
                    _verify_existing_output(output, payloads)
                return True

            temporary_name = f".{output_dir.name}.{secrets.token_hex(16)}"
            os.mkdir(temporary_name, mode=0o700, dir_fd=parent.fd)
            temporary = _open_child_directory(parent, temporary_name)
            renamed = False
            try:
                for name, payload in payloads.items():
                    _write_new_file_at(temporary.fd, name, payload)
                os.fsync(temporary.fd)
                _verify_child_identity(parent, temporary_name, temporary.identity)
                if _rename_noreplace_at(parent.fd, temporary_name, output_dir.name):
                    renamed = True
                    temporary.path = output_dir
                    os.fsync(parent.fd)
                    _verify_child_identity(parent, output_dir.name, temporary.identity)
                    _verify_directory_path_identity(parent)
                    _verify_directory_path_identity(temporary)
                    _verify_existing_output(temporary, payloads)
                    return False
                _cleanup_private_directory(
                    parent,
                    temporary,
                    authoritative_names=frozenset({temporary_name}),
                )
                with _open_child_directory(parent, output_dir.name) as output:
                    _verify_child_identity(parent, output_dir.name, output.identity)
                    _verify_existing_output(output, payloads)
                return True
            except BaseException:
                names = {temporary_name}
                if renamed:
                    names.add(output_dir.name)
                _cleanup_private_directory(
                    parent,
                    temporary,
                    authoritative_names=frozenset(names),
                )
                raise
            finally:
                temporary.close()
    except CompositionUniquePairError as exc:
        raise CompositionThirdHopError(str(exc)) from exc


def audit_deterministic_v2_composition_third_hop(
    *,
    frontier_dir: Path,
    unique_pair_dir: Path,
    seed_dir: Path,
    chain_dir: Path,
    third_hop_roots: Sequence[Path],
    output_dir: Path,
) -> CompositionThirdHopArtifacts:
    """Bind five complete E2 roots and freeze provisional depth-three pairs."""

    frontier_path = _absolute_path(frontier_dir)
    unique_pair_path = _absolute_path(unique_pair_dir)
    seed_path = _absolute_path(seed_dir)
    chain_path = _absolute_path(chain_dir)
    root_paths = tuple(_absolute_path(path) for path in third_hop_roots)
    output_path = _absolute_path(output_dir)
    if len(root_paths) != 5:
        raise CompositionThirdHopError("exactly five completed P14-P18 roots are required")
    protected = (frontier_path, unique_pair_path, seed_path, chain_path, *root_paths)
    if any(output_path == path or output_path.is_relative_to(path) for path in protected):
        raise CompositionThirdHopError("depth-three output overlaps an immutable input")

    try:
        with ExitStack() as stack:
            frontier_root = stack.enter_context(
                _open_held_directory(frontier_path, label="third-hop frontier directory")
            )
            unique_pair_root = stack.enter_context(
                _open_held_directory(
                    unique_pair_path,
                    label="third-hop exact unique-pair directory",
                )
            )
            seed_root = stack.enter_context(
                _open_held_directory(seed_path, label="third-hop exact seed directory")
            )
            chain_root = stack.enter_context(
                _open_held_directory(chain_path, label="third-hop exact chain directory")
            )
            held_roots = tuple(
                stack.enter_context(
                    _open_held_directory(path, label=f"third-hop materialization root {index}")
                )
                for index, path in enumerate(root_paths)
            )
            identities = (
                (frontier_root.identity.device, frontier_root.identity.inode),
                (unique_pair_root.identity.device, unique_pair_root.identity.inode),
                (seed_root.identity.device, seed_root.identity.inode),
                (chain_root.identity.device, chain_root.identity.inode),
                *((root.identity.device, root.identity.inode) for root in held_roots),
            )
            _require(len(set(identities)) == 9, "third-hop inputs alias one another")
            inventory = _load_frontier(frontier_root)
            unique_pairs = _load_unique_pairs(unique_pair_root, frontier=inventory)
            lineage = _load_lineage_inventory(
                seed_root,
                chain_root,
                frontier=inventory,
                unique_pairs=unique_pairs,
            )
            _require(
                len({record.context_id for record in inventory.records}) == 1,
                "third-hop frontier has mixed contexts",
            )

            bindings: list[CompositionSecondHopRootBinding] = []
            audited_roots: list[
                tuple[
                    CompositionSecondHopRootBinding,
                    tuple[
                        tuple[
                            DeterministicCompositionThirdHopChainRecord,
                            TheoremRecord,
                            RepresentationRecord,
                        ],
                        ...,
                    ],
                    Counter[str],
                ]
            ] = []
            built: list[
                tuple[
                    DeterministicCompositionThirdHopChainRecord,
                    TheoremRecord,
                    RepresentationRecord,
                ]
            ] = []
            terminal_counts: Counter[str] = Counter()
            for held_root in held_roots:
                binding, root_chains, root_statuses = _audit_root(
                    held_root=held_root,
                    inventory=inventory,
                    unique_pairs=unique_pairs,
                    lineage=lineage,
                )
                bindings.append(binding)
                audited_roots.append((binding, root_chains, root_statuses))
                built.extend(root_chains)
                terminal_counts.update(root_statuses)
            bindings.sort(key=lambda item: item.root_binding_id)
            _require(
                len({item.root_binding_id for item in bindings}) == 5,
                "third-hop root bindings are duplicated",
            )
            _require(
                tuple(sorted(rule for item in bindings for rule in item.rule_ids))
                == tuple(sorted(_RULES)),
                "third-hop roots must cover P14-P18 exactly once",
            )
            chains, pairs, quarantines, theorems, representations = _deduplicate(built)
            for pair, theorem, representation in zip(pairs, theorems, representations, strict=True):
                _require(
                    pair.selected_final_theorem_id == theorem.theorem_id,
                    "pair/theorem output ordering differs",
                )
                _require(
                    pair.selected_final_representation_id == representation.representation_id,
                    "pair/representation output ordering differs",
                )
                _require(
                    representation.theorem_id == theorem.theorem_id,
                    "output theorem/representation differ",
                )

            chain_payload = _canonical_jsonl(chains)
            pair_payload = _canonical_jsonl(pairs)
            quarantine_payload = _canonical_jsonl(quarantines)
            theorem_payload = _canonical_jsonl(theorems)
            representation_payload = _canonical_jsonl(representations)
            quarantined_chain_ids = {
                chain_id for item in quarantines for chain_id in item.chain_ids
            }
            reason_counts = Counter(
                {
                    reason: sum(
                        len(item.chain_ids) for item in quarantines if reason in item.reason_codes
                    )
                    for reason in {reason for item in quarantines for reason in item.reason_codes}
                }
            )
            gross_chain_rule_counts: Counter[str] = Counter(
                item.third_hop_rule_id for item in chains
            )
            result_rule_counts = {binding.rule_ids[0]: binding.result_count for binding in bindings}
            manifest_data: dict[str, object] = {
                "input_frontier_set_id": inventory.manifest.frontier_set_id,
                "input_frontier_manifest_sha256": inventory.manifest_sha256,
                "input_frontier_records_sha256": inventory.manifest.frontier_output_sha256,
                "input_frontier_theorems_sha256": inventory.manifest.theorem_output_sha256,
                "input_frontier_representations_sha256": (
                    inventory.manifest.representation_output_sha256
                ),
                "input_frontier_count": inventory.manifest.frontier_count,
                "input_unique_pair_set_id": unique_pairs.manifest.unique_pair_set_id,
                "input_unique_pair_manifest_sha256": unique_pairs.manifest_sha256,
                "input_unique_pair_records_sha256": unique_pairs.manifest.unique_output_sha256,
                "input_unique_pair_count": unique_pairs.manifest.unique_pair_count,
                "input_seed_set_id": lineage.seed_manifest.seed_set_id,
                "input_seed_manifest_sha256": lineage.seed_manifest_sha256,
                "input_seed_records_sha256": lineage.seed_manifest.seed_output_sha256,
                "input_seed_theorems_sha256": lineage.seed_manifest.theorem_output_sha256,
                "input_seed_representations_sha256": (
                    lineage.seed_manifest.representation_output_sha256
                ),
                "input_seed_count": lineage.seed_manifest.seed_count,
                "input_chain_set_id": lineage.chain_manifest.chain_set_id,
                "input_chain_manifest_sha256": lineage.chain_manifest_sha256,
                "input_chain_records_sha256": lineage.chain_manifest.chain_output_sha256,
                "input_chain_count": lineage.chain_manifest.chain_count,
                "third_hop_roots": tuple(bindings),
                "third_hop_result_count": sum(item.result_count for item in bindings),
                "third_hop_provisional_result_count": sum(
                    item.provisional_count for item in bindings
                ),
                "terminal_status_counts": dict(sorted(terminal_counts.items())),
                "third_hop_rule_counts": dict(sorted(result_rule_counts.items())),
                "gross_chain_rule_counts": dict(sorted(gross_chain_rule_counts.items())),
                "gross_chain_count": len(chains),
                "expanded_parent_lineage_excess_count": len(chains)
                - sum(item.provisional_count for item in bindings),
                "admitted_chain_count": len(chains) - len(quarantined_chain_ids),
                "quarantined_chain_count": len(quarantined_chain_ids),
                "unique_pair_count": len(pairs),
                "theorem_count": len(theorems),
                "representation_count": len(representations),
                "duplicate_excess_count": len(chains) - len(quarantined_chain_ids) - len(pairs),
                "quarantine_reason_counts": dict(sorted(reason_counts.items())),
                "chain_output_sha256": hashlib.sha256(chain_payload).hexdigest(),
                "unique_output_sha256": hashlib.sha256(pair_payload).hexdigest(),
                "quarantine_output_sha256": hashlib.sha256(quarantine_payload).hexdigest(),
                "theorem_output_sha256": hashlib.sha256(theorem_payload).hexdigest(),
                "representation_output_sha256": hashlib.sha256(representation_payload).hexdigest(),
            }
            placeholder = DeterministicCompositionThirdHopManifest.model_construct(
                _fields_set=None,
                third_hop_set_id=f"detcomp_depth3_set:{'0' * 64}",
                **manifest_data,
            )
            set_id = "detcomp_depth3_set:" + hash_canonical(
                _without_id(placeholder.model_dump(mode="json"), "third_hop_set_id")
            )
            manifest = DeterministicCompositionThirdHopManifest.model_validate(
                {"third_hop_set_id": set_id, **manifest_data}
            )
            payloads = {
                "chains.jsonl": chain_payload,
                "unique_pairs.jsonl": pair_payload,
                "quarantine.jsonl": quarantine_payload,
                "theorems.jsonl": theorem_payload,
                "representations.jsonl": representation_payload,
                "manifest.json": _canonical_line(manifest),
            }
            # The held directory descriptors prevent path substitution, but a
            # producer could still replace files inside a directory between
            # the first audit and publication.  Repeat the complete frontier
            # and E2-root audit immediately before the no-replace publish.
            try:
                final_inventory = _load_frontier(frontier_root)
                _require(
                    final_inventory.manifest_sha256 == inventory.manifest_sha256
                    and final_inventory.manifest == inventory.manifest
                    and final_inventory.records == inventory.records
                    and final_inventory.theorems == inventory.theorems
                    and final_inventory.representations == inventory.representations,
                    "third-hop frontier changed before publication",
                )
                final_unique_pairs = _load_unique_pairs(
                    unique_pair_root,
                    frontier=final_inventory,
                )
                _require(
                    final_unique_pairs.manifest_sha256 == unique_pairs.manifest_sha256
                    and final_unique_pairs.manifest == unique_pairs.manifest
                    and final_unique_pairs.records == unique_pairs.records,
                    "third-hop unique pairs changed before publication",
                )
                final_lineage = _load_lineage_inventory(
                    seed_root,
                    chain_root,
                    frontier=final_inventory,
                    unique_pairs=final_unique_pairs,
                )
                _require(
                    final_lineage.seed_manifest_sha256 == lineage.seed_manifest_sha256
                    and final_lineage.seed_manifest == lineage.seed_manifest
                    and final_lineage.seeds == lineage.seeds
                    and final_lineage.chain_manifest_sha256 == lineage.chain_manifest_sha256
                    and final_lineage.chain_manifest == lineage.chain_manifest
                    and final_lineage.chains == lineage.chains,
                    "third-hop lineage inputs changed before publication",
                )
                for held_root, expected in zip(held_roots, audited_roots, strict=True):
                    observed = _audit_root(
                        held_root=held_root,
                        inventory=inventory,
                        unique_pairs=unique_pairs,
                        lineage=lineage,
                    )
                    _require(
                        observed == expected,
                        "third-hop root changed before publication",
                    )
            except CompositionThirdHopError as exc:
                raise CompositionThirdHopError(
                    f"third-hop inputs changed before publication: {exc}"
                ) from exc
            for item in (frontier_root, unique_pair_root, seed_root, chain_root, *held_roots):
                _verify_directory_path_identity(item)
            replayed = _publish_or_verify(
                output_dir=output_path,
                payloads=payloads,
                forbidden_input_identities=frozenset(identities),
            )
    except CompositionUniquePairError as exc:
        raise CompositionThirdHopError(str(exc)) from exc
    return CompositionThirdHopArtifacts(
        output_dir=output_path,
        manifest_path=output_path / "manifest.json",
        chains_path=output_path / "chains.jsonl",
        unique_pairs_path=output_path / "unique_pairs.jsonl",
        quarantine_path=output_path / "quarantine.jsonl",
        theorem_path=output_path / "theorems.jsonl",
        representation_path=output_path / "representations.jsonl",
        third_hop_set_id=set_id,
        gross_chain_count=len(chains),
        unique_pair_count=len(pairs),
        quarantine_count=len(quarantined_chain_ids),
        replayed=replayed,
    )


__all__ = [
    "CompositionThirdHopArtifacts",
    "CompositionThirdHopError",
    "DeterministicCompositionThirdHopChainRecord",
    "DeterministicCompositionThirdHopManifest",
    "DeterministicCompositionThirdHopPairRecord",
    "DeterministicCompositionThirdHopQuarantineRecord",
    "audit_deterministic_v2_composition_third_hop",
]
