from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from leanfaith.config.hashing import hash_canonical
from leanfaith.config.loading import LoadedConfig
from leanfaith.generation import tranche_expansion as expansion

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "configs/generation/lf021_tranche_expansion_v1.yaml"
ZERO_SHA = "0" * 64


def _binding(name: str) -> expansion.ArtifactBinding:
    return expansion.ArtifactBinding(artifact=f"fake/{name}.json", sha256=ZERO_SHA)


def _member(
    index: int,
    *,
    family: str,
    pool: str,
    proxy: str,
    manifest_id: str = "manifest:fake",
) -> expansion._CandidateMember:
    return expansion._CandidateMember(
        invocation_id=f"invocation:{index:064x}",
        family_id=family,
        pool_id=pool,
        source_proxy=proxy,
        problem_record_id=f"problem:{index:064x}",
        alpha_identity_fingerprint=f"{index + 1:064x}",
        postprocess_manifest_id=manifest_id,
        terminal_artifact=_binding(f"terminal-{index}"),
        screening_artifact=_binding(f"screening-{index}"),
        representation_artifact=_binding(f"representation-{index}"),
    )


def _fake_observation(
    policy: expansion.TrancheExpansionPolicy,
    tranche: expansion.TrancheSpec,
    members: tuple[expansion._CandidateMember, ...],
) -> expansion.LoadedObservation:
    terminals = tuple(
        expansion.OperationalPostprocessTerminalView(
            schema_version=3,
            terminal_id=f"terminal:{index:064x}",
            invocation_id=member.invocation_id,
            family_id=member.family_id,
            problem_record_id=member.problem_record_id,
            seed=tranche.seeds_by_family[member.family_id],
            status="admitted_unresolved",
            parser_executed=True,
            lean_validation_executed=True,
            screening_executed=True,
            output_artifact_hashes={},
            candidate_theorem_id=f"theorem:{index:064x}",
        )
        for index, member in enumerate(members)
    )
    manifest = expansion.OperationalPostprocessManifestView.model_construct(
        schema_version=3,
        manifest_id=f"research_postprocess_v3_manifest:{tranche.order + 1:064x}",
        expected_invocations=len(terminals),
    )
    pool = next(item for item in policy.pools if item.pool_id == tranche.pool_id)
    return expansion.LoadedObservation(
        tranche=tranche,
        binding=expansion.ObservationBinding(
            tranche_id=tranche.tranche_id,
            postprocess_manifest=expansion.ArtifactBinding(
                artifact=f"fake/{tranche.tranche_id}.json",
                sha256=f"{tranche.order + 1:064x}",
            ),
            manifest_id=manifest.manifest_id,
            postprocess_schema_version=manifest.schema_version,
            input_binding_hash=f"{tranche.order + 2:064x}",
        ),
        manifest=manifest,
        terminals=terminals,
        problem_source_proxies={
            f"problem:{index:064x}": pool.declared_source_proxies[
                index % len(pool.declared_source_proxies)
            ]
            for index in range(pool.problem_count)
        },
        candidates=members,
    )


def _loaded_policy(
    policy: expansion.TrancheExpansionPolicy,
) -> LoadedConfig[expansion.TrancheExpansionPolicy]:
    return LoadedConfig(
        config=policy,
        path=POLICY,
        raw=policy.model_dump(mode="json"),
        config_hash=hash_canonical(policy.model_dump(mode="json")),
    )


def _members_for_tranche(
    policy: expansion.TrancheExpansionPolicy,
    tranche: expansion.TrancheSpec,
    *,
    start: int,
    count: int,
) -> tuple[expansion._CandidateMember, ...]:
    pool = next(item for item in policy.pools if item.pool_id == tranche.pool_id)
    families = policy.required_families
    return tuple(
        _member(
            start + offset,
            family=families[offset % len(families)],
            pool=tranche.pool_id,
            proxy=pool.declared_source_proxies[offset % len(pool.declared_source_proxies)],
            manifest_id=f"research_postprocess_v3_manifest:{tranche.order + 1:064x}",
        )
        for offset in range(count)
    )


def test_frozen_policy_has_exact_nonsemantic_sequence() -> None:
    loaded = expansion.load_tranche_expansion_policy(POLICY)
    policy = loaded.config
    assert policy.schema_version == 1
    assert len(policy.required_families) == 3
    assert len(policy.tranches) == 12
    assert [item.tranche_id for item in policy.tranches] == [
        "algebra_s0",
        "cross_domain_s0",
        "algebra_s1",
        "cross_domain_s1",
        "algebra_s2",
        "cross_domain_s2",
        "algebra_s3",
        "cross_domain_s3",
        "algebra_s4",
        "cross_domain_s4",
        "algebra_s5",
        "cross_domain_s5",
    ]
    assert [item.mandatory_before_stopping for item in policy.tranches[:2]] == [
        True,
        True,
    ]
    assert not any(item.mandatory_before_stopping for item in policy.tranches[2:])
    assert policy.frame.minimum_size == 200
    assert policy.frame.preferred_size == 240
    assert policy.frame.maximum_size == 300
    assert set(policy.forbidden_inputs) == {
        "same_claim",
        "relation",
        "faithfulness_judgment",
        "llm_judgment",
        "human_label",
        "proof_search_result",
    }


