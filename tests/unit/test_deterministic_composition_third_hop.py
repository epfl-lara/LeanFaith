"""Fail-closed tests for the exact depth-three composition admission stage."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.config.models import StrictModel
from leanfaith.lean.leaninteract_backend import LeanInteractBackend
from leanfaith.lean.protocol import LeanStatus
from leanfaith.representations import (
    NORMALIZATION_VERSION,
    TheoremForRepresentation,
    alpha_identity_fingerprint,
)
from leanfaith.representations.atoms import operator_tree, semantic_atoms
from leanfaith.schemas.enums import IntendedRelation, Polarity
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_chain import (
    CompositionSecondHopRootBinding,
    DeterministicCompositionChainManifest,
    DeterministicCompositionChainRecord,
)
from leanfaith.transforms.composition_polarity_frontier import (
    DeterministicCompositionPolarityFrontierManifest,
    DeterministicCompositionPolarityFrontierRecord,
)
from leanfaith.transforms.composition_seed import CompositionSeedManifest, CompositionSeedRecord
from leanfaith.transforms.composition_third_hop import (
    CompositionThirdHopError,
    DeterministicCompositionThirdHopChainRecord,
    DeterministicCompositionThirdHopManifest,
    _build_chain,
    _deduplicate,
    audit_deterministic_v2_composition_third_hop,
)
from leanfaith.transforms.composition_unique_pairs import (
    DeterministicCompositionUniquePairManifest,
    DeterministicCompositionUniquePairRecord,
)
from leanfaith.transforms.provisional_pair_combine import (
    FileBinding,
    ProvisionalPairObservation,
    _iter_jsonl_objects,
    _load_root,
    _LoadedRoot,
)
from leanfaith.transforms.scale_materializer import _representation_payload_hash
from leanfaith.transforms.v2_e2_materializer import V2E2MaterializationResult
from leanfaith.transforms.v2_e2_p14_runtime import build_v2_e2_p14_runtime
from leanfaith.transforms.v2_e2_p15_runtime import build_v2_e2_p15_runtime
from leanfaith.transforms.v2_e2_p16_runtime import build_v2_e2_p16_runtime
from leanfaith.transforms.v2_e2_p17_runtime import build_v2_e2_p17_runtime
from leanfaith.transforms.v2_e2_p18_runtime import build_v2_e2_p18_runtime
from leanfaith.transforms.v2_e2_runtime import V2E2Runtime
from leanfaith.transforms.v2_e2_scale_run import run_v2_e2_scale
from tests.unit.test_deterministic_v2_n11_scale import _BatchBackend
from tests.unit.test_deterministic_v2_p18 import _records, _root

_SOURCE = "theorem depth2 (x y : Nat) : x = y := by sorry"


def _line(record: object) -> bytes:
    if isinstance(record, StrictModel):
        record = record.model_dump(mode="json")
    return canonical_json_bytes(record) + b"\n"


def _identified_record(data: dict[str, object]) -> DeterministicCompositionPolarityFrontierRecord:
    placeholder = DeterministicCompositionPolarityFrontierRecord.model_construct(
        _fields_set=None,
        frontier_id="detcomp_frontier:" + "0" * 64,
        **data,
    )
    payload = placeholder.model_dump(mode="json")
    payload.pop("frontier_id")
    return DeterministicCompositionPolarityFrontierRecord.model_validate(
        {"frontier_id": "detcomp_frontier:" + hash_canonical(payload), **data}
    )


def _frontier(tmp_path: Path, *, key: str = "main") -> tuple[Path, Path, Path, Path]:
    theorem, representation = _records(_SOURCE, f"depth3-{key}", _root())
    original_theorem_id = make_id("thm", {"depth3_original": key})
    original_representation_id = make_id("repr", {"depth3_original": key})
    intermediate_theorem_id = make_id("thm", {"depth3_intermediate": key})
    intermediate_representation_id = make_id("repr", {"depth3_intermediate": key})
    theorem = theorem.model_copy(
        update={
            "source": "deterministic_transform",
            "parent_theorem_ids": (intermediate_theorem_id,),
            "statement_content_hash": hashlib.sha256(_SOURCE.encode()).hexdigest(),
        }
    )
    representation = representation.model_copy(
        update={"normalization_version": NORMALIZATION_VERSION, "content_hash": "0" * 64}
    )
    representation = representation.model_copy(
        update={"content_hash": _representation_payload_hash(representation)}
    )
    final_code_hash = hashlib.sha256(_SOURCE.encode()).hexdigest()
    intermediate_theorem = theorem.model_copy(
        update={
            "theorem_id": intermediate_theorem_id,
            "parent_theorem_ids": (original_theorem_id,),
            "declaration_name": f"depth1_{key}",
            "declaration_full_name": f"depth1_{key}",
            "proof_stripped_declaration": _SOURCE.replace("depth2", "depth1"),
            "statement_content_hash": hashlib.sha256(
                _SOURCE.replace("depth2", "depth1").encode()
            ).hexdigest(),
            "metadata": {
                "rule_id": "p14_independent_binder_permutation",
                "family_id": "p14_independent_binder_permutation",
            },
        }
    )
    intermediate_alpha = hash_canonical({"depth3_intermediate_alpha": key})
    intermediate_representation = representation.model_copy(
        update={
            "representation_id": intermediate_representation_id,
            "theorem_id": intermediate_theorem_id,
            "raw_proof_stripped": intermediate_theorem.proof_stripped_declaration,
            "alpha_identity_fingerprint": intermediate_alpha,
            "content_hash": "0" * 64,
        }
    )
    intermediate_representation = intermediate_representation.model_copy(
        update={"content_hash": _representation_payload_hash(intermediate_representation)}
    )
    seed_data: dict[str, object] = {
        "input_combination_hash": "1" * 64,
        "unique_pair_id": "detprov_pair:" + "2" * 64,
        "exact_pair_key": "2" * 64,
        "first_hop_observation_ids": ("detprov_observation:" + "7" * 64,),
        "selected_observation_id": "detprov_observation:" + "7" * 64,
        "first_hop_root_binding_id": "detprov_root:" + "9" * 64,
        "first_hop_result_id": "first-hop-result",
        "first_hop_result_line_number": 1,
        "first_hop_profile_id": "deterministic_v2_e2_p14_experimental",
        "first_hop_rule_id": "p14_independent_binder_permutation",
        "first_hop_family_id": "p14_independent_binder_permutation",
        "first_hop_attempt_id": "first-hop-attempt",
        "first_hop_draft_id": "first-hop-draft",
        "first_hop_audit_id": "first-hop-audit",
        "first_hop_variant_id": "first-hop-variant",
        "source_theorem_id": original_theorem_id,
        "source_representation_id": original_representation_id,
        "intermediate_theorem_id": intermediate_theorem_id,
        "intermediate_representation_id": intermediate_representation_id,
        "context_id": theorem.context_id,
        "root_ancestry_ids": theorem.root_ancestry_ids,
        "source_statement_content_hash": "b" * 64,
        "source_alpha_identity_fingerprint": "c" * 64,
        "intermediate_candidate_code_hash": intermediate_theorem.statement_content_hash,
        "intermediate_alpha_identity_fingerprint": intermediate_alpha,
        "certificate_kind": "binder_permutation_certificate",
        "certificate_sha256": "6" * 64,
        "execution_settings_provenance": "recorded",
        "workers": 1,
    }
    seed_placeholder = CompositionSeedRecord.model_construct(
        _fields_set=None, seed_id="detcomp_seed:" + "0" * 64, **seed_data
    )
    seed_identity = seed_placeholder.model_dump(mode="json")
    seed_identity.pop("seed_id")
    seed = CompositionSeedRecord.model_validate(
        {"seed_id": "detcomp_seed:" + hash_canonical(seed_identity), **seed_data}
    )
    seed_payload = _line(seed)
    seed_theorem_payload = _line(intermediate_theorem)
    seed_representation_payload = _line(intermediate_representation)
    seed_manifest_data: dict[str, object] = {
        "input_combination_hash": "1" * 64,
        "input_combination_manifest_sha256": "2" * 64,
        "input_gross_observations_sha256": "3" * 64,
        "input_unique_pairs_sha256": "4" * 64,
        "input_root_binding_ids": (seed.first_hop_root_binding_id,),
        "input_gross_observation_count": 1,
        "excluded_observation_counts": {},
        "admitted_e2_observation_count": 1,
        "seed_count": 1,
        "exact_duplicate_excess_count": 0,
        "seed_output_sha256": hashlib.sha256(seed_payload).hexdigest(),
        "theorem_output_sha256": hashlib.sha256(seed_theorem_payload).hexdigest(),
        "representation_output_sha256": hashlib.sha256(seed_representation_payload).hexdigest(),
        "theorem_count": 1,
        "representation_count": 1,
    }
    seed_manifest_placeholder = CompositionSeedManifest.model_construct(
        _fields_set=None, seed_set_id="detcomp_seed_set:" + "0" * 64, **seed_manifest_data
    )
    seed_manifest_identity = seed_manifest_placeholder.model_dump(mode="json")
    seed_manifest_identity.pop("seed_set_id")
    seed_manifest = CompositionSeedManifest.model_validate(
        {
            "seed_set_id": "detcomp_seed_set:" + hash_canonical(seed_manifest_identity),
            **seed_manifest_data,
        }
    )
    seed_manifest_payload = _line(seed_manifest)
    seed_output = tmp_path / f"seeds-{key}"
    seed_output.mkdir(parents=True)
    (seed_output / "seeds.jsonl").write_bytes(seed_payload)
    (seed_output / "theorems.jsonl").write_bytes(seed_theorem_payload)
    (seed_output / "representations.jsonl").write_bytes(seed_representation_payload)
    (seed_output / "manifest.json").write_bytes(seed_manifest_payload)

    chain_data: dict[str, object] = {
        "seed_set_id": seed_manifest.seed_set_id,
        "seed_id": seed.seed_id,
        "chain_kind": "P_to_N",
        "context_id": seed.context_id,
        "root_ancestry_ids": seed.root_ancestry_ids,
        "original_source_theorem_id": original_theorem_id,
        "original_source_representation_id": original_representation_id,
        "intermediate_theorem_id": intermediate_theorem_id,
        "intermediate_representation_id": intermediate_representation_id,
        "final_theorem_id": theorem.theorem_id,
        "final_representation_id": representation.representation_id,
        "first_hop_root_binding_id": seed.first_hop_root_binding_id,
        "first_hop_result_id": seed.first_hop_result_id,
        "first_hop_rule_id": seed.first_hop_rule_id,
        "first_hop_attempt_id": seed.first_hop_attempt_id,
        "first_hop_draft_id": seed.first_hop_draft_id,
        "first_hop_audit_id": seed.first_hop_audit_id,
        "first_hop_variant_id": seed.first_hop_variant_id,
        "first_hop_certificate_kind": seed.certificate_kind,
        "first_hop_certificate_sha256": seed.certificate_sha256,
        "second_hop_root_binding_id": "detprov_root:" + "8" * 64,
        "second_hop_result_id": "second-hop-result",
        "second_hop_result_line_number": 1,
        "second_hop_profile_id": "deterministic_v2_d0_n11_experimental",
        "second_hop_rule_id": "n11_bound_variable_substitution",
        "second_hop_family_id": "n11_bound_variable_substitution",
        "second_hop_attempt_id": "second-hop-attempt",
        "second_hop_draft_id": "second-hop-draft",
        "second_hop_audit_id": "second-hop-audit",
        "second_hop_variant_id": "second-hop-variant",
        "second_hop_evidence_class": "D0",
        "second_hop_intended_relation": IntendedRelation.NEAR_MISS,
        "second_hop_polarity_metadata": Polarity.NEGATIVE,
        "final_candidate_code_hash": final_code_hash,
        "final_alpha_identity_fingerprint": representation.alpha_identity_fingerprint,
    }
    chain_placeholder = DeterministicCompositionChainRecord.model_construct(
        _fields_set=None, chain_id="detcomp_chain:" + "0" * 64, **chain_data
    )
    chain_identity = chain_placeholder.model_dump(mode="json")
    chain_identity.pop("chain_id")
    chain = DeterministicCompositionChainRecord.model_validate(
        {"chain_id": "detcomp_chain:" + hash_canonical(chain_identity), **chain_data}
    )
    chain_payload = _line(chain)
    file_binding = FileBinding(relative_path="fixture", sha256="5" * 64, byte_count=1)
    second_root = CompositionSecondHopRootBinding(
        root_binding_id=chain.second_hop_root_binding_id,
        run_kind="d0",
        profile_id=chain.second_hop_profile_id,
        rule_ids=(chain.second_hop_rule_id,),
        context_id=chain.context_id,
        execution_settings_provenance="recorded",
        workers=1,
        run_spec=file_binding,
        materialization_manifest=file_binding,
        results=file_binding,
        journal_files=(file_binding,),
        root_file_count=4,
        root_tree_hash="6" * 64,
        theorem_partition_sha256=seed_manifest.theorem_output_sha256,
        representation_partition_sha256=seed_manifest.representation_output_sha256,
        source_count=1,
        result_count=1,
        provisional_count=1,
    )
    chain_manifest_data: dict[str, object] = {
        "input_seed_set_id": seed_manifest.seed_set_id,
        "input_seed_manifest_sha256": hashlib.sha256(seed_manifest_payload).hexdigest(),
        "input_seed_records_sha256": seed_manifest.seed_output_sha256,
        "input_seed_theorems_sha256": seed_manifest.theorem_output_sha256,
        "input_seed_representations_sha256": seed_manifest.representation_output_sha256,
        "input_seed_count": 1,
        "second_hop_roots": (second_root,),
        "second_hop_root_count": 1,
        "second_hop_result_count": 1,
        "second_hop_terminal_status_counts": {"provisional_variant": 1},
        "chain_count": 1,
        "chain_kind_counts": {"P_to_N": 1},
        "second_hop_rule_counts": {chain.second_hop_rule_id: 1},
        "chain_output_sha256": hashlib.sha256(chain_payload).hexdigest(),
    }
    chain_manifest_placeholder = DeterministicCompositionChainManifest.model_construct(
        _fields_set=None, chain_set_id="detcomp_chain_set:" + "0" * 64, **chain_manifest_data
    )
    chain_manifest_identity = chain_manifest_placeholder.model_dump(mode="json")
    chain_manifest_identity.pop("chain_set_id")
    chain_manifest = DeterministicCompositionChainManifest.model_validate(
        {
            "chain_set_id": "detcomp_chain_set:" + hash_canonical(chain_manifest_identity),
            **chain_manifest_data,
        }
    )
    chain_manifest_payload = _line(chain_manifest)
    chain_output = tmp_path / f"chains-{key}"
    chain_output.mkdir(parents=True)
    (chain_output / "chains.jsonl").write_bytes(chain_payload)
    (chain_output / "manifest.json").write_bytes(chain_manifest_payload)

    chain_id = chain.chain_id
    chain_set_id = chain_manifest.chain_set_id
    assert representation.alpha_identity_fingerprint is not None
    unique_key = hash_canonical(
        {
            "schema": "deterministic_v2_composition_unique_pair_key_v2",
            "original_source_theorem_id": original_theorem_id,
            "final_candidate_code_hash": final_code_hash,
        }
    )
    unique_pair = DeterministicCompositionUniquePairRecord(
        unique_pair_id=f"detcomp_unique_pair:{unique_key}",
        canonical_unique_key=unique_key,
        input_seed_set_id=seed_manifest.seed_set_id,
        input_chain_set_id=chain_set_id,
        context_id=theorem.context_id,
        root_ancestry_ids=theorem.root_ancestry_ids,
        original_source_theorem_id=original_theorem_id,
        original_source_representation_id=original_representation_id,
        source_statement_content_hash="b" * 64,
        source_alpha_identity_fingerprint="c" * 64,
        intermediate_theorem_ids=(intermediate_theorem_id,),
        intermediate_representation_ids=(intermediate_representation_id,),
        final_theorem_ids=(theorem.theorem_id,),
        final_representation_ids=(representation.representation_id,),
        final_candidate_code_hash=final_code_hash,
        final_alpha_identity_fingerprint=representation.alpha_identity_fingerprint,
        chain_ids=(chain_id,),
        chain_sequences=("p14_independent_binder_permutation->n11_bound_variable_substitution",),
        chain_kinds=("P_to_N",),
        gross_chain_count=1,
        duplicate_excess_count=0,
        source_alpha_return=False,
        alpha_novel=True,
    )
    unique_payload = _line(unique_pair)
    unique_manifest_data: dict[str, object] = {
        "input_seed_set_id": unique_pair.input_seed_set_id,
        "input_seed_manifest_sha256": hashlib.sha256(seed_manifest_payload).hexdigest(),
        "input_seed_records_sha256": seed_manifest.seed_output_sha256,
        "input_seed_theorems_sha256": seed_manifest.theorem_output_sha256,
        "input_seed_representations_sha256": seed_manifest.representation_output_sha256,
        "input_chain_set_id": chain_set_id,
        "input_chain_manifest_sha256": hashlib.sha256(chain_manifest_payload).hexdigest(),
        "input_chain_records_sha256": chain_manifest.chain_output_sha256,
        "gross_chain_count": 1,
        "unique_pair_count": 1,
        "duplicate_group_count": 0,
        "duplicate_excess_count": 0,
        "gross_source_alpha_return_count": 0,
        "unique_source_alpha_return_count": 0,
        "gross_alpha_novel_count": 1,
        "unique_alpha_novel_count": 1,
        "gross_chain_kind_counts": {"P_to_N": 1},
        "unique_pair_chain_kind_membership_counts": {"P_to_N": 1},
        "gross_sequence_counts": {
            "p14_independent_binder_permutation->n11_bound_variable_substitution": 1
        },
        "unique_pair_sequence_membership_counts": {
            "p14_independent_binder_permutation->n11_bound_variable_substitution": 1
        },
        "unique_output_sha256": hashlib.sha256(unique_payload).hexdigest(),
    }
    unique_placeholder = DeterministicCompositionUniquePairManifest.model_construct(
        _fields_set=None,
        unique_pair_set_id="detcomp_unique_pair_set:" + "0" * 64,
        **unique_manifest_data,
    )
    unique_manifest_identity = unique_placeholder.model_dump(mode="json")
    unique_manifest_identity.pop("unique_pair_set_id")
    unique_manifest = DeterministicCompositionUniquePairManifest.model_validate(
        {
            "unique_pair_set_id": "detcomp_unique_pair_set:"
            + hash_canonical(unique_manifest_identity),
            **unique_manifest_data,
        }
    )
    unique_manifest_payload = _line(unique_manifest)
    unique_output = tmp_path / f"unique-{key}"
    unique_output.mkdir(parents=True)
    (unique_output / "unique_pairs.jsonl").write_bytes(unique_payload)
    (unique_output / "manifest.json").write_bytes(unique_manifest_payload)
    record = _identified_record(
        {
            "input_unique_pair_id": unique_pair.unique_pair_id,
            "input_chain_set_id": chain_set_id,
            "context_id": theorem.context_id,
            "root_ancestry_ids": theorem.root_ancestry_ids,
            "original_source_theorem_id": original_theorem_id,
            "original_source_representation_id": original_representation_id,
            "depth_two_theorem_ids": (theorem.theorem_id,),
            "depth_two_representation_ids": (representation.representation_id,),
            "selected_frontier_theorem_id": theorem.theorem_id,
            "selected_frontier_representation_id": representation.representation_id,
            "final_candidate_code_hash": final_code_hash,
            "final_alpha_identity_fingerprint": representation.alpha_identity_fingerprint,
            "parent_chain_ids": (chain_id,),
            "parent_chain_sequences": (
                "p14_independent_binder_permutation->n11_bound_variable_substitution",
            ),
            "parent_chain_kind": "P_to_N",
            "preserved_intention": "near_miss_candidate",
            "semantic_negative_hop_count": 1,
        }
    )
    frontier_payload = _line(record)
    theorem_payload = _line(theorem)
    representation_payload = _line(representation)
    data: dict[str, object] = {
        "input_chain_set_id": record.input_chain_set_id,
        "input_chain_manifest_sha256": hashlib.sha256(chain_manifest_payload).hexdigest(),
        "input_chain_records_sha256": chain_manifest.chain_output_sha256,
        "input_unique_pair_set_id": unique_manifest.unique_pair_set_id,
        "input_unique_manifest_sha256": hashlib.sha256(unique_manifest_payload).hexdigest(),
        "input_unique_records_sha256": unique_manifest.unique_output_sha256,
        "input_root_binding_ids": (second_root.root_binding_id,),
        "input_unique_pair_count": 1,
        "excluded_counts": {},
        "frontier_count": 1,
        "intention_counts": {"near_miss_candidate": 1},
        "frontier_output_sha256": hashlib.sha256(frontier_payload).hexdigest(),
        "theorem_output_sha256": hashlib.sha256(theorem_payload).hexdigest(),
        "representation_output_sha256": hashlib.sha256(representation_payload).hexdigest(),
    }
    placeholder = DeterministicCompositionPolarityFrontierManifest.model_construct(
        _fields_set=None,
        frontier_set_id="detcomp_frontier_set:" + "0" * 64,
        **data,
    )
    identity = placeholder.model_dump(mode="json")
    identity.pop("frontier_set_id")
    manifest = DeterministicCompositionPolarityFrontierManifest.model_validate(
        {"frontier_set_id": "detcomp_frontier_set:" + hash_canonical(identity), **data}
    )
    output = tmp_path / f"frontier-{key}"
    output.mkdir(parents=True)
    (output / "frontier.jsonl").write_bytes(frontier_payload)
    (output / "theorems.jsonl").write_bytes(theorem_payload)
    (output / "representations.jsonl").write_bytes(representation_payload)
    (output / "manifest.json").write_bytes(_line(manifest))
    return output, unique_output, seed_output, chain_output


def _install_p18_representation(monkeypatch: pytest.MonkeyPatch) -> None:
    import leanfaith.transforms.v2_e2_scale as module

    def fake_build(
        backend: object,
        inputs: list[TheoremForRepresentation],
        **kwargs: object,
    ) -> list[RepresentationRecord]:
        del backend, kwargs
        output: list[RepresentationRecord] = []
        for item in inputs:
            # Only P18 reaches representation construction in this fixture.
            candidate_root = _root(swapped=True)
            template = _CURRENT_SOURCE_REPRESENTATION[0]
            candidate = template.model_copy(
                update={
                    "representation_id": make_id("repr", {"depth3_p18_candidate": item.theorem_id}),
                    "theorem_id": item.theorem_id,
                    "normalization_version": NORMALIZATION_VERSION,
                    "raw_proof_stripped": item.proof_stripped,
                    "headless": "(x y : Nat) : y = x",
                    "signature_pp": "(x y : Nat) : y = x",
                    "signature_explicit": "∀ (x y : Nat), Eq y x",
                    "semantic_atoms": semantic_atoms(candidate_root),
                    "operator_tree": operator_tree(candidate_root),
                    "alpha_identity_fingerprint": alpha_identity_fingerprint(candidate_root),
                    "content_hash": "0" * 64,
                }
            )
            output.append(
                candidate.model_copy(
                    update={"content_hash": _representation_payload_hash(candidate)}
                )
            )
        return output

    monkeypatch.setattr(module, "build_representations", fake_build)


_CURRENT_SOURCE_REPRESENTATION: list[RepresentationRecord] = []


def _roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    frontier: Path,
) -> tuple[Path, ...]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    theorem_path = frontier / "theorems.jsonl"
    representation_path = frontier / "representations.jsonl"
    _CURRENT_SOURCE_REPRESENTATION[:] = [
        RepresentationRecord.model_validate_json(representation_path.read_bytes())
    ]
    _install_p18_representation(monkeypatch)
    runtimes: tuple[V2E2Runtime, ...] = (
        build_v2_e2_p14_runtime(),
        build_v2_e2_p15_runtime(),
        build_v2_e2_p16_runtime(),
        build_v2_e2_p17_runtime(),
        build_v2_e2_p18_runtime(),
    )
    roots: list[Path] = []
    for index, runtime in enumerate(runtimes, start=14):
        backend = _BatchBackend(
            (LeanStatus.VALID_WITH_SORRY if index == 18 else LeanStatus.INVALID,),
            workers=1,
        )
        output = tmp_path / f"p{index}"
        run_v2_e2_scale(
            backend=cast(LeanInteractBackend, backend),
            runtime=runtime,
            theorem_path=theorem_path,
            representation_path=representation_path,
            project_dir=tmp_path,
            import_header="import LeanFaithFixtures",
            output_dir=output,
            batch_size=1,
            base_seed=300 + index,
            workers=1,
        )
        roots.append(output)
    return tuple(roots)


def _valid_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, tuple[Path, ...]]:
    frontier, unique_pairs, seeds, chains = _frontier(tmp_path)
    return frontier, unique_pairs, seeds, chains, _roots(monkeypatch, tmp_path / "roots", frontier)


def _load_single_chain_inputs(
    frontier: Path,
    unique_pair_dir: Path,
    seed_dir: Path,
    chain_dir: Path,
    roots: tuple[Path, ...],
) -> tuple[
    DeterministicCompositionPolarityFrontierRecord,
    DeterministicCompositionUniquePairRecord,
    CompositionSeedRecord,
    DeterministicCompositionChainRecord,
    TheoremRecord,
    RepresentationRecord,
    _LoadedRoot,
    int,
    ProvisionalPairObservation,
    V2E2MaterializationResult,
]:
    frontier_record = DeterministicCompositionPolarityFrontierRecord.model_validate_json(
        (frontier / "frontier.jsonl").read_bytes()
    )
    unique_pair = DeterministicCompositionUniquePairRecord.model_validate_json(
        (unique_pair_dir / "unique_pairs.jsonl").read_bytes()
    )
    seed = CompositionSeedRecord.model_validate_json((seed_dir / "seeds.jsonl").read_bytes())
    parent_chain = DeterministicCompositionChainRecord.model_validate_json(
        (chain_dir / "chains.jsonl").read_bytes()
    )
    theorem = TheoremRecord.model_validate_json((frontier / "theorems.jsonl").read_bytes())
    representation = RepresentationRecord.model_validate_json(
        (frontier / "representations.jsonl").read_bytes()
    )
    root = next(path for path in roots if "p18" in path.name)
    loaded = _load_root(root)
    line_number, raw, _ = next(_iter_jsonl_objects(root / "results.jsonl"))
    result = V2E2MaterializationResult.model_validate(raw)
    observation = loaded.observations[0]
    return (
        frontier_record,
        unique_pair,
        seed,
        parent_chain,
        theorem,
        representation,
        loaded,
        line_number,
        observation,
        result,
    )


def test_third_hop_binds_five_roots_deduplicates_and_replays(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    frontier, unique_pairs, seeds, parent_chains, roots = _valid_inputs(monkeypatch, tmp_path)
    output = tmp_path / "depth3"
    first = audit_deterministic_v2_composition_third_hop(
        frontier_dir=frontier,
        unique_pair_dir=unique_pairs,
        seed_dir=seeds,
        chain_dir=parent_chains,
        third_hop_roots=roots,
        output_dir=output,
    )
    second = audit_deterministic_v2_composition_third_hop(
        frontier_dir=frontier,
        unique_pair_dir=unique_pairs,
        seed_dir=seeds,
        chain_dir=parent_chains,
        third_hop_roots=tuple(reversed(roots)),
        output_dir=output,
    )
    assert first.replayed is False
    assert second.replayed is True
    assert first.third_hop_set_id == second.third_hop_set_id
    assert first.gross_chain_count == 1
    assert first.unique_pair_count == 1
    assert first.quarantine_count == 0
    manifest = DeterministicCompositionThirdHopManifest.model_validate_json(
        first.manifest_path.read_bytes()
    )
    assert {rule for root in manifest.third_hop_roots for rule in root.rule_ids} == {
        "p14_independent_binder_permutation",
        "p15_root_iff_reversal",
        "p16_conjunction_reassociation",
        "p17_hypothesis_packing",
        "p18_root_equality_symmetry",
    }
    assert manifest.semantic_labels_created is False
    assert manifest.training_eligible is False
    assert manifest.input_unique_pair_set_id.startswith("detcomp_unique_pair_set:")
    assert manifest.original_source_payloads_included is False
    assert (
        manifest.unique_pair_count == manifest.theorem_count == manifest.representation_count == 1
    )
    assert hash_file(first.chains_path) == manifest.chain_output_sha256

    cli_output = tmp_path / "depth3-cli"
    arguments = [
        "audit-deterministic-composition-third-hop",
        "--frontier-dir",
        str(frontier),
        "--unique-pair-dir",
        str(unique_pairs),
        "--seed-dir",
        str(seeds),
        "--chain-dir",
        str(parent_chains),
        "--output-dir",
        str(cli_output),
    ]
    for root in roots:
        arguments.extend(("--third-hop-root", str(root)))
    cli_first = CliRunner().invoke(app, arguments)
    cli_second = CliRunner().invoke(app, arguments)
    assert cli_first.exit_code == 0, cli_first.output
    assert "status=audited" in cli_first.output
    assert "unique_pairs=1" in cli_first.output
    assert cli_second.exit_code == 0, cli_second.output
    assert "status=replayed" in cli_second.output


def test_third_hop_rejects_forged_polarity_and_negative_rule(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    frontier, unique_pairs, seeds, parent_chains, roots = _valid_inputs(monkeypatch, tmp_path)
    (
        frontier_record,
        unique_pair,
        seed,
        parent_chain,
        theorem,
        representation,
        loaded,
        line_number,
        observation,
        result,
    ) = _load_single_chain_inputs(frontier, unique_pairs, seeds, parent_chains, roots)
    assert result.variant is not None
    assert result.candidate_representation is not None
    return_chain, return_theorem, return_representation = _build_chain(
        frontier_set_id=DeterministicCompositionPolarityFrontierManifest.model_validate_json(
            (frontier / "manifest.json").read_bytes()
        ).frontier_set_id,
        frontier=frontier_record,
        unique_pair=unique_pair,
        seed=seed,
        parent_chain=parent_chain,
        source_theorem=theorem,
        source_representation=representation,
        root_binding=loaded.binding,
        line_number=line_number,
        observation=observation,
        result=result,
    )
    assert return_chain.original_source_alpha_return is False
    _, return_pairs, return_quarantine, _, _ = _deduplicate(
        ((return_chain, return_theorem, return_representation),)
    )
    assert len(return_pairs) == 1
    assert return_quarantine == ()

    forged_variant = result.variant.model_copy(update={"polarity_metadata": Polarity.NEGATIVE})
    forged = result.model_copy(update={"variant": forged_variant})
    with pytest.raises(CompositionThirdHopError, match="polarity is not positive"):
        _build_chain(
            frontier_set_id=DeterministicCompositionPolarityFrontierManifest.model_validate_json(
                (frontier / "manifest.json").read_bytes()
            ).frontier_set_id,
            frontier=frontier_record,
            unique_pair=unique_pair,
            seed=seed,
            parent_chain=parent_chain,
            source_theorem=theorem,
            source_representation=representation,
            root_binding=loaded.binding,
            line_number=line_number,
            observation=observation,
            result=forged,
        )
    negative = result.model_copy(update={"rule_id": "n18_root_equality_polarity"})
    with pytest.raises(CompositionThirdHopError, match="outside P14-P18"):
        _build_chain(
            frontier_set_id="detcomp_frontier_set:" + "f" * 64,
            frontier=frontier_record,
            unique_pair=unique_pair,
            seed=seed,
            parent_chain=parent_chain,
            source_theorem=theorem,
            source_representation=representation,
            root_binding=loaded.binding,
            line_number=line_number,
            observation=observation,
            result=negative,
        )


def _reidentify_chain(
    chain: DeterministicCompositionThirdHopChainRecord,
    **updates: object,
) -> DeterministicCompositionThirdHopChainRecord:
    data = chain.model_dump(mode="json")
    data.update(updates)
    data.pop("chain_id")
    return DeterministicCompositionThirdHopChainRecord.model_validate(
        {"chain_id": "detcomp_depth3_chain:" + hash_canonical(data), **data}
    )


def test_third_hop_quarantines_cycles_and_mixed_intention_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    frontier, unique_pairs, seeds, parent_chains, roots = _valid_inputs(monkeypatch, tmp_path)
    result = audit_deterministic_v2_composition_third_hop(
        frontier_dir=frontier,
        unique_pair_dir=unique_pairs,
        seed_dir=seeds,
        chain_dir=parent_chains,
        third_hop_roots=roots,
        output_dir=tmp_path / "base",
    )
    chain = DeterministicCompositionThirdHopChainRecord.model_validate_json(
        result.chains_path.read_bytes()
    )
    theorem = TheoremRecord.model_validate_json(result.theorem_path.read_bytes())
    representation = RepresentationRecord.model_validate_json(
        result.representation_path.read_bytes()
    )

    duplicate = _reidentify_chain(
        chain,
        third_hop_result_id="v2e2_result:" + "d" * 64,
    )
    _, pairs, quarantine, _, _ = _deduplicate(
        ((chain, theorem, representation), (duplicate, theorem, representation))
    )
    assert len(pairs) == 1
    assert pairs[0].gross_chain_count == 2
    assert pairs[0].duplicate_excess_count == 1
    assert quarantine == ()

    cycle = _reidentify_chain(
        chain,
        final_alpha_identity_fingerprint=chain.depth_two_alpha_identity_fingerprint,
        third_hop_source_alpha_return=True,
        lineage_cycle=True,
    )
    _, cycle_pairs, cycle_quarantine, _, _ = _deduplicate(((cycle, theorem, representation),))
    assert cycle_pairs == ()
    assert cycle_quarantine[0].reason_codes == (
        "lineage_cycle",
        "third_hop_source_alpha_return",
    )

    original_return = _reidentify_chain(
        chain,
        final_alpha_identity_fingerprint=chain.original_source_alpha_identity_fingerprint,
        original_source_alpha_return=True,
        lineage_cycle=True,
    )
    _, original_pairs, original_quarantine, _, _ = _deduplicate(
        ((original_return, theorem, representation),)
    )
    assert original_pairs == ()
    assert original_quarantine[0].reason_codes == (
        "lineage_cycle",
        "original_source_alpha_return",
    )

    conflict = _reidentify_chain(
        duplicate,
        input_frontier_id="detcomp_frontier:" + "e" * 64,
        parent_chain_kind="P_to_P",
        preserved_intention="equivalent_candidate",
        semantic_negative_hop_count=0,
    )
    _, conflict_pairs, conflict_quarantine, _, _ = _deduplicate(
        ((chain, theorem, representation), (conflict, theorem, representation))
    )
    assert conflict_pairs == ()
    assert conflict_quarantine[0].reason_codes == ("mixed_preserved_intention",)


@pytest.mark.parametrize(
    ("profile_id", "rule_id", "certificate_kind"),
    (
        (
            "deterministic_v2_e2_p14_experimental",
            "p14_independent_binder_permutation",
            "binder_permutation_certificate",
        ),
        (
            "deterministic_v2_e2_p15_experimental",
            "p15_root_iff_reversal",
            "root_iff_reversal_certificate",
        ),
        (
            "deterministic_v2_e2_p16_experimental",
            "p16_conjunction_reassociation",
            "root_conjunction_reassociation_certificate",
        ),
        (
            "deterministic_v2_e2_p18_experimental",
            "p18_root_equality_symmetry",
            "root_equality_symmetry_certificate",
        ),
    ),
)
def test_repeated_positive_family_return_to_depth_one_alpha_is_a_cycle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    profile_id: str,
    rule_id: str,
    certificate_kind: str,
) -> None:
    frontier, unique_pairs, seeds, parent_chains, roots = _valid_inputs(monkeypatch, tmp_path)
    result = audit_deterministic_v2_composition_third_hop(
        frontier_dir=frontier,
        unique_pair_dir=unique_pairs,
        seed_dir=seeds,
        chain_dir=parent_chains,
        third_hop_roots=roots,
        output_dir=tmp_path / "base-cycle",
    )
    chain = DeterministicCompositionThirdHopChainRecord.model_validate_json(
        result.chains_path.read_bytes()
    )
    theorem = TheoremRecord.model_validate_json(result.theorem_path.read_bytes())
    representation = RepresentationRecord.model_validate_json(
        result.representation_path.read_bytes()
    )
    returned = _reidentify_chain(
        chain,
        third_hop_profile_id=profile_id,
        third_hop_rule_id=rule_id,
        third_hop_family_id=rule_id,
        third_hop_certificate_kind=certificate_kind,
        depth_three_sequences=(f"{chain.parent_chain_sequences[0]}->{rule_id}",),
        final_alpha_identity_fingerprint=chain.depth_one_alpha_identity_fingerprint,
        depth_one_alpha_return=True,
        lineage_cycle=True,
    )
    assert returned.final_candidate_theorem_id != returned.depth_one_theorem_id
    assert returned.depth_one_alpha_return is True
    _, pairs, quarantine, _, _ = _deduplicate(((returned, theorem, representation),))
    assert pairs == ()
    assert quarantine[0].reason_codes == ("lineage_cycle",)


def test_third_hop_rejects_foreign_root_set_and_symlink_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    frontier, unique_pairs, seeds, parent_chains, roots = _valid_inputs(
        monkeypatch, tmp_path / "main"
    )
    foreign_frontier, foreign_unique_pairs, foreign_seeds, foreign_parent_chains = _frontier(
        tmp_path / "foreign", key="foreign"
    )
    foreign_roots = _roots(monkeypatch, tmp_path / "foreign-roots", foreign_frontier)
    mixed = (*roots[:-1], foreign_roots[-1])
    with pytest.raises(CompositionThirdHopError, match="source partitions differ"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=unique_pairs,
            seed_dir=seeds,
            chain_dir=parent_chains,
            third_hop_roots=mixed,
            output_dir=tmp_path / "foreign-output",
        )

    with pytest.raises(CompositionThirdHopError, match="unique-pair artifact differs"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=foreign_unique_pairs,
            seed_dir=foreign_seeds,
            chain_dir=foreign_parent_chains,
            third_hop_roots=roots,
            output_dir=tmp_path / "foreign-unique-output",
        )

    alias = tmp_path / "frontier-alias"
    alias.symlink_to(frontier, target_is_directory=True)
    with pytest.raises(CompositionThirdHopError, match="symlink"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=alias,
            unique_pair_dir=unique_pairs,
            seed_dir=seeds,
            chain_dir=parent_chains,
            third_hop_roots=roots,
            output_dir=tmp_path / "symlink-output",
        )

    unique_alias = tmp_path / "unique-alias"
    unique_alias.symlink_to(unique_pairs, target_is_directory=True)
    with pytest.raises(CompositionThirdHopError, match="symlink"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=unique_alias,
            seed_dir=seeds,
            chain_dir=parent_chains,
            third_hop_roots=roots,
            output_dir=tmp_path / "unique-symlink-output",
        )

    root_alias = tmp_path / "root-alias"
    root_alias.symlink_to(roots[0], target_is_directory=True)
    with pytest.raises(CompositionThirdHopError, match="symlink"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=unique_pairs,
            seed_dir=seeds,
            chain_dir=parent_chains,
            third_hop_roots=(root_alias, *roots[1:]),
            output_dir=tmp_path / "root-symlink-output",
        )

    completed = audit_deterministic_v2_composition_third_hop(
        frontier_dir=frontier,
        unique_pair_dir=unique_pairs,
        seed_dir=seeds,
        chain_dir=parent_chains,
        third_hop_roots=roots,
        output_dir=tmp_path / "real-output",
    )
    output_alias = tmp_path / "output-alias"
    output_alias.symlink_to(completed.output_dir, target_is_directory=True)
    with pytest.raises(CompositionThirdHopError, match="symlink"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=unique_pairs,
            seed_dir=seeds,
            chain_dir=parent_chains,
            third_hop_roots=roots,
            output_dir=output_alias,
        )


def test_third_hop_rejects_root_mutation_before_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.composition_third_hop as module

    frontier, unique_pairs, seeds, parent_chains, roots = _valid_inputs(monkeypatch, tmp_path)
    original = module._deduplicate
    mutated = False

    def mutate_after_build(
        built: object,
    ) -> object:
        nonlocal mutated
        output = original(built)  # type: ignore[arg-type]
        if not mutated:
            with (roots[0] / "results.jsonl").open("ab") as stream:
                stream.write(b"\n")
            mutated = True
        return output

    monkeypatch.setattr(module, "_deduplicate", mutate_after_build)
    with pytest.raises(CompositionThirdHopError, match="changed before publication"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=unique_pairs,
            seed_dir=seeds,
            chain_dir=parent_chains,
            third_hop_roots=roots,
            output_dir=tmp_path / "racy-output",
        )


def test_third_hop_rejects_unique_pair_mutation_before_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.composition_third_hop as module

    frontier, unique_pairs, seeds, parent_chains, roots = _valid_inputs(monkeypatch, tmp_path)
    original = module._deduplicate
    mutated = False

    def mutate_after_build(
        built: object,
    ) -> object:
        nonlocal mutated
        output = original(built)  # type: ignore[arg-type]
        if not mutated:
            with (unique_pairs / "unique_pairs.jsonl").open("ab") as stream:
                stream.write(b"\n")
            mutated = True
        return output

    monkeypatch.setattr(module, "_deduplicate", mutate_after_build)
    with pytest.raises(CompositionThirdHopError, match="changed before publication"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=unique_pairs,
            seed_dir=seeds,
            chain_dir=parent_chains,
            third_hop_roots=roots,
            output_dir=tmp_path / "racy-unique-output",
        )
