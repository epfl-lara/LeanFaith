from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_canonical, hash_file
from leanfaith.generation import frame_freeze_v3 as frame_v3
from leanfaith.generation import post_exhaustion_extension as extension
from leanfaith.generation import post_exhaustion_frame_v1 as subject
from leanfaith.generation import tranche_expansion as v1
from leanfaith.generation import tranche_expansion_v2 as v2

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "configs/generation/lf021_post_exhaustion_frame_v1.yaml"
FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)


def _artifact(path: Path) -> v1.ArtifactBinding:
    return v1.ArtifactBinding(artifact=str(path), sha256=hash_file(path))


def _dummy_artifact(index: int) -> v1.ArtifactBinding:
    return v1.ArtifactBinding(
        artifact=f"synthetic/{index}.json",
        sha256=f"{index + 1:064x}",
    )


def _observation(index: int, path: Path) -> v1.ObservationBinding:
    path.write_bytes(f"observation-{index}".encode())
    return v1.ObservationBinding(
        tranche_id=(f"{'algebra' if index % 2 == 0 else 'cross_domain'}_s{index // 2}"),
        postprocess_manifest=_artifact(path),
        manifest_id=f"research_postprocess_v6_manifest:{index + 1:064x}",
        postprocess_schema_version=6,
        input_binding_hash=f"{index + 1:064x}",
    )


def _population_item(index: int) -> frame_v3.EligiblePopulationItemV3:
    family = FAMILIES[index % len(FAMILIES)]
    pool = "algebra_gate3_docstrings_v1" if index % 2 == 0 else "cross_domain_docstrings_v1"
    proxy = ("Algebra/Group", "Topology", "NumberTheory")[index % 3]
    group = f"nl-problem:{index:064x}"
    alpha = f"{index + 1:064x}"
    problem = f"problem:{index:064x}"
    member = v1._CandidateMember(
        invocation_id=f"invocation:{index:064x}",
        family_id=family,
        pool_id=pool,
        source_proxy=proxy,
        problem_record_id=problem,
        alpha_identity_fingerprint=alpha,
        postprocess_manifest_id=f"research_postprocess_v6_manifest:{index + 1:064x}",
        terminal_artifact=_dummy_artifact(index * 3),
        screening_artifact=_dummy_artifact(index * 3 + 1),
        representation_artifact=_dummy_artifact(index * 3 + 2),
    )
    cluster_id = "candidate_cluster_v2:" + hash_canonical(
        {
            "schema": "lf021_problem_group_alpha_cluster_v2",
            "problem_group": group,
            "alpha_identity_fingerprint": alpha,
        }
    )
    cluster = v2._ProblemAwareCluster(
        cluster_id=cluster_id,
        problem_group=group,
        alpha_identity_fingerprint=alpha,
        representative=member,
        members=(member,),
    )
    return frame_v3._population_item(cluster, problem_groups={problem: group})


def _counts(item_count: int, observation_count: int) -> v1.OperationalCounts:
    representatives = {
        family: sum(1 for index in range(item_count) if FAMILIES[index % len(FAMILIES)] == family)
        for family in FAMILIES
    }
    return v1.OperationalCounts(
        observed_tranche_count=observation_count,
        total_invocations=item_count,
        raw_collected_count=item_count,
        parser_success_count=item_count,
        compile_success_count=item_count,
        benchmark_rejected_count=0,
        benchmark_clear_compile_count=item_count,
        duplicate_member_count=0,
        unique_compiling_count=item_count,
        unique_contribution_by_family=dict(sorted(representatives.items())),
        unique_representative_by_family=dict(sorted(representatives.items())),
        unique_contribution_by_pool={
            "algebra_gate3_docstrings_v1": (item_count + 1) // 2,
            "cross_domain_docstrings_v1": item_count // 2,
        },
        unique_representative_by_pool={
            "algebra_gate3_docstrings_v1": (item_count + 1) // 2,
            "cross_domain_docstrings_v1": item_count // 2,
        },
        unique_contribution_by_family_pool=dict(
            sorted(
                (
                    {f"{family}|algebra_gate3_docstrings_v1": 0 for family in FAMILIES}
                    | {f"{family}|cross_domain_docstrings_v1": 0 for family in FAMILIES}
                ).items()
            )
        ),
        unique_contribution_by_source_proxy={
            "Algebra/Group": (item_count + 2) // 3,
            "NumberTheory": item_count // 3,
            "Topology": (item_count + 1) // 3,
        },
    )