def test_operational_views_accept_successor_extras_without_version_imports() -> None:
    policy = expansion.load_tranche_expansion_policy(POLICY).config
    families = list(policy.required_families)
    terminal_artifacts = {f"fake/terminal-{index}.json": f"{index + 1:064x}" for index in range(3)}
    document = {
        "schema_version": 4,
        "manifest_id": f"research_postprocess_v4_manifest:{1:064x}",
        "input_binding_hash": f"{2:064x}",
        "problem_count": 1,
        "family_count": 3,
        "seed_count_by_family": dict.fromkeys(families, 1),
        "expected_invocations": 3,
        "terminal_invocations": 3,
        "terminal_artifacts": terminal_artifacts,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
        "input_binding": {
            "problem_pool_manifest": {
                "artifact": "fake/pool-manifest.json",
                "location_kind": "future_version_extra",
                "sha256": f"{3:064x}",
            },
            "problem_pool_records": {
                "artifact": "fake/pool-records.jsonl",
                "location_kind": "future_version_extra",
                "sha256": f"{4:064x}",
            },
            "problem_count": 1,
            "family_count": 3,
            "seed_count_by_family": dict.fromkeys(families, 1),
            "expected_invocations": 3,
            "family_ids": families,
            "future_version_field": "ignored",
        },
        "future_version_field": "ignored",
    }
    manifest = expansion._operational_manifest_view(document)
    assert manifest.schema_version == 4
    assert manifest.input_binding.family_ids == policy.required_families

    terminal = expansion._operational_terminal_view(
        {
            "schema_version": 4,
            "terminal_id": f"terminal:{5:064x}",
            "invocation_id": f"invocation:{6:064x}",
            "family_id": families[0],
            "problem_record_id": f"problem:{7:064x}",
            "seed": 30,
            "status": "admitted_unresolved",
            "parser_executed": True,
            "lean_validation_executed": True,
            "screening_executed": True,
            "output_artifact_hashes": {"fake/output.json": f"{8:064x}"},
            "candidate_theorem_id": f"theorem:{9:064x}",
            "semantic_labels_created": False,
            "supervision_eligible": False,
            "gate_5g_credit_claimed": False,
            "gate_5_closed": False,
            "future_version_field": "ignored",
        }
    )
    assert terminal.schema_version == 4
    assert terminal.status == "admitted_unresolved"

    document["semantic_labels_created"] = True
    with pytest.raises(ValidationError):
        expansion._operational_manifest_view(document)


def test_policy_rejects_semantic_input_and_nonprefix_mandatory_tranches() -> None:
    policy = expansion.load_tranche_expansion_policy(POLICY).config
    bad = policy.model_dump(mode="json")
    bad["decision_inputs"][0] = "same_claim"
    with pytest.raises(ValidationError):
        expansion.TrancheExpansionPolicy.model_validate(bad)

    bad = policy.model_dump(mode="json")
    bad["tranches"][0]["mandatory_before_stopping"] = False
    with pytest.raises(ValidationError):
        expansion.TrancheExpansionPolicy.model_validate(bad)


def test_global_alpha_dedup_is_order_invariant() -> None:
    policy = expansion.load_tranche_expansion_policy(POLICY).config
    first = _member(
        1,
        family=policy.required_families[0],
        pool=policy.pools[0].pool_id,
        proxy=policy.pools[0].declared_source_proxies[0],
    )
    duplicate = replace(
        _member(
            2,
            family=policy.required_families[1],
            pool=policy.pools[1].pool_id,
            proxy=policy.pools[1].declared_source_proxies[0],
        ),
        alpha_identity_fingerprint=first.alpha_identity_fingerprint,
    )
    observation_a = _fake_observation(policy, policy.tranches[0], (first,))
    observation_b = _fake_observation(policy, policy.tranches[1], (duplicate,))
    forward = expansion._cluster_candidates(
        (observation_a, observation_b),
        representative_hash_salt=policy.frame.representative_hash_salt,
    )
    reverse = expansion._cluster_candidates(
        (observation_b, observation_a),
        representative_hash_salt=policy.frame.representative_hash_salt,
    )
    assert forward == reverse
    assert len(forward) == 1
    assert {member.family_id for member in forward[0].members} == {
        policy.required_families[0],
        policy.required_families[1],
    }


