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
from leanfaith.schemas.enums import Polarity
from leanfaith.schemas.ids import make_id
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_polarity_frontier import (
    DeterministicCompositionPolarityFrontierManifest,
    DeterministicCompositionPolarityFrontierRecord,
)
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


def _frontier(tmp_path: Path, *, key: str = "main") -> tuple[Path, Path]:
    theorem, representation = _records(_SOURCE, f"depth3-{key}", _root())
    original_theorem_id = make_id("thm", {"depth3_original": key})
    original_representation_id = make_id("repr", {"depth3_original": key})
    theorem = theorem.model_copy(
        update={
            "source": "deterministic_transform",
            "parent_theorem_ids": (make_id("thm", {"depth3_parent": key}),),
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
    chain_id = "detcomp_chain:" + "3" * 64
    chain_set_id = "detcomp_chain_set:" + "2" * 64
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
        input_seed_set_id="detcomp_seed_set:" + "a" * 64,
        input_chain_set_id=chain_set_id,
        context_id=theorem.context_id,
        root_ancestry_ids=theorem.root_ancestry_ids,
        original_source_theorem_id=original_theorem_id,
        original_source_representation_id=original_representation_id,
        source_statement_content_hash="b" * 64,
        source_alpha_identity_fingerprint="c" * 64,
        intermediate_theorem_ids=(make_id("thm", {"depth3_intermediate": key}),),
        intermediate_representation_ids=(make_id("repr", {"depth3_intermediate": key}),),
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
        "input_seed_manifest_sha256": "d" * 64,
        "input_seed_records_sha256": "e" * 64,
        "input_seed_theorems_sha256": "f" * 64,
        "input_seed_representations_sha256": "0" * 64,
        "input_chain_set_id": chain_set_id,
        "input_chain_manifest_sha256": "4" * 64,
        "input_chain_records_sha256": "5" * 64,
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
        "input_chain_manifest_sha256": "4" * 64,
        "input_chain_records_sha256": "5" * 64,
        "input_unique_pair_set_id": unique_manifest.unique_pair_set_id,
        "input_unique_manifest_sha256": hashlib.sha256(unique_manifest_payload).hexdigest(),
        "input_unique_records_sha256": unique_manifest.unique_output_sha256,
        "input_root_binding_ids": ("detprov_root:" + "9" * 64,),
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
    return output, unique_output


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
) -> tuple[Path, Path, tuple[Path, ...]]:
    frontier, unique_pairs = _frontier(tmp_path)
    return frontier, unique_pairs, _roots(monkeypatch, tmp_path / "roots", frontier)


def _load_single_chain_inputs(
    frontier: Path,
    unique_pair_dir: Path,
    roots: tuple[Path, ...],
) -> tuple[
    DeterministicCompositionPolarityFrontierRecord,
    DeterministicCompositionUniquePairRecord,
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
    frontier, unique_pairs, roots = _valid_inputs(monkeypatch, tmp_path)
    output = tmp_path / "depth3"
    first = audit_deterministic_v2_composition_third_hop(
        frontier_dir=frontier,
        unique_pair_dir=unique_pairs,
        third_hop_roots=roots,
        output_dir=output,
    )
    second = audit_deterministic_v2_composition_third_hop(
        frontier_dir=frontier,
        unique_pair_dir=unique_pairs,
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
    frontier, unique_pairs, roots = _valid_inputs(monkeypatch, tmp_path)
    (
        frontier_record,
        unique_pair,
        theorem,
        representation,
        loaded,
        line_number,
        observation,
        result,
    ) = _load_single_chain_inputs(frontier, unique_pairs, roots)
    assert result.variant is not None
    assert result.candidate_representation is not None
    return_unique_pair = unique_pair.model_copy(
        update={
            "source_alpha_identity_fingerprint": (
                result.candidate_representation.alpha_identity_fingerprint
            )
        }
    )
    return_chain, return_theorem, return_representation = _build_chain(
        frontier_set_id=DeterministicCompositionPolarityFrontierManifest.model_validate_json(
            (frontier / "manifest.json").read_bytes()
        ).frontier_set_id,
        frontier=frontier_record,
        unique_pair=return_unique_pair,
        source_theorem=theorem,
        source_representation=representation,
        root_binding=loaded.binding,
        line_number=line_number,
        observation=observation,
        result=result,
    )
    assert return_chain.original_source_alpha_return is True
    _, return_pairs, return_quarantine, _, _ = _deduplicate(
        ((return_chain, return_theorem, return_representation),)
    )
    assert return_pairs == ()
    assert return_quarantine[0].reason_codes == ("original_source_alpha_return",)

    forged_variant = result.variant.model_copy(update={"polarity_metadata": Polarity.NEGATIVE})
    forged = result.model_copy(update={"variant": forged_variant})
    with pytest.raises(CompositionThirdHopError, match="polarity is not positive"):
        _build_chain(
            frontier_set_id=DeterministicCompositionPolarityFrontierManifest.model_validate_json(
                (frontier / "manifest.json").read_bytes()
            ).frontier_set_id,
            frontier=frontier_record,
            unique_pair=unique_pair,
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
    frontier, unique_pairs, roots = _valid_inputs(monkeypatch, tmp_path)
    result = audit_deterministic_v2_composition_third_hop(
        frontier_dir=frontier,
        unique_pair_dir=unique_pairs,
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

    cycle = _reidentify_chain(chain, third_hop_source_alpha_return=True)
    _, cycle_pairs, cycle_quarantine, _, _ = _deduplicate(((cycle, theorem, representation),))
    assert cycle_pairs == ()
    assert cycle_quarantine[0].reason_codes == ("third_hop_source_alpha_return",)

    lineage_cycle = _reidentify_chain(chain, lineage_cycle=True)
    _, lineage_pairs, lineage_quarantine, _, _ = _deduplicate(
        ((lineage_cycle, theorem, representation),)
    )
    assert lineage_pairs == ()
    assert lineage_quarantine[0].reason_codes == ("lineage_cycle",)

    original_return = _reidentify_chain(
        chain,
        final_alpha_identity_fingerprint=chain.original_source_alpha_identity_fingerprint,
        original_source_alpha_return=True,
    )
    _, original_pairs, original_quarantine, _, _ = _deduplicate(
        ((original_return, theorem, representation),)
    )
    assert original_pairs == ()
    assert original_quarantine[0].reason_codes == ("original_source_alpha_return",)

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


def test_third_hop_rejects_foreign_root_set_and_symlink_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    frontier, unique_pairs, roots = _valid_inputs(monkeypatch, tmp_path / "main")
    foreign_frontier, foreign_unique_pairs = _frontier(tmp_path / "foreign", key="foreign")
    foreign_roots = _roots(monkeypatch, tmp_path / "foreign-roots", foreign_frontier)
    mixed = (*roots[:-1], foreign_roots[-1])
    with pytest.raises(CompositionThirdHopError, match="source partitions differ"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=unique_pairs,
            third_hop_roots=mixed,
            output_dir=tmp_path / "foreign-output",
        )

    with pytest.raises(CompositionThirdHopError, match="unique-pair artifact differs"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=foreign_unique_pairs,
            third_hop_roots=roots,
            output_dir=tmp_path / "foreign-unique-output",
        )

    alias = tmp_path / "frontier-alias"
    alias.symlink_to(frontier, target_is_directory=True)
    with pytest.raises(CompositionThirdHopError, match="symlink"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=alias,
            unique_pair_dir=unique_pairs,
            third_hop_roots=roots,
            output_dir=tmp_path / "symlink-output",
        )

    unique_alias = tmp_path / "unique-alias"
    unique_alias.symlink_to(unique_pairs, target_is_directory=True)
    with pytest.raises(CompositionThirdHopError, match="symlink"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=unique_alias,
            third_hop_roots=roots,
            output_dir=tmp_path / "unique-symlink-output",
        )

    root_alias = tmp_path / "root-alias"
    root_alias.symlink_to(roots[0], target_is_directory=True)
    with pytest.raises(CompositionThirdHopError, match="symlink"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=unique_pairs,
            third_hop_roots=(root_alias, *roots[1:]),
            output_dir=tmp_path / "root-symlink-output",
        )

    completed = audit_deterministic_v2_composition_third_hop(
        frontier_dir=frontier,
        unique_pair_dir=unique_pairs,
        third_hop_roots=roots,
        output_dir=tmp_path / "real-output",
    )
    output_alias = tmp_path / "output-alias"
    output_alias.symlink_to(completed.output_dir, target_is_directory=True)
    with pytest.raises(CompositionThirdHopError, match="symlink"):
        audit_deterministic_v2_composition_third_hop(
            frontier_dir=frontier,
            unique_pair_dir=unique_pairs,
            third_hop_roots=roots,
            output_dir=output_alias,
        )


def test_third_hop_rejects_root_mutation_before_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.composition_third_hop as module

    frontier, unique_pairs, roots = _valid_inputs(monkeypatch, tmp_path)
    original = module._deduplicate
    mutated = False

    def mutate_after_build(
        built: object,
    ) -> object:
        nonlocal mutated
        output = original(cast(object, built))  # type: ignore[arg-type]
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
            third_hop_roots=roots,
            output_dir=tmp_path / "racy-output",
        )


def test_third_hop_rejects_unique_pair_mutation_before_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import leanfaith.transforms.composition_third_hop as module

    frontier, unique_pairs, roots = _valid_inputs(monkeypatch, tmp_path)
    original = module._deduplicate
    mutated = False

    def mutate_after_build(
        built: object,
    ) -> object:
        nonlocal mutated
        output = original(cast(object, built))  # type: ignore[arg-type]
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
            third_hop_roots=roots,
            output_dir=tmp_path / "racy-unique-output",
        )