def _handoff_projection(
    *,
    decision_id: str,
    decision_binding: v1.ArtifactBinding,
    items: tuple[frame_v3.EligiblePopulationItemV3, ...],
) -> extension.ExtendedPopulationHandoffProjectionV1:
    loaded = subject.load_post_exhaustion_frame_policy_v1(POLICY).config
    population_bytes = b"".join(
        canonical_json_bytes(item.model_dump(mode="json")) + b"\n" for item in items
    )
    sizes: dict[str, int] = {}
    for item in items:
        sizes[item.sampling_stratum] = sizes.get(item.sampling_stratum, 0) + 1
    payload: dict[str, Any] = {
        "schema_version": 1,
        "projection_kind": "lf021_post_exhaustion_population_handoff_v1",
        "extension_decision_id": decision_id,
        "extension_decision": decision_binding.model_dump(mode="json"),
        "extension_policy": loaded.extension_policy.model_dump(mode="json"),
        "extension_implementation": loaded.extension_implementation.model_dump(mode="json"),
        "frame_v3_policy": loaded.frame_v3_policy.model_dump(mode="json"),
        "frame_v3_implementation": loaded.frame_v3_implementation.model_dump(mode="json"),
        "population_record_schema": "EligiblePopulationItemV3",
        "population_unit": ("problem_group", "alpha_identity_fingerprint"),
        "population_item_count": len(items),
        "population_member_count": sum(item.member_count for item in items),
        "population_items_sha256": subject.sha256_hex(population_bytes),
        "stratum_population_sizes": dict(sorted(sizes.items())),
        "preferred_size": 240,
        "coverage_deficits": (),
        "direct_frozen_v3_decision_compatible": False,
        "required_consumer": "separately_reviewed_extended_population_materializer_v1",
        "frame_created": False,
        "sampling_seed_obtained": False,
        "semantic_labels_inspected": False,
        "semantic_labels_created": False,
        "supervision_eligible": False,
        "gate_5g_credit_claimed": False,
        "gate_5_closed": False,
    }
    projection_id = "lf021_post_exhaustion_population_handoff_v1:" + hash_canonical(
        {"schema": "lf021_post_exhaustion_population_handoff_v1", **payload}
    )
    return extension.ExtendedPopulationHandoffProjectionV1.model_validate(
        {"projection_id": projection_id, **payload}
    )


