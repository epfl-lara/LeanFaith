from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from leanfaith.config.hashing import canonical_json_bytes, hash_file
from leanfaith.generation import post_exhaustion_extension as ext
from leanfaith.generation import tranche_expansion as v1

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "configs/generation/lf021_post_exhaustion_extension_v1.yaml"
CURRENT_SIX_TRANCHE_DECISION = (
    ROOT / "reports/generation/lf021_tranche_expansion_v2/decisions/"
    "4aea1d8756d14212350c75486d28d0e27a65f864edba3e7564537979876f0601.json"
)
FAMILIES = (
    "goedel_formalizer_v2_8b",
    "kimina_autoformalizer_7b",
    "stepfun_formalizer_7b",
)
POOLS = (
    "algebra_gate3_docstrings_v1",
    "cross_domain_docstrings_v1",
)
PROXIES = (
    "Algebra/AffineMonoid",
    "Algebra/Algebra",
    "Algebra/BigOperators",
    "Algebra/Category",
    "Algebra/Exact",
    "Algebra/Group",
    "Algebra/other",
    "Analysis",
    "Combinatorics",
    "Geometry",
    "NumberTheory",
    "Probability",
    "Topology",
)


def _artifact(path: Path) -> v1.ArtifactBinding:
    return v1.ArtifactBinding(artifact=str(path), sha256=hash_file(path))


def _member(
    index: int,
    *,
    family: str,
    pool: str,
    proxy: str,
    manifest_id: str,
) -> v1._CandidateMember:
    def binding(offset: int) -> v1.ArtifactBinding:
        return v1.ArtifactBinding(
            artifact=f"synthetic/{index}-{offset}.json",
            sha256=f"{index * 3 + offset + 1:064x}",
        )

    return v1._CandidateMember(
        invocation_id=f"invocation:{index:064x}",
        family_id=family,
        pool_id=pool,
        source_proxy=proxy,
        problem_record_id=f"problem:{index:064x}",
        alpha_identity_fingerprint=f"{index + 1:064x}",
        postprocess_manifest_id=manifest_id,
        terminal_artifact=binding(0),
        screening_artifact=binding(1),
        representation_artifact=binding(2),
    )


def _observation(
    *,
    tranche: v1.TrancheSpec,
    observation_index: int,
    first_member_index: int,
    member_count: int,
    manifest_path: Path,
    problem_groups: dict[str, str],
) -> v1.LoadedObservation:
    manifest_id = f"research_postprocess_v6_manifest:{observation_index + 1:064x}"
    members: list[v1._CandidateMember] = []
    for offset in range(member_count):
        index = first_member_index + offset
        family = FAMILIES[index % len(FAMILIES)]
        proxy = PROXIES[index % len(PROXIES)]
        members.append(
            _member(
                index,
                family=family,
                pool=tranche.pool_id,
                proxy=proxy,
                manifest_id=manifest_id,
            )
        )
        problem_groups[f"problem:{index:064x}"] = f"nl-problem:{index:064x}"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_bytes(f"manifest-{observation_index}".encode())
    binding = v1.ObservationBinding(
        tranche_id=tranche.tranche_id,
        postprocess_manifest=_artifact(manifest_path),
        manifest_id=manifest_id,
        postprocess_schema_version=6,
        input_binding_hash=f"{observation_index + 1:064x}",
    )
    terminals = tuple(
        SimpleNamespace(
            status="compiled",
            lean_validation_executed=True,
            screening_executed=True,
        )
        for _ in members
    )
    return cast(
        v1.LoadedObservation,
        SimpleNamespace(
            tranche=tranche,
            binding=binding,
            manifest=SimpleNamespace(expected_invocations=len(members)),
            terminals=terminals,
            candidates=tuple(members),
        ),
    )


