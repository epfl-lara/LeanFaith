from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from leanfaith.config.hashing import (
    canonical_json_bytes,
    hash_canonical,
    hash_file,
    sha256_hex,
)
from leanfaith.evaluation.prevalence import estimate_prevalence_from_files
from leanfaith.generation import frame_freeze_v3 as v3
from leanfaith.generation import tranche_expansion as v1
from leanfaith.generation import tranche_expansion_v2 as v2

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "configs/generation/lf021_frame_freeze_v3.yaml"
PREVALENCE_POLICY = ROOT / "policies/lf021_prevalence_design_v2.yaml"
CURRENT_COLLECT_DECISION = (
    ROOT / "reports/generation/lf021_tranche_expansion_v2/decisions/"
    "b0bb67b848e551ea13dfdb16187fb66559863dddff349adc1ac2984dd9d80a6a.json"
)


def _binding(index: int) -> v1.ArtifactBinding:
    return v1.ArtifactBinding(
        artifact=f"synthetic/{index}.json",
        sha256=f"{index + 1:064x}",
    )


def _member(
    index: int,
    *,
    problem: str,
    group: str,
    family: str,
    proxy: str,
) -> tuple[v1._CandidateMember, str]:
    return (
        v1._CandidateMember(
            invocation_id=f"invocation:{index:064x}",
            family_id=family,
            pool_id="algebra_gate3_docstrings_v1",
            source_proxy=proxy,
            problem_record_id=problem,
            alpha_identity_fingerprint=f"{index % 3 + 1:064x}",
            postprocess_manifest_id=f"manifest:{index:064x}",
            terminal_artifact=_binding(index * 3),
            screening_artifact=_binding(index * 3 + 1),
            representation_artifact=_binding(index * 3 + 2),
        ),
        group,
    )


def _cluster(
    *,
    alpha: str,
    group: str,
    members: tuple[v1._CandidateMember, ...],
) -> v2._ProblemAwareCluster:
    cluster_id = "candidate_cluster_v2:" + hash_canonical(
        {
            "schema": "lf021_problem_group_alpha_cluster_v2",
            "problem_group": group,
            "alpha_identity_fingerprint": alpha,
        }
    )
    return v2._ProblemAwareCluster(
        cluster_id=cluster_id,
        problem_group=group,
        alpha_identity_fingerprint=alpha,
        representative=members[0],
        members=members,
    )


