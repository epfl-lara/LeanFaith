"""Focused tests for audit-only deterministic composition deduplication."""

from __future__ import annotations

import ctypes
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import leanfaith.transforms.composition_unique_pairs as unique_pair_module
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
    assert all(item.schema_version == 2 for item in records)
    returned = next(item for item in records if item.source_alpha_return)
    assert returned.chain_sequences == (
        "p14_independent_binder_permutation->p14_independent_binder_permutation",
    )
    assert "source_content_return" not in returned.model_dump()
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
    assert manifest.schema_version == 2
    assert manifest.method_version == "deterministic_v2_composition_unique_pairs_v2"
    assert manifest.unique_pair_count == 1
    assert manifest.duplicate_excess_count == 0
    assert "gross_source_content_return_count" not in manifest.model_dump()
    assert "unique_source_content_return_count" not in manifest.model_dump()
    assert manifest.semantic_labels_created is False
    assert manifest.training_eligible is False
    assert all(item.gate_credit is False for item in records)
    assert all(item.schema_version == 2 for item in records)
    assert b"source_content_return" not in first.unique_pairs_path.read_bytes()
    assert b"source_content_return" not in first.manifest_path.read_bytes()

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


@pytest.mark.parametrize("input_name", ["seed", "chain"])
@pytest.mark.parametrize("through_parent", [False, True])
def test_input_directory_rejects_symlink_traversal(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    input_name: str,
    through_parent: bool,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    target = seed_dir if input_name == "seed" else chain_dir
    if through_parent:
        linked_parent = tmp_path / f"{input_name}-parent-link"
        linked_parent.symlink_to(target.parent, target_is_directory=True)
        unsafe = linked_parent / target.name
    else:
        unsafe = tmp_path / f"{input_name}-link"
        unsafe.symlink_to(target, target_is_directory=True)

    kwargs = {
        "seed_dir": unsafe if input_name == "seed" else seed_dir,
        "chain_dir": unsafe if input_name == "chain" else chain_dir,
        "output_dir": tmp_path / f"rejected-{input_name}-{through_parent}",
    }
    with pytest.raises(CompositionUniquePairError, match="traverses a symlink"):
        postprocess_deterministic_v2_composition_unique_pairs(**kwargs)


def test_output_directory_rejects_symlink_leaf(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    real_output = tmp_path / "real-output"
    real_output.mkdir()
    linked_output = tmp_path / "linked-output"
    linked_output.symlink_to(real_output, target_is_directory=True)

    with pytest.raises(CompositionUniquePairError, match="output cannot be a symlink"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=linked_output,
        )


def test_output_directory_rejects_symlinked_parent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(CompositionUniquePairError, match="parent traverses a symlink"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=linked_parent / "unique",
        )


def test_output_directory_rejects_non_directory_parent_component(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    unsafe_parent = tmp_path / "not-a-directory"
    unsafe_parent.write_text("unsafe", encoding="utf-8")

    with pytest.raises(CompositionUniquePairError, match="component is not a directory"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=unsafe_parent / "unique",
        )


def test_output_inside_input_is_rejected_before_parent_creation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    output = seed_dir / "must-not-exist" / "unique"

    with pytest.raises(CompositionUniquePairError, match="cannot be inside an input"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=output,
        )
    assert not output.parent.exists()


def test_input_root_substitution_race_fails_closed_without_reading_replacement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    moved_seed = tmp_path / "held-seed-root"
    hostile_seed = tmp_path / "hostile-seed-root"
    hostile_seed.mkdir()
    output = tmp_path / "must-not-publish"

    def swap_after_roots_bind(event: str) -> None:
        if event != "after_input_roots_bound":
            return
        seed_dir.rename(moved_seed)
        seed_dir.symlink_to(hostile_seed, target_is_directory=True)

    monkeypatch.setattr(unique_pair_module, "_RACE_HOOK", swap_after_roots_bind)
    with pytest.raises(CompositionUniquePairError, match=r"symlink|identity changed"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=output,
        )

    assert not output.exists()
    assert tuple(hostile_seed.iterdir()) == ()


def test_output_parent_substitution_race_cannot_redirect_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    trusted_parent = tmp_path / "trusted-output-parent"
    held_parent = tmp_path / "held-output-parent"
    outside = tmp_path / "hostile-output-target"
    trusted_parent.mkdir()
    outside.mkdir()
    output = trusted_parent / "unique"

    def swap_before_publish(event: str) -> None:
        if event != "before_output_publish":
            return
        trusted_parent.rename(held_parent)
        trusted_parent.symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(unique_pair_module, "_RACE_HOOK", swap_before_publish)
    with pytest.raises(CompositionUniquePairError, match=r"symlink|identity changed"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=output,
        )

    assert tuple(outside.iterdir()) == ()
    assert tuple(held_parent.iterdir()) == ()


def test_missing_no_follow_support_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    monkeypatch.delattr(os, "O_NOFOLLOW")

    with pytest.raises(CompositionUniquePairError, match=r"no-follow.*unavailable"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=tmp_path / "must-not-publish",
        )


def test_missing_atomic_noreplace_support_fails_closed_and_cleans_temporary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    output_parent = tmp_path / "output-parent"

    class _NoRenameAt2:
        pass

    monkeypatch.setattr(
        ctypes,
        "CDLL",
        lambda *_args, **_kwargs: _NoRenameAt2(),
    )
    with pytest.raises(CompositionUniquePairError, match=r"renameat2.*unavailable"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=output_parent / "unique",
        )

    assert tuple(output_parent.iterdir()) == ()


def test_existing_replay_holds_all_files_until_final_content_reverification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    output = tmp_path / "unique"
    postprocess_deterministic_v2_composition_unique_pairs(
        seed_dir=seed_dir,
        chain_dir=chain_dir,
        output_dir=output,
    )

    def mutate_already_checked_file(event: str) -> None:
        if event != "during_existing_output_verify":
            return
        manifest = output / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")

    monkeypatch.setattr(unique_pair_module, "_RACE_HOOK", mutate_already_checked_file)
    with pytest.raises(CompositionUniquePairError, match=r"changed|content|metadata"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=output,
        )


def test_final_content_verification_catches_mutation_after_path_checks(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    output = tmp_path / "unique"
    postprocess_deterministic_v2_composition_unique_pairs(
        seed_dir=seed_dir,
        chain_dir=chain_dir,
        output_dir=output,
    )

    def mutate_after_path_checks(event: str) -> None:
        if event != "before_final_output_content_verify":
            return
        pairs = output / "unique_pairs.jsonl"
        pairs.write_bytes(pairs.read_bytes() + b" ")

    monkeypatch.setattr(unique_pair_module, "_RACE_HOOK", mutate_after_path_checks)
    with pytest.raises(CompositionUniquePairError, match="output differs"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=output,
        )


def test_pre_publish_temp_substitution_fails_before_rename_and_removes_all_debris(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    output_parent = tmp_path / "output-parent"
    output_parent.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    output = output_parent / "unique"

    def replace_private_temp(event: str) -> None:
        if event != "before_output_publish":
            return
        temporaries = tuple(output_parent.glob(".unique.*"))
        assert len(temporaries) == 1
        parked = output_parent / "parked-original"
        temporaries[0].rename(parked)
        temporaries[0].symlink_to(outside, target_is_directory=True)

    monkeypatch.setattr(unique_pair_module, "_RACE_HOOK", replace_private_temp)
    with pytest.raises(CompositionUniquePairError, match=r"symlink|identity"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=output,
        )

    assert not output.exists()
    assert tuple(output_parent.iterdir()) == ()
    assert tuple(outside.iterdir()) == ()


def test_temp_mkdir_then_open_failure_cleans_private_namespace(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    seed_dir, chain_dir = _real_inputs(monkeypatch, tmp_path / "source")
    output_parent = tmp_path / "output-parent"
    original_open_child = unique_pair_module._open_child_directory

    def fail_private_open(
        parent: unique_pair_module._HeldDirectory,
        name: str,
    ) -> unique_pair_module._HeldDirectory:
        if name.startswith(".unique."):
            raise CompositionUniquePairError("synthetic temp open failure")
        return original_open_child(parent, name)

    monkeypatch.setattr(unique_pair_module, "_open_child_directory", fail_private_open)
    with pytest.raises(CompositionUniquePairError, match="synthetic temp open failure"):
        postprocess_deterministic_v2_composition_unique_pairs(
            seed_dir=seed_dir,
            chain_dir=chain_dir,
            output_dir=output_parent / "unique",
        )

    assert tuple(output_parent.iterdir()) == ()


def test_directory_walk_fstat_failure_is_normalized_and_closes_child_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    original_fstat = os.fstat
    fstat_calls = 0
    failed_fds: list[int] = []

    def fail_child_fstat(fd: int) -> os.stat_result:
        nonlocal fstat_calls
        fstat_calls += 1
        if fstat_calls == 2:
            failed_fds.append(fd)
            raise OSError("synthetic child fstat failure")
        return original_fstat(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fstat", fail_child_fstat)
        with pytest.raises(CompositionUniquePairError, match="descriptor cannot be verified"):
            unique_pair_module._open_held_directory(tmp_path, label="fstat fixture")

    assert len(failed_fds) == 1
    with pytest.raises(OSError):
        original_fstat(failed_fds[0])


def test_child_directory_fstat_failure_is_normalized_and_closes_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    child = tmp_path / "child"
    child.mkdir()
    original_fstat = os.fstat
    failed_fds: list[int] = []

    with unique_pair_module._open_held_directory(tmp_path, label="parent") as parent:

        def fail_opened_fstat(fd: int) -> os.stat_result:
            failed_fds.append(fd)
            raise OSError("synthetic child fstat failure")

        with monkeypatch.context() as patch:
            patch.setattr(os, "fstat", fail_opened_fstat)
            with pytest.raises(
                CompositionUniquePairError,
                match="output descriptor cannot be verified",
            ):
                unique_pair_module._open_child_directory(parent, child.name)

    assert len(failed_fds) == 1
    with pytest.raises(OSError):
        original_fstat(failed_fds[0])