def _fake_replay(
    tmp_path: Path,
) -> tuple[Any, Any, tuple[Path, Path]]:
    items = tuple(
        sorted((_population_item(i) for i in range(245)), key=lambda x: x.population_record_id)
    )
    original_observations = tuple(
        _observation(index, tmp_path / f"original-{index}.json") for index in range(12)
    )
    extension_observations = tuple(
        _observation(index, tmp_path / f"extension-{index}.json") for index in range(12, 14)
    )
    activation_path = tmp_path / "activation.json"
    activation_path.write_bytes(b"activation")
    stop_path = tmp_path / "preferred-stop.json"
    stop_path.write_bytes(b"preferred-stop")
    decision_id = "lf021_post_exhaustion_extension_decision_v1:" + "a" * 64
    stop_binding = _artifact(stop_path)
    counts = _counts(len(items), 14)
    original_decision = SimpleNamespace(
        decision_id="lf021_expansion_decision_v2:" + "b" * 64,
        observations=original_observations,
    )
    original = SimpleNamespace(
        decision=original_decision,
        decision_binding=_artifact(activation_path),
    )
    stop_decision = SimpleNamespace(
        decision_id=decision_id,
        extension_observations=extension_observations,
        counts=counts,
    )
    verified = SimpleNamespace(
        decision=stop_decision,
        decision_binding=stop_binding,
        verified_original_exhaustion=original,
        population_items=items,
        handoff_projection=_handoff_projection(
            decision_id=decision_id,
            decision_binding=stop_binding,
            items=items,
        ),
    )
    authorization_paths: list[Path] = []
    records: list[Any] = []
    bindings: list[v1.ArtifactBinding] = []
    for index in range(2):
        path = tmp_path / f"authorization-{index}.json"
        path.write_bytes(f"authorization-{index}".encode())
        authorization_paths.append(path)
        bindings.append(_artifact(path))
        records.append(
            SimpleNamespace(
                authorization_id=(
                    "lf021_reviewed_extension_collection_authorization_v1:" + f"{index + 1:064x}"
                )
            )
        )
    authorizations = SimpleNamespace(
        records=tuple(records),
        bindings=tuple(bindings),
        postprocess_observations=extension_observations,
    )
    return verified, authorizations, cast(tuple[Path, Path], tuple(authorization_paths))


@pytest.fixture
def fake_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, Any, tuple[Path, Path]]:
    verified, authorizations, paths = _fake_replay(tmp_path)
    monkeypatch.setattr(subject, "_verify_policy_lineage", lambda **_: None)
    monkeypatch.setattr(
        subject,
        "_verified_stop_and_authorizations",
        lambda **_: (verified, authorizations),
    )
    return verified, authorizations, paths


def test_checked_in_policy_binds_distinct_lineage() -> None:
    loaded = subject.load_post_exhaustion_frame_policy_v1(POLICY)
    subject._verify_policy_lineage(repo_root=ROOT, loaded_policy=loaded)
    assert loaded.config.target_frame_size == 240
    assert loaded.config.required_extension_orders == (12, 13, 14, 15)
    assert loaded.config.required_scalable_family_ids == FAMILIES


def test_extended_population_seed_frame_round_trip(
    tmp_path: Path,
    fake_lineage: tuple[Any, Any, tuple[Path, Path]],
) -> None:
    _, _, authorization_paths = fake_lineage
    output = tmp_path / "output"
    population = subject.freeze_extended_eligible_population_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        extension_decision_path=tmp_path / "preferred-stop.json",
        collection_authorization_paths=authorization_paths,
        output_root=output,
        frozen_at="2026-07-24T00:00:00Z",
    )
    assert population.manifest.population_item_count == 245
    assert population.manifest.representative_family_ids == FAMILIES
    assert not population.manifest.semantic_labels_created

    seed = subject.archive_extended_sampling_seed_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2026-07-24T00:01:00Z",
        seed_bytes=bytes(range(32)),
        test_replay_only=True,
    )
    frame = subject.freeze_extended_frame_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        extension_decision_path=tmp_path / "preferred-stop.json",
        collection_authorization_paths=authorization_paths,
        population_manifest_path=population.manifest_path,
        seed_provenance_path=seed.provenance_path,
        output_root=output,
        allow_test_replay=True,
    )
    assert len(frame.items) == 240
    assert frame.decision.test_replay_only
    verified = subject.verify_extended_frame_freeze_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        decision_path=frame.decision_path,
    )
    assert verified.frame_items == frame.items
    assert verified.seed_bytes == bytes(range(32))