def _verified_stop(
    tmp_path: Path,
    *,
    reverse: bool = False,
) -> v3.VerifiedV2Stop:
    families = (
        "goedel_formalizer_v2_8b",
        "kimina_autoformalizer_7b",
        "stepfun_formalizer_7b",
    )
    raw = [
        _member(
            10,
            problem="problem:a",
            group="nl:a",
            family=families[0],
            proxy="Algebra/Group",
        ),
        _member(
            11,
            problem="problem:a-alias",
            group="nl:a",
            family=families[1],
            proxy="Algebra/Group",
        ),
        _member(
            12,
            problem="problem:b",
            group="nl:b",
            family=families[0],
            proxy="Algebra/Group",
        ),
        _member(
            13,
            problem="problem:c",
            group="nl:c",
            family=families[0],
            proxy="Algebra/Group",
        ),
        _member(
            14,
            problem="problem:d",
            group="nl:d",
            family=families[0],
            proxy="Algebra/Group",
        ),
    ]
    problem_groups = {member.problem_record_id: group for member, group in raw}
    first = raw[0][0]
    second = raw[1][0]
    shared_alpha = "a" * 64
    first = replace(first, alpha_identity_fingerprint=shared_alpha)
    second = replace(second, alpha_identity_fingerprint=shared_alpha)
    problem_groups[first.problem_record_id] = "nl:a"
    problem_groups[second.problem_record_id] = "nl:a"
    remaining = [item[0] for item in raw[2:]]
    clusters = (
        _cluster(alpha=shared_alpha, group="nl:a", members=(first, second)),
        *(
            _cluster(
                alpha=item.alpha_identity_fingerprint,
                group=problem_groups[item.problem_record_id],
                members=(item,),
            )
            for item in remaining
        ),
    )
    if reverse:
        clusters = tuple(reversed(clusters))

    base = v1.load_tranche_expansion_policy(
        ROOT / "configs/generation/lf021_tranche_expansion_v1.yaml"
    ).config
    frame = base.frame.model_copy(
        update={
            "minimum_size": 1,
            "preferred_size": 2,
            "maximum_size": 4,
        }
    )
    base = base.model_copy(update={"frame": frame})
    decision_path = tmp_path / "synthetic_v2_stop.json"
    if not decision_path.exists():
        decision_path.write_bytes(b"{}")
    decision_binding = v1.ArtifactBinding(
        artifact=str(decision_path),
        sha256=hash_file(decision_path),
    )
    counts = v1.OperationalCounts(
        observed_tranche_count=1,
        total_invocations=5,
        raw_collected_count=5,
        parser_success_count=5,
        compile_success_count=5,
        benchmark_rejected_count=0,
        benchmark_clear_compile_count=5,
        duplicate_member_count=1,
        unique_compiling_count=4,
        unique_contribution_by_family={
            "goedel_formalizer_v2_8b": 4,
            "kimina_autoformalizer_7b": 1,
            "stepfun_formalizer_7b": 0,
        },
        unique_representative_by_family={
            "goedel_formalizer_v2_8b": 4,
            "kimina_autoformalizer_7b": 0,
            "stepfun_formalizer_7b": 0,
        },
        unique_contribution_by_pool={"algebra_gate3_docstrings_v1": 4},
        unique_representative_by_pool={"algebra_gate3_docstrings_v1": 4},
        unique_contribution_by_family_pool={
            "goedel_formalizer_v2_8b|algebra_gate3_docstrings_v1": 4,
            "kimina_autoformalizer_7b|algebra_gate3_docstrings_v1": 1,
            "stepfun_formalizer_7b|algebra_gate3_docstrings_v1": 0,
        },
        unique_contribution_by_source_proxy={"Algebra/Group": 4},
    )
    fixed_frame = SimpleNamespace(
        item_count=2,
        sampling_method="problem_aware_stratified_hash_srs_without_replacement_v2",
    )
    decision = SimpleNamespace(
        decision_id="lf021_expansion_decision_v2:" + "1" * 64,
        action=v1.DecisionAction.FREEZE_PREFERRED_FRAME,
        observations=(),
        counts=counts,
        coverage_deficits=(),
        next_tranche=None,
        frame=fixed_frame,
    )
    return v3.VerifiedV2Stop(
        decision=cast(v2.ExpansionDecisionV2, decision),
        decision_binding=decision_binding,
        base_policy=base,
        clusters=cast(tuple[v2._ProblemAwareCluster, ...], clusters),
        problem_groups=problem_groups,
    )


def _verified_stop_all_families_census(tmp_path: Path) -> v3.VerifiedV2Stop:
    """Build a four-unit census containing every frozen prevalence family."""

    verified = _verified_stop(tmp_path)
    last_cluster = verified.clusters[-1]
    stepfun_member = replace(
        last_cluster.members[0],
        family_id="stepfun_formalizer_7b",
    )
    clusters = (
        *verified.clusters[:-1],
        _cluster(
            alpha=last_cluster.alpha_identity_fingerprint,
            group=last_cluster.problem_group,
            members=(stepfun_member,),
        ),
    )
    frame_policy = verified.base_policy.frame.model_copy(
        update={
            "minimum_size": 1,
            "preferred_size": 4,
            "maximum_size": 4,
        }
    )
    base_policy = verified.base_policy.model_copy(update={"frame": frame_policy})
    counts = verified.decision.counts.model_copy(
        update={
            "unique_contribution_by_family": {
                "goedel_formalizer_v2_8b": 3,
                "kimina_autoformalizer_7b": 1,
                "stepfun_formalizer_7b": 1,
            },
            "unique_representative_by_family": {
                "goedel_formalizer_v2_8b": 3,
                "kimina_autoformalizer_7b": 0,
                "stepfun_formalizer_7b": 1,
            },
            "unique_contribution_by_family_pool": {
                "goedel_formalizer_v2_8b|algebra_gate3_docstrings_v1": 3,
                "kimina_autoformalizer_7b|algebra_gate3_docstrings_v1": 1,
                "stepfun_formalizer_7b|algebra_gate3_docstrings_v1": 1,
            },
        }
    )
    decision = SimpleNamespace(
        decision_id=verified.decision.decision_id,
        action=verified.decision.action,
        observations=verified.decision.observations,
        counts=counts,
        coverage_deficits=verified.decision.coverage_deficits,
        next_tranche=None,
        frame=SimpleNamespace(
            item_count=4,
            sampling_method=("problem_aware_stratified_hash_srs_without_replacement_v2"),
        ),
    )
    return v3.VerifiedV2Stop(
        decision=cast(v2.ExpansionDecisionV2, decision),
        decision_binding=verified.decision_binding,
        base_policy=base_policy,
        clusters=cast(tuple[v2._ProblemAwareCluster, ...], clusters),
        problem_groups=verified.problem_groups,
    )