def _synthetic_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    Any,
    Path,
    tuple[Path, ...],
    ext.VerifiedOriginalExhaustion,
]:
    loaded = ext.load_post_exhaustion_extension_policy(POLICY)
    base = v1.load_tranche_expansion_policy(ROOT / loaded.config.base_v1_policy.artifact).config
    problem_groups: dict[str, str] = {}
    original: list[v1.LoadedObservation] = []
    member_index = 0
    original_counts = (16,) * 10 + (15,) * 2
    for index, (tranche, count) in enumerate(zip(base.tranches, original_counts, strict=True)):
        original.append(
            _observation(
                tranche=tranche,
                observation_index=index,
                first_member_index=member_index,
                member_count=count,
                manifest_path=tmp_path / "original" / f"{index}.json",
                problem_groups=problem_groups,
            )
        )
        member_index += count
    assert member_index == 190

    extension_counts = (30, 25, 1, 1)
    extension_paths: list[Path] = []
    extension_by_path: dict[Path, v1.LoadedObservation] = {}
    for offset, (tranche, count) in enumerate(
        zip(
            loaded.config.extension_tranches,
            extension_counts,
            strict=True,
        )
    ):
        path = (tmp_path / "extension" / f"{offset}.json").resolve()
        observation = _observation(
            tranche=tranche,
            observation_index=12 + offset,
            first_member_index=member_index,
            member_count=count,
            manifest_path=path,
            problem_groups=problem_groups,
        )
        extension_paths.append(path)
        extension_by_path[path] = observation
        member_index += count

    activation_path = tmp_path / "activation.json"
    activation_path.write_bytes(b"synthetic activation")
    fake_decision = cast(
        Any,
        SimpleNamespace(
            decision_id="lf021_expansion_decision_v2:" + "a" * 64,
            action=v1.DecisionAction.FREEZE_REDUCED_FRAME,
        ),
    )
    verified = ext.VerifiedOriginalExhaustion(
        decision=fake_decision,
        decision_binding=_artifact(activation_path),
        base_policy=base,
        observations=tuple(original),
        problem_groups=problem_groups,
    )
    monkeypatch.setattr(
        ext,
        "load_verified_original_exhaustion",
        lambda **_: verified,
    )

    def fake_load(**kwargs: Any) -> v1.LoadedObservation:
        return extension_by_path[Path(kwargs["manifest_path"]).resolve()]

    monkeypatch.setattr(v1, "load_postprocess_observation", fake_load)
    return loaded, activation_path, tuple(extension_paths), verified


def test_policy_freezes_label_blind_sequence_and_lineage() -> None:
    loaded = ext.load_post_exhaustion_extension_policy(POLICY)
    policy = loaded.config
    assert policy.activation_actions == (
        "freeze_reduced_frame",
        "exhausted_without_frame",
    )
    assert [item.tranche_id for item in policy.extension_tranches] == [
        "algebra_s6",
        "cross_domain_s6",
        "algebra_s7",
        "cross_domain_s7",
    ]
    assert [
        tuple(item.seeds_by_family[family] for family in FAMILIES)
        for item in policy.extension_tranches
    ] == [(36, 6, 6), (36, 6, 6), (37, 7, 7), (37, 7, 7)]
    assert policy.base_v1_policy.sha256 == (
        "0c726e197ef5358ef4a7414e36622c10fb7ff19082199540b62e4bc170834e16"
    )
    assert policy.base_v2_policy.sha256 == (
        "ac16afa29eccbf072e88c3424eb9d717743710f8282d76bd8b217ff55d607b70"
    )
    assert policy.frame_v3_policy.sha256 == (
        "5514f7bbb7620413f48d9c087657d3ae89980a8cb7e2dd802fc3af5b6716cc8f"
    )
    assert not policy.frame_creation_enabled
    assert not policy.forecast_motivation.statistical_guarantee
    assert "same_claim" in policy.forbidden_inputs
    assert "human_label" in policy.forbidden_inputs


def test_current_six_tranche_decision_cannot_activate_extension() -> None:
    loaded = ext.load_post_exhaustion_extension_policy(POLICY)
    with pytest.raises(
        ext.PostExhaustionExtensionError,
        match="only after all 12 original tranches",
    ):
        ext.load_verified_original_exhaustion(
            repo_root=ROOT,
            loaded_policy=loaded,
            activation_v2_decision_path=CURRENT_SIX_TRANCHE_DECISION,
        )