def test_cross_domain_tranche_is_mandatory_before_stopping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = expansion.load_tranche_expansion_policy(POLICY)
    policy = loaded.config
    members = _members_for_tranche(
        policy,
        policy.tranches[0],
        start=0,
        count=260,
    )
    observation = _fake_observation(policy, policy.tranches[0], members)
    monkeypatch.setattr(expansion, "load_postprocess_observation", lambda **_kwargs: observation)

    decision, frame = expansion.evaluate_tranche_expansion(
        repo_root=ROOT,
        loaded_policy=loaded,
        observed_manifests=(Path("unused.json"),),
    )
    assert decision.action is expansion.DecisionAction.COLLECT_NEXT_TRANCHE
    assert decision.next_tranche is not None
    assert decision.next_tranche.tranche_id == "cross_domain_s0"
    assert frame is None


def test_preferred_frame_is_deterministic_and_has_exact_propensities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded = expansion.load_tranche_expansion_policy(POLICY)
    policy = loaded.config
    algebra = _members_for_tranche(
        policy,
        policy.tranches[0],
        start=0,
        count=160,
    )
    cross_domain = _members_for_tranche(
        policy,
        policy.tranches[1],
        start=10_000,
        count=100,
    )
    observations = {
        policy.tranches[0].tranche_id: _fake_observation(policy, policy.tranches[0], algebra),
        policy.tranches[1].tranche_id: _fake_observation(policy, policy.tranches[1], cross_domain),
    }

    def fake_load(**kwargs: object) -> expansion.LoadedObservation:
        tranche = kwargs["tranche"]
        assert isinstance(tranche, expansion.TrancheSpec)
        return observations[tranche.tranche_id]

    monkeypatch.setattr(expansion, "load_postprocess_observation", fake_load)
    args = {
        "repo_root": ROOT,
        "loaded_policy": loaded,
        "observed_manifests": (Path("a.json"), Path("b.json")),
    }
    first, first_frame = expansion.evaluate_tranche_expansion(**args)
    second, second_frame = expansion.evaluate_tranche_expansion(**args)
    assert first == second
    assert first_frame == second_frame
    assert first.action is expansion.DecisionAction.FREEZE_PREFERRED_FRAME
    assert first.frame is not None
    assert first.frame.item_count == 240
    assert first_frame is not None
    rows = [
        expansion.FrameItem.model_validate_json(line)
        for line in first_frame.decode("utf-8").splitlines()
    ]
    assert len(rows) == 240
    assert all(
        item.inclusion_probability_numerator == item.stratum_sample_size
        and item.inclusion_probability_denominator == item.stratum_population_size
        for item in rows
    )
    assert all(item.same_claim is None and item.relation is None for item in rows)
    assert not first.semantic_labels_inspected
    assert not first.gate_5g_credit_claimed


def test_exhausted_sequence_freezes_200_to_239_as_reduced_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = expansion.load_tranche_expansion_policy(POLICY)
    policy = original.config.model_copy(update={"tranches": original.config.tranches[:2]})
    loaded = _loaded_policy(policy)
    first_members = _members_for_tranche(
        policy,
        policy.tranches[0],
        start=0,
        count=130,
    )
    second_members = _members_for_tranche(
        policy,
        policy.tranches[1],
        start=10_000,
        count=80,
    )
    observations = (
        _fake_observation(policy, policy.tranches[0], first_members),
        _fake_observation(policy, policy.tranches[1], second_members),
    )

    def fake_load(**kwargs: object) -> expansion.LoadedObservation:
        tranche = kwargs["tranche"]
        assert isinstance(tranche, expansion.TrancheSpec)
        return observations[tranche.order]

    monkeypatch.setattr(expansion, "load_postprocess_observation", fake_load)
    decision, frame = expansion.evaluate_tranche_expansion(
        repo_root=ROOT,
        loaded_policy=loaded,
        observed_manifests=(Path("a.json"), Path("b.json")),
    )
    assert decision.action is expansion.DecisionAction.FREEZE_REDUCED_FRAME
    assert decision.reduced_data_ablation
    assert decision.frame is not None and decision.frame.item_count == 210
    assert frame is not None and len(frame.splitlines()) == 210
    assert any(
        flag.startswith("preferred_frame_shortfall:") for flag in decision.reduced_data_flags
    )


def test_zero_observation_run_is_immutable_and_creates_no_frame(tmp_path: Path) -> None:
    first = expansion.run_tranche_expansion(
        repo_root=ROOT,
        policy_path=POLICY,
        observed_manifests=(),
        output_root=tmp_path,
    )
    second = expansion.run_tranche_expansion(
        repo_root=ROOT,
        policy_path=POLICY,
        observed_manifests=(),
        output_root=tmp_path,
    )
    assert first.decision == second.decision
    assert first.decision_path.read_bytes() == second.decision_path.read_bytes()
    assert first.decision.action is expansion.DecisionAction.COLLECT_NEXT_TRANCHE
    assert first.decision.next_tranche is not None
    assert first.decision.next_tranche.tranche_id == "algebra_s0"
    assert first.frame_path is None
    assert b"Semantic labels inspected: `false`" in first.report_path.read_bytes()
