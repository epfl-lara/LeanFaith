"""Immutable E2-only seed preparation for deterministic-v2 composition.

This module is deliberately only the admission boundary for a later two-hop
composition run.  It consumes an already completed provisional-pair combine,
revalidates every bound materialization root, admits only clean certificate-
backed E2 positive observations, and emits theorem/representation partitions
that the existing E2 and D0 scale runners can consume.

No semantic label, promotion, evidence record, or training eligibility is
created here.  In particular, D0 observations can never become composition
seeds, which makes an N-to-N composition impossible at the source boundary.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from collections import Counter, defaultdict
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
from leanfaith.transforms.protocol import (
    verify_transformation_attempt_id,
    verify_transformation_audit_id,
    verify_variant_draft_id,
)
from leanfaith.transforms.provisional_pair_combine import (
    MaterializationRootBinding,
    ProvisionalPairCombinationManifest,
    ProvisionalPairCombineError,
    ProvisionalPairObservation,
    UniqueProvisionalPair,
    _iter_jsonl_objects,
    _load_canonical_model,
    _load_root,
    _load_run_models,
    _load_source_inventory,
    _root_binding_identity_payload,
    _unique_pairs,
)
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_e2_materializer import V2E2MaterializationResult

_HEX64 = r"^[0-9a-f]{64}$"
_SEED_ID = r"^detcomp_seed:[0-9a-f]{64}$"
_SEED_SET_ID = r"^detcomp_seed_set:[0-9a-f]{64}$"
_OBSERVATION_ID = r"^detprov_observation:[0-9a-f]{64}$"
_UNIQUE_PAIR_ID = r"^detprov_pair:[0-9a-f]{64}$"
_ROOT_ID = r"^detprov_root:[0-9a-f]{64}$"

_COMBINATION_MANIFEST = "manifest.json"
_GROSS_OBSERVATIONS = "gross_observations.jsonl"
_UNIQUE_PAIRS = "unique_pairs.jsonl"
_SEEDS = "seeds.jsonl"
_THEOREMS = "theorems.jsonl"
_REPRESENTATIONS = "representations.jsonl"
_OUTPUT_MANIFEST = "manifest.json"
_EXPECTED_COMBINATION_FILES = frozenset({_COMBINATION_MANIFEST, _GROSS_OBSERVATIONS, _UNIQUE_PAIRS})
_EXPECTED_OUTPUT_FILES = frozenset({_SEEDS, _THEOREMS, _REPRESENTATIONS, _OUTPUT_MANIFEST})

type CertificateKind = Literal[
    "binder_permutation_certificate",
    "root_iff_reversal_certificate",
    "root_conjunction_reassociation_certificate",
    "hypothesis_packing_certificate",
    "root_equality_symmetry_certificate",
]

_E2_RULE_CERTIFICATES: dict[str, CertificateKind] = {
    "p14_independent_binder_permutation": "binder_permutation_certificate",
    "p15_root_iff_reversal": "root_iff_reversal_certificate",
    "p16_conjunction_reassociation": "root_conjunction_reassociation_certificate",
    "p17_hypothesis_packing": "hypothesis_packing_certificate",
    "p18_root_equality_symmetry": "root_equality_symmetry_certificate",
}


class CompositionSeedError(ValueError):
    """The composition seed boundary failed closed."""


def _seed_identity_payload(payload: Mapping[str, object]) -> dict[str, object]:
    identity = dict(payload)
    identity.pop("seed_id", None)
    return identity


def _seed_set_identity_payload(payload: Mapping[str, object]) -> dict[str, object]:
    identity = dict(payload)
    identity.pop("seed_set_id", None)
    return identity


class CompositionSeedRecord(StrictModel):
    """One exact E2 positive selected as a source for a second hop."""

    schema_version: Literal[1] = 1
    seed_id: str = Field(pattern=_SEED_ID)
    input_combination_hash: str = Field(pattern=_HEX64)
    unique_pair_id: str = Field(pattern=_UNIQUE_PAIR_ID)
    exact_pair_key: str = Field(pattern=_HEX64)
    first_hop_observation_ids: tuple[str, ...] = Field(min_length=1)
    selected_observation_id: str = Field(pattern=_OBSERVATION_ID)
    first_hop_root_binding_id: str = Field(pattern=_ROOT_ID)
    first_hop_result_id: str = Field(min_length=1)
    first_hop_result_line_number: int = Field(ge=1)
    first_hop_profile_id: str = Field(min_length=1)
    first_hop_rule_id: str = Field(min_length=1)
    first_hop_family_id: str = Field(min_length=1)
    first_hop_attempt_id: str = Field(min_length=1)
    first_hop_draft_id: str = Field(min_length=1)
    first_hop_audit_id: str = Field(min_length=1)
    first_hop_variant_id: str = Field(min_length=1)
    source_theorem_id: str = Field(min_length=1)
    source_representation_id: str = Field(min_length=1)
    intermediate_theorem_id: str = Field(min_length=1)
    intermediate_representation_id: str = Field(min_length=1)
    context_id: str = Field(min_length=1)
    root_ancestry_ids: tuple[str, ...] = Field(min_length=1)
    source_statement_content_hash: str = Field(pattern=_HEX64)
    source_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    intermediate_candidate_code_hash: str = Field(pattern=_HEX64)
    intermediate_alpha_identity_fingerprint: str = Field(pattern=_HEX64)
    certificate_kind: CertificateKind
    certificate_sha256: str = Field(pattern=_HEX64)
    execution_settings_provenance: Literal["recorded", "legacy_unknown"]
    workers: int | None = Field(default=None, ge=1)
    memory_hard_limit_mb: int | None = Field(default=None, ge=1)
    chain_depth: Literal[1] = 1
    seed_evidence_class: Literal["E2"] = "E2"
    intended_relation: Literal["equivalent"] = "equivalent"
    polarity_metadata: Literal["positive"] = "positive"
    quality_tier: Literal["provisional"] = "provisional"
    semantic_label_id: None = None
    resolved_label_count: Literal[0] = 0
    promoted_item_count: Literal[0] = 0
    training_eligible: Literal[False] = False
    evaluation_eligible: Literal[False] = False

    @model_validator(mode="after")
    def _coherent(self) -> CompositionSeedRecord:
        payload = _seed_identity_payload(self.model_dump(mode="json"))
        expected = f"detcomp_seed:{hash_canonical(payload)}"
        if self.seed_id != expected:
            raise ValueError("seed_id does not match its immutable payload")
        if self.first_hop_observation_ids != tuple(sorted(set(self.first_hop_observation_ids))):
            raise ValueError("first_hop_observation_ids must be sorted and unique")
        if self.selected_observation_id not in self.first_hop_observation_ids:
            raise ValueError("selected observation is absent from first-hop provenance")
        if self.root_ancestry_ids != tuple(sorted(set(self.root_ancestry_ids))):
            raise ValueError("root_ancestry_ids must be sorted and unique")
        if self.unique_pair_id != f"detprov_pair:{self.exact_pair_key}":
            raise ValueError("unique_pair_id does not match exact_pair_key")
        expected_certificate = _E2_RULE_CERTIFICATES.get(self.first_hop_rule_id)
        if expected_certificate != self.certificate_kind:
            raise ValueError("certificate kind does not match the E2 rule")
        if self.first_hop_family_id != self.first_hop_rule_id:
            raise ValueError("first-hop E2 family and rule must match")
        if self.source_theorem_id == self.intermediate_theorem_id:
            raise ValueError("composition seed cannot be a theorem identity")
        if self.source_statement_content_hash == self.intermediate_candidate_code_hash:
            raise ValueError("composition seed cannot preserve exact source code")
        if self.source_alpha_identity_fingerprint == self.intermediate_alpha_identity_fingerprint:
            raise ValueError("composition seed cannot preserve the alpha identity")
        if self.execution_settings_provenance == "recorded" and self.workers is None:
            raise ValueError("recorded execution settings require workers")
        if self.execution_settings_provenance == "legacy_unknown" and (
            self.workers is not None or self.memory_hard_limit_mb is not None
        ):
            raise ValueError("legacy execution settings must remain unknown")
        return self


class CompositionSeedManifest(StrictModel):
    """Self-authenticating output manifest for one composition seed set."""

    schema_version: Literal[1] = 1
    artifact_kind: Literal["deterministic_v2_composition_seed_set"] = (
        "deterministic_v2_composition_seed_set"
    )
    seed_set_id: str = Field(pattern=_SEED_SET_ID)
    input_combination_hash: str = Field(pattern=_HEX64)
    input_combination_manifest_sha256: str = Field(pattern=_HEX64)
    input_gross_observations_sha256: str = Field(pattern=_HEX64)
    input_unique_pairs_sha256: str = Field(pattern=_HEX64)
    input_root_binding_ids: tuple[str, ...] = Field(min_length=1)
    input_gross_observation_count: int = Field(ge=1)
    excluded_observation_counts: dict[str, int]
    admitted_e2_observation_count: int = Field(ge=1)
    seed_count: int = Field(ge=1)
    exact_duplicate_excess_count: int = Field(ge=0)
    seed_output: Literal["seeds.jsonl"] = "seeds.jsonl"
    seed_output_sha256: str = Field(pattern=_HEX64)
    theorem_output: Literal["theorems.jsonl"] = "theorems.jsonl"
    theorem_output_sha256: str = Field(pattern=_HEX64)
    representation_output: Literal["representations.jsonl"] = "representations.jsonl"
    representation_output_sha256: str = Field(pattern=_HEX64)
    theorem_count: int = Field(ge=1)
    representation_count: int = Field(ge=1)
    seed_source_policy: Literal["clean_certificate_backed_e2_positive_only_v1"] = (
        "clean_certificate_backed_e2_positive_only_v1"
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
    def _coherent(self) -> CompositionSeedManifest:
        expected = "detcomp_seed_set:" + hash_canonical(
            _seed_set_identity_payload(self.model_dump(mode="json"))
        )
        if self.seed_set_id != expected:
            raise ValueError("seed_set_id does not match its immutable payload")
        if self.input_root_binding_ids != tuple(sorted(set(self.input_root_binding_ids))):
            raise ValueError("input_root_binding_ids must be sorted and unique")
        if any(count < 0 for count in self.excluded_observation_counts.values()):
            raise ValueError("excluded observation counts cannot be negative")
        if (
            self.admitted_e2_observation_count + sum(self.excluded_observation_counts.values())
            != self.input_gross_observation_count
        ):
            raise ValueError("admission and exclusion counts do not reconcile")
        if self.exact_duplicate_excess_count != (
            self.admitted_e2_observation_count - self.seed_count
        ):
            raise ValueError("exact duplicate excess does not reconcile")
        if not (self.seed_count == self.theorem_count == self.representation_count):
            raise ValueError("seed/theorem/representation counts do not reconcile")
        return self


@dataclass(frozen=True, slots=True)
class CompositionSeedArtifacts:
    output_dir: Path
    manifest_path: Path
    seeds_path: Path
    theorem_path: Path
    representation_path: Path
    seed_set_id: str
    seed_count: int
    replayed: bool


@dataclass(frozen=True, slots=True)
class _ValidatedRoot:
    path: Path
    binding: MaterializationRootBinding
    results_by_line: Mapping[int, V2E2MaterializationResult]
    source_theorems: Mapping[str, TheoremRecord]
    source_representations: Mapping[str, RepresentationRecord]


@dataclass(frozen=True, slots=True)
class _AdmittedObservation:
    observation: ProvisionalPairObservation
    result: V2E2MaterializationResult
    source_theorem: TheoremRecord
    source_representation: RepresentationRecord
    certificate_kind: CertificateKind
    certificate_sha256: str
    root_binding: MaterializationRootBinding


def _canonical_line(model: StrictModel) -> bytes:
    return canonical_json_bytes(model.model_dump(mode="json")) + b"\n"


def _canonical_jsonl(records: Sequence[StrictModel]) -> bytes:
    return b"".join(_canonical_line(record) for record in records)


def _parse_canonical_jsonl[ModelT: StrictModel](
    path: Path,
    model: type[ModelT],
) -> tuple[ModelT, ...]:
    output: list[ModelT] = []
    for line_number, raw, raw_line in _iter_jsonl_objects(path):
        try:
            record = model.model_validate(raw)
        except ValueError as exc:
            raise CompositionSeedError(
                f"invalid {model.__name__} at {path}:{line_number}: {exc}"
            ) from exc
        if raw_line != _canonical_line(record):
            raise CompositionSeedError(f"non-canonical {model.__name__} at {path}:{line_number}")
        output.append(record)
    return tuple(output)


def _load_combination(
    combination_dir: Path,
) -> tuple[
    ProvisionalPairCombinationManifest,
    tuple[ProvisionalPairObservation, ...],
    tuple[UniqueProvisionalPair, ...],
]:
    combination_dir = combination_dir.resolve(strict=True)
    if not combination_dir.is_dir() or combination_dir.is_symlink():
        raise CompositionSeedError("combination input is not a regular directory")
    actual = {path.name for path in combination_dir.iterdir()}
    if actual != _EXPECTED_COMBINATION_FILES:
        raise CompositionSeedError(
            "combination directory is not exact: "
            f"expected {sorted(_EXPECTED_COMBINATION_FILES)}, found {sorted(actual)}"
        )
    try:
        manifest = _load_canonical_model(
            combination_dir / _COMBINATION_MANIFEST,
            ProvisionalPairCombinationManifest,
        )
        gross = _parse_canonical_jsonl(
            combination_dir / _GROSS_OBSERVATIONS,
            ProvisionalPairObservation,
        )
        unique = _parse_canonical_jsonl(
            combination_dir / _UNIQUE_PAIRS,
            UniqueProvisionalPair,
        )
    except ProvisionalPairCombineError as exc:
        raise CompositionSeedError(str(exc)) from exc
    if hash_file(combination_dir / _GROSS_OBSERVATIONS) != manifest.gross_output_sha256:
        raise CompositionSeedError("gross observations hash differs from combination manifest")
    if hash_file(combination_dir / _UNIQUE_PAIRS) != manifest.unique_output_sha256:
        raise CompositionSeedError("unique pairs hash differs from combination manifest")
    if len(gross) != manifest.gross_observation_count:
        raise CompositionSeedError("gross observation count differs from combination manifest")
    if len(unique) != manifest.unique_pair_count:
        raise CompositionSeedError("unique pair count differs from combination manifest")
    if len({item.observation_id for item in gross}) != len(gross):
        raise CompositionSeedError("combination contains duplicate observation IDs")
    if len({item.unique_pair_id for item in unique}) != len(unique):
        raise CompositionSeedError("combination contains duplicate unique-pair IDs")
    if _unique_pairs(gross) != unique:
        raise CompositionSeedError("unique pairs do not replay from gross observations")
    return manifest, gross, unique


def _load_bound_roots(
    *,
    materialization_roots: Sequence[Path],
    manifest: ProvisionalPairCombinationManifest,
) -> dict[str, _ValidatedRoot]:
    if not materialization_roots:
        raise CompositionSeedError("at least one bound materialization root is required")
    expected_bindings = {item.root_binding_id: item for item in manifest.root_bindings}
    loaded: dict[str, _ValidatedRoot] = {}
    for path in sorted({item.resolve(strict=True) for item in materialization_roots}):
        try:
            root = _load_root(path)
        except ProvisionalPairCombineError as exc:
            raise CompositionSeedError(str(exc)) from exc
        expected = expected_bindings.get(root.binding.root_binding_id)
        if expected is None:
            raise CompositionSeedError(f"materialization root is absent from combination: {path}")
        if _root_binding_identity_payload(root.binding.model_dump(mode="json")) != (
            _root_binding_identity_payload(expected.model_dump(mode="json"))
        ):
            raise CompositionSeedError("materialization root binding differs from combination")
        if root.binding.root_binding_id in loaded:
            raise CompositionSeedError("duplicate materialization root binding")

        run_kind, spec, _ = _load_run_models(path)
        source_inventory = _load_source_inventory(spec)
        source_representations = {
            representation.representation_id: representation
            for _, representation in source_inventory.ordered
        }
        results: dict[int, V2E2MaterializationResult] = {}
        if run_kind == "e2":
            for line_number, raw, raw_line in _iter_jsonl_objects(path / "results.jsonl"):
                try:
                    result = V2E2MaterializationResult.model_validate(raw)
                except ValueError as exc:
                    raise CompositionSeedError(
                        f"invalid E2 result at {path}/results.jsonl:{line_number}: {exc}"
                    ) from exc
                if raw_line != _canonical_line(result):
                    raise CompositionSeedError(
                        f"non-canonical E2 result at {path}/results.jsonl:{line_number}"
                    )
                results[line_number] = result
        loaded[root.binding.root_binding_id] = _ValidatedRoot(
            path=path,
            binding=root.binding,
            results_by_line=results,
            source_theorems=source_inventory.by_theorem_id,
            source_representations=source_representations,
        )
    if set(loaded) != set(expected_bindings):
        missing = sorted(set(expected_bindings) - set(loaded))
        raise CompositionSeedError(f"bound materialization roots are incomplete: {missing}")
    return loaded


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise CompositionSeedError(message)


def _admit_e2_observation(
    observation: ProvisionalPairObservation,
    root: _ValidatedRoot,
) -> _AdmittedObservation:
    _require(root.binding.run_kind == "e2", "composition seed is not from an E2 root")
    result = root.results_by_line.get(observation.result_line_number)
    _require(result is not None, "composition observation result line is unavailable")
    assert result is not None
    _require(result.result_id == observation.result_id, "composition result ID differs")
    _require(result.terminal_status == "provisional_variant", "E2 seed is not provisional")
    _require(result.evidence_class == "E2", "positive seed does not declare E2 evidence")
    _require(result.resolved_label_count == 0, "E2 seed carries resolved labels")
    _require(result.promoted_item_count == 0, "E2 seed carries promoted items")
    _require(result.training_eligible is False, "E2 seed is training eligible")
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
        "E2 seed lacks complete mechanical lineage",
    )
    assert result.draft is not None
    assert result.candidate_theorem is not None
    assert result.candidate_representation is not None
    assert result.audit is not None
    assert result.variant is not None
    verify_transformation_attempt_id(result.attempt)
    verify_variant_draft_id(result.draft)
    verify_transformation_audit_id(result.audit)
    _require(result.rule_id in _E2_RULE_CERTIFICATES, "E2 seed rule is outside P14-P18")
    _require(result.draft.family_id == result.rule_id, "E2 seed family/rule mismatch")
    _require(observation.family_id == result.rule_id, "observation family differs from E2 rule")
    _require(
        result.draft.intended_relation == IntendedRelation.EQUIVALENT,
        "E2 seed intention is not equivalent",
    )
    _require(
        result.variant.polarity_metadata == Polarity.POSITIVE,
        "E2 seed polarity is not positive",
    )
    _require(
        result.variant.quality_tier == QualityTier.PROVISIONAL,
        "E2 seed quality is not provisional",
    )
    _require(result.variant.validation_evidence_id is None, "E2 seed embeds evidence credit")
    _require(not result.audit.violation_codes, "E2 seed audit has violations")
    _require(result.audit.structural_diff_ok is True, "E2 structural certificate failed")
    _require(result.audit.atom_mapping_ok is True, "E2 atom mapping certificate failed")
    _require(result.audit.inverse_or_roundtrip_ok is True, "E2 inverse certificate failed")
    _require(result.audit.metadata.get("evidence_class") == "E2", "E2 audit class differs")
    _require(
        result.audit.metadata.get("resolved_semantic_label") is False,
        "E2 audit claims a semantic label",
    )
    _require(
        result.audit.metadata.get("training_eligible") is False,
        "E2 audit claims training eligibility",
    )
    certificate_kind = _E2_RULE_CERTIFICATES[result.rule_id]
    certificate = result.audit.metadata.get(certificate_kind)
    _require(
        isinstance(certificate, str) and len(certificate) == 64,
        "E2 audit lacks its family certificate hash",
    )
    assert isinstance(certificate, str)
    try:
        int(certificate, 16)
    except ValueError as exc:
        raise CompositionSeedError("E2 family certificate is not hexadecimal") from exc

    _require(len(result.draft.source_theorem_ids) == 1, "E2 seed is not unary")
    _require(len(result.draft.source_representation_ids) == 1, "E2 seed source is ambiguous")
    source_theorem = root.source_theorems.get(result.draft.source_theorem_ids[0])
    source_representation = root.source_representations.get(
        result.draft.source_representation_ids[0]
    )
    _require(source_theorem is not None, "E2 source theorem is unavailable")
    _require(source_representation is not None, "E2 source representation is unavailable")
    assert source_theorem is not None
    assert source_representation is not None
    _require(not source_theorem.parent_theorem_ids, "composition seed source is already derived")
    _require(
        source_theorem.source != "deterministic_transform",
        "composition seed source is already a deterministic candidate",
    )
    _require(
        source_representation.theorem_id == source_theorem.theorem_id,
        "E2 source theorem/representation mismatch",
    )
    _require(
        source_theorem.context_id
        == source_representation.context_id
        == result.candidate_theorem.context_id
        == result.candidate_representation.context_id
        == observation.context_id,
        "E2 composition contexts differ",
    )
    _require(
        result.candidate_theorem.parent_theorem_ids == (source_theorem.theorem_id,),
        "E2 intermediate parent differs from the source",
    )
    _require(
        result.candidate_theorem.root_ancestry_ids == source_theorem.root_ancestry_ids,
        "E2 intermediate root ancestry differs from the source",
    )
    _require(
        observation.source_root_ancestry_ids == source_theorem.root_ancestry_ids,
        "E2 observation root ancestry differs from the source",
    )
    _require(
        result.candidate_representation.theorem_id == result.candidate_theorem.theorem_id,
        "E2 intermediate theorem/representation mismatch",
    )
    _require(
        result.candidate_representation.normalization_version == NORMALIZATION_VERSION,
        "E2 intermediate normalization version differs",
    )
    _require(
        result.candidate_representation.content_hash
        == _representation_payload_hash(result.candidate_representation),
        "E2 intermediate representation hash is invalid",
    )
    _require(
        source_representation.alpha_identity_fingerprint is not None,
        "E2 source lacks an alpha identity fingerprint",
    )
    _require(
        result.candidate_representation.alpha_identity_fingerprint is not None,
        "E2 intermediate lacks an alpha identity fingerprint",
    )
    _require(
        source_theorem.statement_content_hash != result.draft.candidate_code_hash,
        "E2 seed is an exact source-code identity",
    )
    _require(
        source_representation.alpha_identity_fingerprint
        != result.candidate_representation.alpha_identity_fingerprint,
        "E2 seed is an alpha identity",
    )
    return _AdmittedObservation(
        observation=observation,
        result=result,
        source_theorem=source_theorem,
        source_representation=source_representation,
        certificate_kind=certificate_kind,
        certificate_sha256=certificate,
        root_binding=root.binding,
    )


def _build_seed(
    *,
    combination_hash: str,
    group: Sequence[_AdmittedObservation],
) -> tuple[CompositionSeedRecord, TheoremRecord, RepresentationRecord]:
    ordered = tuple(sorted(group, key=lambda item: item.observation.observation_id))
    selected = ordered[0]
    first = selected.observation
    result = selected.result
    assert result.draft is not None
    assert result.audit is not None
    assert result.variant is not None
    assert result.candidate_theorem is not None
    assert result.candidate_representation is not None
    if any(
        item.observation.exact_pair_key != first.exact_pair_key
        or item.source_theorem.theorem_id != selected.source_theorem.theorem_id
        or item.result.draft is None
        or item.result.draft.candidate_code_hash != result.draft.candidate_code_hash
        for item in ordered
    ):
        raise CompositionSeedError("exact E2 seed group has inconsistent payloads")
    data: dict[str, object] = {
        "input_combination_hash": combination_hash,
        "unique_pair_id": f"detprov_pair:{first.exact_pair_key}",
        "exact_pair_key": first.exact_pair_key,
        "first_hop_observation_ids": tuple(item.observation.observation_id for item in ordered),
        "selected_observation_id": first.observation_id,
        "first_hop_root_binding_id": first.root_binding_id,
        "first_hop_result_id": result.result_id,
        "first_hop_result_line_number": first.result_line_number,
        "first_hop_profile_id": result.profile_id,
        "first_hop_rule_id": result.rule_id,
        "first_hop_family_id": result.draft.family_id,
        "first_hop_attempt_id": result.attempt.attempt_id,
        "first_hop_draft_id": result.draft.draft_id,
        "first_hop_audit_id": result.audit.audit_id,
        "first_hop_variant_id": result.variant.variant_id,
        "source_theorem_id": selected.source_theorem.theorem_id,
        "source_representation_id": selected.source_representation.representation_id,
        "intermediate_theorem_id": result.candidate_theorem.theorem_id,
        "intermediate_representation_id": result.candidate_representation.representation_id,
        "context_id": first.context_id,
        "root_ancestry_ids": selected.source_theorem.root_ancestry_ids,
        "source_statement_content_hash": selected.source_theorem.statement_content_hash,
        "source_alpha_identity_fingerprint": (
            selected.source_representation.alpha_identity_fingerprint
        ),
        "intermediate_candidate_code_hash": result.draft.candidate_code_hash,
        "intermediate_alpha_identity_fingerprint": (
            result.candidate_representation.alpha_identity_fingerprint
        ),
        "certificate_kind": selected.certificate_kind,
        "certificate_sha256": selected.certificate_sha256,
        "execution_settings_provenance": selected.root_binding.execution_settings_provenance,
        "workers": selected.root_binding.workers,
        "memory_hard_limit_mb": selected.root_binding.memory_hard_limit_mb,
    }
    placeholder = CompositionSeedRecord.model_construct(
        _fields_set=None,
        seed_id=f"detcomp_seed:{'0' * 64}",
        **data,
    )
    seed_payload = _seed_identity_payload(placeholder.model_dump(mode="json"))
    seed_id = f"detcomp_seed:{hash_canonical(seed_payload)}"
    return (
        CompositionSeedRecord.model_validate({"seed_id": seed_id, **data}),
        result.candidate_theorem,
        result.candidate_representation,
    )


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
        raise CompositionSeedError("existing composition seed output is not a directory")
    actual = {path.name for path in output_dir.iterdir()}
    if actual != _EXPECTED_OUTPUT_FILES:
        raise CompositionSeedError("existing composition seed output is not an exact replay")
    for name, payload in payloads.items():
        path = output_dir / name
        if not path.is_file() or path.is_symlink() or path.read_bytes() != payload:
            raise CompositionSeedError(f"existing composition seed output differs: {path}")


def prepare_deterministic_v2_composition_seeds(
    *,
    combination_dir: Path,
    materialization_roots: Sequence[Path],
    output_dir: Path,
) -> CompositionSeedArtifacts:
    """Prepare immutable clean-E2 intermediate sources for a second hop."""

    manifest, gross, _ = _load_combination(combination_dir)
    roots = _load_bound_roots(materialization_roots=materialization_roots, manifest=manifest)
    rebound_observations = {
        observation.observation_id: observation
        for root in roots.values()
        for observation in _load_root(root.path).observations
    }
    if set(rebound_observations) != {item.observation_id for item in gross}:
        raise CompositionSeedError("bound roots do not reproduce combination observations")
    for observation in gross:
        if rebound_observations[observation.observation_id] != observation:
            raise CompositionSeedError("bound root observation differs from combination")

    excluded: Counter[str] = Counter()
    admitted: list[_AdmittedObservation] = []
    for observation in gross:
        root = roots[observation.root_binding_id]
        if root.binding.run_kind != "e2":
            excluded[f"non_e2_{root.binding.run_kind}"] += 1
            continue
        admitted.append(_admit_e2_observation(observation, root))
    if not admitted:
        raise CompositionSeedError("no clean certificate-backed E2 positive seeds were admitted")

    grouped: dict[str, list[_AdmittedObservation]] = defaultdict(list)
    for item in admitted:
        grouped[item.observation.exact_pair_key].append(item)
    built = tuple(
        _build_seed(combination_hash=manifest.combination_hash, group=group)
        for _, group in sorted(grouped.items())
    )
    seeds = tuple(sorted((item[0] for item in built), key=lambda item: item.seed_id))
    theorem_by_seed = {item[0].seed_id: item[1] for item in built}
    representation_by_seed = {item[0].seed_id: item[2] for item in built}
    theorems = tuple(theorem_by_seed[item.seed_id] for item in seeds)
    representations = tuple(representation_by_seed[item.seed_id] for item in seeds)
    if len({item.theorem_id for item in theorems}) != len(theorems):
        raise CompositionSeedError("composition seeds contain duplicate theorem IDs")
    if len({item.representation_id for item in representations}) != len(representations):
        raise CompositionSeedError("composition seeds contain duplicate representation IDs")
    for seed, theorem, representation in zip(seeds, theorems, representations, strict=True):
        if theorem.theorem_id != seed.intermediate_theorem_id:
            raise CompositionSeedError("seed theorem output order differs")
        if representation.representation_id != seed.intermediate_representation_id:
            raise CompositionSeedError("seed representation output order differs")
        if representation.theorem_id != theorem.theorem_id:
            raise CompositionSeedError("seed theorem/representation output differs")

    seeds_payload = _canonical_jsonl(seeds)
    theorem_payload = _canonical_jsonl(theorems)
    representation_payload = _canonical_jsonl(representations)
    combination_dir = combination_dir.resolve(strict=True)
    manifest_data: dict[str, object] = {
        "input_combination_hash": manifest.combination_hash,
        "input_combination_manifest_sha256": hash_file(combination_dir / _COMBINATION_MANIFEST),
        "input_gross_observations_sha256": hash_file(combination_dir / _GROSS_OBSERVATIONS),
        "input_unique_pairs_sha256": hash_file(combination_dir / _UNIQUE_PAIRS),
        "input_root_binding_ids": tuple(sorted(roots)),
        "input_gross_observation_count": len(gross),
        "excluded_observation_counts": dict(sorted(excluded.items())),
        "admitted_e2_observation_count": len(admitted),
        "seed_count": len(seeds),
        "exact_duplicate_excess_count": len(admitted) - len(seeds),
        "seed_output_sha256": hashlib.sha256(seeds_payload).hexdigest(),
        "theorem_output_sha256": hashlib.sha256(theorem_payload).hexdigest(),
        "representation_output_sha256": hashlib.sha256(representation_payload).hexdigest(),
        "theorem_count": len(theorems),
        "representation_count": len(representations),
    }
    placeholder = CompositionSeedManifest.model_construct(
        _fields_set=None,
        seed_set_id=f"detcomp_seed_set:{'0' * 64}",
        **manifest_data,
    )
    seed_set_id = "detcomp_seed_set:" + hash_canonical(
        _seed_set_identity_payload(placeholder.model_dump(mode="json"))
    )
    output_manifest = CompositionSeedManifest.model_validate(
        {"seed_set_id": seed_set_id, **manifest_data}
    )
    payloads = {
        _SEEDS: seeds_payload,
        _THEOREMS: theorem_payload,
        _REPRESENTATIONS: representation_payload,
        _OUTPUT_MANIFEST: _canonical_line(output_manifest),
    }

    output_dir = output_dir.resolve(strict=False)
    input_roots = {item.resolve(strict=True) for item in materialization_roots}
    if output_dir == combination_dir or output_dir.is_relative_to(combination_dir):
        raise CompositionSeedError("output directory cannot be inside the combination input")
    if any(output_dir == root or output_dir.is_relative_to(root) for root in input_roots):
        raise CompositionSeedError("output directory cannot be inside a materialization root")
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
    return CompositionSeedArtifacts(
        output_dir=output_dir,
        manifest_path=output_dir / _OUTPUT_MANIFEST,
        seeds_path=output_dir / _SEEDS,
        theorem_path=output_dir / _THEOREMS,
        representation_path=output_dir / _REPRESENTATIONS,
        seed_set_id=seed_set_id,
        seed_count=len(seeds),
        replayed=replayed,
    )


__all__ = [
    "CompositionSeedArtifacts",
    "CompositionSeedError",
    "CompositionSeedManifest",
    "CompositionSeedRecord",
    "prepare_deterministic_v2_composition_seeds",
]
