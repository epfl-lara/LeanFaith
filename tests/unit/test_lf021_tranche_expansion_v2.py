from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, sha256_hex
from leanfaith.evaluation.prevalence import (
    PrevalenceInputError,
    load_v2_frame_projection,
    load_verified_v2_frame_projection_bytes,
)
from leanfaith.generation import tranche_expansion as v1
from leanfaith.generation import tranche_expansion_v2 as v2

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "configs/generation/lf021_tranche_expansion_v2.yaml"
MANIFESTS = (
    ROOT
    / (
        "data/raw/real_outputs/gate3_docstrings_operational_v1/v2/local_collection/"
        "3801b405ec8b7008f8c38f449189a52fe5e74bea3a98f5e3e0abdaa75edac62c/"
        "postprocess_v3/manifest.json"
    ),
    ROOT
    / (
        "data/raw/real_outputs/cross_domain_docstrings_operational_v1/v3/"
        "cross_domain_s0/local_collection/"
        "b5080892f0b71e43735dfe3a1f3bf4e227f7988c362196ea7a09ea703db3846c/"
        "postprocess_v4/manifest.json"
    ),
    ROOT
    / (
        "data/raw/real_outputs/gate3_docstrings_operational_v1/v4/algebra_s1/"
        "local_collection/"
        "1936c77952aa28a2f482c60b646cdbde556b9cc52f8472552a83c2158902ac6c/"
        "postprocess_v5/manifest.json"
    ),
    ROOT
    / (
        "data/raw/real_outputs/cross_domain_docstrings_operational_v1/v4/"
        "cross_domain_s1/local_collection/"
        "40860adfb4f149dbf357f3cc624a4df02bdae020be27cdb4f3f16ca4f9771100/"
        "postprocess_v5/manifest.json"
    ),
    ROOT
    / (
        "data/raw/real_outputs/gate3_docstrings_operational_v1/v5/algebra_s2/"
        "local_collection/"
        "9dc8113d3a1529b8e9bfa20bec1f2593a254d2359df0305ecd89521179be6af4/"
        "postprocess_v6/manifest.json"
    ),
)


def _binding(index: int) -> v1.ArtifactBinding:
    return v1.ArtifactBinding(artifact=f"fake/{index}.json", sha256=f"{index + 1:064x}")


def _member(
    index: int,
    *,
    problem: str,
    alpha: str,
    family: str,
    proxy: str,
) -> v1._CandidateMember:
    return v1._CandidateMember(
        invocation_id=f"invocation:{index:064x}",
        family_id=family,
        pool_id="algebra_gate3_docstrings_v1",
        source_proxy=proxy,
        problem_record_id=problem,
        alpha_identity_fingerprint=alpha,
        postprocess_manifest_id=f"manifest:{index:064x}",
        terminal_artifact=_binding(index * 3),
        screening_artifact=_binding(index * 3 + 1),
        representation_artifact=_binding(index * 3 + 2),
    )


def test_v2_policy_binds_v1_and_forbids_semantic_inputs() -> None:
    loaded = v2.load_amendment_v2(POLICY)
    assert loaded.config.scientific_cluster_key == (
        "problem_group",
        "alpha_identity_fingerprint",
    )
    assert loaded.config.base_v1_policy.sha256 == (
        "0c726e197ef5358ef4a7414e36622c10fb7ff19082199540b62e4bc170834e16"
    )
    assert "same_claim" in loaded.config.forbidden_inputs
    assert "human_label" in loaded.config.forbidden_inputs
    assert not loaded.config.semantic_labels_created