def _rehash_population(raw: dict[str, Any]) -> dict[str, Any]:
    raw["population_record_id"] = "lf021_eligible_population_item_v3:" + hash_canonical(
        {
            "schema": "lf021_eligible_population_item_v3",
            **{key: value for key, value in raw.items() if key != "population_record_id"},
        }
    )
    return raw


def _rehash_frame(raw: dict[str, Any]) -> dict[str, Any]:
    raw["frame_record_id"] = "lf021_prevalence_item_v3:" + hash_canonical(
        {
            "schema": "lf021_prevalence_frame_item_v3",
            **{key: value for key, value in raw.items() if key != "frame_record_id"},
        }
    )
    return raw


def test_v3_policy_binds_immutable_v2_and_forbids_fixed_salt() -> None:
    loaded = v3.load_frame_freeze_policy_v3(POLICY)
    assert loaded.config.base_v2_policy.sha256 == hash_file(
        ROOT / loaded.config.base_v2_policy.artifact
    )
    assert loaded.config.base_v2_implementation.sha256 == hash_file(
        ROOT / loaded.config.base_v2_implementation.artifact
    )
    assert loaded.config.sampling_method == (
        "problem_aware_stratified_csprng_srs_without_replacement_v2"
    )
    assert loaded.config.sampling_seed_bytes == 32
    assert not loaded.config.fixed_salt_v2_frame_eligible


def test_hmac_rank_known_vector_locks_message_encoding() -> None:
    assert (
        v3._rank_digest(
            seed=bytes(range(32)),
            domain_separator="leanfaith-lf021-frame-v3-hmac-sha256-v1",
            sampling_stratum=("goedel_formalizer_v2_8b|algebra_gate3_docstrings_v1|Algebra/Group"),
            cluster_id="candidate_cluster_v2:" + "1" * 64,
        )
        == "1e8898f7b78076c06f8a3f761bdf601ee6473872d5d22f128f4a1577e6ce1b71"
    )


def test_v3_rejects_current_collect_next_v2_decision() -> None:
    with pytest.raises(v3.FrameFreezeV3Error, match="only when v2 would stop"):
        v3.load_verified_v2_stop(
            repo_root=ROOT,
            loaded_policy=v3.load_frame_freeze_policy_v3(POLICY),
            decision_path=CURRENT_COLLECT_DECISION,
        )


def test_population_coherence_rejects_rehashed_internal_tampering(
    tmp_path: Path,
) -> None:
    item = v3.build_eligible_population_items_v3(_verified_stop(tmp_path))[0]

    representative_tamper = item.model_dump(mode="json")
    representative_tamper["representative_family_id"] = "not_a_contributor"
    with pytest.raises(ValueError, match="representative fields"):
        v3.EligiblePopulationItemV3.model_validate(_rehash_population(representative_tamper))

    cluster_tamper = item.model_dump(mode="json")
    cluster_tamper["cluster_id"] = "candidate_cluster_v2:" + "f" * 64
    with pytest.raises(ValueError, match="cluster ID"):
        v3.EligiblePopulationItemV3.model_validate(_rehash_population(cluster_tamper))

    multiplicity_tamper = item.model_dump(mode="json")
    multiplicity_tamper["member_count_by_family"] = {"goedel_formalizer_v2_8b": item.member_count}
    with pytest.raises(ValueError, match="family multiplicities"):
        v3.EligiblePopulationItemV3.model_validate(_rehash_population(multiplicity_tamper))


def test_fixed_salt_v2_frame_fails_closed(tmp_path: Path) -> None:
    verified = _verified_stop(tmp_path)
    member = verified.clusters[0].members[0]
    cluster = v2._ProblemAwareCluster(
        cluster_id=verified.clusters[0].cluster_id,
        problem_group=verified.clusters[0].problem_group,
        alpha_identity_fingerprint=verified.clusters[0].alpha_identity_fingerprint,
        representative=member,
        members=(member,),
    )
    amendment = v2.load_amendment_v2(
        ROOT / "configs/generation/lf021_tranche_expansion_v2.yaml"
    ).config
    row = v2._build_frame_items(
        (cluster,),
        target=1,
        base_policy=verified.base_policy,
        amendment=amendment,
    )[0]
    path = tmp_path / "fixed_salt_v2.jsonl"
    path.write_bytes(canonical_json_bytes(row.model_dump(mode="json")) + b"\n")
    with pytest.raises(ValueError):
        v3.load_frame_items_v3(path)