def test_extension_stops_label_blind_at_preferred_eligibility(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, activation, extension_paths, _ = _synthetic_context(
        tmp_path,
        monkeypatch,
    )
    initial = ext.evaluate_post_exhaustion_extension(
        repo_root=ROOT,
        loaded_policy=loaded,
        activation_v2_decision_path=activation,
        extension_observed_manifests=(),
    )
    assert initial.action is ext.ExtensionDecisionAction.COLLECT_NEXT_EXTENSION_TRANCHE
    assert initial.next_tranche is not None
    assert initial.next_tranche.tranche_id == "algebra_s6"
    assert initial.counts.unique_compiling_count == 190

    after_one = ext.evaluate_post_exhaustion_extension(
        repo_root=ROOT,
        loaded_policy=loaded,
        activation_v2_decision_path=activation,
        extension_observed_manifests=extension_paths[:1],
    )
    assert after_one.counts.unique_compiling_count == 220
    assert after_one.next_tranche is not None
    assert after_one.next_tranche.tranche_id == "cross_domain_s6"

    after_two = ext.evaluate_post_exhaustion_extension(
        repo_root=ROOT,
        loaded_policy=loaded,
        activation_v2_decision_path=activation,
        extension_observed_manifests=extension_paths[:2],
    )
    assert after_two.counts.unique_compiling_count == 245
    assert after_two.action is ext.ExtensionDecisionAction.PREFERRED_ELIGIBLE_STOP
    assert after_two.next_tranche is None
    assert after_two.frame is None
    assert after_two.frame_freeze_handoff_required
    assert not after_two.semantic_labels_inspected
    assert not after_two.semantic_labels_created


def test_strict_handoff_replays_v3_population_rows_without_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, activation, extension_paths, _ = _synthetic_context(
        tmp_path,
        monkeypatch,
    )
    decision = ext.evaluate_post_exhaustion_extension(
        repo_root=ROOT,
        loaded_policy=loaded,
        activation_v2_decision_path=activation,
        extension_observed_manifests=extension_paths[:2],
    )
    decision_path = tmp_path / "extended-stop.json"
    decision_path.write_bytes(canonical_json_bytes(decision.model_dump(mode="json")))
    verified = ext.verify_extended_stop_for_frame_v3(
        repo_root=ROOT,
        policy_path=POLICY,
        decision_path=decision_path,
    )
    projection = verified.handoff_projection
    assert projection.population_item_count == 245
    assert len(verified.population_items) == 245
    assert projection.population_member_count == 245
    assert not projection.direct_frozen_v3_decision_compatible
    assert projection.required_consumer == (
        "separately_reviewed_extended_population_materializer_v1"
    )
    assert not projection.frame_created
    assert not projection.sampling_seed_obtained
    assert not list(tmp_path.rglob("*frame*"))
    assert not list(tmp_path.rglob("*seed*"))


def test_decision_content_id_rejects_tampering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded, activation, extension_paths, _ = _synthetic_context(
        tmp_path,
        monkeypatch,
    )
    decision = ext.evaluate_post_exhaustion_extension(
        repo_root=ROOT,
        loaded_policy=loaded,
        activation_v2_decision_path=activation,
        extension_observed_manifests=extension_paths[:2],
    )
    payload = decision.model_dump(mode="json")
    payload["semantic_labels_inspected"] = True
    with pytest.raises(ValueError):
        ext.PostExhaustionExtensionDecisionV1.model_validate(payload)

    payload = decision.model_dump(mode="json")
    payload["counts"]["unique_compiling_count"] = 244
    payload["counts"]["duplicate_member_count"] += 1
    with pytest.raises(ValueError, match="decision ID differs"):
        ext.PostExhaustionExtensionDecisionV1.model_validate(payload)


def test_strict_json_rejects_duplicate_keys() -> None:
    with pytest.raises(
        ext.PostExhaustionExtensionError,
        match="duplicate JSON key",
    ):
        ext._strict_json_object(
            b'{"decision_id":"a","decision_id":"b"}',
            location="memory",
        )


def test_policy_contains_no_unregistered_generation_family() -> None:
    raw = json.loads(
        json.dumps(ext.load_post_exhaustion_extension_policy(POLICY).config.model_dump(mode="json"))
    )
    assert [item["family_id"] for item in raw["family_pins"]] == list(FAMILIES)
    assert [item["pool_id"] for item in raw["pool_pins"]] == list(POOLS)
