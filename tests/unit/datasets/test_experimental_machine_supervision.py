from __future__ import annotations

import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from leanfaith.cli.app import app
from leanfaith.datasets import experimental_machine_supervision as ems
from leanfaith.datasets.denylist import DenylistIndex, FrozenBenchmark, FrozenRegistry
from leanfaith.representations.views import signature_near_dup_hash
from leanfaith.schemas.manifest import CodeState
from leanfaith.schemas.theorem import RepresentationRecord, TheoremRecord
from leanfaith.transforms.composition_seed import CompositionSeedRecord
from leanfaith.transforms.provisional_pair_combine import (
    MaterializationRootBinding,
    ProvisionalPairCombinationManifest,
    ProvisionalPairObservation,
    UniqueProvisionalPair,
)

SHA = "a" * 64


def _config(*, maximum_variants_per_component: int = 4) -> ems.ExperimentalMachineSupervisionConfig:
    return ems.ExperimentalMachineSupervisionConfig(
        profile_id="fixture_2",
        selection_seed="fixture-seed",
        positive_count=1,
        negative_count=1,
        positive_family_quotas={"p14_family": 1},
        negative_family_quotas={"n11_family": 1},
        maximum_variants_per_component=maximum_variants_per_component,
        public_sample_per_target=1,
        audit_manifest_sha256=SHA,
        audit_gross_observations_sha256=SHA,
        audit_unique_pairs_sha256=SHA,
        positive_seed_manifest_sha256=SHA,
        positive_seed_records_sha256=SHA,
        benchmark_manifest_sha256=SHA,
        benchmark_active_registry_sha256=SHA,
        benchmark_authorization_sha256=SHA,
    )


def _observation(
    suffix: str,
    *,
    family: str,
    relation: str,
    root_id: str,
    groups: tuple[str, ...],
) -> ProvisionalPairObservation:
    return ProvisionalPairObservation.model_construct(
        observation_id=f"observation:{suffix}",
        root_binding_id=root_id,
        result_id=f"result:{suffix}",
        result_line_number=1,
        profile_id=f"profile:{suffix}",
        family_id=family,
        rule_id=family,
        context_id="ctx:fixture",
        source_theorem_ids=(f"source-theorem:{suffix}",),
        source_representation_ids=(f"source-representation:{suffix}",),
        source_categories=("mathlib",),
        source_root_ancestry_ids=groups,
        pair_id=f"pair:{suffix}",
        attempt_id=f"attempt:{suffix}",
        draft_id=f"draft:{suffix}",
        audit_id=f"audit:{suffix}",
        variant_id=f"variant:{suffix}",
        candidate_theorem_id=f"candidate-theorem:{suffix}",
        candidate_representation_id=f"candidate-representation:{suffix}",
        candidate_code_hash=(suffix[0] * 64),
        candidate_alpha_identity_fingerprint=(suffix[-1] * 64),
        intended_relation=relation,
        polarity_metadata="positive" if relation == "equivalent" else "negative",
        exact_pair_key=(suffix[0] * 64),
        candidate_code_key=(suffix[1] * 64),
        ancestry_candidate_key=(suffix[-2] * 64),
        alpha_candidate_key=(suffix[-1] * 64),
    )


def _unique(observation: ProvisionalPairObservation) -> UniqueProvisionalPair:
    return UniqueProvisionalPair.model_construct(
        unique_pair_id=f"detprov_pair:{observation.exact_pair_key}",
        exact_pair_key=observation.exact_pair_key,
        source_categories=("mathlib",),
        family_ids=(observation.family_id,),
        intended_relations=(observation.intended_relation,),
        observation_ids=(observation.observation_id,),
        conflicting_intentions=False,
    )


def _candidate(
    suffix: str,
    *,
    family: str,
    target: ems.PseudoTarget,
    groups: tuple[str, ...],
) -> ems._Candidate:
    relation = "equivalent" if target == "same_claim" else "near_miss"
    observation = _observation(
        suffix,
        family=family,
        relation=relation,
        root_id=f"root:{suffix}",
        groups=groups,
    )
    return ems._Candidate(
        unique=_unique(observation),
        observation=observation,
        root=MaterializationRootBinding.model_construct(),
        source_theorem=TheoremRecord.model_construct(),
        source_representation=RepresentationRecord.model_construct(),
        candidate_theorem=TheoremRecord.model_construct(),
        candidate_representation=RepresentationRecord.model_construct(),
        pseudo_target=target,
        evidence_class="E2" if target == "same_claim" else "D0",
        seed=None,
    )