def test_population_seed_frame_replay_order_and_hmac(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verified = _verified_stop(tmp_path)
    monkeypatch.setattr(v3, "load_verified_v2_stop", lambda **_kwargs: verified)
    output = tmp_path / "out"
    population = v3.freeze_eligible_population_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        output_root=output,
        frozen_at="2026-07-24T04:00:00Z",
    )
    assert population.manifest.population_item_count == 4
    assert population.manifest.population_member_count == 5
    assert sum(item.member_count - 1 for item in population.items) == 1

    seed = bytes(range(32))
    seed_run = v3.archive_sampling_seed_v3(
        repo_root=ROOT,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2026-07-24T04:00:01Z",
        seed_bytes=seed,
        test_replay_only=True,
    )
    assert seed_run.provenance.sampling_seed_sha256 == sha256_hex(seed)
    assert seed_run.provenance.source == "test_replay_seed_256"
    assert seed_run.provenance.test_replay_only
    frame = v3.freeze_frame_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        population_manifest_path=population.manifest_path,
        seed_provenance_path=seed_run.provenance_path,
        output_root=output,
        allow_test_replay=True,
    )
    assert frame.decision.sampling_method == (
        "problem_aware_stratified_csprng_srs_without_replacement_v2"
    )
    assert frame.decision.sampling_seed_sha256 == sha256_hex(seed)
    assert frame.decision.frame.item_count == 2
    assert not frame.decision.v2_fixed_salt_frame_reused
    verified_frame = v3.verify_frame_freeze_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        decision_path=frame.decision_path,
    )
    assert verified_frame.frame_items == frame.items
    assert verified_frame.seed_bytes == seed
    assert verified_frame.decision.test_replay_only
    for item in frame.items:
        assert item.sampling_rank_digest == v3._rank_digest(
            seed=seed,
            domain_separator=frame.decision.sampling_domain_separator,
            sampling_stratum=item.sampling_stratum,
            cluster_id=item.cluster_id,
        )

    before = {
        path: path.read_bytes()
        for path in (
            population.population_path,
            population.manifest_path,
            seed_run.seed_path,
            seed_run.provenance_path,
            seed_run.lock_path,
            frame.frame_path,
            frame.decision_path,
        )
    }
    reversed_verified = _verified_stop(tmp_path, reverse=True)
    monkeypatch.setattr(
        v3,
        "load_verified_v2_stop",
        lambda **_kwargs: reversed_verified,
    )
    population_replay = v3.freeze_eligible_population_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        output_root=output,
        frozen_at="2026-07-24T04:00:00Z",
    )
    seed_replay = v3.archive_sampling_seed_v3(
        repo_root=ROOT,
        population_manifest_path=population_replay.manifest_path,
        output_root=output,
        generated_at="2026-07-24T04:00:01Z",
    )
    frame_replay = v3.freeze_frame_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        population_manifest_path=population_replay.manifest_path,
        seed_provenance_path=seed_replay.provenance_path,
        output_root=output,
        allow_test_replay=True,
    )
    assert population_replay.manifest == population.manifest
    assert seed_replay.provenance == seed_run.provenance
    assert frame_replay.decision == frame.decision
    assert all(path.read_bytes() == payload for path, payload in before.items())


def test_prevalence_estimator_replays_complete_production_v3_lineage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verified_stop = _verified_stop_all_families_census(tmp_path)
    monkeypatch.setattr(
        v3,
        "load_verified_v2_stop",
        lambda **_kwargs: verified_stop,
    )
    output = tmp_path / "out"
    population = v3.freeze_eligible_population_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        output_root=output,
        frozen_at="2026-07-24T04:00:00Z",
    )
    monkeypatch.setattr(v3.secrets, "token_bytes", lambda count: b"p" * count)
    seed = v3.archive_sampling_seed_v3(
        repo_root=ROOT,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2026-07-24T04:00:01Z",
    )
    frame = v3.freeze_frame_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        population_manifest_path=population.manifest_path,
        seed_provenance_path=seed.provenance_path,
        output_root=output,
    )
    outcomes = ("same_claim", "not_same_claim", "ambiguous", "same_claim")
    adjudication_path = tmp_path / "adjudications.jsonl"
    adjudication_path.write_bytes(
        b"".join(
            canonical_json_bytes(
                {
                    "schema_version": 1,
                    "adjudication_id": (f"synthetic-adjudication:{item.frame_record_id}"),
                    "frame_record_id": item.frame_record_id,
                    "resolution_outcome": outcome,
                    "terminal": True,
                }
            )
            + b"\n"
            for item, outcome in zip(frame.items, outcomes, strict=True)
        )
    )

    report = estimate_prevalence_from_files(
        repo_root=ROOT,
        frame_decision_path=frame.decision_path,
        adjudication_path=adjudication_path,
        policy_path=PREVALENCE_POLICY,
        frame_freeze_policy_path=POLICY,
    )

    assert report.schema_version == 2
    assert report.input_binding.frame_id == frame.decision.frame.frame_id
    assert report.input_binding.population_id == population.manifest.population_id
    assert report.input_binding.sampling_seed_sha256 == seed.provenance.sampling_seed_sha256
    assert not report.input_binding.test_replay_only
    assert set(report.per_family_retained_invocation) == {
        "goedel_formalizer_v2_8b",
        "kimina_autoformalizer_7b",
        "stepfun_formalizer_7b",
    }


