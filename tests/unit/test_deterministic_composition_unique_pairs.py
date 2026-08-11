"""Focused tests for audit-only deterministic composition deduplication."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.transforms.composition_chain import (
    DeterministicCompositionChainRecord,
    audit_deterministic_v2_composition_chains,
)
from leanfaith.transforms.composition_seed import CompositionSeedRecord
from leanfaith.transforms.composition_unique_pairs import (
    CompositionUniquePairError,
    DeterministicCompositionUniquePairManifest,
    DeterministicCompositionUniquePairRecord,
    _unique_pairs,
    postprocess_deterministic_v2_composition_unique_pairs,
)
from tests.unit.test_deterministic_composition_chain import _run_second_hop, _seed_set


def _synthetic_p14_cycle() -> tuple[CompositionSeedRecord, DeterministicCompositionChainRecord]:
    source_hash = "1" * 64
    source_alpha = "2" * 64
    seed = CompositionSeedRecord.model_construct(
        seed_id="detcomp_seed:" + "3" * 64,
        source_theorem_id="thm:" + "4" * 64,
        source_representation_id="repr:" + "5" * 64,
        intermediate_theorem_id="thm:" + "6" * 64,
        intermediate_representation_id="repr:" + "7" * 64,
        context_id="ctx:" + "8" * 64,
        root_ancestry_ids=("thm:" + "4" * 64,),
        source_statement_content_hash=source_hash,
        source_alpha_identity_fingerprint=source_alpha,
        first_hop_root_binding_id="detprov_root:" + "9" * 64,
        first_hop_result_id="e2-result:first",
        first_hop_rule_id="p14_independent_binder_permutation",
        first_hop_attempt_id="attempt:first",
        first_hop_draft_id="draft:first",
        first_hop_audit_id="audit:first",
        first_hop_variant_id="variant:first",
        certificate_kind="binder_permutation_certificate",
        certificate_sha256="a" * 64,
    )
    chain = DeterministicCompositionChainRecord.model_construct(
        chain_id="detcomp_chain:" + "b" * 64,
        seed_set_id="detcomp_seed_set:" + "c" * 64,
        seed_id=seed.seed_id,
        chain_kind="P_to_P",
        context_id=seed.context_id,
        root_ancestry_ids=seed.root_ancestry_ids,
        original_source_theorem_id=seed.source_theorem_id,
        original_source_representation_id=seed.source_representation_id,
        intermediate_theorem_id=seed.intermediate_theorem_id,
        intermediate_representation_id=seed.intermediate_representation_id,
        final_theorem_id="thm:" + "d" * 64,
        final_representation_id="repr:" + "e" * 64,
        first_hop_root_binding_id=seed.first_hop_root_binding_id,
        first_hop_result_id=seed.first_hop_result_id,
        first_hop_rule_id=seed.first_hop_rule_id,
        first_hop_attempt_id=seed.first_hop_attempt_id,
        first_hop_draft_id=seed.first_hop_draft_id,
        first_hop_audit_id=seed.first_hop_audit_id,
        first_hop_variant_id=seed.first_hop_variant_id,
        first_hop_certificate_kind=seed.certificate_kind,
        first_hop_certificate_sha256=seed.certificate_sha256,
        second_hop_root_binding_id="detprov_root:" + "f" * 64,
        second_hop_result_id="e2-result:second",
        second_hop_result_line_number=1,
        second_hop_profile_id="p14-test",
        second_hop_rule_id="p14_independent_binder_permutation",
        second_hop_family_id="p14_independent_binder_permutation",
        second_hop_attempt_id="attempt:second",
        second_hop_draft_id="draft:second",
        second_hop_audit_id="audit:second",
        second_hop_variant_id="variant:second",
        second_hop_evidence_class="E2",
        second_hop_intended_relation="equivalent",
        second_hop_polarity_metadata="positive",
        second_hop_certificate_kind="binder_permutation_certificate",
        second_hop_certificate_sha256="0" * 64,
        final_candidate_code_hash=source_hash,
        final_alpha_identity_fingerprint=source_alpha,
    )
    return seed, chain


def test_p14_cycle_novel_chain_and_exact_pair_deduplication() -> None:
    seed, cycle = _synthetic_p14_cycle()
    duplicate = cycle.model_copy(
        update={
            "chain_id": "detcomp_chain:" + "1" * 64,
            "final_theorem_id": "thm:" + "2" * 64,
            "final_representation_id": "repr:" + "3" * 64,
        }
    )
    novel = cycle.model_copy(
        update={
            "chain_id": "detcomp_chain:" + "4" * 64,
            "final_theorem_id": "thm:" + "5" * 64,
            "final_representation_id": "repr:" + "6" * 64,
            "final_candidate_code_hash": "7" * 64,
            "final_alpha_identity_fingerprint": "8" * 64,
        }
    )
    records = _unique_pairs(
        seed_set_id=cycle.seed_set_id,
        chain_set_id="detcomp_chain_set:" + "9" * 64,
        chains=(cycle, duplicate, novel),
        seeds_by_id={seed.seed_id: seed},
    )

    assert len(records) == 2
    returned = next(item for item in records if item.source_alpha_return)
    assert returned.chain_sequences == (
        "p14_independent_binder_permutation->p14_independent_binder_permutation",
    )
    assert returned.source_content_return is True
    assert returned.alpha_novel is False
    assert returned.gross_chain_count == 2
    assert returned.duplicate_excess_count == 1
    novel_record = next(item for item in records if item.alpha_novel)
    assert novel_record.source_alpha_return is False
    assert novel_record.gross_chain_count == 1
    assert all(item.training_eligible is False for item in records)


def test_forbidden_chain_eligibility_fails_closed() -> None:
    seed, cycle = _synthetic_p14_cycle()
    forbidden = cycle.model_copy(update={"training_eligible": True})
    with pytest.raises(CompositionUniquePairError, match="training eligible"):
        _unique_pairs(
            seed_set_id=cycle.seed_set_id,
            chain_set_id="detcomp_chain_set:" + "9" * 64,
            chains=(forbidden,),
            seeds_by_id={seed.seed_id: seed},
        )


def _real_inputs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> tuple[Path, Path]:
    seeds = _seed_set(monkeypatch, tmp_path)
    second_hop = _run_second_hop(
        monkeypatch,
        tmp_path,
        kind="e2",
        theorem_path=seeds.theorem_path,
        representation_path=seeds.representation_path,
    )
    chains = audit_deterministic_v2_composition_chains(
        seed_dir=seeds.output_dir,
        second_hop_roots=(second_hop,),
        output_dir=tmp_path / "chains",
    )
    return seeds.output_dir, chains.output_dir


def test_unique_pair_postprocess_is_immutable_and_exactly_replayable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path)
    output = tmp_path / "unique"
    first = postprocess_deterministic_v2_composition_unique_pairs(
        seed_dir=seed_dir,
        chain_dir=chain_dir,
        output_dir=output,
    )
    second = postprocess_deterministic_v2_composition_unique_pairs(
        seed_dir=seed_dir,
        chain_dir=chain_dir,
        output_dir=output,
    )

    assert first.replayed is False
    assert second.replayed is True
    assert first.unique_pair_set_id == second.unique_pair_set_id
    manifest = DeterministicCompositionUniquePairManifest.model_validate_json(
        first.manifest_path.read_bytes()
    )
    records = tuple(
        DeterministicCompositionUniquePairRecord.model_validate_json(line)
        for line in first.unique_pairs_path.read_bytes().splitlines()
    )
    assert manifest.gross_chain_count == 1
    assert manifest.unique_pair_count == 1
    assert manifest.duplicate_excess_count == 0
    assert manifest.semantic_labels_created is False
    assert manifest.training_eligible is False
    assert all(item.gate_credit is False for item in records)

    cli = CliRunner().invoke(
        app,
        [
            "postprocess-deterministic-composition-unique-pairs",
            "--seed-dir",
            str(seed_dir),
            "--chain-dir",
            str(chain_dir),
            "--output-dir",
            str(tmp_path / "cli-unique"),
            "--root",
            str(Path.cwd()),
        ],
    )
    assert cli.exit_code == 0, cli.output
    assert "gross_chains=1" in cli.stdout
    assert "unique_pairs=1" in cli.stdout
    assert "training_eligible=false" in cli.stdout


def test_chain_hash_drift_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    drifted = tmp_path / "drifted"
    shutil.copytree(chain_dir, drifted)
    chain_path = drifted / "chains.jsonl"
    chain_path.write_bytes(chain_path.read_bytes() + b"\n")

    with pytest.raises(CompositionUniquePairError, match="partition differs"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=drifted,
            output_dir=tmp_path / "rejected",
        )