def test_problem_group_alpha_clustering_is_order_invariant() -> None:
    alpha = "a" * 64
    first = _member(
        1,
        problem="problem:one",
        alpha=alpha,
        family="goedel_formalizer_v2_8b",
        proxy="Algebra/Group",
    )
    same_group = _member(
        2,
        problem="problem:alias",
        alpha=alpha,
        family="kimina_autoformalizer_7b",
        proxy="Algebra/Group",
    )
    other_group = _member(
        3,
        problem="problem:other",
        alpha=alpha,
        family="stepfun_formalizer_7b",
        proxy="Algebra/Group",
    )
    observation = cast(
        v1.LoadedObservation,
        SimpleNamespace(candidates=(first, same_group, other_group)),
    )
    groups = {
        "problem:one": "nl-problem:one",
        "problem:alias": "nl-problem:one",
        "problem:other": "nl-problem:other",
    }
    forward = v2._cluster_candidates(
        (observation,),
        problem_groups=groups,
        representative_hash_salt="test",
    )
    reverse_observation = cast(
        v1.LoadedObservation,
        SimpleNamespace(candidates=(other_group, same_group, first)),
    )
    reverse = v2._cluster_candidates(
        (reverse_observation,),
        problem_groups=groups,
        representative_hash_salt="test",
    )
    assert forward == reverse
    assert len(forward) == 2
    by_group = {cluster.problem_group: cluster for cluster in forward}
    assert len(by_group["nl-problem:one"].members) == 2
    assert len(by_group["nl-problem:other"].members) == 1


def test_five_manifest_problem_aware_replay_is_81_and_selects_cross_s2() -> None:
    loaded = v2.load_amendment_v2(POLICY)
    first, first_frame = v2.evaluate_tranche_expansion_v2(
        repo_root=ROOT,
        loaded_amendment=loaded,
        observed_manifests=MANIFESTS,
    )
    second, second_frame = v2.evaluate_tranche_expansion_v2(
        repo_root=ROOT,
        loaded_amendment=loaded,
        observed_manifests=MANIFESTS,
    )
    assert first == second
    assert first_frame == second_frame is None
    assert first.counts.benchmark_clear_compile_count == 93
    assert first.counts.unique_compiling_count == 81
    assert first.action is v1.DecisionAction.COLLECT_NEXT_TRANCHE
    assert first.next_tranche is not None
    assert first.next_tranche.tranche_id == "cross_domain_s2"
    assert not first.semantic_labels_inspected
    assert not first.gate_5g_credit_claimed


def test_v2_frame_is_compatible_with_frozen_prevalence_projection(tmp_path: Path) -> None:
    loaded = v2.load_amendment_v2(POLICY)
    base = v1.load_tranche_expansion_policy(ROOT / loaded.config.base_v1_policy.artifact).config
    member = _member(
        11,
        problem="problem:one",
        alpha="b" * 64,
        family="goedel_formalizer_v2_8b",
        proxy="Algebra/Group",
    )
    cluster_id = "candidate_cluster_v2:" + hash_canonical(
        {
            "schema": "lf021_problem_group_alpha_cluster_v2",
            "problem_group": "nl-problem:one",
            "alpha_identity_fingerprint": "b" * 64,
        }
    )
    cluster = v2._ProblemAwareCluster(
        cluster_id=cluster_id,
        problem_group="nl-problem:one",
        alpha_identity_fingerprint="b" * 64,
        representative=member,
        members=(member,),
    )
    records = v2._build_frame_items(
        (cluster,),
        target=1,
        base_policy=base,
        amendment=loaded.config,
    )
    frame_path = tmp_path / "frame.jsonl"
    frame_path.write_bytes(
        b"".join(canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in records)
    )
    projection = load_v2_frame_projection(frame_path)
    assert len(projection) == 1
    assert projection[0].problem_group == "nl-problem:one"
    assert projection[0].member_count == 1

    incoherent = records[0].model_dump(mode="json")
    incoherent["cluster_id"] = f"candidate_cluster_v2:{'f' * 64}"
    incoherent["frame_record_id"] = "lf021_prevalence_item_v2:" + hash_canonical(
        {
            "schema": "lf021_prevalence_frame_item_v2",
            **{key: value for key, value in incoherent.items() if key != "frame_record_id"},
        }
    )
    frame_path.write_bytes(canonical_json_bytes(incoherent) + b"\n")
    with pytest.raises(PrevalenceInputError, match="cluster_id differs"):
        load_v2_frame_projection(frame_path)