def test_seed_is_single_population_bound_draw(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verified = _verified_stop(tmp_path)
    monkeypatch.setattr(v3, "load_verified_v2_stop", lambda **_kwargs: verified)
    output = tmp_path / "out"
    population = v3.freeze_eligible_population_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        output_root=output,
        frozen_at="2026-07-24T04:00:00Z",
    )
    with pytest.raises(v3.FrameFreezeV3Error, match="test/replay-only"):
        v3.archive_sampling_seed_v3(
            repo_root=ROOT,
            population_manifest_path=population.manifest_path,
            output_root=output,
            generated_at="2026-07-24T04:00:01Z",
            seed_bytes=b"a" * 32,
        )
    first = v3.archive_sampling_seed_v3(
        repo_root=ROOT,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2026-07-24T04:00:01Z",
        seed_bytes=b"a" * 32,
        test_replay_only=True,
    )
    replay = v3.archive_sampling_seed_v3(
        repo_root=ROOT,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2030-01-01T00:00:00Z",
    )
    assert replay.provenance == first.provenance
    with pytest.raises(v3.FrameFreezeV3Error, match="different frozen seed"):
        v3.archive_sampling_seed_v3(
            repo_root=ROOT,
            population_manifest_path=population.manifest_path,
            output_root=output,
            generated_at="2026-07-24T04:00:02Z",
            seed_bytes=b"b" * 32,
            test_replay_only=True,
        )


def test_frame_freeze_cli_rejects_generic_caller_seed_path(
    tmp_path: Path,
) -> None:
    seed_path = tmp_path / "seed.bin"
    seed_path.write_bytes(b"s" * 32)

    result = subprocess.run(
        (
            sys.executable,
            str(ROOT / "scripts/30_freeze_lf021_frame_v3.py"),
            "archive-seed",
            "--seed-file",
            str(seed_path),
        ),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert "unrecognized arguments: --seed-file" in result.stderr


def test_production_seed_sources_are_explicitly_provenanced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verified = _verified_stop(tmp_path)
    monkeypatch.setattr(v3, "load_verified_v2_stop", lambda **_kwargs: verified)

    os_output = tmp_path / "os"
    population = v3.freeze_eligible_population_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        output_root=os_output,
        frozen_at="2026-07-24T04:00:00Z",
    )
    monkeypatch.setattr(v3.secrets, "token_bytes", lambda count: b"o" * count)
    os_seed = v3.archive_sampling_seed_v3(
        repo_root=ROOT,
        population_manifest_path=population.manifest_path,
        output_root=os_output,
        generated_at="2026-07-24T04:00:01Z",
    )
    assert os_seed.provenance.source == "os_csprng_secrets_token_bytes_256"
    assert not os_seed.provenance.test_replay_only
    assert os_seed.provenance.external_beacon_provenance is None
    os_frame = v3.freeze_frame_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        population_manifest_path=population.manifest_path,
        seed_provenance_path=os_seed.provenance_path,
        output_root=os_output,
    )
    assert not os_frame.decision.test_replay_only
    assert not v3.verify_frame_freeze_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        decision_path=os_frame.decision_path,
    ).decision.test_replay_only

    beacon_output = tmp_path / "beacon"
    beacon_population = v3.freeze_eligible_population_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        output_root=beacon_output,
        frozen_at="2026-07-24T04:00:00Z",
    )
    beacon_provenance = tmp_path / "beacon_provenance.json"
    beacon_provenance.write_bytes(
        canonical_json_bytes(
            {
                "schema_version": 3,
                "record_kind": "lf021_external_randomness_beacon_v3",
                "beacon_id": "synthetic-test-beacon",
                "source_reference": "fixture://beacon",
                "obtained_at": "2026-07-24T04:00:00Z",
                "sampling_seed_sha256": sha256_hex(b"b" * 32),
            }
        )
    )
    beacon_seed = v3.archive_sampling_seed_v3(
        repo_root=ROOT,
        population_manifest_path=beacon_population.manifest_path,
        output_root=beacon_output,
        generated_at="2026-07-24T04:00:01Z",
        seed_bytes=b"b" * 32,
        external_beacon_provenance_path=beacon_provenance,
    )
    assert beacon_seed.provenance.source == "external_randomness_beacon_256"
    assert not beacon_seed.provenance.test_replay_only
    assert beacon_seed.provenance.external_beacon_provenance == v1.ArtifactBinding(
        artifact=str(beacon_provenance),
        sha256=hash_file(beacon_provenance),
    )