def test_production_freeze_rejects_test_entropy(
    tmp_path: Path,
    fake_lineage: tuple[Any, Any, tuple[Path, Path]],
) -> None:
    _, _, authorization_paths = fake_lineage
    output = tmp_path / "output"
    population = subject.freeze_extended_eligible_population_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        extension_decision_path=tmp_path / "preferred-stop.json",
        collection_authorization_paths=authorization_paths,
        output_root=output,
        frozen_at="2026-07-24T00:00:00Z",
    )
    seed = subject.archive_extended_sampling_seed_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2026-07-24T00:01:00Z",
        seed_bytes=b"x" * 32,
        test_replay_only=True,
    )
    with pytest.raises(subject.PostExhaustionFrameError, match="test/replay seed"):
        subject.freeze_extended_frame_v1(
            repo_root=tmp_path,
            policy_path=POLICY,
            extension_decision_path=tmp_path / "preferred-stop.json",
            collection_authorization_paths=authorization_paths,
            population_manifest_path=population.manifest_path,
            seed_provenance_path=seed.provenance_path,
            output_root=output,
        )


@pytest.mark.parametrize("length", [31, 33])
def test_seed_requires_exactly_256_bits(
    tmp_path: Path,
    fake_lineage: tuple[Any, Any, tuple[Path, Path]],
    length: int,
) -> None:
    _, _, authorization_paths = fake_lineage
    population = subject.freeze_extended_eligible_population_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        extension_decision_path=tmp_path / "preferred-stop.json",
        collection_authorization_paths=authorization_paths,
        output_root=tmp_path / "output",
        frozen_at="2026-07-24T00:00:00Z",
    )
    with pytest.raises(subject.PostExhaustionFrameError, match="32 bytes"):
        subject.archive_extended_sampling_seed_v1(
            repo_root=tmp_path,
            policy_path=POLICY,
            population_manifest_path=population.manifest_path,
            output_root=tmp_path / "output",
            generated_at="2026-07-24T00:01:00Z",
            seed_bytes=b"x" * length,
            test_replay_only=True,
        )


def test_caller_seed_is_never_a_production_source(
    tmp_path: Path,
    fake_lineage: tuple[Any, Any, tuple[Path, Path]],
) -> None:
    _, _, authorization_paths = fake_lineage
    population = subject.freeze_extended_eligible_population_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        extension_decision_path=tmp_path / "preferred-stop.json",
        collection_authorization_paths=authorization_paths,
        output_root=tmp_path / "output",
        frozen_at="2026-07-24T00:00:00Z",
    )
    with pytest.raises(subject.PostExhaustionFrameError, match="caller seed is test-only"):
        subject.archive_extended_sampling_seed_v1(
            repo_root=tmp_path,
            policy_path=POLICY,
            population_manifest_path=population.manifest_path,
            output_root=tmp_path / "output",
            generated_at="2026-07-24T00:01:00Z",
            seed_bytes=b"x" * 32,
        )


def test_population_seed_is_single_draw_and_cannot_be_replaced(
    tmp_path: Path,
    fake_lineage: tuple[Any, Any, tuple[Path, Path]],
) -> None:
    _, _, authorization_paths = fake_lineage
    output = tmp_path / "output"
    population = subject.freeze_extended_eligible_population_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        extension_decision_path=tmp_path / "preferred-stop.json",
        collection_authorization_paths=authorization_paths,
        output_root=output,
        frozen_at="2026-07-24T00:00:00Z",
    )
    first = subject.archive_extended_sampling_seed_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2026-07-24T00:01:00Z",
        seed_bytes=b"a" * 32,
        test_replay_only=True,
    )
    replay = subject.archive_extended_sampling_seed_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2026-07-24T00:02:00Z",
        seed_bytes=b"a" * 32,
        test_replay_only=True,
    )
    assert replay.provenance == first.provenance
    with pytest.raises(subject.PostExhaustionFrameError, match="different frozen seed"):
        subject.archive_extended_sampling_seed_v1(
            repo_root=tmp_path,
            policy_path=POLICY,
            population_manifest_path=population.manifest_path,
            output_root=output,
            generated_at="2026-07-24T00:03:00Z",
            seed_bytes=b"b" * 32,
            test_replay_only=True,
        )