def test_estimator_rejects_fixed_salt_v2_frame_even_when_binding_matches(
    tmp_path: Path,
) -> None:
    loaded = v2.load_amendment_v2(POLICY)
    base = v1.load_tranche_expansion_policy(ROOT / loaded.config.base_v1_policy.artifact).config
    member = _member(
        21,
        problem="problem:fixed-salt",
        alpha="d" * 64,
        family="goedel_formalizer_v2_8b",
        proxy="Algebra/Group",
    )
    problem_group = "nl-problem:fixed-salt"
    cluster_id = "candidate_cluster_v2:" + hash_canonical(
        {
            "schema": "lf021_problem_group_alpha_cluster_v2",
            "problem_group": problem_group,
            "alpha_identity_fingerprint": "d" * 64,
        }
    )
    records = v2._build_frame_items(
        (
            v2._ProblemAwareCluster(
                cluster_id=cluster_id,
                problem_group=problem_group,
                alpha_identity_fingerprint="d" * 64,
                representative=member,
                members=(member,),
            ),
        ),
        target=1,
        base_policy=base,
        amendment=loaded.config,
    )
    frame_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in records
    )
    frame_sha = sha256_hex(frame_bytes)
    frame_id = f"lf021_prevalence_frame_v2:{'e' * 64}"
    frame_binding = v2.FrameBindingV2(
        frame_id=frame_id,
        artifact=f"frames/{frame_id.rsplit(':', 1)[-1]}.jsonl",
        sha256=frame_sha,
        item_count=1,
        sampling_method="problem_aware_stratified_hash_srs_without_replacement_v2",
        propensity_definition="stratum_sample_size/stratum_population_size",
    )
    base_decision, _ = v2.evaluate_tranche_expansion_v2(
        repo_root=ROOT,
        loaded_amendment=loaded,
        observed_manifests=MANIFESTS,
    )
    payload = base_decision.model_dump(mode="json", exclude={"decision_id"})
    payload.update(
        {
            "action": v1.DecisionAction.FREEZE_REDUCED_FRAME.value,
            "next_tranche": None,
            "frame": frame_binding.model_dump(mode="json"),
            "reduced_data_ablation": True,
        }
    )
    decision_id = "lf021_expansion_decision_v2:" + hash_canonical(
        {"schema": "lf021_expansion_decision_v2", **payload}
    )
    decision = v2.ExpansionDecisionV2.model_validate({"decision_id": decision_id, **payload})
    decision_bytes = canonical_json_bytes(decision.model_dump(mode="json")) + b"\n"
    decision_path = tmp_path / "expansion/decisions/decision.json"
    frame_path = tmp_path / "expansion" / frame_binding.artifact
    decision_path.parent.mkdir(parents=True)
    frame_path.parent.mkdir(parents=True)
    decision_path.write_bytes(decision_bytes)
    frame_path.write_bytes(frame_bytes)

    with pytest.raises(PrevalenceInputError, match="fixed-salt hash ranking"):
        load_verified_v2_frame_projection_bytes(
            repo_root=ROOT,
            expansion_decision_path=decision_path,
            expansion_decision_bytes=decision_bytes,
            frame_path=frame_path,
            frame_bytes=frame_bytes,
        )


def test_v2_fails_closed_when_base_policy_binding_changes(tmp_path: Path) -> None:
    text = POLICY.read_text(encoding="utf-8").replace(
        "0c726e197ef5358ef4a7414e36622c10fb7ff19082199540b62e4bc170834e16",
        "f" * 64,
    )
    policy = tmp_path / "tampered.yaml"
    policy.write_text(text, encoding="utf-8")
    with pytest.raises(v2.TrancheExpansionV2Error, match="bound artifact differs"):
        v2.evaluate_tranche_expansion_v2(
            repo_root=ROOT,
            loaded_amendment=v2.load_amendment_v2(policy),
            observed_manifests=MANIFESTS,
        )