@pytest.mark.parametrize("seed_byte", range(8))
def test_hmac_selection_properties_across_test_seeds(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    seed_byte: int,
) -> None:
    verified = _verified_stop(tmp_path)
    monkeypatch.setattr(v3, "load_verified_v2_stop", lambda **_kwargs: verified)
    output = tmp_path / f"seed_{seed_byte}"
    population = v3.freeze_eligible_population_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        output_root=output,
        frozen_at="2026-07-24T04:00:00Z",
    )
    raw_seed = bytes([seed_byte]) * 32
    seed = v3.archive_sampling_seed_v3(
        repo_root=ROOT,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2026-07-24T04:00:01Z",
        seed_bytes=raw_seed,
        test_replay_only=True,
    )
    frame = v3.freeze_frame_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        population_manifest_path=population.manifest_path,
        seed_provenance_path=seed.provenance_path,
        output_root=output,
        allow_test_replay=True,
    )

    assert len(frame.items) == 2
    assert len({item.population_record_id for item in frame.items}) == len(frame.items)
    by_stratum = {
        stratum: tuple(
            sorted(
                (
                    v3._rank_digest(
                        seed=raw_seed,
                        domain_separator=frame.decision.sampling_domain_separator,
                        sampling_stratum=stratum,
                        cluster_id=item.cluster_id,
                    ),
                    item.population_record_id,
                )
                for item in population.items
                if item.sampling_stratum == stratum
            )
        )
        for stratum in frame.decision.stratum_population_sizes
    }
    for stratum, expected_ranked in by_stratum.items():
        selected_ids = {
            item.population_record_id for item in frame.items if item.sampling_stratum == stratum
        }
        expected_ids = {
            population_record_id
            for _, population_record_id in expected_ranked[
                : frame.decision.stratum_sample_sizes[stratum]
            ]
        }
        assert selected_ids == expected_ids


def test_frame_propensity_and_seed_binding_tamper_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    verified = _verified_stop(tmp_path)
    monkeypatch.setattr(v3, "load_verified_v2_stop", lambda **_kwargs: verified)
    output = tmp_path / "out"
    population = v3.freeze_eligible_population_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        output_root=output,
        frozen_at="2026-07-24T04:00:00Z",
    )
    seed = v3.archive_sampling_seed_v3(
        repo_root=ROOT,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2026-07-24T04:00:01Z",
        seed_bytes=b"c" * 32,
        test_replay_only=True,
    )
    frame = v3.freeze_frame_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        v2_decision_path=tmp_path / "synthetic_v2_stop.json",
        population_manifest_path=population.manifest_path,
        seed_provenance_path=seed.provenance_path,
        output_root=output,
        allow_test_replay=True,
    )
    raw = frame.items[0].model_dump(mode="json")
    raw["inclusion_probability_numerator"] += 1
    with pytest.raises(ValueError, match="inclusion probability"):
        v3.FrameItemV3.model_validate(_rehash_frame(raw))

    seed.seed_path.write_bytes(b"d" * 32)
    with pytest.raises(v3.FrameFreezeV3Error, match="bound artifact differs"):
        v3.freeze_frame_v3(
            repo_root=ROOT,
            policy_path=POLICY,
            v2_decision_path=tmp_path / "synthetic_v2_stop.json",
            population_manifest_path=population.manifest_path,
            seed_provenance_path=seed.provenance_path,
            output_root=output,
            allow_test_replay=True,
        )