def test_pinned_mathlib_2k_config_is_balanced_and_strict() -> None:
    loaded = ems.load_experimental_machine_supervision_config(
        Path("configs/data/experimental_machine_supervision_mathlib_2k_v1.yaml")
    )

    assert loaded.config.positive_count == 1_000
    assert loaded.config.negative_count == 1_000
    assert sum(loaded.config.positive_family_quotas.values()) == 1_000
    assert sum(loaded.config.negative_family_quotas.values()) == 1_000
    assert loaded.config.negative_family_quotas == {
        "n11_bound_variable_substitution": 232,
        "n12_implication_converse": 383,
        "n15_conjunct_omission": 1,
        "n16_domain_guard_removal": 1,
        "n18_root_equality_polarity": 383,
    }
    assert loaded.config.maximum_variants_per_component == 4

    with pytest.raises(ValueError, match="do not sum"):
        ems.ExperimentalMachineSupervisionConfig.model_validate(
            {
                **loaded.config.model_dump(mode="json"),
                "positive_count": 999,
            }
        )


def test_manifest_rejects_dirty_or_untracked_code() -> None:
    config = _config()
    payload = {
        "dataset_id": f"experimental-machine-supervision:{'b' * 64}",
        "profile_id": config.profile_id,
        "config_hash": ems.hash_canonical(config.model_dump(mode="json")),
        "config": config.model_dump(mode="json"),
        "inputs": {
            "fixture": {
                "path": "/fixture/input.jsonl",
                "sha256": SHA,
                "byte_count": 1,
            }
        },
        "record_count": 2,
        "component_count": 2,
        "output_sha256": {name: SHA for name in ems._OUTPUT_FILES if name != "manifest.json"},
    }
    clean = CodeState(
        git_revision="a" * 40,
        git_dirty=False,
        base_git_commit="a" * 40,
        code_tree_hash=SHA,
    )
    assert (
        ems.ExperimentalMachineSupervisionManifest.model_validate(
            {**payload, "code": clean.model_dump(mode="json")}
        ).code
        == clean
    )

    dirty = clean.model_copy(update={"git_dirty": True})
    with pytest.raises(ValueError, match="clean, fully tracked"):
        ems.ExperimentalMachineSupervisionManifest.model_validate(
            {**payload, "code": dirty.model_dump(mode="json")}
        )

    untracked = clean.model_copy(update={"untracked_files": ("scratch.py",)})
    with pytest.raises(ValueError, match="clean, fully tracked"):
        ems.ExperimentalMachineSupervisionManifest.model_validate(
            {**payload, "code": untracked.model_dump(mode="json")}
        )


def test_union_find_components_close_multi_parent_bridge() -> None:
    components = ems._union_component_ids(
        (
            ("left", ("anc:a", "anc:b")),
            ("bridge", ("anc:b", "anc:c")),
            ("right", ("anc:c",)),
            ("separate", ("anc:z",)),
        )
    )

    assert components["left"] == components["bridge"] == components["right"]
    assert components["separate"] != components["left"]


def test_selection_applies_cap_to_union_find_component() -> None:
    positive = _candidate(
        "ab",
        family="p14_family",
        target="same_claim",
        groups=("anc:a", "anc:b"),
    )
    negative = _candidate(
        "cd",
        family="n11_family",
        target="not_same_claim",
        groups=("anc:b", "anc:c"),
    )

    with pytest.raises(ems.ExperimentalMachineSupervisionError, match="after component"):
        ems._select_quotas(
            (positive, negative),
            config=_config(maximum_variants_per_component=1),
        )

    selected = ems._select_quotas(
        (positive, negative),
        config=_config(maximum_variants_per_component=2),
    )
    assert {item.pseudo_target for item in selected} == {"same_claim", "not_same_claim"}