def test_seed_reuse_across_original_registry_is_rejected(
    tmp_path: Path,
    fake_lineage: tuple[Any, Any, tuple[Path, Path]],
) -> None:
    _, _, authorization_paths = fake_lineage
    output = tmp_path / "output"
    population = subject.freeze_extended_eligible_population_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        extension_decision_path=tmp_path / "preferred-stop.json",
        collection_authorization_paths=authorization_paths,
        output_root=output,
        frozen_at="2026-07-24T00:00:00Z",
    )
    seed = b"r" * 32
    original_registry = (
        tmp_path
        / subject.load_post_exhaustion_frame_policy_v1(POLICY).config.original_seed_registry_root
    )
    original_registry.mkdir(parents=True)
    (original_registry / "prior.json").write_bytes(
        canonical_json_bytes(
            {
                "population_id": "prior",
                "sampling_seed_sha256": subject.sha256_hex(seed),
            }
        )
    )
    with pytest.raises(subject.PostExhaustionFrameError, match="already assigned"):
        subject.archive_extended_sampling_seed_v1(
            repo_root=tmp_path,
            policy_path=POLICY,
            population_manifest_path=population.manifest_path,
            output_root=output,
            generated_at="2026-07-24T00:01:00Z",
            seed_bytes=seed,
            test_replay_only=True,
        )


def test_noncanonical_json_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "not-canonical.json"
    path.write_text('{ "schema_version": 1 }\n', encoding="utf-8")
    with pytest.raises(subject.PostExhaustionFrameError, match="not canonical"):
        subject._strict_json(path)


def test_missing_representative_family_blocks_before_population_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verified, authorizations, paths = _fake_replay(tmp_path)
    reduced = tuple(
        item
        for item in verified.population_items
        if item.representative_family_id != "stepfun_formalizer_7b"
    )
    verified.population_items = reduced
    monkeypatch.setattr(
        subject,
        "_verified_stop_and_authorizations",
        lambda **_: (verified, authorizations),
    )
    monkeypatch.setattr(subject, "_verify_policy_lineage", lambda **_: None)
    with pytest.raises(subject.PostExhaustionFrameError, match="lacks all three"):
        subject.freeze_extended_eligible_population_v1(
            repo_root=tmp_path,
            policy_path=POLICY,
            extension_decision_path=tmp_path / "preferred-stop.json",
            collection_authorization_paths=paths,
            output_root=tmp_path / "output",
            frozen_at="2026-07-24T00:00:00Z",
        )
    assert not list((tmp_path / "output").rglob("*.bin"))


def test_old_v3_frame_item_parser_rejects_extended_ids(
    tmp_path: Path,
    fake_lineage: tuple[Any, Any, tuple[Path, Path]],
) -> None:
    _, _, authorization_paths = fake_lineage
    output = tmp_path / "output"
    population = subject.freeze_extended_eligible_population_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        extension_decision_path=tmp_path / "preferred-stop.json",
        collection_authorization_paths=authorization_paths,
        output_root=output,
        frozen_at="2026-07-24T00:00:00Z",
    )
    seed = subject.archive_extended_sampling_seed_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        population_manifest_path=population.manifest_path,
        output_root=output,
        generated_at="2026-07-24T00:01:00Z",
        seed_bytes=b"z" * 32,
        test_replay_only=True,
    )
    frame = subject.freeze_extended_frame_v1(
        repo_root=tmp_path,
        policy_path=POLICY,
        extension_decision_path=tmp_path / "preferred-stop.json",
        collection_authorization_paths=authorization_paths,
        population_manifest_path=population.manifest_path,
        seed_provenance_path=seed.provenance_path,
        output_root=output,
        allow_test_replay=True,
    )
    with pytest.raises(ValueError):
        frame_v3.FrameItemV3.model_validate(frame.items[0].model_dump(mode="json"))