def test_positive_locator_requires_exact_e2_seed_receipt() -> None:
    positive = _observation(
        "ab",
        family="p14_family",
        relation="equivalent",
        root_id="root:e2",
        groups=("anc:a",),
    )
    negative = _observation(
        "cd",
        family="n11_family",
        relation="near_miss",
        root_id="root:d0",
        groups=("anc:c",),
    )
    e2_root = MaterializationRootBinding.model_construct(
        root_binding_id="root:e2",
        run_kind="e2",
    )
    d0_root = MaterializationRootBinding.model_construct(
        root_binding_id="root:d0",
        run_kind="d0",
    )
    manifest = ProvisionalPairCombinationManifest.model_construct(
        combination_hash=SHA,
        root_bindings=(d0_root, e2_root),
    )
    seed = CompositionSeedRecord.model_construct(
        input_combination_hash=SHA,
        unique_pair_id=_unique(positive).unique_pair_id,
        exact_pair_key=positive.exact_pair_key,
        selected_observation_id=positive.observation_id,
        first_hop_root_binding_id=positive.root_binding_id,
        first_hop_result_id=positive.result_id,
        first_hop_result_line_number=positive.result_line_number,
        first_hop_family_id=positive.family_id,
        first_hop_rule_id=positive.rule_id,
        first_hop_attempt_id=positive.attempt_id,
        first_hop_draft_id=positive.draft_id,
        first_hop_audit_id=positive.audit_id,
        first_hop_variant_id=positive.variant_id,
        source_theorem_id=positive.source_theorem_ids[0],
        source_representation_id=positive.source_representation_ids[0],
        intermediate_theorem_id=positive.candidate_theorem_id,
        intermediate_representation_id=positive.candidate_representation_id,
        root_ancestry_ids=positive.source_root_ancestry_ids,
        intermediate_candidate_code_hash=positive.candidate_code_hash,
        intermediate_alpha_identity_fingerprint=(positive.candidate_alpha_identity_fingerprint),
    )

    without_seed = ems._candidate_locators(
        manifest,
        (positive, negative),
        (_unique(positive), _unique(negative)),
        {},
        config=_config(),
    )
    assert [item.pseudo_target for item in without_seed] == ["not_same_claim"]

    with_seed = ems._candidate_locators(
        manifest,
        (positive, negative),
        (_unique(positive), _unique(negative)),
        {_unique(positive).unique_pair_id: seed},
        config=_config(),
    )
    assert {item.evidence_class for item in with_seed} == {"E2", "D0"}

    forged = seed.model_copy(update={"first_hop_result_id": "result:forged"})
    with pytest.raises(ems.ExperimentalMachineSupervisionError, match="receipt disagrees"):
        ems._candidate_locators(
            manifest,
            (positive, negative),
            (_unique(positive), _unique(negative)),
            {_unique(positive).unique_pair_id: forged},
            config=_config(),
        )


def test_denylist_screens_row_identity_and_normalized_signature() -> None:
    protected_signature = "(x : Nat)   ->   x = x"
    registry = FrozenRegistry(
        frozen_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
        representation_signatures_appended=True,
        benchmarks=(
            FrozenBenchmark(
                registry_key="fixture",
                resolved=True,
                row_ids=("source-row",),
                representation_hashes=(signature_near_dup_hash(protected_signature),),
            ),
        ),
    )
    active = cast(
        Any,
        SimpleNamespace(index=DenylistIndex(registry)),
    )
    theorem = TheoremRecord.model_construct(
        theorem_id="theorem:fixture",
        source_record="unprotected-row",
        source_record_id=None,
        upstream_uuid=None,
        proof_stripped_declaration="theorem fixture : True := by sorry",
    )
    representation = RepresentationRecord.model_construct(
        headless=protected_signature,
        signature_pp=None,
        signature_explicit="different",
        alpha_identity_fingerprint="b" * 64,
    )
    assert ems._representation_is_protected(active, theorem, representation)

    row_theorem = theorem.model_copy(update={"source_record": "source-row"})
    clear_representation = representation.model_copy(
        update={"headless": "clear", "signature_explicit": "clear"}
    )
    assert ems._representation_is_protected(active, row_theorem, clear_representation)


def test_immutable_output_replays_and_rejects_tamper(tmp_path: Path) -> None:
    payloads = {name: f"{name}\n".encode() for name in ems._OUTPUT_FILES}
    output = tmp_path / "corpus"

    assert ems._write_or_replay(output, payloads) is False
    assert ems._write_or_replay(output, payloads) is True

    (output / "records.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ems.ExperimentalMachineSupervisionError, match="differs"):
        ems._write_or_replay(output, payloads)


def test_loader_requires_explicit_opt_in_and_admitted_purpose(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ems.ExperimentalMachineSupervisionError, match="requires"):
        ems.load_experimental_machine_supervision(
            tmp_path,
            allow_experimental_machine_supervision=False,
            purpose="smoke_training",
        )
    with pytest.raises(ems.ExperimentalMachineSupervisionError, match="forbidden"):
        ems.load_experimental_machine_supervision(
            tmp_path,
            allow_experimental_machine_supervision=True,
            purpose="model_selection",
        )

    monkeypatch.setattr(
        ems,
        "verify_experimental_machine_supervision",
        lambda _path: SimpleNamespace(allowed_purposes=("smoke_training", "learning_curve")),
    )
    monkeypatch.setattr(ems, "_load_canonical_jsonl", lambda _path, _model: ())
    assert (
        ems.load_experimental_machine_supervision(
            tmp_path,
            allow_experimental_machine_supervision=True,
            purpose="smoke_training",
        )
        == ()
    )


def test_cli_exposes_freeze_and_verify_commands() -> None:
    runner = CliRunner()
    freeze = runner.invoke(app, ["freeze-experimental-machine-supervision", "--help"])
    verify = runner.invoke(app, ["verify-experimental-machine-supervision", "--help"])

    assert freeze.exit_code == 0, freeze.output
    assert verify.exit_code == 0, verify.output
